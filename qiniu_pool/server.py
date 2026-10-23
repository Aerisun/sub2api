"""Authenticated loopback relay: one Sub2API account, bounded credential pool.

Response deltas are forwarded immediately. Requests preserve provider controls
while consuming local routing fields. No chat history or response cache is stored.
Conversation affinity retains the provider's prompt-cache locality.
"""
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import threading
import time

import httpx
from qiniu_pool.transport import ProxySafeTransport
from qiniu_pool.checkpoint import Checkpoint

from qiniu_pool.probe import sanitized

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "deployment/private/config/qiniu-pool.json"
MAX_BODY = 32 * 1024 * 1024


class Unavailable(Exception):
    def __init__(self, message, retry_after=1):
        super().__init__(message)
        self.retry_after = max(1, math.ceil(retry_after))


@dataclass
class Member:
    secret: str = field(repr=False)
    label: str
    inflight: int = 0
    peak: int = 0
    requests: int = 0
    completed: int = 0
    failures: int = 0
    disabled: bool = False
    cooldowns: dict = field(default_factory=dict)
    pending: bool = False


class Lease:
    def __init__(self, pool, index):
        self.pool, self.index, self.closed = pool, index, False

    @property
    def member(self):
        return self.pool.members[self.index]

    def close(self, success=False):
        with self.pool.condition:
            if self.closed:
                return
            self.closed = True
            member = self.member
            member.inflight -= 1
            self.pool.inflight -= 1
            if success:
                member.completed += 1
            else:
                member.failures += 1
            self.pool.condition.notify_all()


class Pool:
    def __init__(self, keys, config, clock=time.monotonic):
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("Expected unique provider credentials")
        self.members = [Member(secret=key, label=f"K{i+1:02d}") for i, key in enumerate(keys)]
        self.capacity = int(config.get("capacity", 4))
        self.per_key = int(config.get("per_key", 1))
        self.model_capacity = int(config.get("model_capacity", self.capacity))
        self.global_rpm = int(config.get("global_rpm", 5))
        self.model_rpm = int(config.get("model_rpm", 5))
        self.queue_seconds = min(float(config.get("queue_seconds", .15)), 1)
        if not 1 <= self.capacity <= 128 or not 1 <= self.per_key <= 16:
            raise ValueError("Invalid concurrency limits")
        self.clock = clock
        self.condition = threading.Condition()
        self.inflight = self.peak = self.cursor = 0
        self.model_inflight = {}
        self.model_peaks = {}
        self.starts = {}
        self.cooldowns = {}
        self.affinity = OrderedDict()
        self.route_affinity = OrderedDict()
        self.routes = config.get("routes", {})
        for source, targets in self.routes.items():
            if (source not in config.get("models", []) or not targets or
                    len({item["model"] for item in targets}) != len(targets) or
                    any(item["model"] not in config["models"] or
                        type(item.get("weight", 1)) is not int or item.get("weight", 1) < 1
                        for item in targets)):
                raise ValueError("Invalid model routes")
        self.route_requests = {}
        self.route_cursor = 0
        self.affinity_ttl = float(config.get("affinity_ttl", 3600))
        self.salt = secrets.token_bytes(32)
        self.affinity_hits = 0
        self.local_rejections = 0
        self.checkpoint = Checkpoint(config["state_path"], config.get("runtime_state_path")) if config.get("state_path") else None
        self.state_path = self.checkpoint.path if self.checkpoint else None
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            offset = time.time()-self.clock()
            for bucket, timestamps in state.get("starts", {}).items():
                self.starts[bucket] = deque(t-offset for t in timestamps if t > time.time()-60)
            self.cooldowns = {bucket: expiry-offset for bucket, expiry in state.get("cooldowns", {}).items()
                              if expiry > time.time()}
            if state.get("affinity_salt"):
                self.salt = bytes.fromhex(state["affinity_salt"])
            previous_members = state.get("affinity_members", [])
            if previous_members and previous_members == self._member_hashes()[:len(previous_members)]:
                self.affinity = OrderedDict((fp, (index, touched-offset))
                    for fp, index, touched in state.get("affinity", [])[-8192:]
                    if 0 <= index < len(self.members) and touched > time.time()-self.affinity_ttl)
            self.route_affinity = OrderedDict((fp, (model, touched-offset))
                for fp, model, touched in state.get("route_affinity", [])[-8192:]
                if touched > time.time()-self.affinity_ttl)

    def _member_hashes(self):
        return [hashlib.sha256(member.secret.encode()).hexdigest() for member in self.members]

    def _persist(self):
        with self.condition:
            if not self.state_path:
                return
            # These maps are ordered by their last touch. Expired cache homes must
            # not turn every later admission into a rewrite of hours of dead entries.
            cutoff = self.clock()-self.affinity_ttl
            for bindings in (self.affinity, self.route_affinity):
                while bindings:
                    if next(iter(bindings.values()))[1] > cutoff:
                        break
                    bindings.popitem(last=False)
            offset = time.time()-self.clock()
            state = {"starts": {bucket: [stamp+offset for stamp in stamps] for bucket,stamps in self.starts.items()},
                     "cooldowns": {bucket: expiry+offset for bucket,expiry in self.cooldowns.items()},
                     "affinity_salt": self.salt.hex(), "affinity_members": self._member_hashes(),
                     "affinity": [[fp, index, touched+offset] for fp, (index, touched) in self.affinity.items()],
                     "route_affinity": [[fp, model, touched+offset] for fp, (model, touched) in self.route_affinity.items()]}
            state.update(self.persistent_extra())
            self.checkpoint.write(state, scheduler=self.condition)

    def persistent_extra(self):
        return {}

    def route_plan(self, model, affinity):
        targets = self.routes.get(model)
        if not targets:
            return [model]
        with self.condition:
            now = self.clock()
            remembered = self.route_affinity.get(affinity) if affinity else None
            choices = [item["model"] for item in targets]
            if remembered and remembered[0] in choices and remembered[1]+self.affinity_ttl > now:
                primary = remembered[0]
            else:
                total = sum(item.get("weight", 1) for item in targets)
                slot = (int(affinity[:16], 16) if affinity else self.route_cursor) % total
                self.route_cursor += 1
                for item in targets:
                    slot -= item.get("weight", 1)
                    if slot < 0:
                        primary = item["model"]
                        break
            return [primary]+[target for target in choices if target != primary]

    def remember_route(self, logical, affinity, physical):
        if logical not in self.routes:
            return
        with self.condition:
            if affinity:
                self.route_affinity[affinity] = (physical, self.clock())
                self.route_affinity.move_to_end(affinity)
                while len(self.route_affinity) > 8192:
                    self.route_affinity.popitem(last=False)
            counts = self.route_requests.setdefault(logical, {})
            counts[physical] = counts.get(physical, 0)+1
            self._persist()

    def fingerprint(self, body, headers):
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        legacy_user_id = metadata.get("user_id")
        user_id = legacy_user_id
        if isinstance(user_id,str) and user_id.lstrip().startswith('{'):
            try:
                identity = json.loads(user_id)
                if isinstance(identity,dict):
                    user_id = identity.get('session_id') or user_id
            except ValueError:
                pass
        explicit = (body.get("prompt_cache_key") or body.get("conversation_id") or
                    metadata.get("conversation_id") or metadata.get("session_id") or
                    user_id or headers.get("X-Session-ID") or
                    headers.get("X-Conversation-ID"))
        if explicit:
            value = [body.get("model"), str(explicit)]
        else:
            prefix = []
            for message in body.get("messages") or []:
                if message.get("role") in ("system", "developer", "user"):
                    prefix.append(self._cache_prefix_message(message))
                if message.get("role") == "user":
                    break
            if not prefix:
                return None
            value = [body.get("model"), prefix]
        def digest(item):
            return hmac.new(self.salt,json.dumps(item,sort_keys=True,ensure_ascii=False,
                separators=(",",":")).encode(),hashlib.sha256).hexdigest()
        result = digest(value)
        if explicit and user_id != legacy_user_id and explicit == user_id:
            legacy = digest([body.get("model"),str(legacy_user_id)])
            with self.condition:
                for table in (self.affinity,self.route_affinity):
                    if result not in table and legacy in table:
                        table[result] = table[legacy]
        return result

    @staticmethod
    def _cache_prefix_message(message):
        # A moving cache breakpoint is a transport hint, not a new conversation.
        def clean(value):
            if isinstance(value,dict):
                return {key:clean(item) for key,item in value.items() if key!='cache_control'}
            if isinstance(value,list):
                return [clean(item) for item in value]
            return value
        result = clean(message)
        content = result.get('content')
        if isinstance(content,list) and all(isinstance(block,dict) and block.get('type')=='text' for block in content):
            result['content'] = ''.join(block.get('text','') for block in content)
        return result

    def _quota_wait(self, bucket, limit, now):
        if limit <= 0:
            return 0
        starts = self.starts.setdefault(bucket, deque())
        while starts and starts[0] <= now - 60:
            starts.popleft()
        return starts[0] + 60 - now if len(starts) >= limit else 0

    def acquire(self, model, affinity=None, exclude=()):
        deadline = self.clock() + self.queue_seconds
        with self.condition:
            while True:
                now = self.clock()
                while self.affinity:
                    _, (_, touched) = next(iter(self.affinity.items()))
                    if touched + self.affinity_ttl > now:
                        break
                    self.affinity.popitem(last=False)
                delay = max(self.cooldowns.get("*", 0), self.cooldowns.get(model, 0)) - now
                delay = max(delay, self._quota_wait("*", self.global_rpm, now),
                            self._quota_wait(model, self.model_rpm, now))
                if delay > 0:
                    self.local_rejections += 1
                    raise Unavailable("Provider request limit is cooling down", delay)
                candidates = [i for i, m in enumerate(self.members)
                              if i not in exclude and not m.disabled and not m.pending and m.inflight < self.per_key
                              and max(m.cooldowns.get("*", 0), m.cooldowns.get(model, 0)) <= now]
                if candidates and self.inflight < self.capacity and self.model_inflight.get(model, 0) < self.model_capacity:
                    preferred = self.affinity.get(affinity) if affinity else None
                    if preferred and preferred[0] in candidates:
                        index = preferred[0]
                        self.affinity_hits += 1
                    else:
                        index = min(candidates, key=lambda i: (self.members[i].inflight,
                                    self.members[i].requests, (i-self.cursor) % len(self.members)))
                    self.cursor = (index + 1) % len(self.members)
                    if affinity:
                        # Keep the warm home key when concurrent work temporarily spills.
                        home = preferred[0] if preferred and not self.members[preferred[0]].disabled else index
                        self.affinity[affinity] = (home, now)
                        self.affinity.move_to_end(affinity)
                        while len(self.affinity) > 8192:
                            self.affinity.popitem(last=False)
                    self.inflight += 1
                    self.peak = max(self.peak, self.inflight)
                    self.model_inflight[model] = self.model_inflight.get(model, 0) + 1
                    self.model_peaks[model] = max(self.model_peaks.get(model, 0), self.model_inflight[model])
                    member = self.members[index]
                    member.inflight += 1
                    member.peak = max(member.peak, member.inflight)
                    member.requests += 1
                    if self.global_rpm:
                        self.starts.setdefault("*", deque()).append(now)
                    if self.model_rpm:
                        self.starts.setdefault(model, deque()).append(now)
                    try:
                        self._persist()
                    except OSError:
                        member.inflight -= 1
                        self.inflight -= 1
                        self.model_inflight[model] -= 1
                        self.condition.notify_all()
                        raise Unavailable("Request-limit state could not be saved")
                    return Lease(self, index)
                if now >= deadline or not any(not m.disabled for m in self.members):
                    self.local_rejections += 1
                    raise Unavailable("Credential pool is busy; retry shortly")
                expiries = [max(m.cooldowns.get("*", 0), m.cooldowns.get(model, 0))-now
                            for i, m in enumerate(self.members) if not m.disabled and i not in exclude]
                next_expiry = min((value for value in expiries if value > 0), default=deadline-now)
                self.condition.wait(min(deadline-now, next_expiry))

    def release(self, lease, model, success):
        with self.condition:
            if not lease.closed:
                self.model_inflight[model] = max(0, self.model_inflight.get(model, 1) - 1)
            lease.close(success)

    def error(self, lease, model, status, retry_after=None):
        with self.condition:
            now = self.clock()
            if status == 401:
                lease.member.disabled = True
            elif status == 429:
                # Shared UID/model quotas must not be bypassed by rotating keys.
                delay = max(1, retry_after or 60)
                self.cooldowns["*" if self.global_rpm else model] = now + delay
            elif status == 402:
                self.cooldowns["*"] = now + max(60, retry_after or 900)
            elif status in (502, 503, 504):
                lease.member.cooldowns[model] = now + max(1, retry_after or 3)
            self._persist()

    def snapshot(self):
        with self.condition:
            now = self.clock()
            return {"capacity": self.capacity, "per_key": self.per_key,
                    "model_capacity": self.model_capacity, "global_rpm": self.global_rpm,
                    "model_rpm": self.model_rpm, "inflight": self.inflight, "peak": self.peak,
                    "model_peaks": dict(self.model_peaks),
                    "available_keys": sum(not m.disabled and not m.pending for m in self.members),
                    "affinity_entries": len(self.affinity), "affinity_hits": self.affinity_hits,
                    "route_affinity_entries": len(self.route_affinity),
                    "route_requests": {model: dict(counts) for model, counts in self.route_requests.items()},
                    "local_rejections": self.local_rejections,
                    "checkpoint": self.checkpoint.status() if self.checkpoint else None,
                    "remaining_rpm": {bucket:max(0,(self.global_rpm if bucket == "*" else self.model_rpm)-sum(t > now-60 for t in stamps))
                                      for bucket,stamps in self.starts.items()},
                    "cooldowns_seconds": {k: math.ceil(v-now) for k,v in self.cooldowns.items() if v > now},
                    "members": [{"label": m.label, "disabled": m.disabled, "inflight": m.inflight,
                                 "peak": m.peak, "requests": m.requests, "completed": m.completed,
                                 "failures": m.failures} for m in self.members]}


def retry_seconds(value):
    if not value:
        return None
    try:
        return max(1, float(value))
    except ValueError:
        try:
            return max(1, (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass

    def reply(self, status, data, retry_after=None, upstream_model=None):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if upstream_model:
            self.send_header("X-Sub2api-Upstream-Model", upstream_model)
        if retry_after:
            self.send_header("Retry-After", str(max(1, math.ceil(retry_after))))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""),
                                   "Bearer " + self.server.internal_key)

    def do_GET(self):
        if self.server.public_asset(self):return
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "service": "qiniu-pool", **self.server.pool.snapshot(),
                                    **self.server.runtime_policy()})
        if not self.authorized():
            return self.reply(401, {"error": {"message": "Invalid internal credential", "type": "authentication_error"}})
        if self.path == "/v1/models":
            return self.reply(200, {"object": "list", "data": [
                {"id": model, "object": "model", "owned_by": "qiniu"} for model in self.server.models]})
        return self.reply(404, {"error": {"message": "Unknown endpoint"}})

    def do_HEAD(self):
        if not self.server.public_asset(self,head=True):
            self.send_response(404);self.send_header('Content-Length','0');self.end_headers()

    def read_body(self):
        limit=self.server.max_request_body
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        length = self.headers.get("Content-Length")
        if encoding:
            if encoding != "chunked" or length is not None:
                raise ValueError("Unsupported or ambiguous transfer encoding")
            chunks, count = [], 0
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise ValueError("Invalid chunk framing")
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size < 0 or count+size > limit:
                    raise ValueError("Request too large")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if trailer == b"\r\n":
                            return b"".join(chunks)
                        if not trailer or not trailer.endswith(b"\r\n"):
                            raise ValueError("Invalid chunk trailer")
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("Incomplete chunk")
                chunks.append(chunk)
                count += size
        if length is None or not 0 < int(length) <= limit:
            raise ValueError("Invalid content length")
        raw = self.rfile.read(int(length))
        if len(raw) != int(length):
            raise ValueError("Incomplete request body")
        return raw

    def do_POST(self):
        self.connection.settimeout(150)
        # Close rejected/unread POST bodies to prevent keepalive desynchronization.
        if self.path != "/v1/chat/completions" or not self.authorized():
            self.close_connection = True
            return self.reply(404 if self.path != "/v1/chat/completions" else 401,
                              {"error": {"message": "Unknown endpoint or invalid internal credential"}})
        try:
            raw = self.read_body()
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("model") not in self.server.models:
                return self.reply(404, {"error": {"message": "Model is not in the verified allowlist"}})
        except (ValueError, TypeError):
            self.close_connection = True
            return self.reply(400, {"error": {"message": "Invalid request body", "type": "invalid_request_error"}})
        pool, logical = self.server.pool, body["model"]
        logical_affinity = pool.fingerprint(body, self.headers)
        plan = pool.route_plan(logical, logical_affinity)
        model = plan[0]
        excluded = {}
        request_started = time.monotonic()
        policy = self.server.request_policy(body)
        pre_deadline = request_started + policy['pre_output_timeout'] if policy['pre_output_timeout'] else 0
        deadline = request_started + policy['response_timeout'] if policy['response_timeout'] else 0
        for attempt in range(self.server.max_attempts):
            if pre_deadline and time.monotonic() >= pre_deadline:
                return self.reply(504, {"error": {"message": "Upstream response deadline exceeded", "type": "upstream_timeout"}})
            native = {**body, "model": model}
            affinity = pool.fingerprint(native, self.headers)
            if model.rsplit("/", 1)[-1] in ("kimi-k3", "kimi-k3-free"):
                # Local routing identifiers are not accepted by K3's strict API.
                for field in ("prompt_cache_key", "metadata", "conversation_id"):
                    native.pop(field, None)
                if "max_completion_tokens" in native:
                    native.setdefault("max_tokens", native.pop("max_completion_tokens"))
            try:
                native = self.server.prepare_request(native)
            except ValueError as error:
                local_error=self.server.local_request_error(error)
                if local_error:
                    return self.reply(local_error[0],{'error':{'message':local_error[1],
                        'type':'invalid_request_error' if local_error[0]==400 else 'upstream_error'}})
                return self.reply(400, {"error": {"message": "Invalid model controls", "type": "invalid_request_error"}})
            upstream_raw = raw if native == body else json.dumps(native, ensure_ascii=False, separators=(",", ":")).encode()
            try:
                lease = self.server.acquire_request(model, affinity, excluded.get(model, []), native)
            except Unavailable as error:
                if attempt == 0 and len(plan) > 1:
                    model = plan[1]
                    continue
                status = getattr(error, "status", 429)
                return self.reply(status, {"error": {"message": str(error), "type": "rate_limit_error" if status == 429 else "upstream_error"}}, error.retry_after)
            lease_model = model
            success = started = False
            upstream_started = time.monotonic()
            try:
                headers = {"Authorization": "Bearer " + lease.member.secret,
                           "Content-Type": "application/json", "Accept-Encoding": "identity"}
                with self.server.upload_context(native) as upload, self.server.client.stream("POST", self.server.upstream+"/v1/chat/completions",
                                               headers=headers, content=upstream_raw if upload is native else json.dumps(upload,ensure_ascii=False,separators=(',',':')).encode(),
                                               **self.server.request_options(pre_deadline, body)) as response:
                    wait = retry_seconds(response.headers.get("Retry-After"))
                    if response.status_code != 200:
                        error = self.server.sanitize(self.server.read_response(response, pre_deadline).decode(errors="replace")) if response.status_code == 403 else None
                        self.server.record_response_error(lease, model, response.status_code, wait, error)
                        if attempt + 1 < self.server.max_attempts and self.server.retry_status(response.status_code, len(plan) > 1, error):
                            excluded.setdefault(model, []).append(lease.index)
                            if response.status_code != 401 and len(plan) > 1:
                                model = plan[1]
                            continue
                        if error is None:
                            error = self.server.sanitize(self.server.read_response(response, pre_deadline).decode(errors="replace"))
                        pool.release(lease,lease_model,False)
                        if response.status_code == 401:
                            return self.reply(503, {"error": {"message": "Provider credentials are unavailable",
                                                              "type": "upstream_auth_error"}}, 1)
                        return self.reply(response.status_code, {"error": {
                            "message": error, "type": "upstream_error"}}, wait or (60 if response.status_code == 429 else None))
                    if not body.get("stream"):
                        data = self.server.read_nonstream(response, pre_deadline, deadline)
                        if not data or len(data) > MAX_BODY:
                            raise ValueError("Empty or oversized upstream response")
                        value = json.loads(data)
                        if not isinstance(value,dict) or not isinstance(value.get("choices"),list) or not value["choices"] or value.get("error"):
                            raise ValueError("Malformed upstream response")
                        if self.server.require_semantic_first_event and not any(
                            isinstance(choice,dict) and isinstance(choice.get("message"),dict) and
                            any(choice["message"].get(field) for field in ("content","reasoning_content","reasoning","thinking","tool_calls","function_call"))
                            for choice in value["choices"]
                        ):
                            raise ValueError("Malformed upstream response")
                        pool.remember_route(logical, logical_affinity, model)
                        success = True
                        pool.release(lease,lease_model,True)
                        self.reply(200, data, upstream_model=model)
                        return
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise ValueError("Upstream did not return an event stream")
                    iterator = response.iter_raw()
                    first = b""
                    valid_event = False
                    for chunk in iterator:
                        if pre_deadline and time.monotonic() >= pre_deadline:
                            raise ValueError("Upstream exceeded first-output deadline")
                        if policy['first_event_seconds'] and time.monotonic()-upstream_started > policy['first_event_seconds']:
                            raise ValueError("Upstream exceeded first-output deadline")
                        first += chunk
                        if len(first) > 131072:
                            raise ValueError("Missing first upstream event")
                        for line in first.split(b"\n")[:-1]:
                            if not line.startswith(b"data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == b"[DONE]":
                                raise ValueError("Empty upstream stream")
                            event = json.loads(payload)
                            if not isinstance(event,dict) or event.get("error"):
                                raise ValueError("Upstream stream failed before content")
                            if event.get("choices") is not None and (not isinstance(event["choices"],list) or
                                    any(not isinstance(choice,dict) or not isinstance(choice.get("delta") or {},dict) for choice in event["choices"])):
                                raise ValueError("Malformed upstream response")
                            valid_event = bool(event.get("choices"))
                            if valid_event and self.server.require_semantic_first_event:
                                valid_event = self.server.first_event_ready(event,body)
                            if valid_event:
                                break
                        if valid_event:
                            break
                    if not valid_event:
                        raise ValueError("Upstream returned no model events")
                    pool.remember_route(logical, logical_affinity, model)
                    terminal=self.server.stream_terminal(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("X-Sub2api-Upstream-Model", model)
                    self.send_header("Transfer-Encoding", "chunked")
                    if terminal is not None:
                        # Provider cleanup must not delay a completed caller or
                        # the next request reusing this local connection.
                        self.send_header('Connection','close')
                        self.close_connection=True
                    for name in ("X-Request-ID", "Request-ID"):
                        if response.headers.get(name):
                            self.send_header(name, response.headers[name])
                    self.end_headers()
                    started = True
                    def emit(chunk):
                        self.wfile.write(f"{len(chunk):x}\r\n".encode()+chunk+b"\r\n")
                        self.wfile.flush()
                    end=terminal.feed(first) if terminal is not None else None
                    if end is not None:first=first[:end]
                    emit(first)
                    tail = first[-64:]
                    completed = end is not None if terminal is not None else b"data: [DONE]" in first
                    for chunk in (() if completed and terminal is not None else iterator):
                        if deadline and time.monotonic() >= deadline:
                            raise TimeoutError("Upstream response deadline exceeded")
                        if chunk:
                            end=terminal.feed(chunk) if terminal is not None else None
                            completed |= end is not None if terminal is not None else b"data: [DONE]" in tail+chunk
                            tail = (tail+chunk)[-64:]
                            emit(chunk if end is None else chunk[:end])
                            if end is not None:break
                    if not completed:
                        emit(b'data: {"error":{"type":"upstream_error","message":"Upstream stream ended before completion"}}\n\n')
                    elif terminal is not None and not terminal.explicit_done:
                        # Original finish and usage frames have been forwarded.
                        # Complete the transport envelope without inventing
                        # model content or waiting for a late provider sentinel.
                        emit(b'data: [DONE]\n\n')
                    success=completed
                    pool.release(lease,lease_model,success)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    if terminal is not None and completed:
                        self.server.drain_completed(response,iterator,deadline)
                    return
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return
            except (httpx.HTTPError, ValueError, OSError) as error:
                local_error=self.server.local_request_error(error)
                if local_error and not started:
                    pool.release(lease,lease_model,False)
                    return self.reply(local_error[0],{'error':{'message':local_error[1],'type':'rate_limit_error'}},1)
                self.server.record_upstream_exception(model, error)
                pool.error(lease, model, 502)
                if not started:
                    if attempt + 1 < self.server.max_attempts and (len(plan) > 1 or self.server.may_retry_transport(error)):
                        excluded.setdefault(model, []).append(lease.index)
                        if len(plan) > 1:
                            model = plan[1]
                        continue
                    pool.release(lease,lease_model,False)
                    return self.reply(502, {"error": {"message": "Upstream response failed", "type": "upstream_error"}})
                pool.release(lease,lease_model,False)
                if self.server.finish_error_stream:
                    try:
                        emit(b'data: {"error":{"type":"upstream_error","message":"Upstream stream failed; partial output was not replayed"}}\n\n')
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                self.close_connection = True
                return
            finally:
                pool.release(lease, lease_model, success)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    pool_class = Pool
    max_request_body = MAX_BODY
    retry_transport_errors = False
    max_attempts = 2
    pre_output_timeout = 0
    response_timeout = 0
    finish_error_stream = False
    sanitize = staticmethod(sanitized)

    def prepare_request(self, body):
        return body

    def acquire_request(self, model, affinity, exclude, body):
        return self.pool.acquire(model, affinity, exclude)

    def may_retry_transport(self, error):
        return self.retry_transport_errors

    def retry_status(self, status, has_fallback, message=None):
        return status in (401, 502, 503, 504) or (status == 429 and has_fallback)

    def record_response_error(self, lease, model, status, retry_after, message=None):
        self.pool.error(lease, model, status, retry_after)

    def upstream_options(self, pre_deadline):
        return {}

    def request_policy(self, body):
        return {'pre_output_timeout':self.pre_output_timeout,
                'response_timeout':self.response_timeout,'first_event_seconds':self.first_event_seconds}

    def stream_terminal(self, body):
        return None

    def first_event_ready(self,event,body):
        return any(any((choice.get('delta') or {}).get(field) for field in
            ('content','reasoning_content','reasoning','thinking','tool_calls','function_call'))
            for choice in event.get('choices',[]))

    def drain_completed(self,response,iterator,deadline):
        response.close()

    def request_options(self, pre_deadline, body):
        return self.upstream_options(pre_deadline)

    @contextmanager
    def upload_context(self, body):
        yield body

    def public_asset(self, handler, head=False):
        return False

    def local_request_error(self, error):
        return None

    def read_response(self, response, deadline):
        return response.read()

    def read_nonstream(self, response, pre_deadline, deadline):
        return self.read_response(response,pre_deadline)

    def record_upstream_exception(self, model, error):
        pass

    def runtime_policy(self):
        return {}

    def __init__(self, address, config, keys, internal_key, upstream="https://api.qnaigc.com"):
        self.pool = self.pool_class(keys, config)
        self.models = set(config["models"])
        self.internal_key = internal_key
        self.upstream = upstream.rstrip("/")
        self.first_event_seconds = float(config.get("first_event_seconds", 0))
        self.require_semantic_first_event = bool(config.get("require_semantic_first_event", False))
        self.client = httpx.Client(trust_env=False,
                                   timeout=httpx.Timeout(float(config.get("read_timeout", 120)), connect=4, write=20, pool=1),
                                   transport=ProxySafeTransport(trust_env=False, proxy=config.get("proxy"),
                                     limits=httpx.Limits(max_connections=self.pool.capacity+4,
                                                      max_keepalive_connections=self.pool.capacity+4,
                                                      keepalive_expiry=300)))
        self.workers = threading.BoundedSemaphore(max(16, self.pool.capacity*3))
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()

    def handle_error(self, *_):
        pass

    def server_close(self):
        super().server_close()
        self.client.close()
        if self.pool.checkpoint:
            self.pool.checkpoint.close()


def main():
    config = json.loads(CONFIG.read_text())
    config.setdefault("runtime_state_path", f"/run/user/{os.getuid()}/sub2api-pool-state/qiniu.json")
    keys = json.loads((ROOT / "secrets/qiniu/api_keys.json").read_text())["keys"]
    internal_key = (ROOT / "secrets/qiniu/internal_key.txt").read_text().strip()
    server = Server(("127.0.0.1", int(config.get("port", 8095))), config, keys, internal_key)
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
