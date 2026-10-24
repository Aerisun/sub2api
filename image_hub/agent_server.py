"""Loopback chat proxy with server-managed image generation tools.

Separate from the task worker so restarts here cannot interrupt or resubmit
paid image tasks. The gateway remains authoritative for customer permissions
and all billing. Request bodies and chat history are never written to disk.
"""
import json
import hashlib
import multiprocessing
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException, IncompleteRead
from urllib.parse import urlsplit, parse_qs

from image_hub.agent_bridge import (ImageExecutor, Stream, eligible, inject,
                                    image_model_for_chat, normalized_path, protocol, run)
from image_hub.server import HubError, NATIVE_MODEL, NATIVE_MODEL_CHOICES, ORIGINS, request_json
from image_hub.model_access import ImageAccessCache
from image_hub.gateway_transport import GatewayTransport
from image_hub.client_disconnect import ClientDisconnectMonitor
from image_hub.response_latency import ResponseLatency, record as record_response_latency
from image_hub.image_intent import self_contained_calculation, wants_image_generation
from internal_tools import http_api
from internal_tools.chat_bridge import ToolExecutor, inject_search, search_eligible
from internal_tools.web_search import WebSearchExecutor, search_model
from internal_tools.image_tools import RECOGNITION_SLOTS, RECOGNITION_UPSTREAM_SLOTS, MAX_BODY as IMAGE_TOOL_MAX_BODY
from internal_tools.image_upload import recognition_upload
from internal_tools.errors import error_details

GATEWAY = os.environ.get("AGENT_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
HUB = "http://127.0.0.1:8092"
MAX_BODY = 256 * 1024 * 1024
IMAGE_ACCESS = ImageAccessCache(ttl=30)
GATEWAY_TRANSPORT = GatewayTransport()
CLIENT_DISCONNECT = ClientDisconnectMonitor()
HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "upgrade",
               "accept-encoding", "proxy-authorization", "proxy-connection", "keep-alive", "te", "trailer"}


class Handler(BaseHTTPRequestHandler):
    response_latency = None
    protocol_version = "HTTP/1.1"
    # SSE headers and small deltas must be sent without a delayed-ACK wait.
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass  # No chat prompts, credentials or generated links in access logs.

    def send_response(self, code, message=None):
        if self.response_latency is not None:
            self.response_latency.status = code
        super().send_response(code, message)

    def end_headers(self):
        try:
            super().end_headers()
        except OSError:
            if self.response_latency is not None:
                self.response_latency.cancelled = True
            raise

    def observe_response(self, raw):
        try:
            if self.response_latency is not None:
                self.response_latency.feed(raw)
        except Exception:
            self.response_latency = None

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def reply(self, status, value, content_type="application/json"):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)
        self.observe_response(raw)

    def forwarded_headers(self):
        return {**{name: value for name, value in self.headers.items() if name.lower() not in HOP_HEADERS},
                "Accept-Encoding": "identity"}

    def open_upstream(self, body, method="POST", client_request=None):
        return GATEWAY_TRANSPORT.open(GATEWAY + normalized_path(self.path), body=body,
                                      headers=self.forwarded_headers(), method=method,
                                      client_request=client_request)

    def passthrough(self, raw=None, method="POST"):
        started = False
        try:
            with CLIENT_DISCONNECT.watch(self.connection) as client_request:
                with self.open_upstream(raw, method, client_request) as response:
                    self.send_response(response.code)
                    hop_headers = HOP_HEADERS | {name.strip().lower() for name in
                                                  response.headers.get("Connection", "").split(",")}
                    for name, value in response.headers.items():
                        if name.lower() not in hop_headers:
                            self.send_header(name, value)
                    bodyless = method == "HEAD" or response.code < 200 or response.code in (204, 304)
                    chunked = not bodyless and response.length is None and self.request_version != "HTTP/1.0"
                    if chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    elif not bodyless and response.length is not None:
                        self.send_header("Content-Length", str(response.length))
                    elif not bodyless:
                        self.send_header("Connection", "close")
                        self.close_connection = True
                    elif method == "HEAD" or response.code == 304:
                        length = response.headers.get("Content-Length")
                        if length is not None and length.isdecimal():
                            self.send_header("Content-Length", length)
                    self.end_headers()
                    started = True
                    if not bodyless:
                        # HTTPResponse decodes upstream chunks. Reframe the
                        # available bytes immediately so ingress can reuse its
                        # connection without buffering a complete generation.
                        while chunk := response.read1(65536):
                            client_request.check()
                            try:
                                self.wfile.write((f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                                                 if chunked else chunk)
                                self.wfile.flush()
                            except OSError:
                                if self.response_latency is not None:
                                    self.response_latency.cancelled = True
                                raise
                            self.observe_response(chunk)
                        if response.length not in (0, None):
                            raise IncompleteRead(b"")
                        client_request.check()
                        if chunked:
                            try:
                                self.wfile.write(b"0\r\n\r\n")
                                self.wfile.flush()
                            except OSError:
                                if self.response_latency is not None:
                                    self.response_latency.cancelled = True
                                raise
        except (OSError, HTTPException):
            self.close_connection = True
            if self.response_latency is not None:
                if client_request.cancelled:
                    self.response_latency.cancelled = True
                elif not self.response_latency.cancelled:
                    self.response_latency.transport_failed = True
            # Once headers are sent, terminate an incomplete response. A second
            # JSON/HTTP response here would corrupt this stream and its framing.
            if not started and not client_request.cancelled:
                raise
        finally:
            self.connection.settimeout(15)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "service": "agent-image-bridge", "model": NATIVE_MODEL,
                                    "models": list(NATIVE_MODEL_CHOICES),
                                    "tool_concurrency": {"image_recognition": RECOGNITION_SLOTS.limit},
                                    "tool_upstream_concurrency": {"image_recognition": RECOGNITION_UPSTREAM_SLOTS.limit},
                                    "enabled": os.environ.get("AGENT_IMAGE_BRIDGE_ENABLED", "1") != "0"})
        try:
            if urlsplit(self.path).path.rstrip('/') in ('/v1/models','/models') and 'client_version' not in parse_qs(urlsplit(self.path).query):
                _,value=request_json(GATEWAY+normalized_path(self.path),http_api.credential(self),timeout=10,headers=http_api.client_headers(self))
                if isinstance(value.get('data'),list):
                    value['data']=[item for item in value['data'] if not str(item.get('id','')).startswith('gpt-image-')]
                    if 'first_id' in value:value['first_id']=value['data'][0]['id'] if value['data'] else ''
                    if 'last_id' in value:value['last_id']=value['data'][-1]['id'] if value['data'] else ''
                    for item in value['data']:
                        if item.get('id') in ('image_generation','image_recognition','web_search'):
                            item.update(capability='tool',endpoint='/v1/tools/'+item['id'])
                return http_api.reply(self,200,value)
            if http_api.matches(self.path):
                if self.headers.get("Origin") and self.headers["Origin"] not in ORIGINS:
                    return self.reply(403, {"error": {"message": "Origin is not allowed"}})
                return http_api.dispatch(self, GATEWAY)
            self.passthrough(method="GET")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            if self.response_latency is not None and not self.response_latency.transport_failed:
                self.response_latency.cancelled = True
        except HubError as error:
            self.reply(error.status,error.body())

    def do_POST(self):
        self.response_latency = None
        request_started = time.monotonic()
        self.connection.settimeout(1590)
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.close_connection = True
                return self.reply(400, {"error": {"code": "invalid_request", "message": "Content-Length must be an integer"}})
            limit = (64*1024 if urlsplit(self.path).path.endswith('/web_search') else IMAGE_TOOL_MAX_BODY) if http_api.matches(self.path) else MAX_BODY
            if not 0 < length <= limit:
                self.close_connection = True
                return self.reply(413 if length > limit else 400, {"error": {"message": "Invalid request body size"}})
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400, {"error": {"message": "Incomplete request body"}})
            content_type = self.headers.get("Content-Type", "")
            if http_api.matches(self.path) and content_type.lower().startswith("multipart/"):
                if self.headers.get("Origin") and self.headers["Origin"] not in ORIGINS:
                    return self.reply(403, {"error": {"message": "Origin is not allowed"}})
                if urlsplit(self.path).path not in ("/v1/tools/image_recognition", "/tools/image_recognition"):
                    return http_api.reply(self, 415, {"error": {"code":"unsupported_media_type", "message":"文件上传请使用 /v1/tools/image_recognition"}})
                try:
                    body = recognition_upload(raw, content_type)
                except HubError as error:
                    return http_api.reply(self,error.status,{"error":error_details(error)})
                return http_api.dispatch(self, GATEWAY, body)
            try:
                body = json.loads(raw)
            except ValueError:
                if http_api.matches(self.path):
                    return http_api.invalid_json(self)
                return self.passthrough(raw)
            origin = self.headers.get("Origin")
            if http_api.matches(self.path):
                if origin and origin not in ORIGINS:
                    return self.reply(403, {"error": {"message": "Origin is not allowed"}})
                return http_api.dispatch(self, GATEWAY, body)
            kind = protocol(self.path)
            model = body.get('model') if isinstance(body, dict) else None
            if kind == 'gemini' and not model:
                model = urlsplit(self.path).path.split('/models/', 1)[-1].split(':', 1)[0]
            if kind and isinstance(model, str) and 0 < len(model) <= 200:
                streaming = bool(body.get('stream')) or kind == 'gemini' and ':streamGenerateContent' in self.path
                self.response_latency = ResponseLatency(model, kind, streaming, len(raw), request_started)
            try:
                allowed = eligible(body, kind, self.path) and wants_image_generation(body, kind)
                search_allowed = search_eligible(body, kind, self.path) and not self_contained_calculation(body, kind)
            except (ValueError, TypeError, AttributeError, KeyError):
                allowed = search_allowed = False
            if os.environ.get("AGENT_IMAGE_BRIDGE_ENABLED", "1") == "0":
                allowed = False
            if os.environ.get("AGENT_WEB_SEARCH_ENABLED", "1") == "0":
                search_allowed = False
            if not allowed and not search_allowed:
                return self.passthrough(raw)
            # Authenticate without granting image permissions to a text-only key.
            # Actual generation checks current permissions and balance again.
            key = http_api.credential(self) or self.headers.get("x-goog-api-key", "")
            if origin and origin not in ORIGINS:
                return self.reply(403, {"error": {"message": "Origin is not allowed"}})
            image_model = image_model_for_chat(body.get('model'))
            available = IMAGE_ACCESS.available(GATEWAY, key)
            allowed_models = [model for model in NATIVE_MODEL_CHOICES if model in available]
            allowed = allowed and bool(allowed_models)
            # A slow optional catalogue must not silently remove search from a
            # conversation. On an unknown hint, offer it provisionally; actual
            # execution rechecks authentication, allowed models and balance.
            search_allowed = search_allowed and bool(key) and (search_model(available) is not None or not IMAGE_ACCESS.has_current_catalogue(GATEWAY, key))
            if not allowed and not search_allowed:
                return self.passthrough(raw)
            if allowed and image_model not in allowed_models:
                image_model = allowed_models[0]
            streaming = bool(body.get("stream")) or kind == "gemini" and ":streamGenerateContent" in self.path
            client_headers = {name: self.headers[name] for name in ("X-Real-IP", "X-Forwarded-For") if name in self.headers}
            identity = self.headers.get("Idempotency-Key")
            identity = hashlib.sha256(identity.encode()).hexdigest()[:32] if identity else None
            image = ImageExecutor(key, client_headers, hub=HUB, identity=identity, model=image_model, allowed_models=allowed_models) if allowed else None
            search = WebSearchExecutor(key, GATEWAY, client_headers) if search_allowed else None
            executor = ToolExecutor(search=search, image=image) if search else image
            if allowed:
                body = inject(body, kind, image_model=image_model, allowed_models=allowed_models)
            if search:
                body = inject_search(body, kind)
            # Gemini streaming must use SSE rather than its default JSON array.
            if streaming and kind == "gemini" and "alt=sse" not in self.path:
                self.path += ("&" if "?" in self.path else "?") + "alt=sse"
            self.bridge(body, kind, streaming, executor)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            if self.response_latency is not None and not self.response_latency.transport_failed:
                self.response_latency.cancelled = True
        except HubError as error:
            self.reply(error.status, error.body())
        except (ValueError, TypeError, KeyError, AttributeError, OSError, HTTPException):
            self.reply(502, {"error": {"message": "The model response could not be processed"}})
        finally:
            record_response_latency(self.response_latency)

    def bridge(self, body, kind, streaming, executor):
        with CLIENT_DISCONNECT.watch(self.connection) as client_request:
            return self.bridge_request(body, kind, streaming, executor, client_request)

    def bridge_request(self, body, kind, streaming, executor, client_request):
        started = False
        model_requests = 0
        disconnected = False
        write_lock = threading.Lock()
        stopped = threading.Event()

        def chunk(raw):
            nonlocal disconnected
            with write_lock:
                disconnected |= client_request.cancelled
                if disconnected and self.response_latency is not None:
                    self.response_latency.cancelled = True
                if disconnected or stopped.is_set():
                    return
                try:
                    self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                    self.wfile.flush()
                    self.observe_response(raw)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    disconnected = True
                    self.close_connection = True
                    if self.response_latency is not None:
                        self.response_latency.cancelled = True

        def emit(event, value):
            encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            raw = (("event: " + event + "\n") if event else "") + "data: " + encoded + "\n\n"
            chunk(raw.encode())

        def heartbeat():
            while not stopped.wait(5):
                chunk(b": keep-alive\n\n")

        def upstream(next_body):
            nonlocal started, model_requests
            if disconnected or client_request.cancelled:
                raise HubError(499, "Client disconnected; image tasks remain queryable")
            response = self.open_upstream(json.dumps(next_body, ensure_ascii=False).encode(),
                                          client_request=client_request)
            model_requests += 1
            if response.code >= 400:
                if not started and model_requests == 1:
                    raw = response.read(65536)
                    self.reply(response.code, raw, response.headers.get("Content-Type", "application/json"))
                    response.close()
                    raise AlreadyReplied()
                response.close()
                raise HubError(502, "Model continuation failed after image execution")
            if streaming and not started:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                # Chunk framing already delimits this response. Keeping the
                # socket lets ingress reuse it for the next independent turn.
                started = True
                threading.Thread(target=heartbeat, daemon=True).start()
            return response

        try:
            stream = Stream(kind, emit, getattr(executor, "names", None)) if streaming else None
            value = run(body, kind, upstream, executor, stream)
            if not streaming:
                self.reply(200, value)
        except AlreadyReplied:
            pass
        except (HubError, ValueError, KeyError, TypeError, OSError, HTTPException) as error:
            if client_request.cancelled:
                disconnected = True
                raise ConnectionAbortedError("Client disconnected") from None
            if not started:
                raise HubError(getattr(error, "status", 502), getattr(error, "message", "Model response was incomplete; no automatic image resubmission")) from None
            if kind == "anthropic":
                emit("error", {"type": "error", "error": {"type": "api_error", "message": getattr(error, "message", "Model stream could not be processed")}})
            else:
                emit("error", {"error": {"message": getattr(error, "message", "Model stream could not be processed")}})
        finally:
            with write_lock:
                stopped.set()
                if started and not disconnected:
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except OSError:
                        self.close_connection = True
                        if self.response_latency is not None:
                            self.response_latency.cancelled = True
            # Active generations retain their existing long read timeout;
            # bound idle keep-alive threads after a completed response.
            self.connection.settimeout(15)


class AlreadyReplied(Exception):
    pass


class AgentHTTPServer(ThreadingHTTPServer):
    # The stdlib's five-connection accept queue drops burst connections into
    # TCP retransmission backoff before the gateway can even schedule them.
    request_queue_size = 512
    daemon_threads = True


def serve_worker(server):
    # Workers inherit only the empty listener and module state. Fork happens
    # before any request threads or customer credentials exist in the parent.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    server.serve_forever()


def serve_workers(server, count):
    if count == 1:
        return server.serve_forever()
    context = multiprocessing.get_context('fork')
    stopped = threading.Event()
    children = [None] * count
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stopped.set())
    try:
        while not stopped.is_set():
            for index, child in enumerate(children):
                if child is not None and child.is_alive():
                    continue
                if child is not None:
                    child.join()
                child = context.Process(target=serve_worker, args=(server,), name=f'agent-bridge-{index + 1}')
                child.start()
                children[index] = child
            stopped.wait(.25)
    finally:
        for child in children:
            if child is not None and child.is_alive():
                child.terminate()
        for child in children:
            if child is not None:
                child.join(timeout=5)
                if child.is_alive():
                    child.kill()
                    child.join(timeout=1)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()


def main():
    count = int(os.environ.get('AGENT_IMAGE_BRIDGE_WORKERS', '4'))
    if not 1 <= count <= 8:
        raise SystemExit('AGENT_IMAGE_BRIDGE_WORKERS must be between 1 and 8')
    server = AgentHTTPServer(("127.0.0.1", int(os.environ.get("AGENT_IMAGE_BRIDGE_PORT", "8094"))), Handler)
    serve_workers(server, count)


if __name__ == "__main__":
    main()
