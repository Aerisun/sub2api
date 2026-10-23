"""Brief, bounded hints for optional image-tool injection, never authentication.

Every model request and image operation still reaches the gateway with the
original key. Only a SHA-256 digest of each key is retained here; failures are
not cached, and a slow catalogue cannot hold up a text request indefinitely.
"""
from collections import OrderedDict
import hashlib
import threading
import time

from image_hub.server import HubError, NATIVE_MODEL, NATIVE_MODELS, request_json


class ImageAccessCache:
    def __init__(self, ttl=2.0, capacity=1024, timeout=0.5, clock=time.monotonic):
        self.ttl, self.capacity, self.timeout, self.clock = ttl, capacity, timeout, clock
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.pending = {}

    def allowed(self, gateway, key, model=NATIVE_MODEL):
        return model in self.available(gateway, key)

    def has_current_catalogue(self, gateway, key):
        """Distinguish a denied tool from an optional catalogue lookup timeout.

        This remains a hint; the actual tool always authenticates again.
        """
        digest = hashlib.sha256((gateway + '\0' + key).encode()).digest()
        with self.lock:
            cached = self.entries.get(digest)
            return cached is not None and cached[0] > self.clock()

    def available(self, gateway, key):
        if not key:
            return frozenset()
        # Coalesced browser requests need the same bounded catalogue budget as
        # their leader, otherwise they intermittently lose the optional tool.
        timeout = max(self.timeout, 2.0) if key.startswith('eyJ') and key.count('.') == 2 else self.timeout
        digest = hashlib.sha256((gateway + '\0' + key).encode()).digest()
        with self.lock:
            cached = self.entries.get(digest)
            if cached is not None:
                if cached[0] > self.clock():
                    self.entries.move_to_end(digest)
                    return cached[1]
                del self.entries[digest]
            event = self.pending.get(digest)
            leader = event is None
            if leader:
                if len(self.pending) >= self.capacity:
                    return frozenset()
                event = threading.Event()
                self.pending[digest] = event
        if not leader:
            event.wait(timeout)
            with self.lock:
                cached = self.entries.get(digest)
                return cached[1] if cached and cached[0] > self.clock() else frozenset()

        allowed = None
        try:
            # JWT chat authenticates the login and private chat credential. A cold
            # authenticated catalogue can take longer than an API-key lookup.
            _, models = request_json(gateway + ('/v1/models' if key.startswith('sk-') else '/api/v1/chat/models'), key, timeout=timeout)
            catalogue = models.get('data')
            if isinstance(catalogue, list):
                allowed = frozenset(item['id'] for item in catalogue if isinstance(item, dict) and isinstance(item.get('id'), str))
                if 'image_generation' in allowed:
                    allowed = allowed | NATIVE_MODELS
            return allowed if allowed is not None else frozenset()
        except (HubError, ValueError, TypeError, AttributeError):
            return frozenset()
        finally:
            with self.lock:
                if allowed is not None:
                    self.entries[digest] = (self.clock() + self.ttl, allowed)
                    self.entries.move_to_end(digest)
                    while len(self.entries) > self.capacity:
                        self.entries.popitem(last=False)
                self.pending.pop(digest, None)
                event.set()
