"""Serialize live route mutations across independently running supervisors."""
from contextlib import contextmanager
import fcntl
import hashlib
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def route_lock(role, timeout=25):
    identity = str(Path(role.get('mihomo_config') or role['provider_file']).resolve()) + '\0' + role['group']
    directory = ROOT / 'data/egress-control-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
    with path.open('a') as file:
        path.chmod(0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Another supervisor is updating this route') from None
                time.sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
