"""Route updates must preserve the complete stored connection record."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ops import nvidia_routes


class NvidiaRouteCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root/'secrets/nvidia').mkdir(parents=True)
        (root/'secrets/nvidia/internal_key.txt').write_text('fixture-pool-key\n')
        replacement = patch.object(nvidia_routes, 'ROOT', root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.ready = {'Nemotron-3.5':'nvidia/nemotron-3.5-lightning-30b-a3b'}

    def desired(self, current):
        return {**current, 'model_mapping':self.ready,
            'base_url':'http://127.0.0.1:8096','api_key':'fixture-pool-key',
            'openai_capabilities':['chat_completions'],'pool_mode':True,
            'pool_mode_retry_count':0,'pool_mode_retry_status_codes':[],
            'pool_manages_transient_errors':True}

    def test_retiring_a_model_preserves_other_stored_fields_and_internal_endpoint(self):
        current = {'api_key':'fixture-pool-key','base_url':'http://127.0.0.1:8096',
            'model_mapping':{**self.ready,'glm-5.3':'z-ai/glm-5.3'},
            'provider_setting':{'keep':'unchanged'},'api_protocol':'chat_completions'}
        expected = self.desired(current)
        with patch.object(nvidia_routes,'account_credentials',side_effect=[{66:current},{66:expected}]), \
                patch.object(nvidia_routes.api,'request') as request:
            nvidia_routes.sync_credentials('admin-fixture',66,self.ready)
        request.assert_called_once_with('/api/v1/admin/accounts/66','admin-fixture',
                                        {'credentials':expected},'PUT')
        self.assertEqual(expected['provider_setting'],current['provider_setting'])

    def test_matching_connection_is_not_rewritten(self):
        with patch.object(nvidia_routes,'account_credentials',return_value={66:self.desired({})}), \
                patch.object(nvidia_routes.api,'request') as request:
            nvidia_routes.sync_credentials('admin-fixture',66,self.ready)
        request.assert_not_called()

    def test_incomplete_save_is_reported_without_secrets(self):
        incomplete = {'api_key':'fixture-pool-key','model_mapping':self.ready}
        with patch.object(nvidia_routes,'account_credentials',return_value={66:incomplete}), \
                patch.object(nvidia_routes.api,'request'):
            with self.assertRaisesRegex(RuntimeError,'not saved completely') as failure:
                nvidia_routes.sync_credentials('admin-fixture',66,self.ready)
        self.assertNotIn('fixture-pool-key',str(failure.exception))

    def test_database_failure_does_not_fall_back_to_a_redacted_record(self):
        with patch.object(nvidia_routes,'account_credentials',side_effect=RuntimeError('fixture-read-error')), \
                patch.object(nvidia_routes.api,'request') as request:
            with self.assertRaisesRegex(RuntimeError,'fixture-read-error'):
                nvidia_routes.sync_credentials('admin-fixture',66,self.ready)
        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
