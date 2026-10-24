"""Regress subscription removal, model gating, and safe pool replacement."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from image_hub.proxy_qualifier import (choose_pool, eligible_nodes, fingerprint,
                                       install_pool, read_yaml, reconcile, qualification_due, probe_live_pools, stable_pool,
                                       diverse_candidates)


def node(name):
    return {'name': name, 'type': 'socks5', 'server': '127.0.0.1', 'port': 9,
            'password': 'private-fixture-password'}


OLD = node('ASIA-Singapore-18')
NEW = node('NA-UnitedStates-Streaming-08')


class FileController:
    """A provider that immediately observes atomic file changes, like Mihomo."""
    def __init__(self, path, reject_new_selection=False):
        self.path = path
        self.selected = OLD['name']
        self.observed = []
        self.reject_new_selection = reject_new_selection

    def request(self, path, method='GET', body=None):
        names = [n['name'] for n in read_yaml(self.path)['proxies']]
        if method == 'PUT':
            if self.reject_new_selection and body['name'] == NEW['name']:
                raise RuntimeError('Simulated controller selection failure')
            self.selected = body['name']
        if self.selected not in names:
            self.selected = names[0] if names else 'COMPATIBLE'
        self.observed.append({'selected': self.selected, 'members': names})
        return {'now': self.selected, 'all': names}


class QualificationTests(unittest.TestCase):
    def test_region_rejection_does_not_hide_working_spares_in_other_regions(self):
        rank = ['NA-UnitedStates-'+str(n) for n in range(20)]
        rank += ['ASIA-Japan-'+str(n) for n in range(4)]
        rank += ['ASIA-Singapore-'+str(n) for n in range(4)]
        selected = diverse_candidates(rank, 8)
        self.assertEqual(selected, ['NA-UnitedStates-0', 'ASIA-Japan-0', 'ASIA-Singapore-0',
                                    'NA-UnitedStates-1', 'ASIA-Japan-1', 'ASIA-Singapore-1',
                                    'NA-UnitedStates-2', 'ASIA-Japan-2'])
        self.assertEqual(diverse_candidates(rank[:4], 8), rank[:4])
        self.assertEqual(diverse_candidates([], 8), [])

    def test_small_latency_changes_do_not_flap_the_freshly_verified_primary(self):
        rows=[{'node':'candidate','score_ms':1500},{'node':'current','score_ms':1800}]
        self.assertEqual(stable_pool(rows,'current')[0]['node'],'current')
        rows[1]['score_ms']=3000
        self.assertEqual(stable_pool(rows,'current')[0]['node'],'candidate')
        self.assertEqual(stable_pool(rows,'failed-or-removed')[0]['node'],'candidate')

    def test_failed_live_pool_requalifies_even_without_a_subscription_update(self):
        previous = {'source_fingerprint': 'same', 'checked_at': 100, 'retry_at': 0}
        self.assertFalse(qualification_due({}, previous, 'same', 120))
        self.assertTrue(qualification_due({}, previous, 'same', 120, unhealthy=True))
        self.assertTrue(qualification_due({}, previous, 'same', 3700))
        previous['retry_at'] = 180
        self.assertFalse(qualification_due({}, previous, 'same', 150, unhealthy=True))
        self.assertTrue(qualification_due({}, previous, 'same', 180, unhealthy=True))
        self.assertTrue(qualification_due({}, previous, 'new', 150, unhealthy=True))

    def test_live_spare_checks_never_select_nodes_or_generate_paid_requests(self):
        class Client:
            def __init__(self):self.paths=[]
            def request(self,path,method='GET',body=None):
                self.assert_read_only(method,body);self.paths.append(path)
                if '/delay?' not in path:return {'now':'broken','all':['broken','working']}
                if path.startswith('/proxies/broken/'):raise RuntimeError('fixture network failure')
                return {'delay':120}
            @staticmethod
            def assert_read_only(method,body):
                if method!='GET' or body is not None:raise AssertionError('Health check changed live state')
        client=Client()
        with patch('image_hub.proxy_qualifier.controller',return_value=client):
            report=probe_live_pools({'roles':[{'name':'gemini','group':'fixture'}]})
        self.assertTrue(report['gemini']['network_ok'])
        self.assertEqual(report['gemini']['healthy_nodes'],['working'])
        self.assertEqual(report['gemini']['selected'],'broken')
        self.assertTrue(all('/v1/chat/completions' not in path for path in client.paths))

    def test_file_provider_members_use_provider_health_endpoint(self):
        class Client:
            def __init__(self):self.paths=[]
            def request(self,path,method='GET',body=None):
                self.paths.append(path)
                if path=='/proxies/fixture':return {'now':'installed','all':['installed']}
                if path=='/providers/proxies':return {'providers':{'file-pool':{'proxies':[{'name':'installed'}]}}}
                if path.startswith('/providers/proxies/file-pool/installed/healthcheck?'):return {'delay':100}
                raise AssertionError('File-provider member is absent from global proxy endpoints')
        client=Client()
        with patch('image_hub.proxy_qualifier.controller',return_value=client):
            report=probe_live_pools({'roles':[{'name':'codex','group':'fixture'}]})
        self.assertTrue(report['codex']['network_ok'])
        self.assertEqual(report['codex']['healthy_nodes'],['installed'])

    def test_region_names_cannot_authorize_direct_or_unrequested_countries(self):
        candidates = [OLD, NEW, node('ASIA-Japan-Optimized-01'), node('EU-Germany-01'),
                      {**OLD, 'name': 'ASIA-Singapore-direct', 'type': 'direct'}]
        self.assertEqual(len(eligible_nodes({'proxies': candidates})), 3)
        results = [{'node': OLD['name'], 'country': 'CN', 'ok': True, 'score_ms': 1, 'network_ms': 1},
                   {'node': NEW['name'], 'country': 'US', 'ok': True, 'score_ms': 900, 'network_ms': 500}]
        self.assertEqual([r['node'] for r in choose_pool(results, 3)], [NEW['name']])

    def test_fast_connectivity_does_not_replace_gemini_with_a_model_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'subscription.yaml'
            pool = root / 'qualified.yaml'
            source.write_text(yaml.safe_dump({'proxies': [NEW]}))
            pool.write_text(yaml.safe_dump({'proxies': [OLD]}))
            before = pool.read_bytes()
            config = {'subscription_file': str(source), 'roles': [
                {'name': 'gemini', 'provider_file': str(pool)}]}
            bad = {'node': NEW['name'], 'country': 'US', 'ok': False,
                   'network_ms': 1, 'config': NEW}
            with patch('image_hub.proxy_qualifier.install_pool') as install:
                report = reconcile(config, validate=lambda _: {'gemini': [bad]})
            install.assert_not_called()
            self.assertEqual(pool.read_bytes(), before)
            self.assertEqual(report['roles']['gemini']['state'], 'retained')
            self.assertNotIn('private-fixture-password', json.dumps(report))

    def test_subscription_removing_every_node_keeps_the_previous_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'subscription.yaml'
            source.write_text('proxies: []\n')
            with patch('image_hub.proxy_qualifier.install_pool') as install:
                report = reconcile({'subscription_file': str(source), 'roles': []},
                                   validate=lambda _: self.fail('Empty input must not test/promote'))
            self.assertEqual(report['state'], 'retained')
            install.assert_not_called()

    def test_new_primary_is_selected_before_old_members_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'qualified.yaml'
            path.write_text(yaml.safe_dump({'proxies': [OLD]}))
            client = FileController(path)
            role = {'name': 'gemini', 'group': 'fixture', 'provider_file': str(path)}
            self.assertTrue(install_pool(role, [NEW], client))
            self.assertEqual(client.selected, NEW['name'])
            self.assertTrue(all(row['members'] and row['selected'] != 'COMPATIBLE'
                                for row in client.observed))
            self.assertTrue(any(set(row['members']) == {OLD['name'], NEW['name']}
                                for row in client.observed))
            self.assertEqual(read_yaml(path)['proxies'], [NEW])

    def test_failed_promotion_restores_previous_file_and_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'qualified.yaml'
            path.write_text(yaml.safe_dump({'proxies': [OLD]}))
            original = path.read_bytes()
            client = FileController(path, reject_new_selection=True)
            role = {'name': 'gemini', 'group': 'fixture', 'provider_file': str(path)}
            with self.assertRaises(RuntimeError):
                install_pool(role, [NEW], client)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(client.selected, OLD['name'])

    def test_same_nodes_in_different_file_order_do_not_trigger_paid_retests(self):
        self.assertEqual(fingerprint([OLD, NEW]), fingerprint([NEW, OLD]))
        self.assertNotEqual(fingerprint([OLD]), fingerprint([NEW]))


if __name__ == '__main__':
    unittest.main()
