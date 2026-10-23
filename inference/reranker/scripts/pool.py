#!/usr/bin/env python3
"""Bounded FIFO admission; forward complete requests to idle native CPU workers."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import threading
import time
import uuid
from result_cache import ExactResultCache

ROOT = Path(__file__).resolve().parent.parent
MODEL = "qwen3-reranker-4b"
MAX_BODY = 10 * 1024 * 1024


class Busy(Exception):
    pass


class Disconnected(Exception):
    pass


class WorkerPool:
    def __init__(self, ports, capacity=16, queue_timeout=120, enabled=None):
        if capacity < len(ports):
            raise ValueError("Admission capacity must cover all workers")
        self.ports = tuple(ports)
        self.enabled = set(ports if enabled is None else enabled)
        self.capacity = capacity
        self.queue_timeout = queue_timeout
        self.condition = threading.Condition()
        self.available = deque(p for p in ports if p in self.enabled)
        self.waiters = deque()
        self.active = set()
        self.leases = {}
        self.port_lease = {}
        self.quarantined = set()
        self.completed = {p: 0 for p in ports}
        self.rejected = self.cancelled = 0
        self.fanout_requests = self.fanout_extra_calls = 0

    def acquire(self, disconnected=lambda: False):
        return self.acquire_many(1, disconnected)[0]

    def acquire_many(self, preferred=1, disconnected=lambda: False):
        if preferred < 1:
            raise ValueError("At least one worker is required")
        ticket = object()
        deadline = time.monotonic() + self.queue_timeout
        with self.condition:
            if len(self.waiters) + len(self.leases) >= self.capacity:
                self.rejected += 1
                raise Busy("Rerank queue is full")
            self.waiters.append(ticket)
            try:
                while True:
                    if disconnected():
                        self.cancelled += 1
                        raise Disconnected()
                    if self.waiters[0] is ticket and self.available:
                        # Physical-core workers precede SMT siblings, so a lone
                        # long request uses a fast worker even after a burst.
                        # Fan out only while the pool is otherwise idle. A
                        # second long request uses one worker, leaving capacity
                        # for short calls instead of occupying all four workers.
                        count = preferred if not self.leases else 1
                        ports = sorted(self.available, key=self.ports.index)[:count]
                        for port in ports:
                            self.available.remove(port)
                            self.active.add(port)
                            self.port_lease[port] = ticket
                        self.leases[ticket] = set(ports)
                        self.waiters.popleft()
                        if len(ports) > 1:
                            self.fanout_requests += 1
                            self.fanout_extra_calls += len(ports) - 1
                        self.condition.notify_all()
                        return ports
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.rejected += 1
                        raise Busy("Timed out waiting for a rerank worker")
                    # Completion wakes waiters immediately. The short periodic
                    # check only discards clients that left while queued.
                    self.condition.wait(min(remaining, 0.1))
            finally:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()

    def release(self, port, uncertain=False):
        with self.condition:
            self.active.remove(port)
            ticket = self.port_lease.pop(port)
            self.leases[ticket].remove(port)
            if not self.leases[ticket]:
                del self.leases[ticket]
            if uncertain:
                # A transport error is not proof that native computation ended.
                # Keep this worker out of rotation until its metrics show idle.
                self.quarantined.add(port)
            else:
                self.completed[port] += 1
                if port in self.enabled:
                    self.available.append(port)
            self.condition.notify_all()

    def recover(self, port):
        with self.condition:
            if port in self.quarantined:
                self.quarantined.remove(port)
                if port in self.enabled:
                    self.available.append(port)
                self.condition.notify_all()

    def enable(self, port):
        with self.condition:
            self.enabled.add(port)
            if port not in self.active:
                self.quarantined.discard(port)
                if port not in self.available:
                    self.available.append(port)
            self.condition.notify_all()

    def quarantine(self, port):
        with self.condition:
            self.quarantined.add(port)
            if port in self.available:
                self.available.remove(port)
            self.condition.notify_all()

    def disable_if_idle(self, port):
        with self.condition:
            if port in self.active or port in self.quarantined:
                return False
            self.enabled.discard(port)
            if port in self.available:
                self.available.remove(port)
            self.condition.notify_all()
            return True

    def pause(self, port):
        """Stop new leases while existing work drains for a rolling update."""
        with self.condition:
            if port not in self.ports:
                raise ValueError("Unknown worker")
            if not (self.enabled - self.quarantined - {port}):
                raise ValueError("At least one healthy worker must remain enabled")
            self.enabled.discard(port)
            if port in self.available:
                self.available.remove(port)
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {"capacity": self.capacity, "worker_count": len(self.ports),
                    "active": len(self.active), "waiting": len(self.waiters),
                    "active_requests": len(self.leases),
                    "available": list(self.available), "quarantined": list(self.quarantined),
                    "enabled": sorted(self.enabled),
                    "workers_active": {p: int(p in self.active) for p in self.ports},
                    "completed": dict(self.completed), "rejected": self.rejected,
                    "cancelled": self.cancelled,
                    "fanout": {"requests": self.fanout_requests, "extra_native_calls": self.fanout_extra_calls}}


def split_complete_documents(body):
    """Balance complete candidates; never split or shorten a candidate."""
    data = json.loads(body)
    documents = data["documents"]
    if (set(data) - {"model", "query", "documents", "top_n"}
            or len(documents) < 2 or sum(map(len, documents)) < 8000):
        return None
    bins, lengths = [[], []], [0, 0]
    for index in sorted(range(len(documents)), key=lambda i: (-len(documents[i]), i)):
        target = min(range(2), key=lambda i: (lengths[i], len(bins[i]), i))
        bins[target].append(index)
        lengths[target] += len(documents[index])
    plans = []
    for indices in bins:
        indices.sort()
        part = dict(data, documents=[documents[i] for i in indices], top_n=len(indices))
        plans.append((indices, json.dumps(part, ensure_ascii=False, allow_nan=False).encode()))
    return plans


def merge_complete_results(responses, plans):
    for response in responses:
        if response[0] != 200:
            return response
    results, tokens = [], 0
    for response, (indices, _) in zip(responses, plans):
        if not ExactResultCache.valid(response, len(indices)):
            return 502, b'{"error":{"message":"Incomplete native rerank result","code":502}}'
        data = json.loads(response[1])
        results.extend(dict(result, index=indices[result["index"]]) for result in data["results"])
        tokens += data["usage"]["prompt_tokens"]
    results.sort(key=lambda r: (-r["relevance_score"], r["index"]))
    return 200, json.dumps({"model": MODEL, "object": "list", "results": results,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}, ensure_ascii=False, allow_nan=False).encode()


def native_get(port, path, key):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + key})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError("Native worker not ready")
        return body
    finally:
        connection.close()


def worker_idle(port, key):
    native_get(port, "/health", key)
    metrics = native_get(port, "/metrics", key).decode()
    samples = {line.split()[0]: float(line.split()[1]) for line in metrics.splitlines()
               if line and not line.startswith("#") and len(line.split()) == 2}
    return (samples.get("llamacpp:requests_processing") == 0
            and samples.get("llamacpp:requests_deferred") == 0)


class PoolServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, pool, key, upstream_timeout=3600, result_cache=False, fanout=False):
        if fanout and not result_cache:
            raise ValueError("Complete-result preparation is required for fanout")
        self.pool = pool
        self.key = key
        self.upstream_timeout = upstream_timeout
        self.http_slots = threading.BoundedSemaphore(24)
        self.admission_slots = threading.BoundedSemaphore(pool.capacity)
        self.cache = ExactResultCache() if result_cache else None
        self.fanout = fanout
        super().__init__(address, PoolHandler)

    def process_request(self, request, client_address):
        if not self.http_slots.acquire(blocking=False):
            body = b'{"error":{"message":"Rerank connection limit reached","code":429}}'
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 429 Too Many Requests\r\nContent-Type: application/json\r\n"
                                + b"Retry-After: 1\r\nContent-Length: " + str(len(body)).encode()
                                + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.http_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.http_slots.release()


class PoolHandler(BaseHTTPRequestHandler):
    disable_nagle_algorithm = True
    # Close each connection after its one response, including overload/errors.
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def send(self, status, body, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def error(self, status, message):
        self.send(status, json.dumps({"error": {"message": message,
                  "type": "upstream_error" if status >= 500 else "invalid_request_error",
                  "code": status}}).encode(), {"Retry-After": "1"} if status == 429 else None)

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.key)

    def disconnected(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def do_GET(self):
        if self.path == "/health":
            state = self.server.pool.snapshot()
            healthy = bool(set(state['enabled']) - set(state['quarantined']))
            self.send(200 if healthy else 503, json.dumps({'status': 'ok' if healthy else 'unavailable'}).encode())
        elif not self.authorized():
            self.error(401, "Invalid API Key")
        elif self.path == "/metrics":
            state = self.server.pool.snapshot()
            if self.server.cache:
                state["result_cache"] = self.server.cache.snapshot()
            if getattr(self.server, 'elastic', None):
                state['elastic'] = self.server.elastic.snapshot()
            self.send(200, json.dumps(state).encode())
        elif self.path in ("/v1/models", "/models"):
            self.send(200, json.dumps({"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "Qwen"}]}).encode())
        else:
            self.error(404, "Unknown endpoint")

    def do_POST(self):
        if not self.authorized():
            return self.error(401, "Invalid API Key")
        if self.path == "/internal/worker-maintenance":
            return self.worker_maintenance()
        if self.path not in ("/rerank", "/v1/rerank", "/reranking", "/v1/reranking"):
            return self.error(404, "Unknown endpoint")
        if self.headers.get("Transfer-Encoding"):
            return self.error(400, "Content-Length is required")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 1 or length > MAX_BODY:
            return self.error(413 if length > MAX_BODY else 400, "Invalid request size")
        if not self.server.admission_slots.acquire(blocking=False):
            # The bounded HTTP handler budget includes room for cached replies.
            # They do not need admission to the saturated native worker queue.
            if self.server.cache:
                try:
                    body = self.rfile.read(length)
                except (OSError, socket.timeout):
                    return
                if len(body) != length:
                    return
                prepared = self.server.cache.prepare(body)
                if prepared:
                    digest, _, top, _ = prepared
                    response = self.server.cache.lookup(digest)
                    if response is not None:
                        return self.reply(self.server.cache.trim(response, top),
                                          "rerank-" + uuid.uuid4().hex, "AerisunRerank; hit")
            return self.error(429, "Rerank admission limit reached")
        try:
            try:
                body = self.rfile.read(length)
            except (OSError, socket.timeout):
                return
            if len(body) != length:
                return
            self.process_body(body)
        finally:
            self.server.admission_slots.release()

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

    def process_body(self, body):
        cache = self.server.cache
        prepared = cache.prepare(body) if cache else None
        request_id = "rerank-" + uuid.uuid4().hex
        if not prepared:
            return self.reply(self.forward(body, request_id), request_id, "AerisunRerank; fwd=bypass")
        digest, forward_body, top, count = prepared
        role, cached = cache.claim(digest)
        if role == "owner":
            response = (502, b'{"error":{"message":"Native rerank worker failed","code":502}}')
            try:
                response = self.forward(forward_body, request_id, prepared=True)
            finally:
                # Errors wake coalesced callers too, but are never stored.
                cache.finish(digest, cached, response, count)
        elif role == "wait":
            while not cached.ready.wait(.1):
                if self.disconnected():
                    return
            response = cached.response
        else:
            response = cached
        if response[0] == 200 and cache.valid(response, count):
            response = cache.trim(response, top)
        cache_status = "AerisunRerank; hit" if role == "hit" else "AerisunRerank; fwd=miss"
        if role == "wait":
            cache_status += "; collapsed"
        self.reply(response, request_id, cache_status)

    def reply(self, response, request_id, cache_status=None):
        headers = dict(response[2]) if len(response) > 2 else {}
        headers["X-Request-ID"] = request_id
        if cache_status is not None:
            headers["Cache-Status"] = cache_status
        self.send(response[0], response[1], headers)

    def forward(self, body, request_id, prepared=False):
        started = time.monotonic()
        plans = split_complete_documents(body) if prepared and self.server.fanout else None
        try:
            ports = self.server.pool.acquire_many(2 if plans else 1, self.disconnected)
        except Disconnected:
            return 499, b'{"error":{"message":"Client disconnected","code":499}}'
        except Busy as exc:
            return 429, json.dumps({"error": {"message": str(exc), "code": 429}}).encode()
        queued_ms = round((time.monotonic() - started) * 1000, 2)
        if len(ports) == 1:
            return self.forward_native(ports[0], body, request_id, started, queued_ms)
        # Only already-reserved idle workers get a task; no child waits in the
        # admission queue. Keep every lease until its native request has ended.
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.forward_native, ports[0], plans[0][1],
                                    request_id + "-part1", started, queued_ms)
            second = self.forward_native(ports[1], plans[1][1], request_id + "-part2", started, queued_ms)
            responses = [first.result(), second]
        return merge_complete_results(responses, plans)

    def forward_native(self, port, body, request_id, started, queued_ms):
        connection = HTTPConnection("127.0.0.1", port, timeout=self.server.upstream_timeout)
        uncertain = False
        status = 502
        try:
            # Forward complete inputs; scores and usage come from native inference.
            connection.request("POST", "/v1/rerank", body, {
                "Content-Type": "application/json", "Authorization": "Bearer " + self.server.key})
            response = connection.getresponse()
            result = response.read()
            status = response.status
            headers = {}
            if response.getheader("Retry-After"):
                headers["Retry-After"] = response.getheader("Retry-After")
            return status, result, headers
        except (OSError, HTTPException):
            uncertain = True
            return 502, b'{"error":{"message":"Native rerank worker connection failed","code":502}}'
        finally:
            connection.close()
            self.server.pool.release(port, uncertain)
            print(json.dumps({"request_id": request_id, "worker": port, "status": status,
                  "queue_ms": queued_ms, "total_ms": round((time.monotonic() - started) * 1000, 2),
                  "uncertain": uncertain}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--workers", default="8093,8098,8099,8100")
    parser.add_argument("--capacity", type=int, default=16)
    parser.add_argument("--queue-timeout", type=float, default=120)
    parser.add_argument("--upstream-timeout", type=float, default=3600)
    parser.add_argument("--result-cache", action="store_true")
    parser.add_argument("--fanout", action="store_true")
    parser.add_argument("--elastic-config")
    args = parser.parse_args()
    key = (ROOT / "secrets/api_key.txt").read_text().strip()
    ports = [int(p) for p in args.workers.split(",")]
    enabled = ports
    if args.elastic_config:
        config = json.loads(Path(args.elastic_config).read_text())
        enabled = ports[:config['minimum']]
    pool = WorkerPool(ports, args.capacity, args.queue_timeout, enabled=enabled)
    for port in enabled:
        models = json.loads(native_get(port, "/v1/models", key))
        assert MODEL in {item["id"] for item in models["data"]}
        if not worker_idle(port, key):
            pool.available.remove(port)
            pool.quarantined.add(port)
    server = PoolServer(("127.0.0.1", args.port), pool, key, args.upstream_timeout, args.result_cache, args.fanout)
    if args.elastic_config:
        sys.path.insert(0, '/data/wenbo/services/cpu-inference')
        from elastic_workers import attach
        server.elastic = attach(pool, args.elastic_config, key)
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(2):
            for port in pool.snapshot()["quarantined"]:
                try:
                    if worker_idle(port, key):
                        pool.recover(port)
                except (OSError, HTTPException, ValueError):
                    pass

    threading.Thread(target=monitor, daemon=True).start()
    def shutdown(*_):
        stopped.set()
        if getattr(server, 'elastic', None):
            server.elastic.stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(json.dumps({"ready": True, "port": args.port, "workers": ports, "capacity": args.capacity}), flush=True)
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if notify_socket:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
            notification.connect("\0" + notify_socket[1:] if notify_socket.startswith("@") else notify_socket)
            notification.sendall(b"READY=1")
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stopped.set()
        server.server_close()


if __name__ == "__main__":
    main()
