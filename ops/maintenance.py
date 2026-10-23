#!/usr/bin/env python3
"""Conservative retention for Sub2api-owned disposable files; dry-run by default."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time

from image_hub.retention import clean as clean_image_hub

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400


def open_inodes():
    """Protect open files; refuse deletion if an nginx process cannot be inspected."""
    opened = set()
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            name = (process / 'comm').read_text().strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        try:
            for fd in (process / 'fd').iterdir():
                try:
                    info = fd.stat()
                    opened.add((info.st_dev, info.st_ino))
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            if name == 'nginx':
                raise RuntimeError('Cannot inspect nginx open files; skipping cleanup')
    return opened


class Cleaner:
    def __init__(self, now, apply, opened):
        self.now, self.apply, self.opened = now, apply, opened
        self.removed, self.bytes, self.busy = [], 0, 0

    def remove(self, path, age):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return
            if self.now - info.st_mtime < age:
                return
            if (info.st_dev, info.st_ino) in self.opened:
                self.busy += 1
                return
            # Recheck identity and modification time just before removal.
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
            ):
                return
            if self.apply:
                path.unlink()
            self.removed.append(str(path))
            self.bytes += info.st_size
        except FileNotFoundError:
            pass


def clean_uploads(root, cleaner):
    if root.is_symlink():
        raise RuntimeError('Upload cache must not be a symlink')
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            if re.fullmatch(r'\d{10}', name):
                cleaner.remove(Path(directory) / name, 2 * 3600)


def regular_files(root, pattern):
    if root.is_symlink():
        raise RuntimeError('Retention directory must not be a symlink')
    result = []
    for path in root.glob(pattern):
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                result.append(path)
        except FileNotFoundError:
            pass
    return result


def clean_backups(root, cleaner):
    candidates = regular_files(root, 'sub2api-*')
    candidates = [p for p in candidates if p.suffix in ('.dump', '.json', '.yaml', '.yml', '.conf')]
    dumps = sorted(
        (p for p in candidates if p.suffix == '.dump' and
         p.name.startswith(('sub2api-before-', 'sub2api-db-'))),
        key=lambda p: p.stat().st_mtime, reverse=True)
    protected = set(dumps[:2])
    # Retain the newest snapshot of each kind of configuration, even if old.
    categories = {}
    for path in candidates:
        if path.suffix != '.dump':
            category = path.name.split('-', 2)[1]
            if category not in categories or path.stat().st_mtime > categories[category].stat().st_mtime:
                categories[category] = path
    protected.update(categories.values())
    for path in candidates:
        if path not in protected:
            cleaner.remove(path, 30 * DAY)


def clean_logs(root, cleaner):
    for path in regular_files(root, '*'):
        if path.name == 'frontend-build.log' or re.fullmatch(r'sub2api-\d.*\.log(?:\.gz)?', path.name):
            cleaner.remove(path, 7 * DAY)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply retention; default only previews')
    args = parser.parse_args()
    lock_fd = os.open(ROOT / 'data/maintenance.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cleaner = Cleaner(time.time(), args.apply, open_inodes())
        clean_uploads(ROOT / 'data/nginx-client-body', cleaner)
        clean_uploads(ROOT / 'data/image-io/request-bodies', cleaner)
        clean_logs(ROOT / 'data/logs', cleaner)
        clean_backups(ROOT / 'backups', cleaner)
        images = clean_image_hub(ROOT / 'data/image-hub', apply=args.apply)
        report = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'applied': args.apply,
                  'files': len(cleaner.removed), 'bytes': cleaner.bytes,
                  'busy_files_skipped': cleaner.busy, 'paths': cleaner.removed,
                  'image_hub': images}
        if args.apply:
            destination = ROOT / 'data/maintenance-status.json'
            temp = destination.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as file:
                json.dump(report, file, ensure_ascii=False, indent=2)
                file.write('\n')
            temp.replace(destination)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
