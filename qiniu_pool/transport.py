"""Release a failed CONNECT/TLS setup before returning its pool slot.

httpcore 1.0.9 leaves a tunnel's CONNECT connection ACTIVE when start_tls
fails. Repeated failures exhaust the pool even with zero live requests.
The guard runs inside that connection's existing setup lock. Established
connections, response reads, certificate verification and retry policy retain
httpx's behavior. No request is replayed here.
"""
import httpx
from httpcore._sync.http_proxy import TunnelHTTPConnection


class _TunnelSetupLock:
    def __init__(self, lock, connection):
        self.lock, self.connection = lock, connection

    def __enter__(self):
        return self.lock.__enter__()

    def __exit__(self, kind, error, traceback):
        try:
            if kind is not None and not self.connection._connected:
                self.connection.close()
        finally:
            self.lock.__exit__(kind, error, traceback)


class ProxySafeTransport(httpx.HTTPTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        factory = self._pool.create_connection

        def create_connection(origin):
            connection = factory(origin)
            if isinstance(connection, TunnelHTTPConnection):
                connection._connect_lock = _TunnelSetupLock(connection._connect_lock, connection)
            return connection

        self._pool.create_connection = create_connection
