#!/usr/bin/env python3
"""Exercise admission, cancellation, failure isolation and byte-preserving relay."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest

from pool import Busy, Disconnected, MAX_BODY, PoolServer, WorkerPool


def until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Expected state was not reached")
        time.sleep(.005)


class AdmissionTests(unittest.TestCase):
    def test_maintenance_drains_active_work_and_keeps_a_healthy_worker(self):
        pool = WorkerPool([1, 2], capacity=4)
        first, second = pool.acquire(), pool.acquire()
        pool.pause(second)
        self.assertEqual(pool.snapshot()["workers_active"][second], 1)
        pool.release(second)
        self.assertNotIn(second, pool.snapshot()["available"])
        with self.assertRaises(ValueError):
            pool.pause(first)
        pool.release(first)
        self.assertEqual(pool.acquire(), first)
        pool.release(first)
        pool.enable(second)
        self.assertEqual(pool.snapshot()["enabled"], [1, 2])
        pool.quarantine(first)
        with self.assertRaises(ValueError):
            pool.pause(second)

    def test_fifo_capacity_and_cancel(self):
        pool = WorkerPool([1, 2], capacity=4)
        first, second = pool.acquire(), pool.acquire()
        cancelled = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            next_request = executor.submit(pool.acquire)
            until(lambda: pool.snapshot()["waiting"] == 1)
            abandoned = executor.submit(pool.acquire, cancelled.is_set)
            until(lambda: pool.snapshot()["waiting"] == 2)
            with self.assertRaises(Busy):
                pool.acquire()
            cancelled.set()
            with self.assertRaises(Disconnected):
                abandoned.result(timeout=2)
            pool.release(second)
            self.assertEqual(next_request.result(timeout=2), second)
        pool.release(first)
        pool.release(second)
        self.assertEqual(pool.snapshot()["active"], 0)

    def test_uncertain_worker_cannot_be_reused(self):
        pool = WorkerPool([1, 2], capacity=2)
        self.assertEqual(pool.acquire(), 1)
        pool.release(1, uncertain=True)
        self.assertEqual(pool.acquire(), 2)
        state = pool.snapshot()
        self.assertEqual(state["available"], [])
        self.assertEqual(state["quarantined"], [1])
        pool.recover(1)
        self.assertEqual(pool.acquire(), 1)
        pool.release(1)
        pool.release(2)


class FakeNative(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append(body)
        self.server.started.set()
        self.server.finish.wait(3)
        if body == b"malformed-response":
            self.connection.sendall(b"invalid HTTP response\r\n\r\n")
            return
        response = b'{"results":[{"index":1,"relevance_score":0.123456789}],"usage":{"prompt_tokens":1234}}'
        status = 400 if body == b"invalid" else 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class RelayTests(unittest.TestCase):
    def test_maintenance_endpoint_requires_authentication_and_boolean_state(self):
        pool = WorkerPool([1, 2], capacity=4)
        self.proxy.pool = pool
        calls = []

        class Controller:
            def set_maintenance(self, port, paused):
                calls.append((port, paused))
                pool.pause(port)

        self.proxy.elastic = Controller()
        for key, payload, expected in [
                ("wrong", {"port": 2, "paused": True}, 401),
                ("test-private-key", {"port": 2, "paused": "yes"}, 400),
                ("test-private-key", {"port": True, "paused": True}, 400),
                ("test-private-key", {"port": 2, "paused": True}, 200),
                ("test-private-key", {"port": 1, "paused": True}, 400)]:
            connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
            connection.request("POST", "/internal/worker-maintenance", json.dumps(payload),
                               {"Authorization": "Bearer " + key})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, expected)
            connection.close()
        self.assertEqual(calls, [(2, True), (1, True)])
        self.assertEqual(pool.snapshot()["enabled"], [1])

    def setUp(self):
        self.native = ThreadingHTTPServer(("127.0.0.1", 0), FakeNative)
        self.native.received = []
        self.native.started = threading.Event()
        self.native.finish = threading.Event()
        self.native.finish.set()
        self.pool = WorkerPool([self.native.server_port], capacity=2)
        self.proxy = PoolServer(("127.0.0.1", 0), self.pool, "test-private-key")
        self.threads = []
        for server in [self.native, self.proxy]:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        self.native.finish.set()
        for server in [self.proxy, self.native]:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, body, key="test-private-key"):
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        try:
            connection.request("POST", "/v1/rerank", body, {"Authorization": "Bearer " + key})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_full_body_scores_usage_and_status_preserved(self):
        body = json.dumps({"query": "中文问题", "documents": ["完整长文" * 5000, "答案"]}, ensure_ascii=False).encode()
        status, response = self.request(body)
        self.assertEqual(status, 200)
        self.assertEqual(self.native.received, [body])
        self.assertEqual(json.loads(response)["usage"]["prompt_tokens"], 1234)
        self.assertEqual(json.loads(response)["results"][0]["relevance_score"], .123456789)
        self.assertEqual(self.request(b"invalid")[0], 400)
        self.assertEqual(self.request(body, key="wrong")[0], 401)
        self.assertEqual(len(self.native.received), 2)

    def test_oversized_body_is_rejected_before_reading_or_native_call(self):
        client = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=2)
        try:
            client.sendall(("POST /rerank HTTP/1.0\r\nAuthorization: Bearer test-private-key\r\n"
                            f"Content-Length: {MAX_BODY + 1}\r\n\r\n").encode())
            self.assertIn(b" 413 ", client.recv(4096).split(b"\r\n", 1)[0])
            self.assertEqual(self.native.received, [])
        finally:
            client.close()

    def test_partial_uploads_are_bounded_before_reading_bodies(self):
        clients = []
        try:
            for _ in range(self.pool.capacity):
                client = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=2)
                clients.append(client)
                client.sendall(b"POST /rerank HTTP/1.0\r\nAuthorization: Bearer test-private-key\r\nContent-Length: 2\r\n\r\n{")
            until(lambda: not self.proxy.admission_slots._value)
            client = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=2)
            clients.append(client)
            client.sendall(b"POST /rerank HTTP/1.0\r\nAuthorization: Bearer test-private-key\r\nContent-Length: 2\r\n\r\n")
            self.assertIn(b" 429 ", client.recv(4096).split(b"\r\n", 1)[0])
            self.assertEqual(self.native.received, [])
        finally:
            for client in clients:
                client.close()
        until(lambda: self.proxy.admission_slots._value == self.pool.capacity)
        self.assertEqual(self.request(b"{}")[0], 200)

    def test_disconnected_active_request_retains_worker_until_finished(self):
        self.native.finish.clear()
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=4)
        connection.request("POST", "/rerank", b"{}", {"Authorization": "Bearer test-private-key"})
        self.assertTrue(self.native.started.wait(2))
        connection.close()
        self.assertEqual(self.pool.snapshot()["active"], 1)
        self.native.finish.set()
        until(lambda: self.pool.snapshot()["active"] == 0)

    def test_transport_failure_quarantines_worker(self):
        self.assertEqual(self.request(b"malformed-response")[0], 502)
        state = self.pool.snapshot()
        self.assertEqual(state["quarantined"], [self.native.server_port])
        self.assertEqual(state["available"], [])
        self.pool.recover(self.native.server_port)
        self.assertEqual(self.request(b"valid-again")[0], 200)

    def test_disconnected_queued_request_is_removed_without_native_call(self):
        self.native.finish.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            active = executor.submit(self.request, b"first")
            self.assertTrue(self.native.started.wait(2))
            client = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=2)
            client.sendall(b"POST /rerank HTTP/1.0\r\nAuthorization: Bearer test-private-key\r\nContent-Length: 2\r\n\r\n{}")
            until(lambda: self.pool.snapshot()["waiting"] == 1)
            client.close()
            until(lambda: self.pool.snapshot()["waiting"] == 0)
            self.assertEqual(self.native.received, [b"first"])
            self.native.finish.set()
            self.assertEqual(active.result(timeout=2)[0], 200)


if __name__ == "__main__":
    unittest.main()
