"""Bounded keep-alive connections to the configured gateway; never retry POSTs.

Only idle sockets are shared. Each request supplies its own headers and owns
its connection until the response has been fully consumed. Partial/error reads
are discarded, so an abandoned SSE stream cannot contaminate the next caller.
"""
from collections import deque
from http.client import HTTPConnection, HTTPSConnection
import select
import threading
import time
from urllib.parse import urlsplit


class GatewayTransport:
    def __init__(self, capacity=128, idle_seconds=15):
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self.idle = deque()
        self.lock = threading.Lock()

    def open(self, url, body=None, headers=None, method="POST", timeout=1500,
             client_request=None):
        target = urlsplit(url)
        if target.scheme not in ("http", "https") or not target.hostname or target.username:
            raise ValueError("Invalid gateway URL")
        origin = (target.scheme, target.hostname, target.port)
        connection = None
        with self.lock:
            now = time.monotonic()
            # Oldest first for expiry; newest first for warm socket reuse.
            while self.idle and now - self.idle[0][0] >= self.idle_seconds:
                self.idle.popleft()[2].close()
            for index in range(len(self.idle) - 1, -1, -1):
                _, saved_origin, candidate = self.idle[index]
                if saved_origin != origin:
                    continue
                del self.idle[index]
                # An idle readable socket has a peer close or unexpected bytes.
                # Discard before sending; never replay a possibly paid request.
                if candidate.sock is None or select.select([candidate.sock], [], [], 0)[0]:
                    candidate.close()
                    continue
                connection = candidate
                break
        if connection is None:
            factory = HTTPSConnection if target.scheme == "https" else HTTPConnection
            connection = factory(target.hostname, target.port, timeout=timeout)
        connection.timeout = timeout
        try:
            if client_request is not None:
                client_request.attach(connection)
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            connection.request(method, path, body=body, headers=headers or {})
            if client_request is not None:
                client_request.connected(connection)
            response = connection.getresponse()
        except BaseException:
            if client_request is not None:
                client_request.detach(connection)
            connection.close()
            raise
        return GatewayResponse(self, origin, connection, response, client_request)

    def release(self, origin, connection):
        with self.lock:
            while len(self.idle) >= self.capacity:
                self.idle.popleft()[2].close()
            self.idle.append((time.monotonic(), origin, connection))


class GatewayResponse:
    def __init__(self, pool, origin, connection, response, client_request=None):
        self.pool, self.origin, self.connection = pool, origin, connection
        self.response = response
        self.client_request = client_request
        self.code, self.headers = response.status, response.headers

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __iter__(self):
        return iter(self.response)

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        self.close(reusable=kind is None)

    def close(self, reusable=True):
        if self.connection is None:
            return
        connection, self.connection = self.connection, None
        if self.client_request is not None and self.client_request.detach(connection):
            reusable = False
        consumed = (self.response.isclosed() and not self.response.will_close
                    and self.response.length in (0, None))
        self.response.close()
        if reusable and consumed and connection.sock is not None:
            self.pool.release(self.origin, connection)
        else:
            connection.close()
