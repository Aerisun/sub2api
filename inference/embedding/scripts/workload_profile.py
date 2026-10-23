"""Bounded embedding workload statistics, with no retained text or vectors."""
from collections import OrderedDict
from datetime import datetime, timezone
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time


class WorkloadProfile:
    def __init__(self, output, seconds=180, limit=500):
        if not 1 <= seconds <= 600 or not 1 <= limit <= 2000:
            raise ValueError("Invalid profile bounds")
        self.output = Path(output)
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.limit = limit
        self.secret = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.rows = []
        self.seen = OrderedDict()
        self.seen_batches = OrderedDict()
        self.active = 0
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished = False
        self.gzip_samples = 0
        threading.Thread(target=self.publish_loop, daemon=True).start()

    def digest(self, value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.secret, raw, hashlib.sha256).digest()

    def start(self, body):
        if time.monotonic() >= self.deadline:
            return None
        try:
            data = json.loads(body)
            value = data.get("input")
            if isinstance(value, str):
                inputs, shape = [value], "string"
            elif isinstance(value, list) and all(isinstance(x, str) for x in value):
                inputs, shape = value, "string_batch"
            else:
                inputs, shape = [], "other"
            row = {"started_s": round(time.monotonic() - self.started, 3), "request_bytes": len(body),
                   "input_shape": shape, "batch_size": len(inputs),
                   "input_chars": [len(x) for x in inputs],
                   "encoding_format": data.get("encoding_format", "float") if data.get("encoding_format", "float") in ("float", "base64") else "other",
                   "dimensions_requested": data.get("dimensions") if type(data.get("dimensions")) is int else None,
                   "user_field": "user" in data,
                   "instruct_prefix_items": sum(x.startswith("Instruct:") for x in inputs),
                   "query_prefix_items": sum(x.startswith(("query:", "Query:")) for x in inputs),
                   "edge_whitespace_items": sum(x != x.strip() for x in inputs),
                   "multiline_items": sum("\n" in x for x in inputs)}
            settings = {k: v for k, v in data.items() if k not in ("input", "user")}
            digests = [self.digest([settings, x]) for x in inputs]
            batch = self.digest([settings, inputs])
            with self.lock:
                if len(self.rows) + self.active >= self.limit:
                    return None
                self.active += 1
                row["duplicate_items_within_batch"] = len(digests) - len(set(digests))
                row["items_seen_in_prior_requests"] = sum(d in self.seen for d in digests)
                row["batch_seen_before_ignoring_user_and_string_wrapper"] = batch in self.seen_batches
                for digest in set(digests):
                    self.seen[digest] = self.seen.get(digest, 0) + 1
                    self.seen.move_to_end(digest)
                self.seen_batches[batch] = self.seen_batches.get(batch, 0) + 1
                self.seen_batches.move_to_end(batch)
                for values in (self.seen, self.seen_batches):
                    while len(values) > 20000:
                        values.popitem(last=False)
            return time.monotonic(), row
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return None

    def finish(self, ticket, status, body, cache_status, queue_ms=None, native_ms=None):
        if ticket is None:
            return
        started, row = ticket
        row.update(status=status, response_bytes=len(body), total_ms=round((time.monotonic() - started) * 1000, 2),
                   cache_status=cache_status, queue_ms=queue_ms, native_ms=native_ms)
        try:
            if status == 200 and len(body) <= 8 * 1024 * 1024:
                data = json.loads(body)
                vectors = data.get("data", [])
                row["vector_count"] = len(vectors)
                row["vector_dimensions"] = sorted({len(x["embedding"]) for x in vectors if isinstance(x.get("embedding"), list)})
                row["base64_vectors"] = sum(isinstance(x.get("embedding"), str) for x in vectors)
                row["prompt_tokens"] = data.get("usage", {}).get("prompt_tokens")
                with self.lock:
                    compress = self.gzip_samples < 10
                    if compress:
                        self.gzip_samples += 1
                if compress:
                    row["gzip_level1_bytes"] = len(gzip.compress(body, compresslevel=1, mtime=0))
        except (ValueError, TypeError, AttributeError, KeyError):
            row["response_shape_unavailable"] = True
        finally:
            with self.lock:
                self.rows.append(row)
                self.active -= 1

    def snapshot(self):
        with self.lock:
            return {"started_at": self.started_at, "seconds_elapsed": round(time.monotonic() - self.started, 2),
                    "accepting_samples": time.monotonic() < self.deadline,
                    "active_samples": self.active, "completed_samples": len(self.rows),
                    "unique_items": len(self.seen), "unique_batches": len(self.seen_batches),
                    "rows": list(self.rows),
                    "privacy": "Only sizes, counts and timing; ephemeral HMACs are never written. No text, vectors, credentials or user IDs."}

    def publish_loop(self):
        while True:
            snapshot = self.snapshot()
            try:
                temp = self.output.with_suffix(".tmp")
                # The destination is explicitly selected by the local operator.
                with open(temp, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                    json.dump(snapshot, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                temp.replace(self.output)
            except OSError:
                pass
            if not snapshot["accepting_samples"] and not snapshot["active_samples"]:
                with self.lock:
                    self.seen.clear()
                    self.seen_batches.clear()
                    self.secret = b""
                    self.finished = True
                return
            time.sleep(2)
