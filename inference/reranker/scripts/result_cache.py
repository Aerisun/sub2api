"""Bounded exact-result reuse and coalescing for identical in-flight inputs."""
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading
import time

NAMESPACE = "qwen3-reranker-4b:102ac400d68f02877c2fdfecf0732872652298448352b6faf28ec7f8dca8c913:vnni-40288286-rank-only-v1"


@dataclass
class Flight:
    ready: threading.Event = field(default_factory=threading.Event)
    response: tuple | None = None


class ExactResultCache:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_entries=4096, ttl=3600):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.ttl = ttl
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.pending = {}
        self.size = 0
        self.hits = self.misses = self.coalesced = 0

    @staticmethod
    def prepare(body):
        try:
            data = json.loads(body)
            docs = data.get("documents")
            query = data.get("query")
            if not isinstance(query, str) or not query.strip():
                return None
            if not isinstance(docs, list) or not 1 <= len(docs) <= 100 or not all(isinstance(d, str) for d in docs):
                return None
            if data.get("model") != "qwen3-reranker-4b" or "texts" in data:
                return None
            top = data.get("top_n", len(docs))
            if type(top) is not int or not 1 <= top <= len(docs):
                return None
            full = dict(data, top_n=len(docs))
            encoded = json.dumps(full, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            digest = hashlib.sha256(NAMESPACE.encode() + b"\0" + encoded).digest()
            return digest, encoded, top, len(docs)
        except (AttributeError, TypeError, ValueError, UnicodeError):
            return None

    def lookup(self, key):
        """Look up only completed work; never create a flight or reserve a worker."""
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            if entry[0] <= time.monotonic():
                self.size -= len(entry[1][1])
                del self.entries[key]
                return None
            self.entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def claim(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry and entry[0] > time.monotonic():
                self.entries.move_to_end(key)
                self.hits += 1
                return "hit", entry[1]
            if entry:
                self.size -= len(entry[1][1])
                del self.entries[key]
            if key in self.pending:
                self.coalesced += 1
                return "wait", self.pending[key]
            flight = Flight()
            self.pending[key] = flight
            self.misses += 1
            return "owner", flight

    @staticmethod
    def valid(response, count):
        status, body = response[:2]
        if status != 200:
            return False
        try:
            data = json.loads(body)
            results = data["results"]
            tokens = data["usage"]["prompt_tokens"]
            return (data["model"] == "qwen3-reranker-4b" and type(tokens) is int and tokens > 0
                    and len(results) == count and {r["index"] for r in results} == set(range(count))
                    and all(math.isfinite(r["relevance_score"]) and 0 <= r["relevance_score"] <= 1 for r in results))
        except (KeyError, TypeError, ValueError):
            return False

    def finish(self, key, flight, response, count):
        with self.lock:
            if self.valid(response, count) and len(response[1]) <= self.max_bytes:
                while self.entries and (self.size + len(response[1]) > self.max_bytes or len(self.entries) >= self.max_entries):
                    _, expired = self.entries.popitem(last=False)
                    self.size -= len(expired[1][1])
                self.entries[key] = (time.monotonic() + self.ttl, response)
                self.size += len(response[1])
            flight.response = response
            self.pending.pop(key, None)
            flight.ready.set()

    @staticmethod
    def trim(response, top):
        status, body = response[:2]
        if status == 200:
            data = json.loads(body)
            data["results"] = data["results"][:top]
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        return (status, body, *response[2:])

    def snapshot(self):
        with self.lock:
            return {"entries": len(self.entries), "bytes": self.size, "pending": len(self.pending),
                    "hits": self.hits, "misses": self.misses, "coalesced": self.coalesced,
                    "max_bytes": self.max_bytes, "ttl_seconds": self.ttl}
