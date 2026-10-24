"""Socket reuse, stream delivery and no-replay guarantees against a real server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import Mock, patch
from image_hub.gateway_transport import GatewayTransport


class GatewayTransportTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True
            def log_message(self, *_): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append((self.client_address[1], self.headers.get("Authorization"), body))
                if self.path == "/drop":
                    self.close_connection = True
                    return
                if self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"3\r\nabc\r\n")
                    self.wfile.flush()
                    time.sleep(.2)
                    self.wfile.write(b"3\r\ndef\r\n0\r\n\r\n")
                    return
                self.send_response(401 if self.path == "/denied" else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.pool = GatewayTransport()

    def tearDown(self):
        for _, _, connection in self.pool.idle:
            connection.close()
        self.server.shutdown()
        self.server.server_close()

    def test_reuses_consumed_socket_with_original_request_credentials(self):
        for key in ("first", "second"):
            with self.pool.open(self.base, b"prompt", {"Authorization": key}) as response:
                self.assertEqual(response.read(), b"OK")
        self.assertEqual(self.seen[0][0], self.seen[1][0])
        self.assertEqual([row[1] for row in self.seen], ["first", "second"])

    def test_sse_is_immediate_and_partial_stream_is_not_reused(self):
        began = time.monotonic()
        with self.pool.open(self.base + "/stream", b"prompt") as response:
            self.assertEqual(response.read1(65536), b"abc")
            self.assertLess(time.monotonic() - began, .15)
        with self.pool.open(self.base, b"next") as response:
            self.assertEqual(response.read(), b"OK")
        self.assertNotEqual(self.seen[0][0], self.seen[1][0])

    def test_does_not_retry_a_post_when_response_is_lost(self):
        with self.assertRaises(Exception):
            self.pool.open(self.base + "/drop", b"paid-operation")
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(len(self.pool.idle), 0)

    def test_expired_idle_connection_is_replaced(self):
        self.pool.idle_seconds = 0
        for _ in range(2):
            with self.pool.open(self.base, b"prompt") as response:
                response.read()
        self.assertNotEqual(self.seen[0][0], self.seen[1][0])

    def test_authentication_error_is_returned_without_replay(self):
        with self.pool.open(self.base + "/denied", b"prompt") as response:
            self.assertEqual(response.code, 401)
            self.assertEqual(response.read(), b"OK")
        self.assertEqual(len(self.seen), 1)

    def test_dead_listener_times_out_before_any_paid_request_is_written(self):
        connection = Mock(sock=None)
        connection.connect.side_effect = TimeoutError('fixture stalled connect')
        with patch('image_hub.gateway_transport.HTTPConnection', return_value=connection) as factory:
            with self.assertRaises(TimeoutError):
                self.pool.open(self.base, b'paid-operation', timeout=1500)
        self.assertEqual(factory.call_args.kwargs['timeout'], 3)
        connection.request.assert_not_called()
        connection.close.assert_called_once()
        self.assertEqual(len(self.pool.idle), 0)

    def test_cancel_during_connect_prevents_submission_after_socket_becomes_ready(self):
        from image_hub.client_disconnect import ClientRequest
        request = ClientRequest(Mock())
        connection = Mock(sock=None)
        def connect():
            request.cancel()
            connection.sock = Mock()
        connection.connect.side_effect = connect
        with patch('image_hub.gateway_transport.HTTPConnection', return_value=connection):
            with self.assertRaises(ConnectionAbortedError):
                self.pool.open(self.base, b'paid-operation', client_request=request)
        connection.request.assert_not_called()
        connection.close.assert_called_once()

    def test_connect_budget_does_not_shorten_an_accepted_long_stream(self):
        with self.pool.open(self.base+'/stream', b'prompt', timeout=60) as response:
            self.assertEqual(response.connection.sock.gettimeout(), 60)
            self.assertEqual(response.read(), b'abcdef')
