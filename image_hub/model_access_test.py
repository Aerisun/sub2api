"""Check that catalogue hints coalesce bursts without granting API access."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from image_hub.model_access import ImageAccessCache
from image_hub.server import HubError, NATIVE_MODEL as MODEL


class ImageAccessTests(unittest.TestCase):
    def test_burst_coalesces_positive_and_negative_catalogues(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                cache = ImageAccessCache(timeout=1)
                arrived, release = threading.Event(), threading.Event()

                def fetch(*args, **kwargs):
                    arrived.set()
                    self.assertTrue(release.wait(1))
                    return 200, {'data': [{'id': MODEL}] if allowed else []}

                with patch('image_hub.model_access.request_json', side_effect=fetch) as loader:
                    with ThreadPoolExecutor(max_workers=32) as pool:
                        futures = [pool.submit(cache.allowed, 'http://gateway', 'key') for _ in range(32)]
                        self.assertTrue(arrived.wait(1))
                        release.set()
                        self.assertEqual([f.result() for f in futures], [allowed] * 32)
                    self.assertEqual(loader.call_count, 1)
                    self.assertIs(cache.allowed('http://gateway', 'key'), allowed)
                    self.assertEqual(loader.call_count, 1)

    def test_expiry_and_isolation_between_keys_and_gateways(self):
        now = [10.0]
        cache = ImageAccessCache(clock=lambda: now[0])
        with patch('image_hub.model_access.request_json', side_effect=[
            (200, {'data': [{'id': MODEL}]}), (200, {'data': []}),
            (200, {'data': []}), (200, {'data': []}),
        ]) as loader:
            self.assertTrue(cache.allowed('http://gateway', 'key-a'))
            self.assertFalse(cache.allowed('http://gateway', 'key-b'))
            self.assertFalse(cache.allowed('http://other', 'key-a'))
            now[0] += 2.01
            self.assertFalse(cache.allowed('http://gateway', 'key-a'))
            self.assertEqual(loader.call_count, 4)
        self.assertNotIn('key-a', repr(cache.entries))
        self.assertNotIn('key-b', repr(cache.entries))

    def test_failures_do_not_cache_and_pending_is_released(self):
        for failure in (HubError(401, 'revoked'), HubError(504, 'timeout'), ValueError('invalid')):
            cache = ImageAccessCache()
            with patch('image_hub.model_access.request_json', side_effect=[failure, (200, {'data': [{'id': MODEL}]})]):
                self.assertFalse(cache.allowed('http://gateway', 'key'))
                self.assertEqual(cache.pending, {})
                self.assertEqual(len(cache.entries), 0)
                self.assertTrue(cache.allowed('http://gateway', 'key'))

    def test_cold_browser_permission_lookup_has_an_authenticated_timeout_budget(self):
        cache=ImageAccessCache(ttl=30)
        with patch('image_hub.model_access.request_json',return_value=(200,{'data':[{'id':MODEL}]})) as request:
            self.assertTrue(cache.allowed('http://gateway','eyJlogin.payload.signature'))
            self.assertEqual(request.call_args.kwargs['timeout'],2.0)
            self.assertTrue(cache.allowed('http://gateway','eyJlogin.payload.signature'))
            self.assertEqual(request.call_count,1)

    def test_api_key_and_browser_jwt_use_distinct_authenticated_catalogues(self):
        cache = ImageAccessCache()
        with patch('image_hub.model_access.request_json', return_value=(200, {'data': [{'id': MODEL}]})) as request:
            self.assertTrue(cache.allowed('http://gateway', 'sk-customer'))
            self.assertTrue(cache.allowed('http://gateway', 'jwt-test'))
        self.assertEqual([call.args[0] for call in request.call_args_list], ['http://gateway/v1/models', 'http://gateway/api/v1/chat/models'])

    def test_empty_key_and_malformed_catalogue_never_enable_tools(self):
        cache = ImageAccessCache()
        with patch('image_hub.model_access.request_json', return_value=(200, {'data': None})) as loader:
            self.assertFalse(cache.allowed('http://gateway', ''))
            loader.assert_not_called()
            self.assertFalse(cache.allowed('http://gateway', 'key'))
            self.assertEqual(len(cache.entries), 0)

    def test_lru_and_inflight_are_bounded(self):
        cache = ImageAccessCache(capacity=2)
        with patch('image_hub.model_access.request_json', return_value=(200, {'data': []})) as loader:
            for key in ('a', 'b', 'a', 'c', 'b'):
                self.assertFalse(cache.allowed('http://gateway', key))
            self.assertEqual(loader.call_count, 4)
            self.assertEqual(len(cache.entries), 2)
            cache.pending = {b'a': threading.Event(), b'b': threading.Event()}
            self.assertFalse(cache.allowed('http://gateway', 'new-key'))
            self.assertEqual(loader.call_count, 4)

    def test_follower_does_not_wait_for_a_stuck_catalogue(self):
        cache = ImageAccessCache(timeout=.03)
        entered, release = threading.Event(), threading.Event()

        def fetch(*args, **kwargs):
            entered.set()
            release.wait(1)
            return 200, {'data': []}

        with patch('image_hub.model_access.request_json', side_effect=fetch):
            with ThreadPoolExecutor(max_workers=1) as pool:
                leader = pool.submit(cache.allowed, 'http://gateway', 'key')
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                self.assertFalse(cache.allowed('http://gateway', 'key'))
                self.assertLess(time.monotonic() - started, .2)
                release.set()
                self.assertFalse(leader.result())
        self.assertEqual(cache.pending, {})

    def test_cached_tool_hint_cannot_bypass_gateway_revocation(self):
        import json
        from http.server import BaseHTTPRequestHandler
        from urllib.error import HTTPError
        from urllib.request import Request, ProxyHandler, build_opener
        from image_hub import agent_server

        received = []

        class RevokedGateway(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_POST(self):
                received.append(self.headers.get('Authorization'))
                self.rfile.read(int(self.headers['Content-Length']))
                raw = b'{"error":{"message":"Revoked API key"}}'
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        gateway = agent_server.AgentHTTPServer(('127.0.0.1', 0), RevokedGateway)
        bridge = agent_server.AgentHTTPServer(('127.0.0.1', 0), agent_server.Handler)
        servers = (gateway, bridge)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{gateway.server_port}'
        cache = ImageAccessCache()
        try:
            with patch('image_hub.model_access.request_json', return_value=(200, {'data': [{'id': MODEL}]})) as loader:
                # The key had image access before being revoked at the gateway.
                self.assertTrue(cache.allowed(base, 'revoked-key'))
                with patch.object(agent_server, 'GATEWAY', base), patch.object(agent_server, 'IMAGE_ACCESS', cache):
                    for streaming in (False, True):
                        request = Request(f'http://127.0.0.1:{bridge.server_port}/v1/chat/completions',
                            data=json.dumps({'model': 'gemini-3.8-flash', 'stream': streaming,
                                             'messages': [{'role': 'user', 'content': 'hello'}]}).encode(),
                            headers={'Authorization': 'Bearer revoked-key', 'Content-Type': 'application/json'})
                        with self.assertRaises(HTTPError) as caught:
                            build_opener(ProxyHandler({})).open(request, timeout=2)
                        self.assertEqual(caught.exception.code, 401)
                        self.assertIn(b'Revoked API key', caught.exception.read())
                        caught.exception.close()
                self.assertEqual(loader.call_count, 1)
                self.assertEqual(received, ['Bearer revoked-key'] * 2)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
