"""Prove GPT failover with a local controller and synthetic generation results."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from image_hub.codex_watchdog import CodexController, CodexWatchdog, ProviderUnavailable, SchedulingPaused, load_config
from image_hub.egress_watchdog import APIError, Watchdog
from image_hub.egress_watchdog_test import controller_fixture, NAMES
from image_hub.proxy_qualifier import fingerprint, guard_exclusions, reconcile, probe_live_models, evaluate


@contextmanager
def fixture():
    with controller_fixture() as (state, config, _):
        root = Path(config['state_file']).parent
        pool = root/'qualified.yaml'
        nodes = [{'name': name, 'type': 'socks5', 'server': '127.0.0.1', 'port': 9} for name in NAMES]
        pool.write_text(yaml.safe_dump({'proxies': nodes}))
        mihomo = Path(config['mihomo_config'])
        value = yaml.safe_load(mihomo.read_text())
        value.update({'mixed-port': 17995, 'proxy-providers': {
            'existing-nodes': {'type': 'file', 'path': str(pool)}}})
        mihomo.write_text(yaml.safe_dump(value))
        role = {'name': 'codex', 'group': config['group'], 'account_id': 91,
                'provider_file': str(pool), 'mihomo_config': str(mihomo),
                'guard_state_file': config['state_file']}
        qualifier = {'roles': [role], 'upstream_route_state_file': str(root/'routes.json')}
        config.update(model_probe_seconds=60, error_poll_seconds=10, first_verified_spare=True,
                      probes=[{'url': 'https://chatgpt.com/cdn-cgi/trace', 'expected': '200'}])
        clock = [0]
        controller = CodexController(config, qualifier, role, clock=lambda: clock[0])
        state.update(model_broken=set(), generations=[])
        def measure(node, model, confirm):
            state['generations'].append((node['name'], model, confirm))
            ok = node['name'] not in state['model_broken']
            return {'ok': ok, 'model': model, 'checks': [{'ok': ok}]*(2 if confirm else 1)}
        watcher = CodexWatchdog(controller, config, clock=lambda: clock[0])
        try:
            with patch('image_hub.codex_watchdog.recent_transport_failures', return_value={}) as errors, \
                 patch.object(controller, 'ensure_scheduling'), \
                 patch.object(controller, 'measure', side_effect=measure):
                yield state, config, controller, watcher, clock, errors
        finally:
            watcher.close()


class CodexWatchdogTests(unittest.TestCase):
    def test_periodic_check_and_full_scan_do_not_spend_a_paused_account(self):
        config = {'roles': [{'name': 'codex', 'account_id': 91}]}
        account = {'extra': {'codex_7d_used_percent': 88, 'auto_pause_7d_threshold': .7}}
        def response(path, *args):
            return account if '/accounts/' in path else {}
        with patch('ops.admin_api.login', return_value='fixture'), \
             patch('ops.admin_api.request', side_effect=response), \
             patch('ops.credentials.accounts', return_value={91: {}}), \
             patch('image_hub.proxy_qualifier.real_check') as generation:
            report = probe_live_models(config, {'codex': {'network_ok': True}})
            self.assertIsNone(report['codex']['ok'])
            self.assertEqual(report['codex']['skipped']['reason'], 'quota_auto_pause_7d')
            self.assertEqual(evaluate(config, [])['codex'][0]['skipped']['reason'], 'quota_auto_pause_7d')
            generation.assert_not_called()

    def test_local_quota_pause_prevents_generation_and_route_changes(self):
        with fixture() as (state, _, controller, watcher, _, _):
            with patch.object(controller, 'ensure_scheduling', side_effect=SchedulingPaused('fixture')):
                with self.assertRaises(SchedulingPaused):
                    watcher.tick()
            self.assertEqual(state['switches'], [])
            self.assertEqual(state['generations'], [])

    def test_policy_cache_refresh_observes_an_operator_approved_recovery(self):
        with fixture() as (_, _, controller, _, clock, _):
            account = {'extra': {'codex_7d_used_percent': 88, 'auto_pause_7d_threshold': .7}}
            def response(path):
                return account if '/accounts/' in path else {}
            with patch.object(controller.admin, 'request', side_effect=response):
                with self.assertRaises(SchedulingPaused):
                    CodexController.ensure_scheduling(controller)
                account['extra']['auto_pause_7d_threshold'] = .95
                clock[0] = 16
                CodexController.ensure_scheduling(controller)
                self.assertEqual(controller.details['scheduling']['reason'], 'eligible')

    def test_normal_checks_rotate_models_and_recent_failure_takes_precedence(self):
        with fixture() as (_, config, controller, _, clock, _):
            config['probe_models'] = ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-sol']
            controller.allowed_models = set(config['probe_models'])
            self.assertEqual(controller.probe_model(), 'gpt-6.1-sol')
            clock[0] = 61
            self.assertEqual(controller.probe_model(), 'gpt-6-luna')
            controller.hint_model, controller.fault_until = 'gpt-6.1-sol', 180
            self.assertEqual(controller.probe_model(), 'gpt-6.1-sol')
            clock[0] = 181
            self.assertEqual(controller.probe_model(), 'gpt-6.1-sol')

    def test_slow_spare_does_not_delay_switch_to_an_already_verified_spare(self):
        with fixture() as (state, config, controller, watcher, _, _):
            state['model_broken'].add(NAMES[0])
            self.assertEqual(watcher.tick()['state'], 'suspect')
            finish = threading.Event()
            original = controller.measure.side_effect
            def slow(node, model, confirm):
                if node['name'] == NAMES[1]:
                    finish.wait(4)
                return original(node, model, confirm)
            with patch.object(controller, 'measure', side_effect=slow):
                worker = threading.Thread(target=watcher.tick)
                worker.start()
                try:
                    deadline = time.monotonic()+2
                    while state['current'] != NAMES[2] and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(state['current'], NAMES[2])
                    self.assertTrue(worker.is_alive(), 'Other accepted diagnostic finishes after the switch')
                    deadline = time.monotonic()+2
                    while True:
                        status = json.loads(Path(config['state_file']).read_text())
                        if status['state'] == 'healthy' or time.monotonic() >= deadline:
                            break
                        time.sleep(.01)
                    self.assertIn(NAMES[0], status['excluded_nodes'])
                finally:
                    finish.set();worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_actual_measure_requires_two_completed_generations_for_a_spare(self):
        with fixture() as (_, _, controller, _, _, _):
            controller.allowed_models = {'gpt-6-luna'}
            with patch.object(controller, 'credentials', return_value={'access_token': 'fixture'}), \
                 patch('image_hub.codex_watchdog.isolated_proxy', return_value=nullcontext((None, 'http://127.0.0.1:1'))), \
                 patch('image_hub.codex_watchdog.codex_generation', return_value={'ok': True, 'http_status': 200}) as generation:
                result = CodexController.measure(controller, {'name': NAMES[1]}, 'gpt-6-luna', True)
            self.assertTrue(result['ok'])
            self.assertEqual(generation.call_count, 2)
            self.assertTrue(all(call.kwargs['model'] == 'gpt-6-luna' for call in generation.call_args_list))

    def test_measure_treats_upstream_quota_as_a_provider_problem(self):
        with fixture() as (_, _, controller, _, _, _):
            controller.allowed_models = {'gpt-6-sol'}
            with patch.object(controller, 'credentials', return_value={'access_token': 'fixture'}), \
                 patch('image_hub.codex_watchdog.isolated_proxy', return_value=nullcontext((None, 'http://127.0.0.1:1'))), \
                 patch('image_hub.codex_watchdog.codex_generation', return_value={'ok': False, 'http_status': 429}) as generation:
                with self.assertRaises(ProviderUnavailable):
                    CodexController.measure(controller, {'name': NAMES[1]}, 'gpt-6-sol', True)
            self.assertEqual(generation.call_count, 1)

    def test_removed_model_mapping_does_not_trigger_an_unconfigured_model(self):
        with fixture() as (_, _, controller, _, _, _):
            with patch.object(controller, 'credentials', return_value={}), \
                 patch('image_hub.codex_watchdog.codex_generation') as generation:
                with self.assertRaises(ProviderUnavailable):
                    CodexController.measure(controller, {'name': NAMES[1]}, 'gpt-6-sol', False)
                generation.assert_not_called()

    def test_former_primary_requires_two_fresh_checks_before_becoming_a_spare(self):
        with fixture() as (state, _, controller, watcher, _, _):
            watcher.tick()
            state['current'] = NAMES[1]
            _, nodes = controller.snapshot()
            old = next(n for n in nodes if n.name == NAMES[0])
            self.assertIsNotNone(controller.probe(old))
            self.assertEqual(state['generations'][-1], (NAMES[0], 'gpt-6-sol', True))

    def test_network_healthy_but_generation_broken_uses_only_verified_spare(self):
        with fixture() as (state, config, controller, watcher, _, _):
            state['model_broken'].update(NAMES[:2])
            self.assertEqual(watcher.tick()['state'], 'suspect')
            self.assertEqual(state['switches'], [])
            result = watcher.tick()
            self.assertEqual(result['state'], 'healthy')
            self.assertEqual(state['current'], NAMES[2])
            self.assertEqual(state['switches'], [NAMES[2], NAMES[2]])
            self.assertIn((NAMES[2], 'gpt-6-sol', True), state['generations'])
            routes = json.loads(Path(controller.qualifier['upstream_route_state_file']).read_text())
            node = next(n for n in yaml.safe_load(Path(controller.role['provider_file']).read_text())['proxies']
                        if n['name'] == NAMES[2])
            self.assertEqual(routes['routes']['http://127.0.0.1:17995'], fingerprint([node]))

    def test_real_customer_eof_burst_cannot_be_hidden_by_cached_healthy_probe(self):
        with fixture() as (state, config, controller, watcher, clock, errors):
            self.assertEqual(watcher.tick()['state'], 'healthy')
            controller.allowed_models = {'gpt-6-sol', 'gpt-6-luna'}
            clock[0] = 11
            errors.return_value = {'failures': 2, 'latest_id': 123, 'model': 'gpt-6-luna'}
            self.assertEqual(watcher.tick()['state'], 'suspect')
            self.assertEqual(state['current'], NAMES[0])
            selected = watcher.tick()['node']
            self.assertIn(selected, NAMES[1:])
            self.assertIn((selected, 'gpt-6-luna', True), state['generations'])

    def test_model_check_cache_expires_and_configuration_change_invalidates_it(self):
        with fixture() as (state, config, controller, watcher, clock, _):
            watcher.tick(); watcher.tick()
            self.assertEqual(len(state['generations']), 1)
            pool = Path(controller.role['provider_file'])
            nodes = yaml.safe_load(pool.read_text())
            nodes['proxies'][0]['port'] = 10
            pool.write_text(yaml.safe_dump(nodes))
            watcher.tick()
            self.assertEqual(len(state['generations']), 2)
            clock[0] = 61
            watcher.tick()
            self.assertEqual(len(state['generations']), 3)

    def test_quota_or_authentication_failure_does_not_change_route(self):
        with fixture() as (state, config, controller, watcher, _, _):
            with patch.object(controller, 'measure', side_effect=ProviderUnavailable('fixture')):
                with self.assertRaises(ProviderUnavailable):
                    watcher.tick()
            self.assertEqual(state['switches'], [])

    def test_concurrent_qualification_decision_is_preserved(self):
        with fixture() as (state, config, controller, watcher, _, _):
            original = controller.measure.side_effect
            def concurrent(node, model, confirm):
                result = original(node, model, confirm)
                state['current'] = NAMES[1]
                return result
            with patch.object(controller, 'measure', side_effect=concurrent):
                with self.assertRaises(APIError):
                    watcher.tick()
            self.assertEqual(state['current'], NAMES[1])
            self.assertEqual(state['switches'], [])

    def test_optional_database_observation_failure_does_not_disable_generation_checks(self):
        with fixture() as (state, config, controller, watcher, _, errors):
            errors.side_effect = OSError('fixture database outage')
            self.assertEqual(watcher.tick()['state'], 'healthy')
            self.assertEqual(controller.details['error_observation'], 'unavailable')

    def test_changed_provider_cannot_be_selected_using_an_older_probe(self):
        with fixture() as (state, config, controller, watcher, _, _):
            controller.snapshot()
            node = controller.snapshot()[1][1]
            self.assertIsNotNone(controller.probe(node))
            pool = Path(controller.role['provider_file'])
            value = yaml.safe_load(pool.read_text());value['proxies'][1]['port'] = 12
            pool.write_text(yaml.safe_dump(value))
            with self.assertRaises(APIError):
                controller.select(node)
            self.assertEqual(state['switches'], [])

    def test_only_qualified_file_provider_is_loaded(self):
        with fixture() as (state, config, controller, watcher, _, _):
            root = Path(config['state_file']).parent
            qualifier = root/'qualifier.json';qualifier.write_text(json.dumps(controller.qualifier))
            path = root/'guard.json';path.write_text(json.dumps({
                'qualifier_config': str(qualifier), 'state_file': config['state_file']}))
            loaded, _, _ = load_config(path)
            self.assertEqual(loaded['providers'], ['existing-nodes'])
            self.assertEqual(loaded['interval'], 5)
            mihomo = Path(controller.role['mihomo_config']);value = yaml.safe_load(mihomo.read_text())
            value['proxy-providers']['existing-nodes']['path'] = 'unqualified.yaml'
            mihomo.write_text(yaml.safe_dump(value))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_full_scan_does_not_reselect_an_exit_under_fast_guard_quarantine(self):
        with fixture() as (state, config, controller, watcher, _, _):
            role = controller.role
            Path(role['guard_state_file']).write_text(json.dumps({'account_id': 91,
                'group': role['group'], 'excluded_nodes': {NAMES[0]: 200}}))
            with patch('image_hub.proxy_qualifier.time.time', return_value=100):
                self.assertEqual(guard_exclusions(role), {NAMES[0]})
                source = Path(config['state_file']).parent/'subscription.yaml'
                source.write_bytes(Path(role['provider_file']).read_bytes())
                nodes = yaml.safe_load(source.read_text())['proxies']
                rows = [{'node': n['name'], 'config': n, 'country': 'SG', 'ok': True,
                         'score_ms': i+1, 'network_ms': 1} for i, n in enumerate(nodes)]
                with patch('image_hub.proxy_qualifier.install_pool') as install, \
                     patch('image_hub.proxy_qualifier.controller', return_value=controller.api):
                    report = reconcile(dict(controller.qualifier, subscription_file=str(source)),
                                       validate=lambda _: {'codex': rows})
                self.assertNotIn(NAMES[0], report['roles']['codex']['pool'])
                self.assertTrue(install.called)


if __name__ == '__main__':
    unittest.main()
