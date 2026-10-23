"""HTTP cache visibility preserves vectors, usage, authentication and coalescing."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest

from embedding_pool import PoolServer, WorkerPool


class CacheStatusTests(unittest.TestCase):
    def setUp(self):
        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.calls += 1
                self.server.gate.wait(3)
                status = 400 if data.get("fail") else 200
                payload = b'{"data":[{"embedding":[0.6,0.8],"index":0}],"usage":{"prompt_tokens":12,"total_tokens":12}}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        self.native.calls = 0
        self.native.gate = threading.Event()
        self.native.gate.set()
        self.proxy = PoolServer(("127.0.0.1", 0), WorkerPool([self.native.server_port], concurrency=1, per_worker=1), "private")
        for server in (self.native, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.native.gate.set()
        for server in (self.proxy, self.native):
            server.shutdown()
            server.server_close()

    def call(self, payload=b'{"input":"complete input"}', key="private"):
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        try:
            connection.request("POST", "/v1/embeddings", payload, {"Authorization": "Bearer " + key})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Cache-Status")
        finally:
            connection.close()

    def test_miss_hit_bypass_and_authentication(self):
        cold, hit = self.call(), self.call()
        self.assertEqual(cold[:2], hit[:2])
        self.assertEqual(cold[2], "AerisunEmbedding; fwd=miss")
        self.assertEqual(hit[2], "AerisunEmbedding; hit")
        self.assertEqual(self.native.calls, 1)
        self.assertEqual(self.call(key="wrong")[2], None)
        self.proxy.cache = None
        self.assertEqual(self.call()[2], "AerisunEmbedding; fwd=bypass")

    def test_error_is_not_a_cache_hit(self):
        for _ in range(2):
            response = self.call(b'{"input":"same","fail":true}')
            self.assertEqual(response[0], 400)
            self.assertEqual(response[2], "AerisunEmbedding; fwd=miss")
        self.assertEqual(self.native.calls, 2)

    def test_cached_response_survives_full_inference_admission(self):
        first = self.call()
        capacity = self.proxy.pool.capacity
        for _ in range(capacity):
            self.assertTrue(self.proxy.admission_slots.acquire(blocking=False))
        try:
            hit = self.call(b'{ "input" : "complete input" }')
            self.assertEqual(hit[:2], first[:2])
            self.assertEqual(hit[2], "AerisunEmbedding; hit")
            self.assertEqual(self.call(key="wrong")[0], 401)
            self.assertEqual(self.call(b'{"input":"new input"}')[0], 429)
            self.assertEqual(self.native.calls, 1)
            self.assertEqual(self.proxy.cache.snapshot()["pending"], 0)
            # Expired values must never pass through the overload fast path.
            for digest in list(self.proxy.cache.entries):
                _, response = self.proxy.cache.entries[digest]
                self.proxy.cache.entries[digest] = (time.monotonic() - 1, response)
            self.assertEqual(self.call()[0], 429)
            self.proxy.cache = None
            self.assertEqual(self.call()[0], 429)
        finally:
            for _ in range(capacity):
                self.proxy.admission_slots.release()

    def test_coalesced_request_is_not_a_stored_hit(self):
        self.native.gate.clear()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.call) for _ in range(2)]
            deadline = time.monotonic() + 2
            while self.proxy.cache.snapshot()["coalesced"] != 1:
                if time.monotonic() >= deadline:
                    self.fail("request did not coalesce")
                time.sleep(.005)
            self.native.gate.set()
            responses = [f.result(timeout=3) for f in futures]
        self.assertEqual(responses[0][:2], responses[1][:2])
        self.assertEqual({r[2] for r in responses}, {"AerisunEmbedding; fwd=miss", "AerisunEmbedding; fwd=miss; collapsed"})
        self.assertEqual(self.call()[2], "AerisunEmbedding; hit")


if __name__ == "__main__":
    unittest.main()
