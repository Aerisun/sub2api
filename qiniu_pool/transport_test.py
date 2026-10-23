from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import Mock
import httpx
from qiniu_pool.transport import ProxySafeTransport, _TunnelSetupLock


class ProxyTransportTests(unittest.TestCase):
    def test_repeated_tls_failures_do_not_exhaust_pool_or_replay_requests(self):
        attempts = []
        class Proxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_): pass
            def do_CONNECT(self):
                attempts.append(self.command)
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True  # Fail TLS after successful CONNECT.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            errors = []
            # Run both variants: the regression must fail with the old transport.
            for cls in (httpx.HTTPTransport, ProxySafeTransport):
                transport = cls(proxy=f"http://127.0.0.1:{server.server_port}",
                                limits=httpx.Limits(max_connections=2), trust_env=False)
                names = []
                with httpx.Client(transport=transport, timeout=.2) as client:
                    for _ in range(5):
                        try:
                            client.post("https://upstream.invalid/", content=b"paid-request")
                        except httpx.TransportError as error:
                            names.append(type(error).__name__)
                errors.append(names)
            self.assertIn("PoolTimeout", errors[0])
            self.assertEqual(errors[1], ["ConnectError"] * 5)
            self.assertEqual(len(attempts), 7)  # Two old + five new, zero replay.
        finally:
            server.shutdown()
            server.server_close()

    def test_cleanup_happens_before_unlock_and_leaves_established_connections(self):
        connection = Mock(_connected=False)
        lock = threading.Lock()
        connection.close.side_effect = lambda: self.assertTrue(lock.locked())
        guard = _TunnelSetupLock(lock, connection)
        with self.assertRaises(ValueError):
            with guard:
                raise ValueError("failed TLS")
        connection.close.assert_called_once()
        self.assertFalse(lock.locked())
        connection.reset_mock()
        connection._connected = True
        with self.assertRaises(ValueError):
            with guard:
                raise ValueError("established response failure")
        connection.close.assert_not_called()
