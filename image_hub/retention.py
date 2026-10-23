"""Bounded storage and conservative cleanup for generated images and task metadata."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time

TTL = 7 * 86400
MAX_CACHE_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 512 * 1024**2
MAX_TASKS = 10000
IMAGE_NAME = re.compile(r"[0-9a-f]{48}\.(png|jpg|webp)")


def clean(directory, now=None, apply=False):
    directory = Path(directory)
    now = now or time.time()
    if directory.is_symlink() or (directory / "generated").is_symlink():
        raise RuntimeError("Image cache directories must not be symlinks")
    files, byte_count, tasks = 0, 0, 0
    for path in (directory / "generated").glob("*"):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            continue
        age = now - info.st_mtime
        if (IMAGE_NAME.fullmatch(path.name) and age > TTL) or (re.fullmatch(r"[0-9a-f]{48}\.part", path.name) and age > 3600):
            if apply:
                path.unlink(missing_ok=True)
            files += 1
            byte_count += info.st_size
    db_path = directory / "tasks.sqlite3"
    if db_path.exists():
        if db_path.is_symlink():
            raise RuntimeError("Task database must not be a symlink")
        with closing(sqlite3.connect(db_path, timeout=10)) as db:
            # Seven days exceeds the bounded worker deadline. Stale processing
            # records must also expire if their worker could not save a result.
            tasks = db.execute("SELECT count(*) FROM tasks WHERE json_extract(record,'$.expires_at') < ?", (now,)).fetchone()[0]
            if apply:
                db.execute("DELETE FROM tasks WHERE json_extract(record,'$.expires_at') < ?", (now,))
                db.commit()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if tasks:
                    # Reclaim pages even for databases created before auto_vacuum was enabled.
                    db.execute("VACUUM")
    return {"generated_files": files, "generated_bytes": byte_count, "expired_tasks": tasks, "applied": apply}


def capacity_available(directory, additional_bytes=24 * 1024**2):
    directory = Path(directory)
    size = sum(path.stat().st_size for path in (directory / "generated").glob("*") if path.is_file() and not path.is_symlink())
    if size + additional_bytes > MAX_CACHE_BYTES or shutil.disk_usage(directory).free < MIN_FREE_BYTES + additional_bytes:
        return False
    with closing(sqlite3.connect(directory / "tasks.sqlite3", timeout=10)) as db:
        return db.execute("SELECT count(*) FROM tasks").fetchone()[0] < MAX_TASKS
