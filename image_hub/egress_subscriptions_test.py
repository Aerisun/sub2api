import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from image_hub.egress_subscriptions import merge_nodes, normalized_nodes, refresh, subscription_nodes


def node(name,server='192.0.2.1'):
    return {'name':name,'type':'ss','server':server,'port':443,'password':'fixture-secret'}


class SubscriptionTests(unittest.TestCase):
    def test_region_aliases_keep_credentials_private_and_skip_non_proxy_entries(self):
        values=[node('🇯🇵 JP-M1'),node('🇺🇸 [v6]US-B1'),node('🇸🇬 SG-M1'),node('Website: example.invalid')]
        nodes=normalized_nodes(values,'reserve')
        self.assertEqual(len(nodes),3)
        self.assertTrue(nodes[0]['name'].startswith('ASIA-Japan-'))
        self.assertEqual(nodes[0]['password'],'fixture-secret')
        self.assertTrue(all(n['name'].endswith(' [reserve]') for n in nodes))

    def test_same_node_deduplicates_but_distinct_credentials_with_same_name_survive(self):
        first=node('NA-UnitedStates-same')
        other={**first,'password':'different-secret'}
        nodes=merge_nodes([('',[first]),('reserve',[first,other])])
        self.assertEqual(len(nodes),2)
        self.assertEqual(nodes[0]['name'],first['name'])
        self.assertNotEqual(nodes[0]['name'],nodes[1]['name'])

    def test_empty_invalid_and_metadata_only_subscription_is_rejected(self):
        for value in ('proxies: []','bad yaml: [','<html>error</html>','proxies: [{name: expired}]'):
            with self.assertRaises(ValueError):subscription_nodes(value.encode())

    def test_failed_refresh_keeps_cached_subscription_and_existing_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            raw=yaml.safe_dump({'proxies':[node('NA-UnitedStates-primary')]}).encode()
            (p/'primary').write_bytes(raw)
            (p/'reserve').write_text(yaml.safe_dump({'proxies':[node('🇯🇵 JP-spare','192.0.2.2')]}))
            cfg={'primary_file':str(p/'primary'),'primary_cache_file':str(p/'primary-cache'),
                 'output_file':str(p/'combined'),'sources':[{'name':'reserve','cache_file':str(p/'reserve')}]}
            with patch('image_hub.egress_subscriptions.fetch_source',side_effect=RuntimeError('fixture network failure')):
                result=refresh(cfg)
                self.assertEqual(result['eligible_nodes'],2)
                original=(p/'combined').read_bytes()
                (p/'primary').write_text('broken: [')
                result=refresh(cfg)
                self.assertEqual(result['state'],'unchanged')
                self.assertEqual((p/'combined').read_bytes(),original)
                self.assertTrue(all(r['state']=='cached' for r in result['sources']))
                self.assertNotIn('fixture-secret',json.dumps(result))
