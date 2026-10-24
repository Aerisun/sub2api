"""Maintenance must preserve whole chat turns and the operator's timer state."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import Mock, patch

from ops import services


class ServiceDrainTests(unittest.TestCase):
    def restart(self, states, timer_active=True, commands=None):
        commands = commands or Mock()
        slots = SimpleNamespace(limit=2, acquire=lambda: nullcontext())
        with patch.object(services.admin_api, 'login', return_value='fixture'), \
                patch.object(services.admin_api, 'list_items', return_value=[]), \
                patch.object(services, 'systemctl', commands), \
                patch.object(services.subprocess, 'run', return_value=SimpleNamespace(returncode=0 if timer_active else 3)), \
                patch.object(services, 'active', side_effect=states), \
                patch.object(services, 'health', return_value={'status': 'ok'}), \
                patch.object(services, 'RECOGNITION_SLOTS', slots), \
                patch.object(services.time, 'sleep'), redirect_stdout(StringIO()):
            services.restart(['sub2api-agent-images'])
        return commands

    def test_chat_between_tool_legs_must_finish_before_addon_restart(self):
        states = [{'gateway': 0, 'chat_generations': 1},
                  {'gateway': 0, 'chat_generations': 0},
                  {'gateway': 0, 'chat_generations': 0},
                  {'gateway': 0, 'chat_generations': 0}]
        commands = self.restart(states)
        self.assertEqual(commands.call_args_list[1].args,
                         ('restart', 'sub2api-agent-images.service'))
        self.assertEqual(commands.call_args_list[-1].args,
                         ('start', 'sub2api-vision-monitor.timer'))

    def test_a_previously_stopped_probe_timer_stays_stopped(self):
        commands = self.restart([{'chat_generations': 0}]*3, timer_active=False)
        self.assertFalse(any(call.args[0] == 'start' for call in commands.call_args_list))

    def test_failed_durable_inspection_leaves_addons_running_and_restores_timer(self):
        commands = Mock()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.restart([subprocess.TimeoutExpired('fixture', 5)], commands=commands)
        self.assertFalse(any(call.args[0] == 'restart' for call in commands.call_args_list))
        self.assertEqual(commands.call_args_list[-1].args, ('start', 'sub2api-vision-monitor.timer'))

    def test_socket_counters_at_zero_do_not_hide_an_active_ui_turn(self):
        with patch.object(services.sqlite3, 'connect') as connect, \
                patch.object(services, 'redis_active', return_value=0), \
                patch.object(services, 'health', return_value={'inflight': 0}), \
                patch.object(services, 'durable_chat_active', return_value=1):
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (0,)
            self.assertEqual(services.active(), {'gateway': 0, 'nvidia': 0, 'qiniu': 0,
                                               'generating': 0, 'chat_generations': 1})

    def test_durable_query_is_bounded_and_credentials_are_not_command_arguments(self):
        values = {'DATABASE_HOST': '127.0.0.1', 'DATABASE_PORT': '55432',
                  'DATABASE_USER': 'fixture', 'DATABASE_DBNAME': 'fixture',
                  'DATABASE_PASSWORD': 'fixture-secret'}
        with patch.object(services, 'read_env', return_value=values), \
                patch.object(services.subprocess, 'run', return_value=SimpleNamespace(stdout='2\n')) as run:
            self.assertEqual(services.durable_chat_active(), 2)
        self.assertIn("SELECT COUNT(*) FROM chat_generations WHERE status='running'", run.call_args.args[0])
        self.assertNotIn('fixture-secret', ' '.join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertIn('statement_timeout=2000', run.call_args.kwargs['env']['PGOPTIONS'])


if __name__ == '__main__':
    unittest.main()
