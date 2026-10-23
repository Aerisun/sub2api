#!/usr/bin/env python3
"""Native image tasks and stateless HTTP MCP; RightAPI is retired.

Customer requests always execute through Sub2API for model permissions and
billing. The retired adapter returns HTTP 410 without upstream calls. API keys stay in
memory; the task database stores their hashes, never their values.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from image_hub.retention import clean, capacity_available
from image_hub.concurrency import configured_limit
from image_hub.gateway_transport import GatewayTransport

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-image-2.5"
NATIVE_MODEL = "gpt-image-2.5-flare"
NATIVE_MODELS = frozenset({"gpt-image-2", NATIVE_MODEL, "gpt-image-2.5-sunburst"})
NATIVE_MODEL_CHOICES = (NATIVE_MODEL, "gpt-image-2.5-sunburst", "gpt-image-2")
PRICE = 0.2
MAX_BODY = 24 * 1024 * 1024
TTL = 7 * 86400
TIMEOUT = 1500
JSON_GATEWAY_TRANSPORT = GatewayTransport()
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
ORIGINS = {"https://aerisun.top", "https://vcl-qiyu-main.tail246500.ts.net",
           "http://127.0.0.1:8081", "http://localhost:8081"}


class HubError(Exception):
    def __init__(self, status, message, code="image_hub_error"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code

    def body(self):
        return {"error": {"type": self.code, "message": self.message}}


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never send a bearer token to a redirect-selected origin.
        if urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            raise HubError(502, "Unexpected upstream redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_env():
    return dict(line.split("=", 1) for line in (ROOT / "deployment/private/config/operator.env").read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#"))


def request_json(url, key, body=None, timeout=30, headers=None):
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                               "User-Agent": "Aerisun-Image-Hub/1.0", **(headers or {})})
    opener = build_opener(ProxyHandler({}), SafeRedirect())
    try:
        target = urlparse(url)
        if target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost", "::1"}:
            # Internal authenticated requests share only fully consumed sockets.
            # Headers remain request-owned and failed POSTs are never replayed.
            with JSON_GATEWAY_TRANSPORT.open(url, body=request.data, headers=dict(request.header_items()),
                                             method=request.get_method(), timeout=timeout) as response:
                status = response.code
                raw = response.read(4096 if status >= 400 else MAX_BODY + 1)
            if 300 <= status < 400:
                raise HubError(502, "Unexpected internal redirect")
            if status >= 400:
                raise_json_upstream_error(status, raw, key)
        else:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
                status = response.status
    except HTTPError as error:
        raw = error.read(4096)
        error.close()
        raise_json_upstream_error(error.code, raw, key)
    except (URLError, TimeoutError, OSError, HTTPException):
        raise HubError(504, "Upstream request timed out or connection failed") from None
    if len(raw) > MAX_BODY:
        raise HubError(502, "Upstream response is too large")
    try:
        value = json.loads(raw)
    except ValueError:
        raise HubError(502, "Upstream returned an invalid response") from None
    if not isinstance(value, dict):
        raise HubError(502, "Upstream returned an invalid response")
    return status, value


def raise_json_upstream_error(status, raw, key):
    try:
        value = json.loads(raw)
        message = value.get("error", {}).get("message") or value.get("message") or "Upstream request failed"
    except (ValueError, AttributeError):
        message = "Upstream temporarily unavailable"
    raise HubError(status, str(message).replace(key, "[redacted]")[:500]) from None


def validate_payload(body, allow_legacy=False):
    if not isinstance(body, dict):
        raise HubError(400, "Request body must be an object")
    model = body.get("model", NATIVE_MODEL)
    if model not in NATIVE_MODELS and not (allow_legacy and model == MODEL):
        raise HubError(400, "Unsupported image model")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise HubError(400, "Provide a prompt of 1–32000 characters")
    n = body.get("n", 1)
    if type(n) is not int or not 1 <= n <= 4:
        raise HubError(400, "n must be an integer from 1 to 4")
    if body.get("stream"):
        raise HubError(400, "Use asynchronous tasks for this model; streaming is unsupported")
    size = body.get("size", "1024x1024")
    if not isinstance(size, str) or not re.fullmatch(r"(?:\d{2,5}x\d{2,5}|1:1|16:9|9:16|4:3|3:4|auto)", size):
        raise HubError(400, "Invalid image dimensions or aspect ratio")
    image_size = body.get("imageSize")
    if image_size is not None and image_size not in ("1K", "2K", "4K"):
        raise HubError(400, "imageSize must be 1K, 2K, or 4K")
    images = body.get("image", [])
    if isinstance(images, str):
        images = [images]
    if not isinstance(images, list) or len(images) > 4:
        raise HubError(400, "Provide up to four reference images")
    for item in images:
        if not isinstance(item, str) or not re.match(r"^data:image/(png|jpeg|webp);base64,", item):
            raise HubError(400, "Reference images must be PNG, JPEG, or WebP data URLs")
        try:
            base64.b64decode(item.split(",", 1)[1], validate=True)
        except ValueError:
            raise HubError(400, "Invalid reference image encoding") from None
    payload = {"model": model, "prompt": prompt, "n": n, "size": size,
               "response_format": "b64_json" if model in NATIVE_MODELS else "url"}
    quality = body.get("quality")
    if quality is not None:
        if model not in NATIVE_MODELS or quality not in ("auto", "low", "medium", "high", "xhigh", "max"):
            raise HubError(400, "Unsupported image quality")
        payload["quality"] = quality
    if image_size:
        payload["imageSize"] = image_size
    if images:
        payload["image"] = images
    return payload


TOOLS = [
    {"name": "generate_image", "description": "Generate or edit images using OpenAI's native image_generation tool. Choose Flare for everyday generation and speed, Sunburst for complex instructions and precise editing, or Image 2 for compatibility. Returns an asynchronous task immediately; poll the same task_id without resubmitting. Use only when the user requests image generation or editing.",
     "inputSchema": {"type": "object", "properties": {
         "model": {"type": "string", "enum": list(NATIVE_MODEL_CHOICES), "default": NATIVE_MODEL,
                   "description": "Choose gpt-image-2.5-flare for everyday/fast images; gpt-image-2.5-sunburst for complex compositions or precise edits; gpt-image-2 for compatibility."},
         "prompt": {"type": "string", "minLength": 1},
         "size": {"type": "string", "default": "1024x1024"},
         "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
         "quality": {"type": "string", "enum": ["auto", "low", "medium", "high", "xhigh", "max"], "default": "auto"},
         "imageSize": {"type": "string", "enum": ["1K", "2K", "4K"]},
         "image": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
         "required": ["prompt"], "additionalProperties": False}},
    {"name": "get_image_task", "description": "Get the status and image URLs of a previously submitted image task. Querying does not generate another image or incur another image charge.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "wait_for_image", "description": "Wait up to 50 seconds for an image task. Returns the generated image URLs and a viewable image when complete. If still processing, call again with the same task_id; do not resubmit generation.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"},
         "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 50, "default": 45}},
         "required": ["task_id"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}}
]


class Hub:
    def __init__(self, directory=None, gateway="http://127.0.0.1:8080", upstream="https://www.rightapi.ai", env=None):
        self.directory = Path(directory or ROOT / "data/image-hub")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.images = self.directory / "generated"
        self.images.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.directory / "tasks.sqlite3"
        self.gateway, self.upstream, self.env = gateway, upstream, env or read_env()
        self.max_concurrent_tasks = configured_limit("IMAGE_GENERATION_CONCURRENCY", 16)
        self.slots = threading.BoundedSemaphore(self.max_concurrent_tasks)
        self.cache_lock = threading.Lock()
        self.task_changed = threading.Condition()
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL, record TEXT NOT NULL)")
            # A restart must never resubmit an operation that could already be billed.
            for id_, record in db.execute("SELECT id,record FROM tasks").fetchall():
                record = json.loads(record)
                if record["status"] == "processing":
                    record.update(status="interrupted", error={"message": "The service restarted during generation. Do not resubmit automatically; check usage before trying again."})
                    db.execute("UPDATE tasks SET record=? WHERE id=?", (json.dumps(record), id_))
                if record["expires_at"] < time.time():
                    db.execute("DELETE FROM tasks WHERE id=?", (id_,))
        self.db_path.chmod(0o600)
        clean(self.directory, apply=True)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def housekeeping(self):
        while True:
            try:
                report = clean(self.directory, apply=True)
                (self.directory / "retention-status.json").write_text(json.dumps({"checked_at": int(time.time()), **report}) + "\n")
            except (OSError, sqlite3.Error):
                pass
            time.sleep(600)

    @staticmethod
    def owner(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def authenticate(self, key, headers=None, model=None):
        if not key:
            raise HubError(401, "Use your site's API key", "authentication_error")
        _, models = request_json(self.gateway + ("/v1/models" if key.startswith("sk-") else "/api/v1/chat/models"), key, headers=headers)
        available = {m.get("id") for m in models.get("data", [])}
        if not (model in available if model else available & NATIVE_MODELS):
            raise HubError(403, "This API key does not have access to " + (model or "native image models"), "permission_error")

    def get_task(self, key, task_id):
        with self.db() as db:
            row = db.execute("SELECT record FROM tasks WHERE id=? AND owner=?", (task_id, self.owner(key))).fetchone()
        if not row:
            raise HubError(404, "Image task not found")
        record = json.loads(row[0])
        if record["expires_at"] < time.time():
            raise HubError(404, "Image task expired")
        return record

    def save_task(self, key, record):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?)", (record["task_id"], self.owner(key), json.dumps(record)))
        with self.task_changed:
            self.task_changed.notify_all()

    def wait_task(self, key, task_id, seconds):
        deadline = time.monotonic() + seconds
        with self.task_changed:
            while True:
                task = self.get_task(key, task_id)
                remaining = deadline - time.monotonic()
                if task["status"] != "processing" or remaining <= 0:
                    return task
                self.task_changed.wait(remaining)

    def submit(self, key, body, headers=None, idempotency=None):
        payload = validate_payload(body)
        self.authenticate(key, headers, model=payload["model"])
        # Scope duplicate suppression to one key AND one exact request body.
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if idempotency:
            if len(idempotency) > 128:
                raise HubError(400, "Idempotency-Key is too long")
            task_id = "imgtask_" + hmac.new(key.encode(), idempotency.encode(), hashlib.sha256).hexdigest()[:32]
        else:
            task_id = "imgtask_" + secrets.token_hex(16)
        if idempotency:
            try:
                previous = self.get_task(key, task_id)
            except HubError as error:
                if error.status != 404 or error.message != "Image task not found":
                    raise
            else:
                if previous.get("request_fingerprint") != fingerprint:
                    raise HubError(409, "Idempotency-Key was already used for a different request")
                # Reading an existing operation requires no generation slot or
                # free output storage, even when all other image jobs are busy.
                return previous
        if not capacity_available(self.directory):
            raise HubError(503, "Image storage is at capacity; try again after cleanup")
        if not self.slots.acquire(blocking=False):
            raise HubError(429, "Image hub is busy; try again shortly")
        record = {"id": task_id, "task_id": task_id, "object": "image.generation.task", "status": "processing",
                  "model": payload["model"], "created_at": int(time.time()), "expires_at": int(time.time()) + TTL,
                  "poll_url": "/v1/images/tasks/" + task_id,
                  "pricing": {"currency": "USD", "per_image": PRICE, "requested_images": payload["n"], "estimated_cost": round(payload["n"] * PRICE, 2)},
                  "request_fingerprint": fingerprint}
        if payload["model"] in NATIVE_MODELS:
            # Native generation is billed by the gateway's configured model
            # pricing. Never label it with the separate RightAPI flat price.
            record["pricing"] = {"currency": "USD", "billing": "gateway_usage", "requested_images": payload["n"]}
        with self.db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,?)", (task_id, self.owner(key), json.dumps(record))).rowcount
        if not inserted:
            self.slots.release()
            previous = self.get_task(key, task_id)
            if previous.get("request_fingerprint") != fingerprint:
                raise HubError(409, "Idempotency-Key was already used for a different request")
            return previous
        threading.Thread(target=self.execute, args=(key, payload, record, headers), daemon=True).start()
        return record

    def execute(self, key, payload, record, headers=None):
        try:
            status, result = request_json(self.gateway + ("/v1/images/generations" if key.startswith("sk-") else "/api/v1/chat/images/generations"), key, payload,
                                          timeout=TIMEOUT + 60, headers={**(headers or {}), "X-Client-Request-ID": record["task_id"]})
            items = result.get("data", [])
            if not items or any(not (item.get("url") or item.get("b64_json")) for item in items):
                raise HubError(502, "Image generation returned no image")
            # Cache actual generated outputs for the website and agent previews.
            for item in items:
                try:
                    raw, mime = self.download_image(item)
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
                    filename = secrets.token_hex(24) + suffix
                    path = self.images / filename
                    with self.cache_lock:
                        if not capacity_available(self.directory, len(raw)):
                            raise HubError(503, "Image cache is at capacity")
                        temporary = path.with_suffix(".part")
                        temporary.write_bytes(raw)
                        temporary.chmod(0o600)
                        temporary.replace(path)
                    item["url"] = "https://aerisun.top/sub2api/image-hub/results/" + filename
                    item.pop("b64_json", None)
                except (HubError, OSError, KeyError):
                    # Generation and billing already succeeded: preserve its usable URL.
                    pass
            record.update(status="completed", http_status=status, result=result,
                          image_url=items[0].get("url", ""), image_count=len(items))
            if payload["model"] == MODEL:
                record["cost"] = round(len(items) * PRICE, 2)
        except HubError as error:
            record.update(status="failed", http_status=error.status, error=error.body()["error"])
        except Exception:
            record.update(status="failed", http_status=500, error={"message": "Image task could not be completed"})
        finally:
            record["completed_at"] = int(time.time())
            try:
                self.save_task(key, record)
            finally:
                self.slots.release()

    def download_image(self, item):
        encoded = item.get("b64_json")
        if encoded:
            raw = base64.b64decode(encoded, validate=True)
        else:
            url = item.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise HubError(502, "Invalid generated image URL")
            for address in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
                if not ipaddress.ip_address(address[4][0]).is_global:
                    raise HubError(502, "Invalid generated image host")
            try:
                with build_opener(ProxyHandler({}), SafeRedirect()).open(Request(url, headers={"User-Agent": "Aerisun-Image-Hub/1.0"}), timeout=45) as response:
                    raw = response.read(MAX_BODY + 1)
            except (HTTPError, URLError, TimeoutError, OSError):
                raise HubError(502, "Could not cache image") from None
        if len(raw) > MAX_BODY:
            raise HubError(502, "Generated image too large")
        mime = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None
        if not mime:
            raise HubError(502, "Invalid generated image bytes")
        return raw, mime

    def generate_upstream(self, body):
        # Retired: no request may reach the former provider, including old
        # internal adapter URLs and accidentally restored account bindings.
        raise HubError(410, "RightAPI is retired; use a native image model", "provider_retired")

    def rpc(self, key, message, headers=None, idempotency=None):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise HubError(400, "Invalid JSON-RPC request")
        id_, method = message.get("id"), message.get("method")
        if "id" not in message:
            return None
        if method == "initialize":
            version = message.get("params", {}).get("protocolVersion")
            result = {"protocolVersion": version if version in PROTOCOLS else "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "aerisun-image-hub", "version": "1.0.0"},
                      "instructions": "Choose among the three native image models according to the task. Generate once, then poll the same task_id. Image usage is billed by the gateway. Preserve the user's requested subject and return the resulting image URL."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            args, name = params.get("arguments") or {}, params.get("name")
            try:
                if not isinstance(args, dict):
                    raise HubError(400, "Tool arguments must be an object")
                if name == "generate_image":
                    value = self.submit(key, args, headers, idempotency)
                elif name in ("get_image_task", "wait_for_image"):
                    task_id = args.get("task_id")
                    if not isinstance(task_id, str):
                        raise HubError(400, "task_id is required")
                    timeout = args.get("timeout_seconds", 45) if name == "wait_for_image" else 0
                    if type(timeout) is not int or not 0 <= timeout <= 50:
                        raise HubError(400, "timeout_seconds must be an integer from 1 to 50")
                    value = self.wait_task(key, task_id, timeout)
                else:
                    raise HubError(400, "Unknown tool")
                content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
                if name == "wait_for_image" and value["status"] == "completed":
                    filename = value.get("image_url", "").rsplit("/", 1)[-1]
                    if re.fullmatch(r"[0-9a-f]{48}\.(png|jpg|webp)", filename):
                        path = self.images / filename
                        if path.exists() and path.stat().st_size <= 5 * 1024 * 1024:
                            mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[path.suffix]
                            content.append({"type": "image", "mimeType": mime, "data": base64.b64encode(path.read_bytes()).decode()})
                result = {"content": content, "structuredContent": value, "isError": value.get("status") in ("failed", "interrupted")}
            except HubError as error:
                result = {"content": [{"type": "text", "text": json.dumps(error.body(), ensure_ascii=False)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": id_, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def reply(self, status, body=None, content_type="application/json", headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if not any(name.lower() == "cache-control" for name in (headers or {})):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise HubError(400, "Invalid request length") from None
        if not 0 < size <= MAX_BODY:
            raise HubError(413 if size > MAX_BODY else 400, "Invalid request body size")
        raw = self.rfile.read(size)
        if "multipart/form-data" in self.headers.get("Content-Type", ""):
            form = BytesParser(policy=email_policy).parsebytes(("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            data, images = {}, []
            for part in form.iter_parts():
                name = part.get_param("name", header="content-disposition")
                value = part.get_payload(decode=True)
                if part.get_filename():
                    if name not in ("image", "image[]", "images", "images[]"):
                        raise HubError(400, "Masks are not supported by this image provider")
                    images.append("data:" + part.get_content_type() + ";base64," + base64.b64encode(value).decode())
                elif name:
                    data[name] = value.decode()
            if images:
                data["image"] = images
            if "n" in data:
                try:
                    data["n"] = int(data["n"])
                except ValueError:
                    raise HubError(400, "Invalid image count") from None
            return data
        try:
            return json.loads(raw)
        except ValueError:
            raise HubError(400, "Invalid JSON request") from None

    def handle_request(self, post=False):
        try:
            self.connection.settimeout(TIMEOUT + 90)
            path = urlparse(self.path).path
            origin = self.headers.get("Origin")
            if origin and origin not in ORIGINS:
                raise HubError(403, "Origin not allowed")
            hub = self.server.hub
            if path == "/health":
                self.reply(200, {"status": "ok", "model": NATIVE_MODEL, "models": list(NATIVE_MODEL_CHOICES),
                                 "max_concurrent_tasks": hub.max_concurrent_tasks})
                return
            key = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if path.startswith("/upstream/"):
                raise HubError(410, "RightAPI is retired; use a native image model", "provider_retired")
            if path.startswith("/image-hub/results/") and not post:
                filename = path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[0-9a-f]{48}\.(png|jpg|webp)", filename):
                    raise HubError(404, "Image not found")
                image = hub.images / filename
                if not image.exists():
                    raise HubError(404, "Image expired")
                with image.open("rb") as source:
                    stat = os.fstat(source.fileno())
                    remaining = int(stat.st_mtime + TTL - time.time())
                    if remaining <= 0:
                        raise HubError(404, "Image expired")
                    # Output capability URLs are immutable. Cache them privately
                    # for five minutes at most, never past their original expiry.
                    etag = f'"{filename}-{stat.st_size:x}-{stat.st_mtime_ns:x}"'
                    cached = any(tag.strip().removeprefix("W/") in (etag, "*") for tag in
                                 self.headers.get("If-None-Match", "").split(","))
                    self.send_response(304 if cached else 200)
                    self.send_header("Content-Type", {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[image.suffix])
                    self.send_header("Cache-Control", f"private, max-age={min(300, remaining)}, immutable")
                    self.send_header("ETag", etag)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    if not cached:
                        self.send_header("Content-Length", str(stat.st_size))
                    self.end_headers()
                    if not cached and self.command != "HEAD":
                        while chunk := source.read(65536):
                            self.wfile.write(chunk)
                return
            forwarded = {name: self.headers[name] for name in ("X-Real-IP", "X-Forwarded-For") if name in self.headers}
            if path == "/image-hub/mcp":
                hub.authenticate(key, forwarded)
                if not post:
                    self.reply(405, headers={"Allow": "POST"})
                    return
                if self.headers.get("MCP-Protocol-Version", "2025-03-26") not in PROTOCOLS:
                    raise HubError(400, "Unsupported MCP protocol version")
                result = hub.rpc(key, self.body(), forwarded, self.headers.get("Idempotency-Key"))
                self.reply(202 if result is None else 200, result)
            elif path == "/v1/images/generations/async" and post:
                result = hub.submit(key, self.body(), forwarded, self.headers.get("Idempotency-Key"))
                self.reply(202, result, headers={"Location": result["poll_url"], "Retry-After": "3"})
            elif path.startswith("/v1/images/tasks/") and not post:
                if not key:
                    raise HubError(401, "Use your site's API key", "authentication_error")
                task = hub.get_task(key, path.rsplit("/", 1)[-1])
                hub.authenticate(key, forwarded, model=task.get("model", MODEL))
                waits = parse_qs(urlparse(self.path).query).get("wait_seconds", ["0"])
                if len(waits) != 1 or not waits[0].isdecimal() or not 0 <= int(waits[0]) <= 30:
                    raise HubError(400, "wait_seconds must be an integer from 0 to 30")
                if int(waits[0]) and task["status"] == "processing":
                    task = hub.wait_task(key, task["task_id"], int(waits[0]))
                self.reply(200, task)
            else:
                hub.authenticate(key, forwarded)
                raise HubError(404, "Image hub endpoint not found")
        except HubError as error:
            self.reply(error.status, error.body())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:
            self.reply(500, {"error": {"message": "Image hub request failed"}})
        finally:
            self.connection.settimeout(15)

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        if not urlparse(self.path).path.startswith("/image-hub/results/"):
            return self.reply(405, headers={"Allow": "GET, POST"})
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


if __name__ == "__main__":
    os.umask(0o077)
    server = ThreadingHTTPServer(("127.0.0.1", 8092), Handler)
    server.hub = Hub()
    threading.Thread(target=server.hub.housekeeping, daemon=True).start()
    server.serve_forever()
