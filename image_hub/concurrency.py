"""Host-wide slots with immediate admission or bounded waiting; exits release leases."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import time

ROOT = Path(__file__).resolve().parents[1]


def configured_limit(name, default):
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= 1024:
        raise ValueError(name + " must be between 1 and 1024")
    return value


class SlotLimitReached(Exception):
    pass


class FileSlotPool:
    """Each open file owns one flock, including across threads and workers.

    Slot files contain no request data. Keep them in place: unlinking a locked
    file would create a second inode and let another process bypass its lease.
    """

    def __init__(self, limit, directory=None):
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("Slot limit must be between 1 and 1024")
        self.limit = limit
        self.directory = Path(directory or ROOT / "data/tool-concurrency/image-recognition")

    @contextmanager
    def acquire(self, timeout=0):
        deadline=time.monotonic()+max(0,timeout)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = None
        while lease is None:
            start = secrets.randbelow(self.limit)
            for offset in range(self.limit):
                path = self.directory / str((start + offset) % self.limit)
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                    continue
                except BaseException:
                    os.close(descriptor)
                    raise
                lease = descriptor
                break
            if lease is not None or time.monotonic()>=deadline:break
            time.sleep(max(0,min(deadline-time.monotonic(),.02+secrets.randbelow(20)/1000)))
        if lease is None:
            raise SlotLimitReached()
        try:
            yield
        finally:
            os.close(lease)
