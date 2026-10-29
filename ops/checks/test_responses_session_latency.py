"""Validate real-content timing and diagnostic cleanup with streamed fixtures."""
import json
import unittest
from unittest.mock import patch

import httpx

from ops.checks.responses_session_latency import drain_temporary_user, run_turn


class ResponsesSessionLatencyTests(unittest.TestCase):
    def turn(self, events, lines=0):
        def handler(request):
            data = ''.join('data: '+json.dumps(event)+'\n\n' for event in events)
            return httpx.Response(200, content=data)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return run_turn(client, 'fixture', 'gpt-6.1-sol', 'xhigh', 'priority',
                            'fixture', [], 0, stream_lines=lines)

    def completed(self):
        return {'type': 'response.completed', 'response': {'model': 'gpt-6.1-sol',
                'usage': {'input_tokens': 100, 'output_tokens': 6, 'total_tokens': 106}}}

    def test_whitespace_deltas_are_preserved_without_counting_as_first_text(self):
        events = [{'type': 'response.created'},
                  {'type': 'response.output_text.delta', 'delta': '\n'}]
        for number in range(1, 4):
            events.extend([{'type': 'response.output_text.delta', 'delta': str(number)},
                           {'type': 'response.output_text.delta', 'delta': '\n'}])
        row = self.turn(events+[self.completed()], lines=3)
        self.assertTrue(row['ok'])
        self.assertEqual(row['answer_characters'], 7)
        self.assertIsNotNone(row['first_text_ms'])

    def test_heartbeat_and_whitespace_do_not_become_success_or_first_text(self):
        row = self.turn([{'type': 'response.created'},
                         {'type': 'response.output_text.delta', 'delta': ' '}, self.completed()])
        self.assertFalse(row['ok'])
        self.assertIsNone(row['first_semantic_ms'])
        self.assertIsNone(row['first_text_ms'])
        self.assertEqual(row['error_type'], 'unexpected_output')

    def test_partial_answer_followed_by_failure_remains_a_failure(self):
        row = self.turn([{'type': 'response.output_text.delta', 'delta': '200'},
                         {'type': 'response.failed'}])
        self.assertFalse(row['ok'])
        self.assertEqual(row['error_type'], 'response.failed')
        self.assertIsNotNone(row['first_text_ms'])

    def test_eof_after_first_text_is_not_a_completed_answer(self):
        row = self.turn([{'type': 'response.output_text.delta', 'delta': '200'}])
        self.assertFalse(row['ok'])
        self.assertEqual(row['error_type'], 'incomplete_stream')

    def test_active_or_unverifiable_user_is_not_ready_for_deletion(self):
        with patch('ops.checks.responses_session_latency.temporary_user_active', return_value=1):
            self.assertFalse(drain_temporary_user(999, timeout=0))
        with patch('ops.checks.responses_session_latency.temporary_user_active', side_effect=OSError):
            self.assertFalse(drain_temporary_user(999, timeout=0))


if __name__ == '__main__':
    unittest.main()
