#!/usr/bin/env python3
"""Download pinned model ranges concurrently, resume parts, and verify SHA-256."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "model.json").read_text())
DEST = ROOT / "models" / MANIFEST["filename"]
PARTS = ROOT / "artifacts" / "model-parts"
CHUNK = 64 * 1024 * 1024
PROXY = os.environ.get("RERANK_DOWNLOAD_PROXY", "http://127.0.0.1:17890")
# The metadata endpoint needs this host's proxy; the storage CDN is reachable
# directly and avoids routing gigabytes through the metadata proxy.
os.environ["no_proxy"] = ",".join(filter(None, [os.environ.get("no_proxy", ""),
    ".hf.co"]))
progress = {}
lock = threading.Lock()


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(index):
    start = index * CHUNK
    end = min(start + CHUNK, MANIFEST["size"]) - 1
    part = PARTS / f"{index:04d}.part"
    for attempt in range(8):
        done = part.stat().st_size if part.exists() else 0
        with lock:
            progress[index] = done
        if done == end - start + 1:
            return part
        if done > end - start + 1:
            raise RuntimeError(f"Oversized download part {index}")
        try:
            opener = build_opener(ProxyHandler({"https": PROXY} if PROXY else {}))
            request = Request(MANIFEST["url"], headers={
                "Range": f"bytes={start + done}-{end}",
                "Accept-Encoding": "identity",
            })
            with opener.open(request, timeout=45) as response:
                expected = f"bytes {start + done}-{end}/{MANIFEST['size']}"
                if response.status != 206 or response.headers.get("Content-Range") != expected:
                    raise RuntimeError(f"Unexpected range response for part {index}")
                with part.open("ab") as out:
                    while block := response.read(256 * 1024):
                        if done + len(block) > end - start + 1:
                            raise RuntimeError("Server sent too many bytes")
                        out.write(block)
                        done += len(block)
                        with lock:
                            progress[index] = done
            if done == end - start + 1:
                return part
        except Exception as error:
            print(f"Retry part {index}: {type(error).__name__}", flush=True)
            if attempt == 7:
                raise RuntimeError(f"Part {index} failed: {error}") from error
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Incomplete part {index}")


def main():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    if DEST.exists() and digest(DEST) == MANIFEST["sha256"]:
        print("Model already verified.", flush=True)
        return
    # Preserve bytes from an earlier sequential download.
    old = DEST.with_suffix(DEST.suffix + ".part")
    if old.exists() and not any(PARTS.iterdir()):
        with old.open("rb") as source:
            index = 0
            while block := source.read(CHUNK):
                (PARTS / f"{index:04d}.part").write_bytes(block)
                index += 1
    count = (MANIFEST["size"] + CHUNK - 1) // CHUNK
    started = time.monotonic()
    initial = sum(p.stat().st_size for p in PARTS.glob("*.part"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        pending = {executor.submit(download, i) for i in range(count)}
        while pending:
            finished, pending = concurrent.futures.wait(pending, timeout=10,
                return_when=concurrent.futures.FIRST_EXCEPTION)
            for result in finished:
                result.result()
            with lock:
                received = sum(progress.values())
            elapsed = max(time.monotonic() - started, 0.1)
            print(f"Model: {received / MANIFEST['size']:.1%}; "
                  f"{received / 2**20:.0f} MiB; "
                  f"{max(received - initial, 0) / 2**20 / elapsed:.1f} MiB/s", flush=True)
    temporary = DEST.with_suffix(DEST.suffix + ".verified-part")
    result = hashlib.sha256()
    with temporary.open("wb") as out:
        for i in range(count):
            with (PARTS / f"{i:04d}.part").open("rb") as source:
                for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    result.update(block)
                    out.write(block)
    if result.hexdigest() != MANIFEST["sha256"]:
        temporary.unlink()
        raise RuntimeError("Model SHA-256 mismatch; do not start the service")
    temporary.replace(DEST)
    for part in PARTS.glob("*.part"):
        part.unlink()
    old.unlink(missing_ok=True)
    print("Model SHA-256 verified.", flush=True)


if __name__ == "__main__":
    main()
