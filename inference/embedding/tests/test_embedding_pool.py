#!/usr/bin/env python3
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler

from embedding_pool import Busy, Disconnected, PoolServer, ResultCache, WorkerPool


class SchedulingTest(unittest.TestCase):
    def test_cache_sdk_formatting_reuses_original_usage_without_merging_inputs(self):
        cache = ResultCache()
        body = b'{"model":"Qwen3-Embedding-4B", "input":["complete","text"]}'
        digest, _, flight = cache.claim(body)
        response = (200, b'{"data":[{"index":0,"embedding":[1]}],"usage":{"prompt_tokens":42}}')
        cache.finish(digest, flight, response)
        self.assertEqual(cache.claim(b'{ "input" : [ "complete", "text" ], "model" : "Qwen3-Embedding-4B" }')[1:],
                         ("hit", response))
        self.assertEqual(cache.claim(b'{"model":"Qwen3-Embedding-4B","input":["text","complete"]}')[1], "owner")
        self.assertEqual(cache.claim(b'{"input":["complete","text"],"model":"Qwen3-Embedding-4B","dimensions":128}')[1], "owner")
        self.assertEqual(cache.claim(b'{"input":"bad","input":["complete","text"],"model":"Qwen3-Embedding-4B"}')[1], "owner")

    def test_cache_expiry_survives_lru_reordering_and_evicts_indexes_together(self):
        cache = ResultCache(max_bytes=250, ttl=10)
        response = (200, b'{"data":[{"index":0,"embedding":[1]}],"usage":{"prompt_tokens":42}}')
        with patch('embedding_pool.time.monotonic', return_value=100):
            a, _, flight = cache.claim(b'a')
            cache.finish(a, flight, response)
        with patch('embedding_pool.time.monotonic', return_value=105):
            b, _, flight = cache.claim(b'b')
            cache.finish(b, flight, response)
            self.assertEqual(cache.claim(b'a')[1], 'hit')
        with patch('embedding_pool.time.monotonic', return_value=111):
            self.assertEqual(cache.claim(b'a')[1], 'owner')
            self.assertEqual(cache.claim(b'b')[1], 'hit')
        self.assertEqual(set(cache.entries), set(cache.expirations))

    def test_capacity_cancellation_and_uncertain_recovery(self):
        pool = WorkerPool([1, 2], concurrency=2, per_worker=1, capacity=2)
        a, pa = pool.acquire(20)
        b, pb = pool.acquire(40)
        self.assertNotEqual(pa, pb)
        with self.assertRaises(Busy):
            pool.acquire(1)
        pool.release(a, uncertain=True)
        with self.assertRaises(Disconnected):
            pool.acquire(1, lambda: True)
        self.assertEqual(pool.snapshot()["waiting"], 0)
        self.assertIn(pa, pool.snapshot()["quarantined"])
        pool.release(b)
        c, pc = pool.acquire(100)
        self.assertEqual(pc, pb)
        pool.release(c)
        pool.recover(pa)
        self.assertEqual(pool.snapshot()["quarantined"], [])

    def test_pending_work_prevents_early_recovery(self):
        pool = WorkerPool([1], concurrency=2, per_worker=2)
        a, _ = pool.acquire(1)
        b, _ = pool.acquire(1)
        pool.release(a, True)
        pool.recover(1)
        self.assertEqual(pool.snapshot()["quarantined"], [1])
        pool.release(b)
        pool.recover(1)
        self.assertEqual(pool.snapshot()["quarantined"], [])

    def test_cache_preserves_usage_coalesces_and_does_not_cache_errors(self):
        cache = ResultCache(max_bytes=200)
        response = (200, b'{"data":[{"index":0,"embedding":[0.3,0.4]}],"usage":{"prompt_tokens":8192,"total_tokens":8192}}')
        digest, role, pending = cache.claim(b'complete body')
        self.assertEqual(role, "owner")
        _, role, waiter = cache.claim(b'complete body')
        self.assertEqual(role, "wait")
        self.assertIs(waiter, pending)
        cache.finish(digest, pending, response)
        self.assertTrue(waiter["ready"].is_set())
        self.assertEqual(cache.claim(b'complete body')[2], response)
        digest, _, pending = cache.claim(b'other body')
        cache.finish(digest, pending, (400, b'error'))
        self.assertEqual(cache.claim(b'other body')[1], "owner")
        for i in range(4):
            digest, _, pending = cache.claim(str(i).encode())
            cache.finish(digest, pending, response)
        self.assertLessEqual(cache.snapshot()["bytes"], 200)


class HttpTest(unittest.TestCase):
    def test_32_complete_requests_limits_and_billing_usage(self):
        received, peaks, active = [], {}, {}
        lock = threading.Lock()
        barrier = threading.Barrier(33)

        class NativeHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                port = self.server.server_port
                with lock:
                    received.append(body)
                    active[port] = active.get(port, 0) + 1
                    peaks[port] = max(peaks.get(port, 0), active[port])
                time.sleep(.08)
                output = json.dumps({"data": [{"index": 0, "embedding": [0.6, 0.8]}],
                                     "usage": {"prompt_tokens": len(body), "total_tokens": len(body)}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)
                with lock:
                    active[port] -= 1

        natives = [ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler) for _ in range(4)]
        for native in natives:
            native.request_queue_size = 128
            threading.Thread(target=native.serve_forever, daemon=True).start()
        pool = WorkerPool([s.server_port for s in natives], concurrency=32, per_worker=8)
        server = PoolServer(("127.0.0.1", 0), pool, "unit-test-only", cache=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        bodies = [json.dumps({"model": "Qwen3-Embedding-4B", "input": "全量中文资料" * 2000,
                              "request": i}, ensure_ascii=False).encode() for i in range(32)]

        def call(index):
            request = Request(f"http://127.0.0.1:{server.server_port}/v1/embeddings", data=bodies[index],
                              headers={"Content-Type": "application/json", "Authorization": "Bearer unit-test-only"})
            barrier.wait()
            with build_opener(ProxyHandler({})).open(request, timeout=15) as r:
                self.assertEqual(json.load(r)["usage"]["prompt_tokens"], len(bodies[index]))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
                futures = [executor.submit(call, i) for i in range(32)]
                barrier.wait()
                for future in futures:
                    future.result()
            self.assertCountEqual(received, bodies)
            self.assertEqual(pool.snapshot()["active"], 0)
            self.assertTrue(all(peak <= 8 for peak in peaks.values()))
            self.assertEqual(sum(pool.snapshot()["completed"].values()), 32)
        finally:
            server.shutdown()
            server.server_close()
            for native in natives:
                native.shutdown()
                native.server_close()


if __name__ == "__main__":
    unittest.main()
