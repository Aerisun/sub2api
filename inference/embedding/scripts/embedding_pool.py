#!/usr/bin/env python3
"""Bounded CPU embedding dispatcher. Forward complete inputs and native usage."""
import argparse
from collections import OrderedDict, deque
import errno
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import signal
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 10 * 1024 * 1024


class Busy(Exception):
    pass


class Disconnected(Exception):
    pass


def request_work(body):
    """Estimate scheduling work only. Native validation and token usage are authoritative."""
    try:
        value = json.loads(body).get("input")
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list) and value:
            items = [value] if all(isinstance(v, int) for v in value) else value
        else:
            return 1, max(1, len(body))
        cost = 0
        for item in items:
            if isinstance(item, str):
                cost += max(1, len(item.encode("utf-8")))
            elif isinstance(item, list) and all(isinstance(v, int) for v in item):
                cost += max(1, len(item) * 4)
            else:
                return 1, max(1, len(body))
        return len(items), cost
    except (ValueError, AttributeError, UnicodeError):
        return 1, max(1, len(body))


class WorkerPool:
    def __init__(self, ports, concurrency=32, per_worker=4, capacity=64, queue_timeout=180, enabled=None,
                 scheduler="fifo", aging_seconds=15, max_bypass=4, short_cost_limit=512):
        if not ports or len(set(ports)) != len(ports) or not 0 < concurrency <= per_worker * len(ports):
            raise ValueError("Invalid execution capacity")
        if capacity < concurrency:
            raise ValueError("Admission must cover execution capacity")
        if scheduler not in ("fifo", "bounded-shortest", "fair-shortest") or aging_seconds <= 0 or max_bypass < 1 or short_cost_limit < 1:
            raise ValueError("Invalid scheduling policy")
        self.ports = tuple(ports)
        self.enabled = set(ports if enabled is None else enabled)
        self.concurrency, self.per_worker, self.capacity = concurrency, per_worker, capacity
        self.queue_timeout = queue_timeout
        self.scheduler, self.aging_seconds, self.max_bypass = scheduler, aging_seconds, max_bypass
        self.short_cost_limit = short_cost_limit
        self.short_streak = self.short_dispatches = self.background_dispatches = 0
        self.condition = threading.Condition()
        self.waiters = deque()
        self.queued = {}
        self.leases = {}
        self.reservations = {}
        self.active = {p: 0 for p in ports}
        self.reserved = {p: 0 for p in ports}
        self.bytes = {p: 0 for p in ports}
        self.completed = {p: 0 for p in ports}
        self.quarantined = set()
        self.rejected = self.cancelled = 0
        self.priority_dispatches = self.bypasses = 0

    def choose(self, now):
        if not self.waiters or len(self.leases) >= self.concurrency:
            return None, []
        available = [p for p in self.ports if p in self.enabled and p not in self.quarantined
                     and self.active[p] < self.per_worker]
        if self.scheduler == "fifo":
            return self.waiters[0], available
        if self.scheduler == "fair-shortest":
            return self.choose_fair(now, available)
        urgent = [t for t in self.waiters if now - self.queued[t]["since"] >= self.aging_seconds
                  or self.queued[t]["bypasses"] >= self.max_bypass]
        # An aged batch reserves the next idle worker instead of being kept
        # waiting by an endless stream of requests that fit a single free slot.
        candidates = urgent[:1] if urgent else sorted(self.waiters, key=lambda t: self.queued[t]["cost"])
        for ticket in candidates:
            ports = [p for p in available if self.reserved[p] + self.queued[ticket]["slots"] <= self.per_worker]
            if ports:
                return ticket, ports
        return None, []

    def choose_fair(self, now, available):
        def aged(ticket):
            return (now - self.queued[ticket]["since"] >= self.aging_seconds
                    or self.queued[ticket]["bypasses"] >= self.max_bypass)

        def ordered(tickets):
            return sorted(tickets, key=lambda t: (not aged(t),
                          self.queued[t]["since"] if aged(t) else self.queued[t]["cost"]))

        short = ordered([t for t in self.waiters if self.queued[t]["cost"] <= self.short_cost_limit])
        background = ordered([t for t in self.waiters if self.queued[t]["cost"] > self.short_cost_limit])

        def fits(ticket, ports):
            return [p for p in ports if self.reserved[p] + self.queued[ticket]["slots"] <= self.per_worker]

        # Aging is applied inside each class. A burst of old document jobs
        # cannot turn the entire queue into FIFO ahead of every new query.
        background_turn = background and (not short or self.short_streak >= self.max_bypass)
        primary = background if background_turn else short
        secondary = short if background_turn else background
        for ticket in primary:
            ports = fits(ticket, available)
            if ports:
                return ticket, ports
            if aged(ticket):
                # Reserve one worker for an aged batch. Spare capacity on the
                # other worker can still serve queries while that worker drains.
                protected = min((p for p in self.enabled if p not in self.quarantined),
                                key=lambda p: (self.reserved[p], self.bytes[p], self.ports.index(p)), default=None)
                for other in secondary:
                    ports = fits(other, [p for p in available if p != protected])
                    if ports:
                        return other, ports
                return None, []
        for ticket in secondary:
            ports = fits(ticket, available)
            if ports:
                return ticket, ports
        return None, []

    def acquire(self, size, disconnected=lambda: False, items=1, cost=None):
        ticket = object()
        deadline = time.monotonic() + self.queue_timeout
        with self.condition:
            if len(self.waiters) + len(self.leases) >= self.capacity:
                self.rejected += 1
                raise Busy("Embedding queue is full")
            self.waiters.append(ticket)
            self.queued[ticket] = {"slots": min(self.per_worker, max(1, items)) if self.scheduler != "fifo" else 1,
                                   "cost": size if cost is None else cost,
                                   "since": time.monotonic(), "bypasses": 0}
            try:
                while True:
                    if disconnected():
                        self.cancelled += 1
                        raise Disconnected()
                    selected, available = self.choose(time.monotonic())
                    if selected is ticket and available:
                        # Byte lengths balance work; token billing always comes from native usage.
                        # Prefer the warm baseline when idle, so light traffic
                        # does not keep an excess model resident indefinitely.
                        port = min(available, key=lambda p: (self.reserved[p], self.bytes[p], self.ports.index(p)))
                        if ticket is not self.waiters[0]:
                            self.priority_dispatches += 1
                        for older in self.waiters:
                            if older is ticket:
                                break
                            self.queued[older]["bypasses"] += 1
                            self.bypasses += 1
                        self.leases[ticket] = port, size
                        if self.queued[ticket]["cost"] <= self.short_cost_limit:
                            self.short_streak += 1
                            self.short_dispatches += 1
                        else:
                            self.short_streak = 0
                            self.background_dispatches += 1
                        self.reservations[ticket] = self.queued[ticket]["slots"]
                        self.reserved[port] += self.reservations[ticket]
                        self.active[port] += 1
                        self.bytes[port] += size
                        self.waiters.remove(ticket)
                        self.condition.notify_all()
                        return ticket, port
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.rejected += 1
                        raise Busy("Embedding queue deadline exceeded")
                    self.condition.wait(min(.1, remaining))
            finally:
                self.queued.pop(ticket, None)
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()

    def release(self, ticket, uncertain=False):
        with self.condition:
            port, size = self.leases.pop(ticket)
            self.reserved[port] -= self.reservations.pop(ticket)
            self.active[port] -= 1
            self.bytes[port] -= size
            if uncertain:
                self.quarantined.add(port)
            else:
                self.completed[port] += 1
            self.condition.notify_all()

    def configure_scheduler(self, policy, aging_seconds, max_bypass, short_cost_limit):
        if policy not in ("fifo", "bounded-shortest", "fair-shortest"):
            raise ValueError("Invalid scheduling policy")
        if type(aging_seconds) not in (int, float) or not 0 < aging_seconds < 180:
            raise ValueError("Invalid queue aging")
        if type(max_bypass) is not int or not 1 <= max_bypass <= 16:
            raise ValueError("Invalid fairness limit")
        if type(short_cost_limit) is not int or not 1 <= short_cost_limit <= 4096:
            raise ValueError("Invalid short request limit")
        with self.condition:
            if (policy == "fifo") != (self.scheduler == "fifo") and (self.leases or self.waiters):
                raise RuntimeError("FIFO reservation changes require an idle pool")
            self.scheduler, self.aging_seconds = policy, aging_seconds
            self.max_bypass, self.short_cost_limit = max_bypass, short_cost_limit
            self.short_streak = 0
            self.condition.notify_all()

    def recover(self, port):
        with self.condition:
            if not self.active[port]:
                self.quarantined.discard(port)
                self.condition.notify_all()

    def enable(self, port):
        with self.condition:
            self.enabled.add(port)
            if not self.active[port]:
                self.quarantined.discard(port)
            self.condition.notify_all()

    def quarantine(self, port):
        with self.condition:
            self.quarantined.add(port)
            self.condition.notify_all()

    def disable_if_idle(self, port):
        with self.condition:
            if self.active[port] or port in self.quarantined:
                return False
            self.enabled.discard(port)
            self.condition.notify_all()
            return True

    def pause(self, port):
        """Stop assigning new work while existing leases finish normally."""
        with self.condition:
            if port not in self.ports:
                raise ValueError("Unknown worker")
            if not (self.enabled - self.quarantined - {port}):
                raise ValueError("At least one healthy worker must remain enabled")
            self.enabled.discard(port)
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"concurrency": self.concurrency, "admission_capacity": self.capacity,
                    "worker_count": len(self.ports), "per_worker_concurrency": self.per_worker,
                    "active": len(self.leases), "waiting": len(self.waiters),
                    "workers_active": dict(self.active), "quarantined": sorted(self.quarantined),
                    "workers_reserved_slots": dict(self.reserved),
                    "scheduler": {"policy": self.scheduler, "aging_seconds": self.aging_seconds,
                                  "max_bypass": self.max_bypass, "priority_dispatches": self.priority_dispatches,
                                  "short_cost_limit": self.short_cost_limit,
                                  "short_dispatches": self.short_dispatches,
                                  "background_dispatches": self.background_dispatches,
                                  "short_streak": self.short_streak,
                                  "short_waiting": sum(v["cost"] <= self.short_cost_limit for v in self.queued.values()),
                                  "bypasses": self.bypasses,
                                  "oldest_wait_seconds": round(max((time.monotonic() - v["since"]
                                                                 for v in self.queued.values()), default=0), 3)},
                    "enabled": sorted(self.enabled),
                    "completed": dict(self.completed), "rejected": self.rejected, "cancelled": self.cancelled}


class ResultCache:
    """Exact whole-request reuse; preserve complete vectors and token usage for billing."""
    def __init__(self, max_bytes=64 * 1024 * 1024, ttl=600):
        self.lock = threading.Lock()
        self.entries, self.pending = OrderedDict(), {}
        self.expirations = OrderedDict()
        self.max_bytes, self.ttl = max_bytes, ttl
        self.bytes = self.hits = self.coalesced = 0

    @staticmethod
    def digest(body):
        # SDK whitespace/key ordering does not change inference semantics.
        # Keep the original body for native validation and forward it unchanged.
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result
            parsed = json.loads(body, object_pairs_hook=unique_object)
            canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError, UnicodeError):
            canonical = body
        return hashlib.sha256(canonical).digest()

    def lookup(self, body):
        """Serve a stored result without occupying an inference queue slot."""
        digest = self.digest(body)
        with self.lock:
            value = self.entries.get(digest)
            if value is None:
                return None
            if value[0] <= time.monotonic():
                del self.entries[digest]
                self.expirations.pop(digest)
                self.bytes -= len(value[1][1])
                return None
            self.hits += 1
            self.entries.move_to_end(digest)
            return value[1]

    def claim(self, body):
        digest = self.digest(body)
        with self.lock:
            now = time.monotonic()
            # Expiry order is independent of LRU order. Hits no longer scan
            # every cached 2560-dimensional response while holding the lock.
            while self.expirations:
                key, expires = next(iter(self.expirations.items()))
                if expires > now:
                    break
                self.expirations.pop(key)
                _, value = self.entries.pop(key)
                self.bytes -= len(value[1])
            if digest in self.entries:
                self.hits += 1
                value = self.entries.pop(digest)
                self.entries[digest] = value
                return digest, "hit", value[1]
            if digest in self.pending:
                self.coalesced += 1
                return digest, "wait", self.pending[digest]
            pending = {"ready": threading.Event(), "response": None}
            self.pending[digest] = pending
            return digest, "owner", pending

    def finish(self, digest, pending, response):
        with self.lock:
            if response[0] == 200 and len(response[1]) <= self.max_bytes:
                try:
                    data = json.loads(response[1])
                    valid = isinstance(data.get("data"), list) and bool(data["data"]) and isinstance(data["usage"]["prompt_tokens"], int)
                except (KeyError, ValueError, TypeError, AttributeError):
                    valid = False
                if valid:
                    while self.entries and self.bytes + len(response[1]) > self.max_bytes:
                        old_key, (_, old) = self.entries.popitem(last=False)
                        self.expirations.pop(old_key)
                        self.bytes -= len(old[1])
                    expires = time.monotonic() + self.ttl
                    self.entries[digest] = expires, response
                    self.expirations[digest] = expires
                    self.bytes += len(response[1])
            pending["response"] = response
            self.pending.pop(digest, None)
            pending["ready"].set()

    def snapshot(self):
        with self.lock:
            return {"bytes": self.bytes, "max_bytes": self.max_bytes, "entries": len(self.entries),
                    "pending": len(self.pending), "hits": self.hits, "coalesced": self.coalesced}


class PoolServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, pool, key, upstream_timeout=300, handlers=80, cache=True, profile=None,
                 bind_and_activate=True, shared=None):
        self.pool, self.key, self.upstream_timeout = pool, key, upstream_timeout
        self.http_slots = shared.http_slots if shared else threading.BoundedSemaphore(handlers)
        self.admission_slots = shared.admission_slots if shared else threading.BoundedSemaphore(pool.capacity)
        self.cache = shared.cache if shared else (ResultCache() if cache else None)
        self.profile = shared.profile if shared else profile
        self.root_server = shared.root_server if shared else self
        self.secondary_servers = []
        self.secondary_lock = threading.Lock()
        super().__init__(address, PoolHandler, bind_and_activate=bind_and_activate)

    def process_request(self, request, address):
        if not self.http_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 429 Too Many Requests\r\nContent-Length: 0\r\nRetry-After: 1\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.http_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.http_slots.release()


class PoolHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    disable_nagle_algorithm = True

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.profile_ticket = None
        self.profile_queue_ms = self.profile_native_ms = None

    def log_message(self, *args):
        pass

    def send(self, status, body, cache_status=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cache_status is not None:
                self.send_header("Cache-Status", cache_status)
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        finally:
            self.finish_profile(status, body, cache_status)

    def finish_profile(self, status, body, cache_status):
        if self.profile_ticket is not None:
            ticket, self.profile_ticket = self.profile_ticket, None
            try:
                self.server.profile.finish(ticket, status, body, cache_status,
                                           self.profile_queue_ms, self.profile_native_ms)
            except Exception:
                pass

    def profile_input(self, body):
        if self.server.profile is not None and self.path != "/tokenize":
            try:
                self.profile_ticket = self.server.profile.start(body)
            except Exception:
                pass

    def error(self, status, message):
        self.send(status, json.dumps({"error": {"message": message, "code": status}}).encode())

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + self.server.key).encode())

    def disconnected(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def do_GET(self):
        if self.path == "/health":
            state = self.server.pool.snapshot()
            healthy = bool(set(state["enabled"]) - set(state["quarantined"]))
            return self.send(200 if healthy else 503, json.dumps({"status": "ok" if healthy else "unavailable"}).encode())
        if not self.authorized():
            return self.error(401, "Invalid API Key")
        if self.path in ("/v1/models", "/models"):
            return self.send(200, b'{"object":"list","data":[{"id":"Qwen3-Embedding-4B","object":"model","owned_by":"Qwen"}]}')
        if self.path == "/metrics":
            state = self.server.pool.snapshot()
            root = self.server.root_server
            with root.secondary_lock:
                state["listen_ports"] = sorted([root.server_port] + [s.server_port for s in root.secondary_servers])
            if self.server.cache:
                state["result_cache"] = self.server.cache.snapshot()
            if getattr(self.server, 'elastic', None):
                state['elastic'] = self.server.elastic.snapshot()
            return self.send(200, json.dumps(state).encode())
        self.error(404, "Unknown endpoint")

    def do_POST(self):
        if not self.authorized():
            return self.error(401, "Invalid API Key")
        if self.path == "/internal/worker-maintenance":
            return self.worker_maintenance()
        if self.path == "/internal/scheduler":
            return self.scheduler_settings()
        if self.path not in ("/v1/embeddings", "/embeddings", "/tokenize"):
            return self.error(404, "Unknown endpoint")
        if self.headers.get("Transfer-Encoding"):
            return self.error(400, "Content-Length is required")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self.error(413 if length > MAX_BODY else 400, "Invalid request size")
        if not self.server.admission_slots.acquire(blocking=False):
            # HTTP handlers remain bounded separately. A stored vector needs no
            # native execution slot, even while the inference queue is full.
            if self.server.cache and self.path != "/tokenize":
                try:
                    body = self.rfile.read(length)
                except OSError:
                    return
                if len(body) != length:
                    return
                self.profile_input(body)
                response = self.server.cache.lookup(body)
                if response is not None:
                    return self.send(*response, cache_status="AerisunEmbedding; hit")
            return self.error(429, "Embedding admission limit reached")
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                return
            self.profile_input(body)
            path = "/tokenize" if self.path == "/tokenize" else "/v1/embeddings"
            cache = self.server.cache if path != "/tokenize" else None
            cache_status = "AerisunEmbedding; fwd=bypass"
            if not cache:
                response = self.forward(path, body)
            else:
                digest, role, value = cache.claim(body)
                cache_status = "AerisunEmbedding; hit" if role == "hit" else "AerisunEmbedding; fwd=miss"
                if role == "wait":
                    cache_status += "; collapsed"
                if role == "owner":
                    response = (502, b'{"error":{"message":"Embedding worker failed","code":502}}')
                    try:
                        response = self.forward(path, body)
                    finally:
                        cache.finish(digest, value, response)
                elif role == "wait":
                    while not value["ready"].wait(.1):
                        if self.disconnected():
                            return
                    response = value["response"]
                else:
                    response = value
            self.send(*response, cache_status=cache_status)
        except OSError:
            pass
        finally:
            self.server.admission_slots.release()
            self.finish_profile(499, b'{}', None)

    def worker_maintenance(self):
        controller = getattr(self.server, "elastic", None)
        if controller is None:
            return self.error(404, "Worker management is disabled")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 256:
                raise ValueError("Invalid maintenance request size")
            data = json.loads(self.rfile.read(length))
            port, paused = data["port"], data["paused"]
            if type(port) is not int or type(paused) is not bool:
                raise ValueError("Worker port and boolean paused are required")
            controller.set_maintenance(port, paused)
        except (ValueError, TypeError, KeyError):
            return self.error(400, "Invalid maintenance request or no remaining healthy worker")
        except RuntimeError:
            return self.error(409, "Worker transition is busy or worker is not ready")
        except OSError:
            return
        self.send(200, json.dumps(self.server.pool.snapshot()).encode())

    def scheduler_settings(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 512:
                raise ValueError("Invalid scheduler request size")
            data = json.loads(self.rfile.read(length))
            self.server.pool.configure_scheduler(data["policy"], data["aging_seconds"],
                                                 data["max_bypass"], data["short_cost_limit"])
        except (ValueError, TypeError, KeyError):
            return self.error(400, "Invalid scheduler settings")
        except RuntimeError:
            return self.error(409, "Reservation changes require an idle pool")
        except OSError:
            return
        self.send(200, json.dumps(self.server.pool.snapshot()).encode())

    def forward(self, path, body):
        started = time.monotonic()
        items, cost = request_work(body)
        try:
            ticket, port = self.server.pool.acquire(len(body), self.disconnected, items=items, cost=cost)
        except Disconnected:
            return 499, b'{"error":{"message":"Client disconnected","code":499}}'
        except Busy as exc:
            return 429, json.dumps({"error": {"message": str(exc), "code": 429}}).encode()
        native_started = time.monotonic()
        self.profile_queue_ms = round((native_started - started) * 1000, 2)
        connection = HTTPConnection("127.0.0.1", port, timeout=self.server.upstream_timeout)
        uncertain = False
        try:
            connection.request("POST", path, body, {"Content-Type": "application/json",
                               "Authorization": "Bearer " + self.server.key})
            response = connection.getresponse()
            return response.status, response.read()
        except (OSError, HTTPException):
            # A failed connection does not prove that native computation stopped.
            uncertain = True
            return 502, b'{"error":{"message":"Native embedding connection failed","code":502}}'
        finally:
            connection.close()
            self.server.pool.release(ticket, uncertain)
            self.profile_native_ms = round((time.monotonic() - native_started) * 1000, 2)


def worker_idle(port, key):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/metrics", headers={"Authorization": "Bearer " + key})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return False
        samples = {line.split()[0]: float(line.split()[1]) for line in body.decode().splitlines()
                   if line and not line.startswith("#") and len(line.split()) == 2}
        return samples.get("llamacpp:requests_processing") == 0 and samples.get("llamacpp:requests_deferred") == 0
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        connection.close()


def start_secondary(server, port, stop):
    """Acquire the old listener after a live migration drains its previous owner."""
    def bind_when_free():
        while not stop.is_set():
            try:
                alias = PoolServer(('127.0.0.1', port), server.pool, server.key,
                                   server.upstream_timeout, shared=server)
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                stop.wait(.02)
                continue
            alias.elastic = getattr(server, 'elastic', None)
            with server.secondary_lock:
                if stop.is_set():
                    alias.server_close()
                    return
                server.secondary_servers.append(alias)
                threading.Thread(target=lambda: alias.serve_forever(poll_interval=.05), daemon=True).start()
            return
    threading.Thread(target=bind_when_free, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--workers", default="8111,8112,8113,8114,8115,8116,8117,8118")
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--per-worker", type=int, default=4)
    parser.add_argument("--capacity", type=int, default=64)
    parser.add_argument("--handlers", type=int, default=80)
    parser.add_argument("--upstream-timeout", type=int, default=300)
    parser.add_argument("--scheduler", choices=("fifo", "bounded-shortest", "fair-shortest"), default="fifo")
    parser.add_argument("--aging-seconds", type=float, default=15)
    parser.add_argument("--max-bypass", type=int, default=4)
    parser.add_argument("--short-cost-limit", type=int, default=512)
    parser.add_argument("--standby-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--secondary-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--profile-output")
    parser.add_argument("--profile-seconds", type=int, default=180)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--elastic-config")
    args = parser.parse_args()
    if args.secondary_port is not None and (not 1024 < args.secondary_port < 65536 or args.secondary_port == args.port):
        raise ValueError("Invalid secondary listener")
    key = (ROOT / "secrets/backend_internal_key.txt").read_text().strip()
    if not key:
        raise ValueError("Internal API key is empty")
    ports = [int(p) for p in args.workers.split(",")]
    enabled = None
    if args.elastic_config:
        config = json.loads(Path(args.elastic_config).read_text())
        enabled = ports[:config['minimum']]
    pool = WorkerPool(ports, args.concurrency, args.per_worker, args.capacity, enabled=enabled,
                      scheduler=args.scheduler, aging_seconds=args.aging_seconds, max_bypass=args.max_bypass,
                      short_cost_limit=args.short_cost_limit)
    profile = None
    if args.profile_output:
        from workload_profile import WorkloadProfile
        profile = WorkloadProfile(args.profile_output, args.profile_seconds)
    server = PoolServer(("127.0.0.1", args.port), pool, key, args.upstream_timeout, args.handlers,
                        not args.no_cache, profile, bind_and_activate=args.standby_fd is None)
    control = None
    if args.standby_fd is not None:
        # Load and validate the replacement before stopping the serving process.
        # This descriptor is private to the reload process, never a public API.
        control = socket.socket(fileno=args.standby_fd)
        control.settimeout(45)
        control.sendall(b"ready\n")
        if control.recv(32) != b"activate\n":
            server.server_close()
            raise RuntimeError("Dispatcher activation cancelled")
        server.server_bind()
        server.server_activate()
    if args.elastic_config:
        sys.path.insert(0, '/data/wenbo/services/cpu-inference')
        from elastic_workers import attach
        server.elastic = attach(pool, args.elastic_config, key)
    stop = threading.Event()

    def monitor():
        while not stop.wait(5):
            for port in pool.snapshot()["quarantined"]:
                if worker_idle(port, key):
                    pool.recover(port)

    def shutdown(*_):
        stop.set()
        if getattr(server, 'elastic', None):
            server.elastic.stopped.set()
        def stop_http():
            with server.secondary_lock:
                aliases = list(server.secondary_servers)
            for alias in aliases:
                alias.shutdown()
            server.shutdown()
        threading.Thread(target=stop_http, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if profile is not None:
        signal.signal(signal.SIGUSR1, lambda *_: setattr(profile, "deadline", 0))
    threading.Thread(target=monitor, daemon=True).start()
    if args.secondary_port is not None:
        start_secondary(server, args.secondary_port, stop)
    if control is not None:
        control.sendall(b"serving\n")
        control.close()
    try:
        server.serve_forever(poll_interval=.05)
    finally:
        stop.set()
        server.server_close()
        with server.secondary_lock:
            for alias in server.secondary_servers:
                alias.server_close()


if __name__ == "__main__":
    main()
