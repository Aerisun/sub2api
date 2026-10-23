import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from nvidia_pool.egress import candidate_names,delay,failover,qualified,reconcile,publish_route


class EgressTests(unittest.TestCase):
    def test_returning_to_a_previous_exit_uses_a_new_request_generation(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);provider=root/'provider.yaml';route=root/'route.json'
            provider.write_text(yaml.safe_dump({'proxies':[{'name':'a'},{'name':'b'}]}))
            role={'provider_file':str(provider),'route_state_file':str(route)}
            publish_route(role,'a');first=json.loads(route.read_text())['generation']
            publish_route(role,'b');publish_route(role,'a')
            self.assertNotEqual(first,json.loads(route.read_text())['generation'])

    def test_network_heartbeat_keeps_real_generation_proof_for_the_same_exit(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);provider=root/'provider.yaml';route=root/'route.json'
            provider.write_text(yaml.safe_dump({'proxies':[{'name':'verified','type':'ss','password':'fixture-secret'}]}))
            role={'provider_file':str(provider),'route_state_file':str(route)}
            publish_route(role,'verified',['model'])
            before=route.read_bytes();publish_route(role,'verified')
            self.assertEqual(route.read_bytes(),before)
            self.assertEqual(json.loads(before)['verified_models'],['model'])
            self.assertNotIn('fixture-secret',before.decode())

    def test_provider_member_health_uses_its_provider_endpoint(self):
        class Client:
            def request(self,path):
                if path=='/providers/proxies':
                    return {'providers':{'qualified nodes':{'proxies':[{'name':'US spare'}]}}}
                if path.startswith('/providers/proxies/qualified%20nodes/US%20spare/healthcheck?'):
                    return {'delay':95}
                raise AssertionError('Provider member must not use the global proxy endpoint')
        self.assertEqual(delay(Client(),'US spare'),95)

    def test_isolated_inline_member_keeps_global_health_endpoint(self):
        class Client:
            def request(self,path):
                if path=='/providers/proxies':return {'providers':{}}
                if path.startswith('/proxies/inline/delay?'):return {'delay':120}
                raise AssertionError(path)
        self.assertEqual(delay(Client(),'inline'),120)

    def test_actual_model_tests_include_regional_alternatives(self):
        rank=['NA-UnitedStates-fast','NA-UnitedStates-next','NA-UnitedStates-third','ASIA-Japan-fast','ASIA-Singapore-fast']
        self.assertEqual(candidate_names(rank,4),['NA-UnitedStates-fast','ASIA-Japan-fast','ASIA-Singapore-fast','NA-UnitedStates-next'])

    def test_actual_model_success_and_latency_rank_before_network_ping(self):
        rows=[{'node':'fast-ping-broken-model','network_ms':1,'score_ms':1,'ok':False},
              {'node':'good','network_ms':200,'score_ms':300,'ok':True,'checks':[{'ok':True}]},
              {'node':'slow-model','network_ms':2,'score_ms':2000,'ok':True,'checks':[{'ok':True}]},
              {'node':'partly-broken','network_ms':1,'score_ms':10,'ok':True,'checks':[{'ok':True},{'ok':False}]}]
        self.assertEqual([r['node'] for r in qualified(rows)],['good','slow-model'])

    def test_empty_or_failed_subscription_preserves_working_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);subscription=root/'source.yaml';provider=root/'qualified.yaml'
            provider.write_text('old working provider')
            cfg={'subscription_file':str(subscription),'role':{'provider_file':str(provider)}}
            subscription.write_text('proxies: []')
            self.assertEqual(reconcile(cfg,validate=lambda *_:[])['state'],'retained')
            subscription.write_text(yaml.safe_dump({'proxies':[{'name':'ASIA-Japan-test','type':'ss'}]}))
            self.assertEqual(reconcile(cfg,validate=lambda *_:[])['state'],'retained')
            self.assertEqual(provider.read_text(),'old working provider')

    def test_pending_subscription_change_is_not_marked_as_already_tested(self):
        from image_hub.proxy_qualifier import fingerprint
        original=[{'name':'ASIA-Japan-old','type':'ss'}]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'nodes.yaml';path.write_text('proxies: []')
            result=reconcile({'subscription_file':str(path)},validate=lambda *_:[],source=original)
            self.assertEqual(result['source_fingerprint'],fingerprint(original))

    @patch('nvidia_pool.egress.delay')
    def test_live_failover_uses_only_qualified_healthy_backup_without_restart(self,delay):
        class Client:
            def __init__(self):self.switches=[];self.current='old'
            def request(self,path,method='GET',body=None):
                if method=='PUT':self.switches.append(body['name']);self.current=body['name'];return {}
                return {'now':self.current,'all':['old','bad','good']}
        cfg={'role':{'group':'NVIDIA egress','mihomo_config':__file__}};client=Client()
        delay.side_effect=lambda _,name:120 if name=='good' else None
        self.assertTrue(failover(cfg,client));self.assertEqual(client.switches,['good'])
        delay.side_effect=lambda *_:None;client.switches=[]
        self.assertFalse(failover(cfg,client));self.assertEqual(client.switches,[])

    @patch('nvidia_pool.egress.publish_route')
    @patch('nvidia_pool.egress.delay')
    def test_failed_post_switch_verification_restores_the_previous_route(self,delay,publish):
        class Client:
            def __init__(self):self.current='old';self.switches=[]
            def request(self,path,method='GET',body=None):
                if method=='PUT':self.current=body['name'];self.switches.append(self.current);return {}
                return {'now':self.current,'all':['old','candidate']}
        client=Client();checks=iter([None,100,None])
        delay.side_effect=lambda *_:next(checks)
        self.assertFalse(failover({'role':{'group':'fixture','mihomo_config':__file__}},client))
        self.assertEqual(client.switches,['candidate','old'])
        self.assertEqual(client.current,'old')
        publish.assert_called_once_with({'group':'fixture','mihomo_config':__file__},'old')

    @patch('nvidia_pool.egress.delay',return_value=100)
    def test_healthy_live_proxy_is_kept_while_real_candidates_are_tested(self,_):
        class Client:
            def request(self,path,method='GET',body=None):
                assert method=='GET'
                return {'now':'healthy','all':['healthy','other']}
        self.assertFalse(failover({'role':{'group':'NVIDIA egress'}},Client()))
