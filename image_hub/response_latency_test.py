from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import Mock

from image_hub.response_latency import MAX_FRAME, PREFIX, ResponseLatency, record, summarize, summarize_reliability


class ResponseLatencyTests(unittest.TestCase):
    def metric(self, streaming=True, kind='chat'):
        self.now = 0
        metric = ResponseLatency('fixture-model', kind, streaming, 123, clock=lambda: self.now)
        metric.status = 200
        return metric

    def event(self, metric, seconds, value):
        self.now = seconds
        metric.feed(('data: '+json.dumps(value, ensure_ascii=False)+'\n\n').encode())

    def test_real_text_is_distinct_from_early_thought_heartbeats_and_tool_output(self):
        m = self.metric()
        self.event(m, 1, {'choices': [{'delta': {'role': 'assistant'}}]})
        m.feed(b': heartbeat\n\n')
        self.event(m, 2, {'type': 'tool_call', 'delta': 'private tool output'})
        self.assertIsNone(m.first_text)
        self.event(m, 3, {'choices': [{'delta': {'reasoning_content': 'private reasoning'}}]})
        self.event(m, 20, {'choices': [{'delta': {'content': 'private answer'}}]})
        m.feed(b'data: [DONE]\n\n')
        with redirect_stdout(StringIO()) as output:
            record(m)
        row = json.loads(output.getvalue()[len(PREFIX):])
        self.assertEqual(row['first_reasoning_ms'], 3000)
        self.assertEqual(row['first_text_ms'], 20000)
        self.assertEqual(row['largest_semantic_gap_ms'], 17000)
        self.assertEqual(row['tool_events'], 1)
        self.assertTrue(row['terminal_seen'])
        self.assertNotIn('private', output.getvalue())
        self.assertIsNone(m.finish(), 'completed observations cannot be logged twice')

    def test_fragmented_unicode_and_crlf_do_not_create_fake_first_content(self):
        m = self.metric(); self.now = .25
        wire = ('data: '+json.dumps({'choices': [{'delta': {'content': '你好🌟'}}]}, ensure_ascii=False)+'\r\n\r\n').encode()
        for at in range(0, len(wire), 3):
            m.feed(wire[at:at+3])
        self.assertEqual(m.first_text, 250)
        self.assertEqual(m.buffer, '')

    def test_provider_protocols_keep_reasoning_and_text_separate(self):
        for kind, thought, text in [
            ('anthropic', {'type': 'content_block_delta', 'delta': {'thinking': 'thought'}},
             {'type': 'content_block_start', 'content_block': {'type': 'text', 'text': 'answer'}}),
            ('responses', {'type': 'response.reasoning_summary_text.delta', 'delta': 'thought'},
             {'type': 'response.output_text.delta', 'delta': 'answer'}),
            ('gemini', {'candidates': [{'content': {'parts': [{'thought': True, 'text': 'thought'}]}}]},
             {'candidates': [{'content': {'parts': [{'text': 'answer'}]}}]})
        ]:
            with self.subTest(kind=kind):
                m = self.metric(kind=kind)
                self.event(m, 1, thought); self.event(m, 5, text)
                row = m.finish()
                self.assertEqual(row['first_reasoning_ms'], 1000)
                self.assertEqual(row['first_text_ms'], 5000)

    def test_nonstreaming_latency_is_measured_at_completion(self):
        m = self.metric(streaming=False); self.now = 1
        m.feed(json.dumps({'choices': [{'message': {'content': 'answer'}}]}).encode())
        self.assertIsNone(m.first_text)
        self.now = 4
        self.assertEqual(m.finish()['first_text_ms'], 4000)

    def test_oversized_diagnostics_are_bounded_and_excluded_from_aggregates(self):
        m = self.metric(); m.feed(b'x'*(MAX_FRAME+1))
        self.assertLessEqual(len(m.buffer), MAX_FRAME)
        m.feed(b'\n\n')
        self.event(m, 3, {'choices': [{'delta': {'content': 'answer'}}]})
        row = m.finish()
        self.assertTrue(row['truncated'])
        self.assertEqual(summarize([row]*3), [])

    def test_rejected_or_content_free_requests_do_not_enter_success_metrics(self):
        m = self.metric(); self.event(m, 1, {'usage': {'total_tokens': 2}})
        row = m.finish()
        self.assertEqual(row['outcome'], 'incomplete_stream')
        self.assertEqual(summarize([row]*3), [])
        m = self.metric(); m.status = 401
        self.event(m, 1, {'choices': [{'delta': {'content': 'not accepted'}}]})
        self.assertIsNone(m.finish())

    def test_observer_failure_cannot_raise_through_the_http_handler(self):
        from image_hub.agent_server import Handler
        handler = object.__new__(Handler)
        handler.response_latency = Mock()
        handler.response_latency.feed.side_effect = RuntimeError('diagnostic unavailable')
        handler.observe_response(b'data: genuine wire bytes\n\n')
        self.assertIsNone(handler.response_latency)

    def test_aggregates_do_not_mix_tool_waits_nonstreaming_and_first_text(self):
        base = {'model': 'fixture', 'streaming': True, 'tool_events': 0, 'input_bytes': 100,
                'largest_semantic_gap_ms': 0, 'truncated': False}
        rows = [dict(base, first_text_ms=t) for t in (1000, 2000, 18000)]
        rows += [dict(base, first_text_ms=25000, tool_events=1)]*3
        rows += [dict(base, first_text_ms=30000, streaming=False)]*3
        groups = summarize(rows)
        plain = next(row for row in groups if row['streaming'] and not row['with_tools'])
        self.assertEqual(plain['first_text_p50_ms'], 2000)
        self.assertEqual(plain['first_text_p95_ms'], 18000)
        self.assertEqual(len(groups), 3)

    def test_http_200_does_not_hide_an_interrupted_answer_or_stream_error(self):
        for error in (False, True):
            m = self.metric()
            self.event(m, 1, {'choices': [{'delta': {'content': 'actual partial text'}}]})
            if error:
                self.event(m, 2, {'error': {'message': 'private provider error'}})
                m.feed(b'data: [DONE]\n\n')
            row = m.finish()
            self.assertEqual(row['outcome'], 'upstream_error' if error else 'incomplete_stream')
            self.assertEqual(summarize([row]*3), [])
            summary = summarize_reliability([row]*3)[0]
            self.assertEqual(summary['failures'], 3)
            self.assertEqual(summary['failure_rate'], 1)

    def test_failures_before_any_content_are_counted_without_logging_provider_text(self):
        for status in (500, 502, 503, 504):
            m = self.metric(); m.status = status
            m.feed(b'data: {"error":{"message":"private details"}}\n\n')
            with redirect_stdout(StringIO()) as output:
                record(m)
            row = json.loads(output.getvalue()[len(PREFIX):])
            self.assertEqual(row['outcome'], 'upstream_error')
            self.assertIsNone(row['first_text_ms'])
            self.assertNotIn('private', output.getvalue())
        m = self.metric(); m.status = None; m.transport_failed = True
        self.assertEqual(m.finish()['outcome'], 'upstream_error')

    def test_user_cancellation_and_limits_do_not_become_upstream_outages(self):
        cancelled = self.metric(); cancelled.cancelled = True
        limited = self.metric(); limited.status = 429
        counts = summarize_reliability([cancelled.finish(), limited.finish()])[0]
        self.assertEqual(counts['client_cancelled'], 1)
        self.assertEqual(counts['rate_limited'], 1)
        self.assertEqual(counts['failures'], 0)
        self.assertEqual(counts['attempts'], 0)

    def test_whitespace_deltas_do_not_hide_waiting_for_real_content(self):
        m = self.metric()
        self.event(m, .1, {'choices': [{'delta': {'content': ' \n\t'}}]})
        self.assertIsNone(m.first_text)
        self.event(m, 12, {'choices': [{'delta': {'content': 'answer'}}]})
        self.assertEqual(m.first_text, 12000)

    def test_nonstreaming_provider_replies_and_tool_only_outputs_are_valid(self):
        replies = [
            ('anthropic', {'content': [{'type': 'text', 'text': 'answer'}]}),
            ('responses', {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'answer'}]}]}),
            ('gemini', {'candidates': [{'content': {'parts': [{'text': 'answer'}]}, 'finishReason': 'STOP'}]})]
        for kind, body in replies:
            with self.subTest(kind=kind):
                m = self.metric(streaming=False, kind=kind); self.now = 3
                m.feed(json.dumps(body).encode()); row = m.finish()
                self.assertEqual(row['outcome'], 'completed')
                self.assertEqual(row['first_text_ms'], 3000)
        for kind, body in [('chat', {'choices': [{'message': {'tool_calls': [{'id': 'fixture'}]}}]}),
                           ('anthropic', {'content': [{'type': 'tool_use', 'name': 'fixture'}]}),
                           ('responses', {'output': [{'type': 'function_call', 'name': 'fixture'}]}),
                           ('gemini', {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'fixture'}}]}, 'finishReason': 'STOP'}]})]:
            with self.subTest(tool_protocol=kind):
                m = self.metric(streaming=False, kind=kind); m.feed(json.dumps(body).encode())
                row = m.finish(); self.assertEqual(row['outcome'], 'completed')
                self.assertIsNone(row['first_text_ms'])

    def test_completed_empty_output_is_distinct_from_diagnostic_loss(self):
        m = self.metric(); m.feed(b'data: [DONE]\n\n')
        self.assertEqual(m.finish()['outcome'], 'empty_response')
        m = self.metric(); m.feed(b'x'*(MAX_FRAME+1))
        row = m.finish()
        self.assertEqual(row['outcome'], 'observation_truncated')
        self.assertEqual(summarize_reliability([row]*3)[0]['failures'], 0)


if __name__ == '__main__':
    unittest.main()
