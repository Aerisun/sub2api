"""Observe delivery timing without persisting prompts, keys or model text."""
import codecs
import json
import math
import statistics
import time

PREFIX = 'sub2api_response_latency '
MAX_FRAME = 128*1024


class ResponseLatency:
    def __init__(self, model, kind, streaming, input_bytes, started=None, clock=time.monotonic):
        self.model, self.kind, self.streaming = model, kind, streaming
        self.input_bytes, self.clock = input_bytes, clock
        self.started = clock() if started is None else started
        self.status = None
        self.first_text = self.first_reasoning = self.last_semantic = None
        self.largest_gap = self.tool_events = 0
        self.terminal = self.truncated = self.discarding = self.finished = False
        self.buffer = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def semantic(self, value, reasoning=False):
        if not isinstance(value, str) or not value:
            return
        elapsed = round((self.clock()-self.started)*1000)
        if reasoning:
            if self.first_reasoning is None:
                self.first_reasoning = elapsed
        elif self.first_text is None:
            self.first_text = elapsed
        if self.last_semantic is not None:
            self.largest_gap = max(self.largest_gap, elapsed-self.last_semantic)
        self.last_semantic = elapsed

    def value(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get('type', '')
        if kind == 'tool_call':
            self.tool_events += 1
        for choice in value.get('choices') or []:
            message = choice.get('delta') or choice.get('message') or {}
            self.semantic(message.get('content'))
            self.semantic(message.get('reasoning_content') or message.get('reasoning'), True)
        if kind == 'content_block_delta':
            delta = value.get('delta') or {}
            self.semantic(delta.get('text'))
            self.semantic(delta.get('thinking'), True)
        if kind == 'content_block_start':
            block = value.get('content_block') or {}
            self.semantic(block.get('text'))
            self.semantic(block.get('thinking'), True)
        if kind in ('response.output_text.delta', 'response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
            self.semantic(value.get('delta'), kind != 'response.output_text.delta')
        for candidate in value.get('candidates') or []:
            for part in (candidate.get('content') or {}).get('parts') or []:
                self.semantic(part.get('text'), part.get('thought') is True)
            self.terminal |= bool(candidate.get('finishReason'))
        self.terminal |= kind in ('message_stop', 'response.completed')

    def feed(self, chunk):
        if self.finished:
            return
        if len(chunk) > MAX_FRAME or len(self.buffer)+len(chunk) > MAX_FRAME*2:
            self.truncated = self.discarding = True
            self.buffer = ''
            self.decoder.reset()
            return
        self.buffer = (self.buffer+self.decoder.decode(chunk)).replace('\r\n', '\n')
        if not self.streaming:
            return
        while '\n\n' in self.buffer:
            frame, self.buffer = self.buffer.split('\n\n', 1)
            if self.discarding or len(frame) > MAX_FRAME:
                self.discarding = False
                self.truncated = True
                continue
            payload = '\n'.join(line[5:].lstrip() for line in frame.split('\n') if line.startswith('data:'))
            if payload == '[DONE]':
                self.terminal = True
            elif payload:
                try:
                    self.value(json.loads(payload))
                except (ValueError, TypeError, AttributeError):
                    pass
        if len(self.buffer) > MAX_FRAME:
            self.buffer = self.buffer[-1:]
            self.truncated = self.discarding = True

    def finish(self):
        if self.finished:
            return None
        if not self.streaming and not self.truncated:
            try:
                self.value(json.loads(self.buffer))
                self.terminal = True
            except (ValueError, TypeError, AttributeError):
                pass
        self.finished = True
        self.buffer = ''
        if self.status != 200 or self.first_text is None and self.first_reasoning is None:
            return None
        return {'model': self.model, 'protocol': self.kind, 'streaming': self.streaming,
                'input_bytes': self.input_bytes, 'first_text_ms': self.first_text,
                'first_reasoning_ms': self.first_reasoning, 'largest_semantic_gap_ms': self.largest_gap,
                'total_ms': round((self.clock()-self.started)*1000), 'tool_events': self.tool_events,
                'terminal_seen': self.terminal, 'truncated': self.truncated}


def record(metric):
    if metric is None:
        return
    try:
        value = metric.finish()
        if value:
            print(PREFIX+json.dumps(value, ensure_ascii=False, separators=(',', ':')), flush=True)
    except Exception:
        pass  # Diagnostic failure must never fail, retry or delay a paid reply.


def summarize(records, minimum=3):
    groups = {}
    for row in records:
        if row.get('truncated') or row.get('input_bytes', MAX_FRAME) > 65536:
            continue
        first = row.get('first_text_ms')
        if not isinstance(first, (int, float)) or first < 0 or not isinstance(row.get('model'), str):
            continue
        key = row['model'], bool(row.get('streaming')), bool(row.get('tool_events'))
        groups.setdefault(key, []).append(row)
    result = []
    for (model, streaming, tools), rows in sorted(groups.items()):
        if len(rows) < minimum:
            continue
        times = sorted(row['first_text_ms'] for row in rows)
        gaps = sorted(row.get('largest_semantic_gap_ms', 0) for row in rows)
        result.append({'model': model, 'streaming': streaming, 'with_tools': tools, 'samples': len(rows),
                       'first_text_p50_ms': statistics.median(times),
                       'first_text_p95_ms': times[math.ceil(.95*len(times))-1],
                       'semantic_gap_p95_ms': gaps[math.ceil(.95*len(gaps))-1]})
    return result
