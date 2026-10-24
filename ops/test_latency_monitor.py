from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops import latency_monitor


class LatencyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'deployment').mkdir()
        (self.root/'deployment/site.json').write_text(json.dumps({'public_url': 'https://example.invalid/sub2api'}))
        self.patcher = patch.object(latency_monitor, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        observation = patch.object(latency_monitor, 'response_latency', return_value=[])
        observation.start()
        self.addCleanup(observation.stop)

    def site(self, assets_ok=True):
        return {'all_calls_passed': assets_ok, 'model_calls': 0,
                'checks': [{'origin': 'public', 'endpoint': '/health', 'p95_ms': 100}],
                'entries': [{'ok': assets_ok}]}

    def read(self):
        return json.loads((self.root/'var/reports/latency-current.json').read_text())

    def test_broken_assets_trigger_an_alarm_after_three_checks_even_with_healthy_api(self):
        with patch.object(latency_monitor, 'measure', side_effect=lambda *a, **k: self.site(False)), \
                patch.object(latency_monitor, 'model_latency', return_value=[]), redirect_stdout(StringIO()):
            for _ in range(2):
                latency_monitor.main()
            with self.assertRaises(SystemExit):
                latency_monitor.main()
        report = self.read()
        self.assertEqual(report['consecutive_failures'], 3)
        self.assertEqual(report['state'], 'unhealthy')

    def test_recovery_resets_the_failure_streak(self):
        with patch.object(latency_monitor, 'measure', side_effect=[self.site(False), self.site()]), \
                patch.object(latency_monitor, 'model_latency', return_value=[]), redirect_stdout(StringIO()):
            latency_monitor.main()
            latency_monitor.main()
        self.assertEqual(self.read()['state'], 'healthy')
        self.assertEqual(self.read()['consecutive_failures'], 0)

    def test_model_slowdown_is_observed_without_marking_the_whole_site_failed(self):
        model = {'model': 'test-model', 'samples': 3, 'first_event_p50_ms': 1000, 'first_event_p95_ms': 12000}
        with patch.object(latency_monitor, 'measure', return_value=self.site()), \
                patch.object(latency_monitor, 'model_latency', return_value=[model]), redirect_stdout(StringIO()):
            latency_monitor.main()
        report = self.read()
        self.assertEqual(report['state'], 'degraded')
        self.assertEqual(report['slow_models'], ['test-model'])
        self.assertEqual(report['consecutive_failures'], 0)
        self.assertEqual(report['model_calls'], 0)

    def test_failed_database_observation_does_not_expose_error_contents_or_stop_health_checks(self):
        with patch.object(latency_monitor, 'measure', return_value=self.site()), \
                patch.object(latency_monitor, 'model_latency', side_effect=subprocess.TimeoutExpired('private command', 5)), \
                redirect_stdout(StringIO()) as log:
            latency_monitor.main()
        self.assertEqual(self.read()['model_observation_error'], 'TimeoutExpired')
        self.assertNotIn('private command', log.getvalue())

    def test_early_gateway_events_cannot_hide_a_slow_first_text(self):
        row = {'model': 'fixture', 'streaming': True, 'with_tools': False, 'samples': 3,
               'first_text_p95_ms': 17000, 'semantic_gap_p95_ms': 16000}
        with patch.object(latency_monitor, 'measure', return_value=self.site()), \
                patch.object(latency_monitor, 'model_latency', return_value=[]), \
                patch.object(latency_monitor, 'response_latency', return_value=[row]), redirect_stdout(StringIO()):
            latency_monitor.main()
        self.assertEqual(self.read()['slow_models'], ['fixture'])

    def test_tool_and_nonstreaming_waits_are_reported_without_false_first_token_alarms(self):
        rows = [{'model': 'with-tool', 'streaming': True, 'with_tools': True, 'samples': 3,
                 'first_text_p95_ms': 40000, 'semantic_gap_p95_ms': 30000},
                {'model': 'whole-output', 'streaming': False, 'with_tools': False, 'samples': 3,
                 'first_text_p95_ms': 40000, 'semantic_gap_p95_ms': 0}]
        with patch.object(latency_monitor, 'measure', return_value=self.site()), \
                patch.object(latency_monitor, 'model_latency', return_value=[]), \
                patch.object(latency_monitor, 'response_latency', return_value=rows), redirect_stdout(StringIO()):
            latency_monitor.main()
        self.assertEqual(self.read()['state'], 'healthy')


if __name__ == '__main__':
    unittest.main()
