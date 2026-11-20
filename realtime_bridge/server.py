"""Internal PCM WebSocket event bridge to the account's verified WebRTC route."""
import argparse
import asyncio
import base64
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import time
from urllib.parse import urlsplit

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioResampler
import httpx

MODELS = {'gpt-realtime-2.1', 'gpt-realtime-2.1-mini', 'gpt-realtime-2', 'gpt-realtime-1.5'}
UPSTREAM = 'https://chatgpt.com/backend-api/codex/realtime/calls'
MAX_LINE = 1024 * 1024
PCM = {'type': 'audio/pcm', 'rate': 24000}
PCM_CHUNK_BYTES = 24000 * 2 * 60 // 1000
AUDIO_DRAIN_IDLE_SECONDS = 5
AUDIO_DRAIN_LIMIT_SECONDS = 180
AUDIO_RTP_TAIL_SECONDS = .12
RESPONSE_FIELDS = {'instructions', 'output_modalities', 'max_output_tokens'}
WEB_SEARCH = {
    'type': 'function', 'name': 'web_search',
    'description': 'Search the public web for current factual information when the user asks to search or the answer needs up-to-date sources.',
    'parameters': {'type': 'object', 'properties': {
        'query': {'type': 'string', 'description': 'A concise web search query.'}},
        'required': ['query'], 'additionalProperties': False},
}
LOG = logging.getLogger(__name__)
RETRYABLE_SETUP = (asyncio.TimeoutError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def event_json(event):
    return json.dumps(event, ensure_ascii=False, separators=(',', ':'))


def error_event(code, message, event_id=None):
    return {'type': 'error', 'error': {'type': 'invalid_request_error', 'code': code,
                                      'message': message, 'event_id': event_id}}


def setup_request(init):
    model = init['model']
    proxy = urlsplit(init['proxy'])
    if (model not in MODELS or proxy.scheme != 'http' or proxy.hostname != '127.0.0.1'
            or not proxy.port or proxy.username or proxy.password or proxy.query or proxy.fragment):
        raise ValueError('invalid bridge setup')
    allowed = {'authorization', 'chatgpt-account-id', 'originator', 'version', 'user-agent'}
    headers = {k: v[0] for k, v in init['headers'].items() if k.lower() in allowed
               and isinstance(v, list) and len(v) == 1}
    if not any(k.lower() == 'authorization' and v.startswith('Bearer ') for k, v in headers.items()):
        raise ValueError('missing upstream authorization')
    return model, f'http://127.0.0.1:{proxy.port}', headers


class Session:
    def __init__(self, model, reader, writer, *, browser_tools=False):
        self.model, self.reader, self.writer = model, reader, writer
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTransceiver('audio', direction='recvonly')
        self.dc = self.pc.createDataChannel('oai-events')
        self.events = asyncio.Queue(128)
        self.ready, self.ended, self.audio_drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.active = self.settling = self.disconnected = False
        self.audio_drained.set()
        self.audio_part, self.audio_done, self.response_done = None, None, None
        self.pending_pcm = bytearray()
        self.first_audio_packet = True
        self.last_voice_activity = 0.0
        self.browser_tools = browser_tools
        self.pending_search_calls = set()
        self.input_bytes = 0
        self.tasks = set()
        self.dc.on('message', self.on_event)
        self.dc.on('close', self.ended.set)
        self.pc.on('track', lambda track: self.spawn(self.receive_audio(track)) if track.kind == 'audio' else None)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def emit(self, event):
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            self.ended.set()

    def on_event(self, raw):
        try:
            v = json.loads(raw)
            typ = v['type']
            if typ in ('session.created', 'session.updated'):
                if v['session']['model'] != self.model:
                    self.emit(error_event('upstream_model_mismatch', 'Upstream selected a different model.'))
                    self.ended.set()
                    self.ready.set()
                    return
                if typ == 'session.created':
                    self.ready.set()
            elif typ == 'response.content_part.added' and v.get('part', {}).get('type') in ('audio', 'output_audio'):
                self.flush_audio()
                self.audio_drained.clear()
                self.audio_part = {k: v[k] for k in ('response_id', 'item_id', 'output_index', 'content_index') if k in v}
                self.first_audio_packet = True
            elif typ == 'response.output_audio.done':
                self.audio_done = v
                return
            elif typ in ('output_audio_buffer.stopped', 'output_audio_buffer.cleared'):
                response_id = (self.audio_part or {}).get('response_id')
                if not response_id or not v.get('response_id') or response_id == v['response_id']:
                    self.audio_drained.set()
                return
            elif typ == 'response.done':
                if not self.active or self.response_done is not None:
                    self.ended.set()
                    return
                self.response_done = v
                self.last_voice_activity = time.monotonic()
                if self.browser_tools:
                    self.remember_search_calls(v['response'])
                self.spawn(self.finish_turn())
                return
            elif typ == 'error':
                # A rejected create must not make the session billable twice.
                if self.active and self.response_done is None:
                    self.emit(v)
                    self.ended.set()
                    return
            self.emit(v)
        except (ValueError, KeyError, TypeError):
            self.ended.set()

    async def receive_audio(self, track):
        resampler = AudioResampler(format='s16', layout='mono', rate=24000)
        while not self.ended.is_set():
            try:
                frame = await track.recv()
            except Exception:
                return
            for packet in resampler.resample(frame):
                if self.audio_part is not None and not self.disconnected:
                    pcm = memoryview(packet.planes[0]).cast('B')[:packet.samples * 2]
                    self.pending_pcm.extend(pcm)
                    if self.response_done is not None and any(abs(sample) > 64 for sample in pcm.cast('h')[::8]):
                        self.last_voice_activity = time.monotonic()
                    if self.first_audio_packet or len(self.pending_pcm) >= PCM_CHUNK_BYTES:
                        self.flush_audio()
                        self.first_audio_packet = False

    def flush_audio(self):
        if self.pending_pcm:
            self.emit({'type': 'response.output_audio.delta', **self.audio_part,
                       'delta': base64.b64encode(self.pending_pcm).decode()})
            self.pending_pcm.clear()

    def remember_search_calls(self, response):
        self.pending_search_calls.clear()
        if response.get('status') != 'completed':
            return
        for item in response.get('output') or []:
            if not isinstance(item, dict) or item.get('type') != 'function_call' or item.get('name') != 'web_search':
                continue
            call_id = item.get('call_id')
            if isinstance(call_id, str) and 0 < len(call_id) <= 128 and item.get('status', 'completed') == 'completed':
                self.pending_search_calls.add(call_id)
                if len(self.pending_search_calls) == 4:
                    break

    async def finish_turn(self):
        # The data-channel terminal can arrive before the final RTP packets.
        # Keep forwarding media while the output-buffer completion arrives.
        if self.audio_part is not None and not self.disconnected:
            deadline = time.monotonic() + AUDIO_DRAIN_LIMIT_SECONDS
            while not self.audio_drained.is_set():
                remaining = min(deadline, self.last_voice_activity + AUDIO_DRAIN_IDLE_SECONDS) - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.audio_drained.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
            if not self.disconnected:
                await asyncio.sleep(AUDIO_RTP_TAIL_SECONDS)
        self.flush_audio()
        self.audio_part = None
        if self.audio_done is not None:
            self.emit(self.audio_done)
        self.audio_done = None
        self.settling = True
        await self.events.put(self.response_done)

    def send(self, event):
        if self.dc.readyState != 'open' or self.dc.bufferedAmount > 256 * 1024:
            self.ended.set()
            raise ConnectionError('upstream data channel unavailable')
        self.dc.send(event_json(event))

    async def read_client(self):
        while not self.ended.is_set() or (self.active and self.response_done is not None):
            line = await self.reader.readline()
            v = json.loads(line) if line else {'type': 'bridge.disconnect'}
            typ = v.get('type')
            if typ == 'bridge.disconnect':
                self.disconnected = True
                self.audio_drained.set()
                if self.active and self.response_done is None and self.dc.readyState == 'open':
                    self.send({'type': 'response.cancel'})
                if not self.active or not line:
                    return
                asyncio.get_running_loop().call_later(8, self.ended.set)
                continue
            if typ == 'bridge.settled':
                if not self.settling:
                    raise ValueError('unexpected settlement')
                self.active = self.settling = False
                self.response_done = None
                if self.disconnected:
                    return
                continue
            try:
                self.apply_event(v)
            except (ValueError, TypeError, KeyError) as exc:
                event = error_event('invalid_realtime_event', str(exc), v.get('event_id'))
                self.emit({'type': 'bridge.create_rejected', 'event': event} if typ == 'response.create' else event)

    def apply_event(self, v):
        typ = v['type']
        if typ == 'input_audio_buffer.append':
            data = base64.b64decode(v['audio'], validate=True)
            if not data or len(data) % 2 or len(data) > 24000:
                raise ValueError('Append up to 0.5 seconds of PCM16 mono 24 kHz audio per event.')
            if self.input_bytes + len(data) > 30 * 48000:
                raise ValueError('Commit or clear the buffer before adding more than 30 seconds.')
            self.input_bytes += len(data)
            self.send({'type': typ, 'audio': v['audio']})
        elif typ in ('input_audio_buffer.commit', 'input_audio_buffer.clear'):
            if self.active:
                raise ValueError('Wait for the current response to finish before committing another turn.')
            self.send({'type': typ})
            self.input_bytes = 0
        elif typ == 'response.create':
            if self.active:
                raise ValueError('A response is already active.')
            raw = v.get('response') or {}
            tool_fields = {'tool_choice'} if self.browser_tools else set()
            if set(raw) - RESPONSE_FIELDS - tool_fields:
                raise ValueError('Unsupported response field.')
            out = self.response_options(raw, allow_named_search=self.browser_tools)
            self.pending_search_calls.clear()
            self.active = True
            self.send({'type': typ, 'response': out})
        elif typ == 'response.cancel':
            if self.active and self.response_done is None:
                self.send({'type': typ})
        elif typ == 'session.update':
            raw = v.get('session') or {}
            tool_fields = {'tools', 'tool_choice'} if self.browser_tools else set()
            if set(raw) - {'type', 'model', 'audio'} - RESPONSE_FIELDS - tool_fields:
                raise ValueError('Unsupported session field.')
            if raw.get('type', 'realtime') != 'realtime' or raw.get('model', self.model) != self.model:
                raise ValueError('The model and session type are fixed for this connection.')
            audio = raw.get('audio') or {}
            audio_in, audio_out = audio.get('input', {}), audio.get('output', {})
            if (set(audio) - {'input', 'output'} or set(audio_in) - {'format', 'turn_detection'}
                    or set(audio_out) - {'format', 'voice'} or audio_in.get('turn_detection') is not None
                    or audio_in.get('format', PCM) != PCM or audio_out.get('format', PCM) != PCM):
                raise ValueError('Use PCM16 mono 24 kHz and manual audio commit (turn_detection=null).')
            out = self.response_options(raw)
            out.update(type='realtime', audio={'input': {'turn_detection': None}})
            if 'tools' in raw:
                tools = raw['tools']
                if tools == []:
                    out['tools'] = []
                elif (isinstance(tools, list) and len(tools) == 1 and isinstance(tools[0], dict)
                      and tools[0].get('type') == 'function' and tools[0].get('name') == 'web_search'):
                    out['tools'] = [WEB_SEARCH]
                else:
                    raise ValueError('Only the site web search tool is available.')
            if voice := audio_out.get('voice'):
                if not isinstance(voice, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', voice):
                    raise ValueError('Invalid voice name.')
                out['audio']['output'] = {'voice': voice}
            self.send({'type': typ, 'session': out})
        elif typ == 'conversation.item.create':
            item = v.get('item') or {}
            if isinstance(item, dict) and item.get('type') == 'function_call_output':
                call_id, output = item.get('call_id'), item.get('output')
                if (not self.browser_tools or self.active or not isinstance(call_id, str)
                        or call_id not in self.pending_search_calls):
                    raise ValueError('No matching completed web search call.')
                if not isinstance(output, str) or not 0 < len(output.encode('utf-8')) <= 16384:
                    raise ValueError('Web search output must contain 1 to 16384 UTF-8 bytes.')
                self.send({'type': typ, 'item': {'type': 'function_call_output', 'call_id': call_id, 'output': output}})
                self.pending_search_calls.remove(call_id)
                return
            content = item.get('content') or []
            if (item.get('type') != 'message' or item.get('role') != 'user'
                    or not 1 <= len(content) <= 4 or any(x.get('type') != 'input_text'
                    or not isinstance(x.get('text'), str) or len(x['text']) > 4000 for x in content)):
                raise ValueError('Only short user text messages are supported by this route.')
            self.send({'type': typ, 'item': {'type': 'message', 'role': 'user',
                                           'content': [{'type': 'input_text', 'text': x['text']} for x in content]}})
        else:
            raise ValueError('Unsupported realtime event.')

    @staticmethod
    def response_options(raw, *, allow_named_search=False):
        options = {'max_output_tokens': raw.get('max_output_tokens', 256)}
        if 'tool_choice' in raw:
            choice = raw['tool_choice']
            if choice not in ('auto', 'none') and not (allow_named_search and choice == {'type': 'function', 'name': 'web_search'}):
                raise ValueError('Unsupported tool choice.')
            options['tool_choice'] = choice
        if type(options['max_output_tokens']) is not int or not 1 <= options['max_output_tokens'] <= 2048:
            raise ValueError('max_output_tokens must be between 1 and 2048.')
        if 'instructions' in raw:
            if not isinstance(raw['instructions'], str) or len(raw['instructions']) > 8000:
                raise ValueError('Instructions exceed the allowed length.')
            options['instructions'] = raw['instructions']
        if 'output_modalities' in raw:
            if raw['output_modalities'] not in (['audio'], ['text']):
                raise ValueError('Choose output_modalities=["audio"] or ["text"].')
            options['output_modalities'] = raw['output_modalities']
        return options

    async def flush(self):
        while True:
            event = await self.events.get()
            self.writer.write((event_json(event) + '\n').encode())
            await asyncio.wait_for(self.writer.drain(), timeout=4)

    async def prepare(self, client, headers):
        await self.pc.setLocalDescription(await self.pc.createOffer())
        session = {'type': 'realtime', 'model': self.model, 'output_modalities': ['audio'],
                   'audio': {'input': {'turn_detection': None}}}
        if self.model != 'gpt-realtime-1.5':
            session['reasoning'] = {'effort': 'minimal'}
        r = await client.post(UPSTREAM, headers=headers, json={'sdp': self.pc.localDescription.sdp, 'session': session})
        if r.status_code != 201 or not r.text.startswith('v=0'):
            if r.status_code in (502, 503, 504):
                raise httpx.RemoteProtocolError(f'Upstream gateway HTTP {r.status_code}')
            self.writer.write((event_json(error_event('upstream_session_rejected', f'Upstream session rejected (HTTP {r.status_code}).'))+'\n').encode())
            await self.writer.drain()
            return False
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=r.text, type='answer'))
        await self.ready.wait()
        if self.ended.is_set():
            error = self.events.get_nowait()
            self.writer.write((event_json(error) + '\n').encode())
            await self.writer.drain()
            return False
        return True

    async def run(self):
        outgoing = self.spawn(self.flush())
        incoming = self.spawn(self.read_client())
        peer_closed = self.spawn(self.ended.wait())
        completed, _ = await asyncio.wait((outgoing, incoming, peer_closed), timeout=600,
                                          return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
        # If the peer closes just after sending the terminal, it has already
        # reported billable usage. Let the local gateway finish settlement.
        if not self.active or outgoing.done() or incoming.done():
            return
        if not completed or self.response_done is not None:
            self.disconnected = True
            self.audio_drained.set()
            if self.response_done is None:
                self.send({'type': 'response.cancel'})
            async def settle():
                while self.active:
                    await asyncio.sleep(.05)
            await asyncio.wait_for(settle(), timeout=8)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await self.pc.close()


async def serve(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise RuntimeError('Bridge socket directory must be private.')
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    clients, tasks = {}, set()

    async def connection(reader, writer):
        session = None
        task = asyncio.current_task()
        if len(tasks) >= 8:
            writer.write((event_json(error_event('realtime_capacity', 'Realtime capacity is full.'))+'\n').encode())
            await writer.drain()
            writer.close()
            return
        tasks.add(task)
        try:
            init = json.loads(await asyncio.wait_for(reader.readline(), timeout=3))
            model, proxy, headers = setup_request(init)
            if proxy not in clients:
                if len(clients) >= 8:
                    raise ValueError('Too many upstream proxies.')
                clients[proxy] = httpx.AsyncClient(proxy=proxy, trust_env=False, follow_redirects=False,
                    limits=httpx.Limits(max_connections=8,max_keepalive_connections=4,keepalive_expiry=60),
                    timeout=httpx.Timeout(12,connect=5))
            started = time.monotonic()
            for attempt in range(2):
                session = Session(model, reader, writer, browser_tools=init.get('browser_tools') is True)
                try:
                    remaining = 30 - (time.monotonic() - started)
                    ready = await asyncio.wait_for(session.prepare(clients[proxy], headers), timeout=min(16, remaining))
                except RETRYABLE_SETUP as exc:
                    LOG.warning('Realtime setup model=%s attempt=%s elapsed_ms=%d error=%s',
                                model, attempt + 1, (time.monotonic() - started) * 1000, type(exc).__name__)
                    if attempt:
                        raise
                    await session.close()
                    session = None
                    continue
                # Input is read only after session.created. A new WebRTC offer
                # during setup cannot replay any user turn.
                if ready:
                    await session.run()
                break
        except (ValueError, OSError, KeyError, TypeError, asyncio.TimeoutError, httpx.HTTPError):
            if not writer.is_closing():
                try:
                    writer.write((event_json(error_event('realtime_bridge_unavailable', 'Realtime session could not continue.'))+'\n').encode())
                    await asyncio.wait_for(writer.drain(), timeout=1)
                except (OSError, asyncio.TimeoutError):
                    pass
        finally:
            if session:
                await session.close()
            writer.close()
            tasks.discard(task)

    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() and not path.is_socket():
            raise RuntimeError('Refusing to replace a non-socket file.')
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(connection, path=str(path), limit=MAX_LINE)
        path.chmod(0o600)
        async with server:
            await stop.wait()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for client in clients.values():
            await client.aclose()
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, default=Path('var/run/openai-realtime/bridge.sock'))
    asyncio.run(serve(parser.parse_args().socket))
