import asyncio
import base64
import json
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from av import AudioFrame

from realtime_bridge import server


class AudioTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader = asyncio.StreamReader()
        self.session = server.Session('gpt-realtime-2.1-mini', self.reader, None)
        self.session.active = True
        self.queue = asyncio.Queue()
        self.session.spawn(self.session.receive_audio(SimpleNamespace(recv=self.queue.get)))
        self.event('response.content_part.added', response_id='r', part={'type': 'audio'})
        self.addAsyncCleanup(self.session.close)

    def event(self, kind, **fields):
        self.session.on_event(json.dumps(dict(type=kind, **fields)))

    async def frame(self, value):
        data = struct.pack('<h', value) * 480
        frame = AudioFrame(format='s16', layout='mono', samples=480)
        frame.planes[0].update(data)
        frame.sample_rate = 24000
        self.queue.put_nowait(frame)
        await asyncio.sleep(0)
        return data

    def done(self):
        self.event('response.output_audio.done', response_id='r')
        self.event('response.done', response={'id': 'r', 'status': 'completed', 'usage': {}})

    async def collect_turn(self):
        events = []
        while not events or events[-1]['type'] != 'response.done':
            events.append(await asyncio.wait_for(self.session.events.get(), timeout=1))
        return events

    async def test_first_packet_immediate_then_coalesced_and_tail_preserved(self):
        expected = await self.frame(100)
        self.assertEqual(self.session.events.get_nowait()['type'], 'response.content_part.added')
        first = self.session.events.get_nowait()
        self.assertEqual(base64.b64decode(first['delta']), expected)
        expected = b''
        for value in range(101, 150):
            expected += await self.frame(value)
        self.done()
        self.event('output_audio_buffer.stopped', response_id='r')
        events = await self.collect_turn()
        chunks = [e for e in events if e['type'] == 'response.output_audio.delta']
        self.assertEqual(len(chunks), 17)  # 49 incoming RTP frames
        self.assertEqual(b''.join(base64.b64decode(e['delta']) for e in chunks), expected)
        self.assertEqual([e['type'] for e in events[-2:]], ['response.output_audio.done', 'response.done'])
        self.assertTrue(all(e['response_id'] == 'r' for e in chunks))

    async def test_ongoing_speech_extends_terminal_wait_and_stale_stop_is_ignored(self):
        with patch.object(server, 'AUDIO_DRAIN_IDLE_SECONDS', .09):
            self.done()
            self.event('output_audio_buffer.stopped', response_id='previous')
            for _ in range(9):
                await self.frame(1000)
                await asyncio.sleep(.02)
            self.assertFalse(self.session.settling)
            self.event('output_audio_buffer.stopped', response_id='r')
            await self.collect_turn()

    async def test_missing_stop_with_continuous_speech_has_a_hard_limit(self):
        with patch.object(server, 'AUDIO_DRAIN_LIMIT_SECONDS', .09):
            self.done()
            for _ in range(6):
                await self.frame(1000)
                await asyncio.sleep(.02)
            await self.collect_turn()

    async def test_peer_close_after_terminal_still_delivers_usage_exactly_once(self):
        sent = []
        def write(line):
            event = json.loads(line)
            sent.append(event['type'])
            if event['type'] == 'response.done':
                self.reader.feed_data(b'{"type":"bridge.settled"}\n')
        self.session.writer = SimpleNamespace(write=write, drain=lambda: asyncio.sleep(0))
        self.done()
        self.session.ended.set()
        await asyncio.wait_for(self.session.run(), timeout=1)
        self.assertEqual(sent.count('response.done'), 1)
        self.assertFalse(self.session.active)

    async def test_web_search_is_restricted_to_site_sessions_and_completed_calls(self):
        sent = []
        self.session.send = sent.append
        declared = {'type': 'function', 'name': 'web_search', 'description': 'client description'}
        update = {'type': 'session.update', 'session': {'tools': [declared], 'tool_choice': 'auto'}}
        choice = {'type': 'function', 'name': 'web_search'}
        create = {'type': 'response.create', 'response': {'tool_choice': choice}}
        self.session.active = False
        for event in (update, create):
            with self.assertRaises(ValueError):
                self.session.apply_event(event)
        self.session.browser_tools = True
        self.session.apply_event(update)
        self.assertEqual(sent[-1]['session']['tools'], [server.WEB_SEARCH])
        with self.assertRaises(ValueError):
            self.session.apply_event({'type': 'response.create', 'response': {'tool_choice': 'required'}})
        self.session.apply_event(create)
        self.assertEqual(sent[-1]['response']['tool_choice'], choice)
        output = {'type': 'conversation.item.create',
                  'item': {'type': 'function_call_output', 'call_id': 'call_one', 'output': '检索结果'}}
        self.event('response.done', response={'id': 'r', 'status': 'completed', 'output': [
            {'type': 'function_call', 'name': 'web_search', 'call_id': 'call_one', 'status': 'completed'}]})
        with self.assertRaises(ValueError):
            self.session.apply_event(output)
        self.event('output_audio_buffer.stopped', response_id='r')
        await self.collect_turn()
        self.reader.feed_data(b'{"type":"bridge.settled"}\n')
        self.session.spawn(self.session.read_client())
        await asyncio.sleep(0)
        for fields in ({'call_id': 'wrong'}, {'output': '语' * 6000}):
            with self.assertRaises(ValueError):
                self.session.apply_event(dict(output, item=dict(output['item'], **fields)))
        self.session.apply_event(output)
        self.assertEqual(sent[-1], output)
        with self.assertRaises(ValueError):
            self.session.apply_event(output)
        self.session.apply_event({'type': 'response.create', 'response': {'tool_choice': 'none'}})
        self.assertEqual(sent[-1]['response']['tool_choice'], 'none')
