import unittest

import httpx

from ops.checks.site_latency import check_entries, probe


class SiteLatencyTests(unittest.TestCase):
    def test_http_200_html_cannot_masquerade_as_a_javascript_asset(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text='<html>SPA fallback</html>',
                                                                      headers={'content-type': 'text/html'}))
        with httpx.Client(transport=transport) as client:
            result = probe(client, 'https://example.invalid/assets/app.js', expected_type=('application/javascript',))
        self.assertFalse(result['ok'])

    def test_health_checks_reject_error_envelopes_and_malformed_json(self):
        for content in ('{"status":"down"}', '{"error":{"message":"unavailable"}}', '[]', '{'):
            with self.subTest(content=content):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content,
                                                                              headers={'content-type': 'application/json'}))
                with httpx.Client(transport=transport) as client:
                    self.assertFalse(probe(client, 'https://example.invalid/health')['ok'])

    def test_public_probe_detects_a_broken_subpath_even_if_the_entry_returns_200(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text='<script src="/assets/app.js"></script>',
                                                                      headers={'content-type': 'text/html'}))
        with httpx.Client(transport=transport) as client:
            result = check_entries(client, 'https://example.invalid/sub2api')
        self.assertEqual(len(result), 2)
        self.assertFalse(any(row['ok'] for row in result))

    def test_connection_failures_are_recorded_without_losing_the_report(self):
        def failure(request):
            raise httpx.ConnectTimeout('test')
        with httpx.Client(transport=httpx.MockTransport(failure)) as client:
            result = probe(client, 'https://example.invalid/health')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'ConnectTimeout')


if __name__ == '__main__':
    unittest.main()
