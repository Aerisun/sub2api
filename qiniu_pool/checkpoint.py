"""Atomic local checkpoints, with coalesced durable copies off the request path.

The local file survives process crashes and service restarts. Losing it (for
example, after a host reboot) restores the durable copy conservatively: callers
must wait one quota window before admitting new work. No prompts or keys belong
in either checkpoint.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time


def atomic_write(path, raw):
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def bootstrap_write(path, raw):
    """Durably mark recovery policy once, before any live admission exists."""
    atomic_write(path, raw)
    with path.open("rb") as baseline:
        os.fsync(baseline.fileno())
    parent = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class Checkpoint:
    def __init__(self, durable, runtime=None, interval=.25):
        self.durable = Path(durable)
        self.path = Path(runtime) if runtime else self.durable
        if self.path == self.durable and runtime:
            raise ValueError("Runtime and durable checkpoints must differ")
        self.interval = interval
        self.condition = threading.Condition()
        self.pending = None
        self.revision = self.saved = self.failures = 0
        self.stopping = False
        self.restore_cooldown = False
        self.worker = None
        self.local_worker = None
        self.local_pending = None
        self.local_revision = 0
        self.local_waiters = []
        self.local_active = self.local_stopping = False
        self.local_failures = 0
        self.local_commit_max_ms = 0
        self.pending_since = None
        self.owner = None
        if runtime:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.owner = os.fdopen(os.open(str(self.path)+".owner", os.O_RDWR|os.O_CREAT, 0o600), "a+b")
            try:
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                self.owner.close()
                self.owner = None
                raise OSError("Pool checkpoint already has an active owner") from None
            if not self.path.exists() and not self.durable.exists():
                # Establish a durable bootstrap marker before admitting the
                # very first request. Even a reboot before the first background
                # copy must restore with a quota guard rather than an empty pool.
                bootstrap_write(self.durable, b'{"runtime_checkpoint":true}\n')
                atomic_write(self.path, self.durable.read_bytes())
            if not self.path.exists() and self.durable.exists():
                raw = self.durable.read_bytes()
                state = json.loads(raw)
                self.restore_cooldown = bool(state.get("runtime_checkpoint"))
                if self.restore_cooldown:
                    # Persist the guard before exposing the restored file. A
                    # second process restart must not erase this quota window.
                    cooldowns = state.setdefault("cooldowns", {})
                    cooldowns["*"] = max(cooldowns.get("*", 0), time.time()+60)
                state["runtime_checkpoint"] = True
                raw = (json.dumps(state, separators=(",", ":"))+"\n").encode()
                if not self.restore_cooldown:
                    # Mark a legacy migration before accepting its first new
                    # request, including a reboot before the first async copy.
                    bootstrap_write(self.durable, raw)
                atomic_write(self.path, raw)
            if self.path.exists():
                self.pending = self.path.read_bytes()
                self.revision = 1
                self.pending_since = time.monotonic()
            self.worker = threading.Thread(target=self._copy, name="pool-checkpoint", daemon=True)
            self.worker.start()
            self.local_worker = threading.Thread(target=self._commit, name="pool-local-commit", daemon=True)
            self.local_worker.start()

    def write(self, state, scheduler=None):
        if self.worker:
            state = {**state, "runtime_checkpoint": True}
        if not self.local_worker:
            raw = (json.dumps(state, separators=(",", ":")) + "\n").encode()
            atomic_write(self.path, raw)
            return
        done, errors = threading.Event(), []
        with self.condition:
            if self.local_stopping or len(self.local_waiters) >= 1024:
                raise OSError("Pool checkpoint is unavailable")
            self.local_revision += 1
            # The caller supplies an immutable snapshot. Encoding and all file
            # I/O happen in this single writer, outside the scheduler lock.
            self.local_pending = (self.local_revision, state)
            self.local_waiters.append((self.local_revision, done, errors, scheduler))
            self.condition.notify_all()
        # Admission is acknowledged only after an atomic local commit. Release
        # the scheduler's full (possibly recursive) lock while awaiting it.
        completed = scheduler.wait_for(done.is_set, timeout=2) if scheduler else done.wait(2)
        if not completed or errors:
            raise OSError("Local pool checkpoint could not be committed")

    def _commit(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.local_pending is not None or self.local_stopping)
                if self.local_pending is None:
                    return
                revision, state = self.local_pending
                self.local_active = True
            failed = False
            started = time.monotonic()
            try:
                raw = (json.dumps(state, separators=(",", ":"))+"\n").encode()
                atomic_write(self.path, raw)
            except (OSError, ValueError, TypeError):
                failed = True
            with self.condition:
                self.local_commit_max_ms = max(self.local_commit_max_ms, (time.monotonic()-started)*1000)
                if self.local_pending[0] == revision:
                    self.local_pending = None
                completed = [w for w in self.local_waiters if w[0] <= revision]
                self.local_waiters = [w for w in self.local_waiters if w[0] > revision]
                if failed:
                    self.local_failures += 1
                else:
                    if self.pending is None:
                        self.pending_since = time.monotonic()
                    self.pending = raw
                    self.revision += 1
                self.local_active = False
                self.condition.notify_all()
            schedulers = set()
            for _, done, errors, scheduler in completed:
                if failed:
                    errors.append(True)
                done.set()
                if scheduler is not None:
                    schedulers.add(scheduler)
            # Never acquire a scheduler lock while holding the writer lock.
            for scheduler in schedulers:
                with scheduler:
                    scheduler.notify_all()

    def status(self):
        with self.condition:
            return {"local_checkpoint": self.worker is not None,
                    "durable_pending": self.revision != self.saved,
                    "durable_write_failures": self.failures,
                    "local_pending": len(self.local_waiters),
                    "local_write_failures": self.local_failures,
                    "local_commit_max_ms": round(self.local_commit_max_ms, 3),
                    "durable_pending_age_ms": round((time.monotonic()-self.pending_since)*1000) if self.pending_since is not None else 0,
                    "local_writer_alive": self.local_worker.is_alive() if self.local_worker else False,
                    "durable_writer_alive": self.worker.is_alive() if self.worker else False}

    def _copy(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.stopping)
                if self.pending is None:
                    return
                # Coalesce consecutive admissions into one durable write. This
                # lock is separate from the scheduler lock, including during I/O.
                if not self.stopping:
                    self.condition.wait_for(lambda: self.stopping, timeout=self.interval)
                raw, revision = self.pending, self.revision
            try:
                atomic_write(self.durable, raw)
            except OSError:
                with self.condition:
                    self.failures += 1
                    if self.stopping:
                        return  # The atomic local file remains authoritative.
                    self.condition.wait(1)
                continue
            with self.condition:
                self.saved = revision
                if revision == self.revision:
                    self.pending = None
                    self.pending_since = None
                self.condition.notify_all()

    def flush(self, timeout=10):
        deadline = time.monotonic() + timeout
        with self.condition:
            self.condition.notify_all()
            while self.local_pending is not None or self.local_active or self.revision != self.saved:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True

    def close(self):
        if self.local_worker:
            with self.condition:
                self.local_stopping = True
                self.condition.notify_all()
            self.local_worker.join(timeout=10)
        if self.worker:
            with self.condition:
                self.stopping = True
                self.condition.notify_all()
            self.worker.join(timeout=10)
        if self.owner and not any(t and t.is_alive() for t in (self.worker, self.local_worker)):
            self.owner.close()
            self.owner = None
