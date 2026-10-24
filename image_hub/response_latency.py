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
        self.error_seen = self.cancelled = self.transport_failed = self.structured = False
        self.buffer = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def semantic(self, value, reasoning=False):
        if not isinstance(value, str) or not value.strip():
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

    def content(self, parts):
        if isinstance(parts, str):
            return self.semantic(parts)
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            kind = part.get('type', 'text')
            if kind in ('text', 'output_text', 'input_text', 'summary_text'):
                self.semantic(part.get('text'))
            elif kind in ('thinking', 'reasoning_text'):
                self.semantic(part.get('thinking') or part.get('text'), True)
            elif kind in ('tool_use', 'image', 'image_url', 'refusal'):
                self.structured = True
                if kind == 'refusal':
                    self.semantic(part.get('refusal'))

    def output_item(self, item):
        if not isinstance(item, dict):
            return
        kind = item.get('type', '')
        if kind in ('function_call', 'image_generation_call', 'web_search_call', 'code_interpreter_call', 'mcp_call'):
            self.structured = True
            self.tool_events += 1
        elif not self.streaming:
            if kind == 'message':
                self.content(item.get('content'))
            elif kind == 'reasoning':
                for part in item.get('summary') or []:
                    if isinstance(part, dict):
                        self.semantic(part.get('text'), True)

    def value(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get('type', '')
        self.error_seen |= bool(value.get('error')) or kind in ('error', 'response.failed')
        if kind == 'tool_call':
            self.tool_events += 1
        for choice in value.get('choices') or []:
            message = choice.get('delta') or choice.get('message') or {}
            self.content(message.get('content'))
            self.semantic(message.get('refusal'))
            self.semantic(message.get('reasoning_content') or message.get('reasoning'), True)
            if message.get('tool_calls') or message.get('function_call'):
                self.tool_events += 1
                self.structured = True
        if kind == 'content_block_delta':
            delta = value.get('delta') or {}
            self.semantic(delta.get('text'))
            self.semantic(delta.get('thinking'), True)
        if kind == 'content_block_start':
            block = value.get('content_block') or {}
            self.semantic(block.get('text'))
            self.semantic(block.get('thinking'), True)
            if block.get('type') == 'tool_use':
                self.tool_events += 1
                self.structured = True
        if kind in ('response.output_text.delta', 'response.refusal.delta', 'response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
            self.semantic(value.get('delta'), kind not in ('response.output_text.delta', 'response.refusal.delta'))
        for candidate in value.get('candidates') or []:
            for part in (candidate.get('content') or {}).get('parts') or []:
                self.semantic(part.get('text'), part.get('thought') is True)
                if part.get('functionCall') or part.get('inlineData'):
                    self.structured = True
                    self.tool_events += 1
            self.terminal |= bool(candidate.get('finishReason'))
        if kind in ('response.output_item.added', 'response.output_item.done'):
            self.output_item(value.get('item'))
        if not self.streaming:
            self.content(value.get('content'))
            for item in value.get('output') or []:
                self.output_item(item)
        self.terminal |= kind in ('message_stop', 'response.completed', 'response.incomplete')

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
        if self.cancelled:
            outcome = 'client_cancelled'
        elif self.transport_failed or self.status is not None and self.status >= 500:
            outcome = 'upstream_error'
        elif self.status == 429:
            outcome = 'rate_limited'
        elif self.status != 200:
            return None
        elif self.error_seen:
            outcome = 'upstream_error'
        elif self.truncated:
            outcome = 'observation_truncated'
        elif not self.terminal:
            outcome = 'incomplete_stream' if self.streaming else 'empty_response'
        elif self.first_text is None and self.first_reasoning is None and not self.structured and not self.tool_events:
            outcome = 'empty_response'
        else:
            outcome = 'completed'
        return {'model': self.model, 'protocol': self.kind, 'streaming': self.streaming,
                'status': self.status, 'outcome': outcome,
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
        if row.get('truncated') or row.get('input_bytes', MAX_FRAME) > 65536 or row.get('outcome', 'completed') != 'completed':
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


def summarize_reliability(records):
    outcomes = ('completed', 'upstream_error', 'incomplete_stream', 'empty_response',
                'client_cancelled', 'rate_limited', 'observation_truncated')
    groups = {}
    for row in records:
        outcome, model = row.get('outcome'), row.get('model')
        if outcome not in outcomes or not isinstance(model, str):
            continue
        counts = groups.setdefault(model, dict.fromkeys(outcomes, 0))
        counts[outcome] += 1
    result = []
    for model, counts in sorted(groups.items()):
        failures = sum(counts[name] for name in ('upstream_error', 'incomplete_stream', 'empty_response'))
        attempts = counts['completed']+failures
        result.append({'model': model, **counts, 'attempts': attempts, 'failures': failures,
                       'failure_rate': failures/attempts if attempts else 0})
    return result
