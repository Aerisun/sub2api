"""Real-socket checks for reuse, first-content delivery and request isolation."""
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from image_hub import agent_server, server
from image_hub.gateway_transport import GatewayTransport


class TransportLatencyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Gateway(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.do_POST()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append((self.client_address[1], self.headers.get("Authorization"), body))
                if self.path == "/drop":
                    self.close_connection = True
                    return
                if self.path == "/v1/chat/completions":
                    fault = json.loads(body).get('fixture_fault')
                    if fault == 'provider_error':
                        self.send_response(503)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    def emit(value):
                        raw = b"data: " + (value if isinstance(value, bytes) else json.dumps(value).encode()) + b"\n\n"
                        self.wfile.write(f"{len(raw):x}\r\n".encode()+raw+b"\r\n")
                        self.wfile.flush()
                    emit({"id":"fixture", "choices":[{"index":0,"delta":{"role":"assistant","content":"a"}}]})
                    if fault in ('semantic_truncation', 'wire_truncation'):
                        if fault == 'semantic_truncation':
                            self.wfile.write(b"0\r\n\r\n")
                        else:
                            self.close_connection = True
                        return
                    time.sleep(.35)
                    try:
                        emit({"id":"fixture", "choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"stop"}],
                              "usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}})
                        emit(b"[DONE]")
                        self.wfile.write(b"0\r\n\r\n")
                    except OSError:
                        self.close_connection = True
                    return
                value = {"authorization":self.headers.get("Authorization"), "data":body.decode()}
                if self.path == "/denied":
                    value = {"error":{"message":"invalid "+self.headers["Authorization"]}}
                raw = b"invalid-json" if self.path == "/bad-json" else json.dumps(value).encode()
                self.send_response(401 if self.path == "/denied" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.gateway.daemon_threads = True
        threading.Thread(target=lambda:self.gateway.serve_forever(poll_interval=.02), daemon=True).start()
        self.base = f"http://127.0.0.1:{self.gateway.server_port}"
        self.pool = GatewayTransport()
        self.pool_patch = patch.object(server, "JSON_GATEWAY_TRANSPORT", self.pool)
        self.pool_patch.start()

    def tearDown(self):
        self.pool_patch.stop()
        for _, _, connection in self.pool.idle:
            connection.close()
        self.gateway.shutdown()
        self.gateway.server_close()

    def test_internal_json_reuses_socket_with_each_callers_own_body_and_identity(self):
        bodies = [{"input":"你好", "reasoning_effort":"high"}, {"input":"second", "stream":False}]
        for key, body in zip(("first-key", "second-key"), bodies):
            status, value = server.request_json(self.base+"/json", key, body)
            self.assertEqual(status, 200)
            self.assertEqual(value["authorization"], "Bearer "+key)
            self.assertEqual(json.loads(value["data"]), body)
        self.assertEqual(self.requests[0][0], self.requests[1][0])

    def test_internal_auth_failure_keeps_status_redacts_key_and_is_not_replayed(self):
        with self.assertRaises(server.HubError) as raised:
            server.request_json(self.base+"/denied", "private-fixture-key", {"paid":True})
        self.assertEqual(raised.exception.status, 401)
        self.assertNotIn("private-fixture-key", raised.exception.message)
        self.assertEqual(len(self.requests), 1)

    def test_lost_internal_post_is_never_replayed(self):
        with self.assertRaises(server.HubError):
            server.request_json(self.base+"/drop", "fixture", {"paid":True})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.pool.idle), 0)

    def test_invalid_json_stays_an_error(self):
        with self.assertRaises(server.HubError) as raised:
            server.request_json(self.base+"/bad-json", "fixture")
        self.assertEqual(raised.exception.status, 502)

    def test_stream_reuses_ingress_socket_and_delivers_text_before_generation_finishes(self):
        ingress = []
        class Bridge(agent_server.Handler):
            def do_POST(self):
                ingress.append(self.client_address[1])
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.bridge(body, "chat", True, None)
        bridge = agent_server.AgentHTTPServer(("127.0.0.1", 0), Bridge)
        threading.Thread(target=lambda:bridge.serve_forever(poll_interval=.02), daemon=True).start()
        connection = HTTPConnection("127.0.0.1", bridge.server_port, timeout=3)
        transport = GatewayTransport()
        try:
            with patch.object(agent_server, "GATEWAY", self.base), patch.object(agent_server, "GATEWAY_TRANSPORT", transport):
                for key in ("first", "second"):
                    body = {"model":"fixture-model", "stream":True, "reasoning_effort":"high",
                            "messages":[{"role":"user","content":"fixture"}]}
                    started = time.monotonic()
                    connection.request("POST", "/v1/chat/completions", json.dumps(body),
                                       {"Content-Type":"application/json", "Authorization":"Bearer "+key})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    first = b""
                    while b'"content":"a"' not in first:
                        line = response.readline()
                        self.assertTrue(line)
                        first += line
                    self.assertLess(time.monotonic()-started, .3)
                    complete = first+response.read()
                    self.assertIn(b'"content":"b"', complete)
                    self.assertIn(b"data: [DONE]", complete)
                    self.assertFalse(response.will_close)
                self.assertEqual(len(ingress), 2)
                self.assertEqual(ingress[0], ingress[1])
                self.assertEqual([r[1] for r in self.requests], ["Bearer first", "Bearer second"])
                self.assertEqual([json.loads(r[2])["reasoning_effort"] for r in self.requests], ["high", "high"])
        finally:
            connection.close()
            for _, _, idle in transport.idle:
                idle.close()
            bridge.shutdown()
            bridge.server_close()

    def test_public_handler_distinguishes_partial_streams_provider_errors_and_client_cancellation(self):
        bridge = agent_server.AgentHTTPServer(('127.0.0.1', 0), agent_server.Handler)
        threading.Thread(target=lambda: bridge.serve_forever(poll_interval=.02), daemon=True).start()
        observations, finished = [], threading.Event()
        transport = GatewayTransport()
        def record(metric):
            observations.append(metric.finish())
            finished.set()
        try:
            with patch.object(agent_server, 'GATEWAY', self.base), \
                    patch.object(agent_server, 'GATEWAY_TRANSPORT', transport), \
                    patch.object(agent_server, 'eligible', return_value=False), \
                    patch.object(agent_server, 'search_eligible', return_value=False), \
                    patch.object(agent_server, 'record_response_latency', side_effect=record):
                for fault, outcome in [('semantic_truncation', 'incomplete_stream'),
                                       ('wire_truncation', 'upstream_error'),
                                       ('provider_error', 'upstream_error'),
                                       ('client_cancel', 'client_cancelled'),
                                       ('none', 'completed')]:
                    with self.subTest(fault=fault):
                        finished.clear()
                        connection = HTTPConnection('127.0.0.1', bridge.server_port, timeout=3)
                        try:
                            body = {'model': 'fixture-model', 'stream': True, 'fixture_fault': fault,
                                    'messages': [{'role': 'user', 'content': 'fixture'}]}
                            connection.request('POST', '/v1/chat/completions', json.dumps(body),
                                               {'Content-Type': 'application/json'})
                            response = connection.getresponse()
                            self.assertEqual(response.status, 503 if fault == 'provider_error' else 200)
                            if fault == 'client_cancel':
                                self.assertIn(b'fixture', response.readline())
                                connection.sock.shutdown(socket.SHUT_RDWR)
                                connection.close()
                                response.close()
                            elif fault == 'wire_truncation':
                                with self.assertRaises(IncompleteRead):
                                    response.read()
                            else:
                                response.read()
                            self.assertTrue(finished.wait(3), 'request outcome was not recorded')
                            self.assertEqual(observations[-1]['outcome'], outcome)
                        finally:
                            connection.close()
            self.assertEqual(len(self.requests), 5)  # None of the paid POSTs was replayed.
        finally:
            for _, _, idle in transport.idle:
                idle.close()
            bridge.shutdown()
            bridge.server_close()


if __name__ == "__main__":
    unittest.main()
