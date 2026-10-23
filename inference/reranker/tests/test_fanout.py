"""Check complete-input fanout, bounded leases, failures and billed usage."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from pool import MODEL, PoolServer, WorkerPool, merge_complete_results, split_complete_documents
from test_pool import until


class LeaseTests(unittest.TestCase):
    def test_busy_pool_keeps_capacity_for_short_requests(self):
        pool = WorkerPool([1, 2, 3, 4], capacity=16)
        self.assertEqual(pool.acquire_many(2), [1, 2])
        self.assertEqual(pool.acquire_many(2), [3])
        self.assertEqual(pool.acquire(), 4)
        self.assertEqual(pool.snapshot()["active_requests"], 3)
        for port in [1, 2, 3, 4]:
            pool.release(port)

    def test_multiple_workers_are_one_admission_and_release_independently(self):
        pool = WorkerPool([1, 2, 3], capacity=3)
        self.assertEqual(pool.acquire_many(2), [1, 2])
        self.assertEqual(pool.acquire_many(2), [3])
        self.assertEqual(pool.snapshot()["active_requests"], 2)
        with ThreadPoolExecutor(max_workers=1) as executor:
            queued = executor.submit(pool.acquire_many, 2)
            until(lambda: pool.snapshot()["waiting"] == 1)
            pool.release(1, uncertain=True)
            self.assertEqual(pool.snapshot()["active_requests"], 2)
            self.assertEqual(pool.snapshot()["available"], [])
            pool.release(2)
            self.assertEqual(queued.result(timeout=2), [2])
        pool.release(2)
        pool.release(3)
        self.assertEqual(pool.snapshot()["active_requests"], 0)
        self.assertEqual(pool.snapshot()["quarantined"], [1])


class Native(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(data)
        self.server.gate.wait(3)
        docs = data["documents"]
        # Count full query and template for each complete candidate.
        tokens = sum(len(d) + len(data["query"]) + 100 for d in docs)
        results = [{"index": i, "relevance_score": .9 if d.endswith("答案") else .1} for i, d in enumerate(docs)]
        results.sort(key=lambda r: (-r["relevance_score"], r["index"]))
        response = json.dumps({"model": MODEL, "object": "list", "results": results[:data["top_n"]],
                               "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}).encode()
        self.send_response(400 if data["query"] == "invalid" else 200)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class FanoutTests(unittest.TestCase):
    def setUp(self):
        self.natives = [ThreadingHTTPServer(("127.0.0.1", 0), Native) for _ in range(2)]
        for native in self.natives:
            native.calls = []
            native.gate = threading.Event()
            native.gate.set()
        self.pool = WorkerPool([n.server_port for n in self.natives], capacity=4)
        self.proxy = PoolServer(("127.0.0.1", 0), self.pool, "private", result_cache=True, fanout=True)
        for server in [*self.natives, self.proxy]:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for native in self.natives:
            native.gate.set()
        for server in [self.proxy, *self.natives]:
            server.shutdown()
            server.server_close()

    def call(self, data):
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        try:
            connection.request("POST", "/rerank", json.dumps(data, ensure_ascii=False).encode(),
                               {"Authorization": "Bearer private"})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("X-Request-ID")
        finally:
            connection.close()

    def payload(self, **overrides):
        return dict(model=MODEL, query="中文问题", documents=["候选甲" * 1700, "候选乙" * 1600 + "答案", "短文"],
                    top_n=1, **overrides)

    def test_complete_documents_indices_usage_cache_and_request_ids(self):
        data = self.payload()
        status, first, first_id = self.call(data)
        status2, full, second_id = self.call(dict(data, top_n=3))
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual([r["index"] for r in full["results"]], [1, 0, 2])
        self.assertEqual(first["results"], full["results"][:1])
        received = [call for native in self.natives for call in native.calls]
        self.assertEqual(len(received), 2)
        self.assertCountEqual([doc for call in received for doc in call["documents"]], data["documents"])
        self.assertTrue(all(call["query"] == data["query"] for call in received))
        expected = sum(len(d) + len(data["query"]) + 100 for d in data["documents"])
        self.assertEqual(first["usage"], {"prompt_tokens": expected, "total_tokens": expected})
        self.assertEqual(first["usage"], full["usage"])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.pool.snapshot()["fanout"]["extra_native_calls"], 1)

    def test_one_idle_worker_falls_back_to_whole_request(self):
        occupied = self.pool.acquire()
        data = self.payload()
        self.assertEqual(self.call(data)[0], 200)
        received = [call for native in self.natives for call in native.calls]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["documents"], data["documents"])
        self.pool.release(occupied)

    def test_error_waits_for_other_part_and_is_never_cached(self):
        for native in self.natives:
            native.gate.clear()
        data = dict(self.payload(), query="invalid")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.call, data)
            until(lambda: all(n.calls for n in self.natives))
            self.assertEqual(self.pool.snapshot()["active_requests"], 1)
            self.natives[0].gate.set()
            until(lambda: self.pool.snapshot()["active"] == 1)
            self.assertFalse(future.done())
            self.natives[1].gate.set()
            self.assertEqual(future.result(timeout=2)[0], 400)
        self.assertEqual(self.call(data)[0], 400)
        self.assertEqual(self.proxy.cache.snapshot()["entries"], 0)
        self.assertEqual(sum(len(n.calls) for n in self.natives), 4)
        self.assertEqual(self.pool.snapshot()["active_requests"], 0)

    def test_incomplete_success_does_not_return_partial_ranking(self):
        body = json.dumps(dict(self.payload(), top_n=3)).encode()
        plans = split_complete_documents(body)
        bad = (200, b'{"model":"qwen3-reranker-4b","usage":{"prompt_tokens":1},"results":[]}')
        self.assertEqual(merge_complete_results([bad, bad], plans)[0], 502)


if __name__ == "__main__":
    unittest.main()
