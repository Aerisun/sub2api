"""Run failover against an isolated controller; never interrupt production traffic."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit, parse_qs

from image_hub.egress_watchdog import (AccountRecovery, APIError, Controller,
                                     Watchdog, load_config, local_url)

NAMES = ['ASIA-Singapore-01', 'ASIA-Singapore-10', 'ASIA-Singapore-12']


@contextmanager
def controller_fixture():
    state = {'current': NAMES[0], 'broken': set(), 'switches': [],
             'latency': dict(zip(NAMES, [30, 20, 10]))}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, value=None):
            self.send_response(code)
            self.end_headers()
            if value is not None:
                self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            if self.headers.get('Authorization') != 'Bearer controller-test-secret':
                return self.respond(403, {'message': 'do not expose this response'})
            path = unquote(urlsplit(self.path).path)
            if path == '/proxies/Sub2API egress':
                return self.respond(200, {'type': 'Fallback', 'now': state['current'],
                                          'all': NAMES + ['REJECT']})
            if path == '/providers/proxies':
                return self.respond(200, {'providers': {'existing-nodes': {
                    'proxies': [{'name': name} for name in NAMES]}}})
            if path.endswith('/healthcheck'):
                name = path.split('/')[-2]
                host = urlsplit(parse_qs(urlsplit(self.path).query)['url'][0]).hostname
                if name in state['broken'] or (name, host) in state['broken']:
                    time.sleep(state.get('failure_delay', 0))
                    return self.respond(503, {'message': 'private upstream error'})
                return self.respond(200, {'delay': state['latency'][name]})
            return self.respond(404)

        def do_PUT(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['current'] = value['name']
            state['switches'].append(value['name'])
            return self.respond(204)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        mihomo = root / 'mihomo.yaml'
        mihomo.write_text(f'external-controller: 127.0.0.1:{server.server_port}\n'
                          'secret: controller-test-secret\n')
        config = {'mihomo_config': str(mihomo), 'group': 'Sub2API egress',
                  'providers': ['existing-nodes'], 'node_pattern': '^ASIA-Singapore-\\d+$',
                  'interval': .05, 'timeout_ms': 100, 'failure_threshold': 2,
                  'cooldown_seconds': 30, 'recovery_interval': 30,
                  'state_file': str(root / 'state.json'),
                  'probes': [{'url': 'https://oauth2.googleapis.com/token', 'expected': '404'},
                             {'url': 'https://daily-cloudcode-pa.googleapis.com/', 'expected': '404'}]}
        path = root / 'watchdog.json'
        path.write_text(json.dumps(config))
        try:
            yield state, config, path
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class EgressWatchdogTests(unittest.TestCase):
    def test_status_disk_failure_does_not_stop_checks_or_error_reporting(self):
        with controller_fixture() as (state, config, _):
            watcher = Watchdog(Controller(config), config)
            try:
                watcher.tick()
                previous = Path(config['state_file']).read_text()
                with patch.object(Path, 'write_text', side_effect=OSError(28, 'Disk full')):
                    self.assertEqual(watcher.tick()['state'], 'healthy')
                    self.assertEqual(watcher.publish(state='controller_unavailable')['state'],
                                     'controller_unavailable')
                self.assertTrue(watcher.persistence_failed)
                self.assertEqual(Path(config['state_file']).read_text(), previous)
                self.assertEqual(watcher.tick()['state'], 'healthy')
                self.assertFalse(watcher.persistence_failed)
                self.assertEqual(json.loads(Path(config['state_file']).read_text())['state'], 'healthy')
            finally:
                watcher.close()

    def test_slow_working_route_moves_to_verified_faster_route(self):
        with controller_fixture() as (state, config, _):
            config.update(max_healthy_delay_ms=650, latency_probe_interval=30)
            state['latency'].update({NAMES[0]: 1200, NAMES[1]: 500, NAMES[2]: 350})
            watcher = Watchdog(Controller(config), config)
            try:
                result = watcher.tick()
                self.assertEqual(result['state'], 'healthy')
                self.assertEqual(result['node'], NAMES[2])
                self.assertEqual(result['switch_reason'], 'latency')
                self.assertEqual(result['previous_node'], NAMES[0])
            finally:
                watcher.close()

    def test_latency_preference_requires_large_improvement_and_waits_between_scans(self):
        with controller_fixture() as (state, config, _):
            config.update(max_healthy_delay_ms=650, latency_probe_interval=30)
            state['latency'].update({NAMES[0]: 1000, NAMES[1]: 850, NAMES[2]: 800})
            clock = [0]
            watcher = Watchdog(Controller(config), config, clock=lambda: clock[0])
            try:
                watcher.tick()
                self.assertEqual(state['current'], NAMES[0])
                state['latency'][NAMES[2]] = 350
                clock[0] = 10
                watcher.tick()
                self.assertEqual(state['current'], NAMES[0])
                clock[0] = 31
                watcher.tick()
                self.assertEqual(state['current'], NAMES[2])
            finally:
                watcher.close()

    def test_slow_route_does_not_switch_to_oauth_only_healthy_alternative(self):
        with controller_fixture() as (state, config, _):
            config.update(max_healthy_delay_ms=650)
            state['latency'][NAMES[0]] = 1200
            state['broken'].update((name, 'daily-cloudcode-pa.googleapis.com') for name in NAMES[1:])
            watcher = Watchdog(Controller(config), config)
            try:
                watcher.tick()
                self.assertEqual(state['current'], NAMES[0])
                self.assertEqual(state['switches'], [NAMES[0]])
            finally:
                watcher.close()

    def test_actual_controller_failover_and_no_flapping(self):
        with controller_fixture() as (state, config, _):
            watcher = Watchdog(Controller(config), config)
            try:
                self.assertEqual(watcher.tick()['state'], 'healthy')
                state['broken'].add(NAMES[0])
                self.assertEqual(watcher.tick()['state'], 'suspect')
                self.assertEqual(state['current'], NAMES[0])
                result = watcher.tick()
                self.assertEqual(result['state'], 'healthy')
                self.assertEqual(state['current'], NAMES[2])
                self.assertEqual(result['previous_node'], NAMES[0])
                state['broken'].clear()
                watcher.tick()
                self.assertEqual(state['current'], NAMES[2])
                self.assertEqual(state['switches'], [NAMES[0], NAMES[2], NAMES[2]])
            finally:
                watcher.close()

    def test_model_endpoint_failure_rejects_oauth_healthy_route(self):
        with controller_fixture() as (state, config, _):
            state['broken'].add((NAMES[0], 'daily-cloudcode-pa.googleapis.com'))
            watcher = Watchdog(Controller(config), config)
            try:
                watcher.tick()
                self.assertEqual(watcher.tick()['node'], NAMES[2])
            finally:
                watcher.close()

    def test_all_nodes_down_does_not_select_an_unverified_node(self):
        with controller_fixture() as (state, config, _):
            state['broken'].update(NAMES)
            watcher = Watchdog(Controller(config), config)
            try:
                watcher.tick()
                self.assertEqual(watcher.tick()['state'], 'no_healthy_nodes')
                self.assertEqual(state['switches'], [])
            finally:
                watcher.close()

    def test_transient_failure_does_not_switch(self):
        with controller_fixture() as (state, config, _):
            state['broken'].add(NAMES[0])
            watcher = Watchdog(Controller(config), config)
            try:
                self.assertEqual(watcher.tick()['state'], 'suspect')
                state['broken'].clear()
                self.assertEqual(watcher.tick()['state'], 'healthy')
                self.assertEqual(state['current'], NAMES[0])
            finally:
                watcher.close()

    def test_daemon_recovers_and_shuts_down_without_production_outage(self):
        with controller_fixture() as (state, config, path):
            # Production timings, with two real seconds spent waiting for a dead node.
            config.update(interval=2, timeout_ms=2000)
            path.write_text(json.dumps(config))
            state['failure_delay'] = 2
            child = subprocess.Popen([sys.executable, '-m', 'image_hub.egress_watchdog',
                                      '--config', str(path)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            def wait_for(predicate):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    self.assertIsNone(child.poll())
                    try:
                        value = json.loads(Path(config['state_file']).read_text())
                        if predicate(value):
                            return value
                    except (FileNotFoundError, ValueError):
                        pass
                    time.sleep(.01)
                self.fail('Daemon did not recover before the test deadline')
            try:
                wait_for(lambda value: value['state'] == 'healthy')
                started = time.monotonic()
                state['broken'].add(NAMES[0])
                result = wait_for(lambda value: value['state'] == 'healthy' and
                                  value['node'] == NAMES[2])
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 10)
                print(f'Isolated failover with production timing: {elapsed:.2f}s')
                self.assertEqual(result['previous_node'], NAMES[0])
            finally:
                child.terminate()
                child.wait(timeout=5)
            self.assertEqual(child.returncode, 0)

    def test_recovery_leaves_quota_holds_and_other_proxies_alone(self):
        recovery = AccountRecovery({'base_url': 'http://127.0.0.1:8080', 'proxy_id': 3,
                                    'account_ids': [1, 3, 4, 5]})
        calls = []
        def request(path, method='GET', body=None):
            calls.append((path, method))
            if method == 'GET':
                number = int(path.split('/')[-1])
                return {'platform': 'antigravity', 'status': 'active',
                        'proxy_id': 1 if number == 4 else 3,
                        'temp_unschedulable_until': 'tomorrow',
                        'temp_unschedulable_reason': 'quota exceeded' if number == 3
                        else 'invalid_grant: proxy unavailable' if number == 5
                        else 'proxy unavailable: TLS handshake timeout'}
            return {}
        with patch.object(recovery, 'request', side_effect=request):
            recovery.run()
        self.assertEqual([call for call in calls if call[1] != 'GET'], [
            ('/api/v1/admin/accounts/1/refresh', 'POST'),
            ('/api/v1/admin/accounts/1/temp-unschedulable', 'DELETE')])

    def test_failed_refresh_does_not_clear_hold(self):
        recovery = AccountRecovery({'base_url': 'http://127.0.0.1:8080', 'proxy_id': 3,
                                    'account_ids': [1]})
        calls = []
        def request(path, method='GET', body=None):
            calls.append(method)
            if method == 'POST':
                raise APIError('HTTP 502')
            return {'platform': 'antigravity', 'status': 'active', 'proxy_id': 3,
                    'temp_unschedulable_until': 'tomorrow',
                    'temp_unschedulable_reason': 'proxy unavailable'}
        with patch.object(recovery, 'request', side_effect=request):
            recovery.run()
        self.assertNotIn('DELETE', calls)

    def test_response_body_does_not_leak_into_error(self):
        with controller_fixture() as (_, config, _):
            client = Controller(config).api
            client.token = 'wrong-controller-secret'
            with self.assertRaisesRegex(APIError, '^HTTP 403$'):
                client.request('/proxies/Sub2API%20egress')

    def test_rejects_remote_credentials_and_nonservice_probe_hosts(self):
        with self.assertRaises(ValueError):
            local_url('https://example.com')
        with controller_fixture() as (_, config, path):
            config['probes'][0]['url'] = 'https://example.com/'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == '__main__':
    unittest.main()
