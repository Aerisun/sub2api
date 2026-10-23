"""Check exact reuse, full scores, coalescing, permissions and bounded storage."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from pool import PoolServer, WorkerPool
from result_cache import ExactResultCache
from test_pool import until


def body(query="问题", docs=None, top=2):
    return json.dumps({"model": "qwen3-reranker-4b", "query": query,
                       "documents": docs or ["正文甲" * 5000, "正文乙"], "top_n": top}, ensure_ascii=False).encode()


class Native(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(request)
        self.server.gate.wait(3)
        count = len(request["documents"])
        status = 400 if request["query"] == "invalid" else 200
        result = {"model": "qwen3-reranker-4b", "object": "list",
                  "usage": {"prompt_tokens": 1234, "total_tokens": 1234},
                  "results": [{"index": i, "relevance_score": (i + 1)/(count + 1)}
                              for i in reversed(range(count))][:request["top_n"]]}
        encoded = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class CacheRelayTests(unittest.TestCase):
    def setUp(self):
        self.native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        self.native.calls = []
        self.native.gate = threading.Event()
        self.native.gate.set()
        self.proxy = PoolServer(("127.0.0.1", 0), WorkerPool([self.native.server_port], 8), "private", result_cache=True)
        for server in [self.native, self.proxy]:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.native.gate.set()
        for server in [self.proxy, self.native]:
            server.shutdown()
            server.server_close()

    def call(self, payload, key="private"):
        status, data, _ = self.call_details(payload, key)
        return status, data

    def call_details(self, payload, key="private"):
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        try:
            connection.request("POST", "/rerank", payload, {"Authorization": "Bearer " + key})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("Cache-Status")
        finally:
            connection.close()

    def test_top_n_reuses_full_scores_and_usage_auth_still_required(self):
        first = self.call(body(top=1))[1]
        full = self.call(body(top=2))[1]
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(first["results"], full["results"][:1])
        self.assertEqual(first["usage"], full["usage"])
        self.assertEqual(self.native.calls[0]["top_n"], 2)
        self.assertEqual(self.native.calls[0]["documents"], json.loads(body())["documents"])
        self.assertEqual(self.call(body(), key="wrong")[0], 401)
        self.assertEqual(len(self.native.calls), 1)

    def test_changed_query_or_document_never_hits(self):
        for payload in [body(), body(query="不同问题"), body(docs=["改变的正文", "正文乙"])]:
            self.assertEqual(self.call(payload)[0], 200)
        self.assertEqual(len(self.native.calls), 3)

    def test_cached_scores_survive_full_inference_admission(self):
        first = self.call_details(body())
        capacity = self.proxy.pool.capacity
        for _ in range(capacity):
            self.assertTrue(self.proxy.admission_slots.acquire(blocking=False))
        try:
            hit = self.call_details(body(top=1))
            self.assertEqual(hit[0], 200)
            self.assertEqual(hit[1]["results"], first[1]["results"][:1])
            self.assertEqual(hit[1]["usage"], first[1]["usage"])
            self.assertEqual(hit[2], "AerisunRerank; hit")
            self.assertEqual(self.call(body(), key="wrong")[0], 401)
            self.assertEqual(self.call(body(query="new query"))[0], 429)
            self.assertEqual(len(self.native.calls), 1)
            self.assertEqual(self.proxy.cache.snapshot()["pending"], 0)
            for digest in list(self.proxy.cache.entries):
                _, response = self.proxy.cache.entries[digest]
                self.proxy.cache.entries[digest] = (time.monotonic() - 1, response)
            self.assertEqual(self.call(body())[0], 429)
            self.proxy.cache = None
            self.assertEqual(self.call(body())[0], 429)
        finally:
            for _ in range(capacity):
                self.proxy.admission_slots.release()

    def test_identical_concurrent_requests_compute_once(self):
        self.native.gate.clear()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self.call, body()) for _ in range(4)]
            until(lambda: self.proxy.cache.snapshot()["coalesced"] == 3)
            self.assertEqual(len(self.native.calls), 1)
            self.native.gate.set()
            results = [f.result(timeout=3) for f in futures]
        self.assertTrue(all(r == results[0] and r[0] == 200 for r in results))
        self.assertEqual(self.proxy.cache.snapshot()["pending"], 0)

    def test_standard_cache_status_preserves_scores_and_usage(self):
        cold = self.call_details(body())
        hit = self.call_details(body())
        self.assertEqual(cold[:2], hit[:2])
        self.assertEqual(cold[2], "AerisunRerank; fwd=miss")
        self.assertEqual(hit[2], "AerisunRerank; hit")
        self.assertEqual(self.call_details(body(), key="wrong")[2], None)
        for _ in range(2):
            self.assertEqual(self.call_details(body(query="invalid"))[2], "AerisunRerank; fwd=miss")
        self.proxy.cache = None
        self.assertEqual(self.call_details(body())[2], "AerisunRerank; fwd=bypass")

    def test_collapsed_response_is_distinct_from_stored_cache_hit(self):
        self.native.gate.clear()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.call_details, body()) for _ in range(2)]
            until(lambda: self.proxy.cache.snapshot()["coalesced"] == 1)
            self.native.gate.set()
            results = [f.result(timeout=3) for f in futures]
        self.assertEqual(results[0][:2], results[1][:2])
        self.assertEqual({r[2] for r in results}, {"AerisunRerank; fwd=miss", "AerisunRerank; fwd=miss; collapsed"})
        self.assertEqual(self.call_details(body())[2], "AerisunRerank; hit")

    def test_errors_never_cached(self):
        self.assertEqual(self.call(body(query="invalid"))[0], 400)
        self.assertEqual(self.call(body(query="invalid"))[0], 400)
        self.assertEqual(len(self.native.calls), 2)
        self.assertEqual(self.proxy.cache.snapshot()["entries"], 0)

    def test_expiry_and_eviction_bound_memory(self):
        cache = ExactResultCache(max_bytes=1024, max_entries=1)
        response = (200, json.dumps({"model": "qwen3-reranker-4b", "usage": {"prompt_tokens": 1},
                                   "results": [{"index": 0, "relevance_score": .1}]}).encode())
        for key in [b"one", b"two"]:
            _, flight = cache.claim(key)
            cache.finish(key, flight, response, 1)
        self.assertEqual(cache.snapshot()["entries"], 1)
        self.assertLessEqual(cache.snapshot()["bytes"], 1024)
        self.assertEqual(cache.claim(b"one")[0], "owner")
        cache = ExactResultCache(ttl=0)
        _, flight = cache.claim(b"one")
        cache.finish(b"one", flight, response, 1)
        self.assertEqual(cache.claim(b"one")[0], "owner")


if __name__ == "__main__":
    unittest.main()
