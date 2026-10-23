import unittest
from image_hub.sse_terminal import SSETerminal


class SSETerminalTests(unittest.TestCase):
    def test_terminal_frames_split_at_every_boundary_ignore_literal_model_text(self):
        prefix=b'data: {"choices":[{"delta":{"content":"data: [DONE]"}}]}\r\n\r\n'
        done=b'data: [DONE]\r\n: comment\r\n\r\n'
        for split in range(1,len(done)):
            terminal=SSETerminal()
            self.assertIsNone(terminal.feed(prefix+done[:split]))
            self.assertEqual(terminal.feed(done[split:]+b'data: not part of response\n\n'),len(done)-split)
            self.assertTrue(terminal.finished)

    def test_multiline_payload_with_a_done_line_is_not_terminal(self):
        terminal=SSETerminal()
        self.assertIsNone(terminal.feed(b'data: [DONE]\ndata: extra\n\n'))
        self.assertEqual(terminal.feed(b'data: [DONE]\n\n'),14)

    def test_image_completion_requires_finish_and_measured_usage(self):
        terminal=SSETerminal(complete_on_usage=True)
        self.assertIsNone(terminal.feed(b'data: {"choices":[{"index":0,"delta":{"content":"red"}}]}\n\n'))
        self.assertIsNone(terminal.feed(b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'))
        self.assertIsNone(terminal.feed(b'data: {"choices":[],"usage":{"total_tokens":0}}\n\n'))
        usage=b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
        self.assertEqual(terminal.feed(usage),len(usage));self.assertFalse(terminal.explicit_done)

    def test_failed_or_unfinished_image_cannot_finish_from_partial_usage(self):
        terminal=SSETerminal(complete_on_usage=True)
        self.assertIsNone(terminal.feed(b'data: {"usage":{"total_tokens":12}}\n\n'))
        self.assertIsNone(terminal.feed(b'data: {"error":{"message":"failed"}}\n\n'))
        self.assertIsNone(terminal.feed(b'data: {"choices":[{"finish_reason":"stop"}]}\n\n'))

    def test_cumulative_usage_before_finish_cannot_replace_final_counts(self):
        terminal=SSETerminal(complete_on_usage=True)
        self.assertIsNone(terminal.feed(b'data: {"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10}}\n\n'))
        self.assertIsNone(terminal.feed(b'data: {"choices":[{"index":0,"finish_reason":"stop"}]}\n\n'))
        self.assertFalse(terminal.usage_seen)
        final=b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n'
        self.assertEqual(terminal.feed(final),len(final))
