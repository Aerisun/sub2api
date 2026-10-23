"""Release the request's upstream socket when its ingress peer disconnects.

The monitor is started lazily after the bridge workers fork. It only peeks at
ingress sockets; request parsing and all response reads stay with their owner.
"""
import select
import socket
import threading


class ClientRequest:
    def __init__(self, ingress):
        self.ingress = ingress
        self.lock = threading.Lock()
        self.connection = None
        self.upstream_socket = None
        self.cancelled = False

    def attach(self, connection):
        with self.lock:
            if self.cancelled:
                raise ConnectionAbortedError("Client disconnected")
            self.connection = connection
            self.upstream_socket = connection.sock

    def connected(self, connection):
        with self.lock:
            if self.cancelled:
                raise ConnectionAbortedError("Client disconnected")
            self.upstream_socket = connection.sock

    def detach(self, connection):
        # Detach before a connection can go back into the idle pool. A late
        # disconnect must never shut down another customer's borrowed socket.
        with self.lock:
            if self.connection is connection:
                self.connection = None
                self.upstream_socket = None
            return self.cancelled

    def check(self):
        with self.lock:
            if self.cancelled:
                raise ConnectionAbortedError("Client disconnected")

    def cancel(self):
        with self.lock:
            self.cancelled = True
            connection = self.connection
            upstream_socket = self.upstream_socket
            if connection is not None and connection.sock is not None:
                upstream_socket = connection.sock
            if upstream_socket is not None:
                try:
                    # shutdown wakes getresponse/read without closing a file
                    # object concurrently with its owning request thread.
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class ClientDisconnectMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = set()
        self.thread = None

    def watch(self, ingress):
        monitor = self

        class Scope:
            def __enter__(self):
                self.request = ClientRequest(ingress)
                with monitor.lock:
                    monitor.requests.add(self.request)
                    if monitor.thread is None:
                        monitor.thread = threading.Thread(target=monitor.run, daemon=True,
                                                          name="client-disconnect")
                        monitor.thread.start()
                return self.request

            def __exit__(self, *_):
                with monitor.lock:
                    monitor.requests.discard(self.request)

        return Scope()

    def run(self):
        interval = threading.Event()
        while not interval.wait(.2):
            with self.lock:
                requests = tuple(self.requests)
            for request in requests:
                try:
                    if not select.select([request.ingress], [], [], 0)[0]:
                        continue
                    if request.ingress.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        continue  # A pipelined request belongs to the parser.
                except BlockingIOError:
                    continue
                except (OSError, ValueError):
                    pass
                request.cancel()
