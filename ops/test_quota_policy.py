"""Guard must respect live account policy without trapping expired snapshots."""
from datetime import datetime, timezone
import unittest

from ops.quota_policy import probe_pause


class QuotaPolicyTests(unittest.TestCase):
    now = datetime(2026, 11, 2, 2, tzinfo=timezone.utc).timestamp()

    def extra(self, **updates):
        return {'extra': dict(codex_7d_used_percent=88, auto_pause_7d_threshold=.7,
            codex_usage_updated_at='2026-11-02T01:00:00Z',
            codex_7d_reset_at='2026-11-04T06:00:00Z', **updates)}

    def test_explicit_threshold_and_global_default_protect_the_account(self):
        account = self.extra()
        self.assertEqual(probe_pause(account, now=self.now)['reason'], 'quota_auto_pause_7d')
        account['extra'].pop('auto_pause_7d_threshold')
        self.assertIsNone(probe_pause(account, now=self.now))
        self.assertEqual(probe_pause(account, {'default_threshold_7d': .7}, self.now)['threshold'], .7)

    def test_raised_threshold_and_explicit_disable_are_respected(self):
        account = self.extra(); account['extra']['auto_pause_7d_threshold'] = .95
        self.assertIsNone(probe_pause(account, now=self.now))
        account['extra'].update(auto_pause_7d_threshold=.7, auto_pause_7d_disabled='true')
        self.assertIsNone(probe_pause(account, {'default_threshold_7d': .7}, self.now))

    def test_reset_and_two_hour_staleness_release_old_quota_snapshots(self):
        account = self.extra()
        self.assertIsNone(probe_pause(account, now=self.now+3600))
        account['extra']['codex_usage_updated_at'] = '2026-11-02T01:59:00Z'
        account['extra']['codex_7d_reset_at'] = '2026-11-02T01:59:00Z'
        self.assertIsNone(probe_pause(account, now=self.now))

    def test_relative_reset_anchors_to_snapshot_not_the_check_time(self):
        account = self.extra(); account['extra'].pop('codex_7d_reset_at')
        account['extra']['codex_7d_reset_after_seconds'] = 3600
        self.assertIsNone(probe_pause(account, now=self.now))

    def test_missing_snapshot_time_cannot_silently_bypass_protection(self):
        account = self.extra(); account['extra'].pop('codex_usage_updated_at')
        self.assertIsNotNone(probe_pause(account, now=self.now))

    def test_credit_reset_or_manually_disabled_accounts_defer_diagnostics(self):
        account = self.extra(auto_reset_credit_enabled=True)
        self.assertEqual(probe_pause(account, now=self.now)['reason'], 'credit_policy_managed')
        account = {'schedulable': False}
        self.assertEqual(probe_pause(account, now=self.now)['reason'], 'account_not_schedulable')


if __name__ == '__main__':
    unittest.main()
