#!/usr/bin/env python3
"""A small, OpenAI-compatible model gateway with virtual-key billing.

The gateway intentionally uses the standard library only.  Models are an
explicit allow-list in gateway_models.json; a request can never choose an
arbitrary upstream URL or leak an upstream provider key.
"""
from __future__ import annotations

import gzip
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from quota_store import (
    MICRO_USD_PER_USD,
    CreditExceeded,
    CreditNotEnabled,
    DisabledAPIKey,
    InvalidAPIKey,
    QuotaExceeded,
    QuotaSnapshot,
    authenticate,
    finalize_credit_reservation,
    finalize_reservation,
    init_database,
    recover_abandoned_reservations,
    reserve_credit,
    reserve_tokens,
)


PROJECT_DIR = Path(__file__).resolve().parent.parent
SUPPORTED_ENDPOINTS = frozenset({"embeddings", "chat/completions", "responses"})
HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def read_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for raw_line in (PROJECT_DIR / "config.env").read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        config[key] = value
    return config


def usd_to_microusd(value: object, field: str) -> int:
    """Parse a non-negative USD value exactly into integer micro-USD."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal USD amount")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a decimal USD amount") from error
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{field} must be a non-negative finite amount")
    scaled = decimal_value * MICRO_USD_PER_USD
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{field} supports at most six decimal places")
    return int(scaled)


@dataclass(frozen=True)
class ModelRoute:
    public_id: str
    provider: str
    endpoints: frozenset[str]
    upstream_model: str
    input_price_microusd_per_million: int
    output_price_microusd_per_million: int
    base_url: str | None = None
    api_key_file: Path | None = None
    max_output_tokens: int | None = None
    chat_max_output_parameter: str = "max_completion_tokens"

    @property
    def is_billable(self) -> bool:
        return (
            self.input_price_microusd_per_million > 0
            or self.output_price_microusd_per_million > 0
        )


def _model_config_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = (PROJECT_DIR / value).resolve()
    if PROJECT_DIR != path and PROJECT_DIR not in path.parents:
        raise ValueError(f"{field} must stay inside the project directory")
    return path


def load_model_routes(path: Path) -> dict[str, ModelRoute]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise RuntimeError(f"model configuration is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid model configuration {path}: {error}") from error
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("model configuration needs a non-empty 'models' array")

    routes: dict[str, ModelRoute] = {}
    for index, entry in enumerate(entries):
        label = f"models[{index}]"
        if not isinstance(entry, dict):
            raise RuntimeError(f"{label} must be an object")
        public_id = entry.get("id")
        provider = entry.get("provider")
        upstream_model = entry.get("upstream_model")
        endpoints = entry.get("endpoints")
        if not isinstance(public_id, str) or not public_id.strip():
            raise RuntimeError(f"{label}.id must be a non-empty string")
        if public_id in routes:
            raise RuntimeError(f"duplicate model id: {public_id}")
        if provider not in {"local_llama", "openai_compatible"}:
            raise RuntimeError(f"{label}.provider is not supported")
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise RuntimeError(f"{label}.upstream_model must be a non-empty string")
        if not isinstance(endpoints, list) or not endpoints or not all(
            isinstance(item, str) and item in SUPPORTED_ENDPOINTS for item in endpoints
        ):
            raise RuntimeError(f"{label}.endpoints contains an unsupported endpoint")
        if provider == "local_llama" and set(endpoints) != {"embeddings"}:
            raise RuntimeError(f"{label}: local_llama currently supports only embeddings")

        try:
            input_price = usd_to_microusd(
                entry.get("input_price_per_million_usd", "0"),
                f"{label}.input_price_per_million_usd",
            )
            output_price = usd_to_microusd(
                entry.get("output_price_per_million_usd", "0"),
                f"{label}.output_price_per_million_usd",
            )
        except ValueError as error:
            raise RuntimeError(str(error)) from error

        base_url: str | None = None
        api_key_file: Path | None = None
        if provider == "openai_compatible":
            raw_base_url = entry.get("base_url")
            if not isinstance(raw_base_url, str):
                raise RuntimeError(f"{label}.base_url must be a URL")
            parsed = urlsplit(raw_base_url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.query or parsed.fragment:
                raise RuntimeError(f"{label}.base_url must be an absolute URL without query")
            base_url = raw_base_url.rstrip("/")
            try:
                api_key_file = _model_config_path(entry.get("api_key_file"), f"{label}.api_key_file")
            except ValueError as error:
                raise RuntimeError(str(error)) from error

        max_output_tokens = entry.get("max_output_tokens")
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise RuntimeError(f"{label}.max_output_tokens must be a positive integer")
        chat_max_output_parameter = entry.get(
            "chat_max_output_parameter", "max_completion_tokens"
        )
        if chat_max_output_parameter not in {"max_completion_tokens", "max_tokens"}:
            raise RuntimeError(
                f"{label}.chat_max_output_parameter must be max_completion_tokens or max_tokens"
            )
        routes[public_id] = ModelRoute(
            public_id=public_id,
            provider=provider,
            endpoints=frozenset(endpoints),
            upstream_model=upstream_model,
            input_price_microusd_per_million=input_price,
            output_price_microusd_per_million=output_price,
            base_url=base_url,
            api_key_file=api_key_file,
            max_output_tokens=max_output_tokens,
            chat_max_output_parameter=chat_max_output_parameter,
        )
    return routes


CONFIG = read_config()
DB_PATH = PROJECT_DIR / CONFIG["EMBED_QUOTA_DB"]
BACKEND_URL = f"http://{CONFIG['EMBED_BACKEND_HOST']}:{CONFIG['EMBED_BACKEND_PORT']}"
BACKEND_KEY = (PROJECT_DIR / CONFIG["EMBED_BACKEND_API_KEY_FILE"]).read_text().strip()
BACKEND_TIMEOUT = int(CONFIG.get("EMBED_BACKEND_TIMEOUT_SECONDS", "300"))
MAX_BODY_BYTES = int(CONFIG.get("EMBED_MAX_BODY_BYTES", "10485760"))
MAX_INPUTS = int(CONFIG.get("EMBED_MAX_INPUTS_PER_REQUEST", "1024"))
DEFAULT_MAX_OUTPUT_TOKENS = int(CONFIG.get("GATEWAY_DEFAULT_MAX_OUTPUT_TOKENS", "4096"))
MODEL_CONFIG_PATH = PROJECT_DIR / CONFIG.get("GATEWAY_MODELS_FILE", "gateway_models.json")
MODEL_ROUTES = load_model_routes(MODEL_CONFIG_PATH)


def http_header_is_safe(name: str, value: str) -> bool:
    """Return whether BaseHTTPRequestHandler can safely serialize a header."""
    if not HTTP_HEADER_NAME.fullmatch(name):
        return False
    if any(character in value for character in ("\r", "\n", "\0")):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def format_usd(microusd: int | None) -> str:
    if microusd is None:
        return "unlimited"
    return f"{microusd / MICRO_USD_PER_USD:.6f}"


def quota_headers(snapshot: QuotaSnapshot) -> dict[str, str]:
    credit_limit = "disabled" if not snapshot.credit_enabled else format_usd(snapshot.credit_microusd)
    credit_used = "disabled" if not snapshot.credit_enabled else format_usd(snapshot.spent_microusd)
    credit_reserved = "disabled" if not snapshot.credit_enabled else format_usd(snapshot.reserved_microusd)
    credit_remaining = "disabled" if not snapshot.credit_enabled else format_usd(snapshot.remaining_microusd)
    return {
        "X-Token-Quota-Limit": "unlimited" if snapshot.quota_tokens is None else str(snapshot.quota_tokens),
        "X-Token-Quota-Used": str(snapshot.used_tokens),
        "X-Token-Quota-Reserved": str(snapshot.reserved_tokens),
        "X-Token-Quota-Remaining": "unlimited" if snapshot.remaining_tokens is None else str(snapshot.remaining_tokens),
        "X-Credit-Limit-USD": credit_limit,
        "X-Credit-Used-USD": credit_used,
        "X-Credit-Reserved-USD": credit_reserved,
        "X-Credit-Remaining-USD": credit_remaining,
    }


def backend_request(
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    timeout: int | None = None,
) -> tuple[int, bytes, str]:
    headers = {"Authorization": f"Bearer {BACKEND_KEY}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{BACKEND_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
            request, timeout=BACKEND_TIMEOUT if timeout is None else timeout
        ) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "application/json")


def upstream_request(
    route: ModelRoute, endpoint: str, body: bytes
) -> tuple[int, bytes, str]:
    assert route.base_url is not None and route.api_key_file is not None
    try:
        upstream_key = route.api_key_file.read_text().strip()
    except OSError as error:
        raise RuntimeError(f"upstream key file is unavailable for model '{route.public_id}'") from error
    if not upstream_key:
        raise RuntimeError(f"upstream key file is empty for model '{route.public_id}'")
    request = urllib.request.Request(
        f"{route.base_url}/{endpoint}",
        data=body,
        headers={
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=BACKEND_TIMEOUT) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "application/json")


def count_tokens(texts: list[str]) -> int:
    total = 0
    for text in texts:
        payload = json.dumps({"content": text, "add_special": True}, ensure_ascii=False).encode("utf-8")
        status, body, _ = backend_request("/tokenize", method="POST", body=payload)
        if status != HTTPStatus.OK:
            raise RuntimeError(f"tokenizer returned HTTP {status}")
        result = json.loads(body)
        tokens = result.get("tokens")
        if not isinstance(tokens, list):
            raise RuntimeError("tokenizer returned an invalid response")
        total += len(tokens)
    return total


def _ceil_div(dividend: int, divisor: int) -> int:
    return (dividend + divisor - 1) // divisor


def cost_microusd(route: ModelRoute, input_tokens: int, output_tokens: int) -> int:
    if min(input_tokens, output_tokens) < 0:
        raise ValueError("token counts cannot be negative")
    total = (
        input_tokens * route.input_price_microusd_per_million
        + output_tokens * route.output_price_microusd_per_million
    )
    return _ceil_div(total, 1_000_000)


def response_usage(payload: dict[str, Any]) -> tuple[int, int] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or input_tokens < 0
        or output_tokens < 0
    ):
        return None
    return input_tokens, output_tokens


def max_output_for_request(
    endpoint: str, route: ModelRoute, payload: dict[str, Any]
) -> tuple[int, str | None]:
    """Return requested maximum output and add a safe default when absent."""
    if endpoint == "embeddings":
        return 0, None
    parameter = "max_output_tokens" if endpoint == "responses" else route.chat_max_output_parameter
    candidate_names = ("max_output_tokens", "max_completion_tokens", "max_tokens")
    specified = [(name, payload[name]) for name in candidate_names if name in payload]
    if len(specified) > 1:
        raise ValueError("only one max output token parameter may be supplied")
    if specified:
        _, value = specified[0]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("max output tokens must be a non-negative integer")
        maximum = value
    else:
        maximum = route.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
        payload[parameter] = maximum
    configured_maximum = route.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    if maximum > configured_maximum:
        raise ValueError(f"max output tokens exceeds configured limit ({configured_maximum})")
    return maximum, parameter


class ModelGateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "ModelGateway/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}\n")

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
            use_gzip = accepts_gzip and len(body) >= 1024
            response_body = gzip.compress(body, compresslevel=6) if use_gzip else body

            self.send_response(status)
            if not http_header_is_safe("Content-Type", content_type):
                self.log_message("replacing unsafe backend Content-Type header")
                content_type = "application/octet-stream"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Cache-Control", "no-store")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            for key, value in (headers or {}).items():
                if not http_header_is_safe(key, value):
                    self.log_message("omitting unsafe response header %r", key)
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response_body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)

    def send_error_json(
        self,
        status: int,
        message: str,
        error_type: str,
        code: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_json(status, {"error": {"message": message, "type": error_type, "code": code}}, headers=headers)

    def bearer_key(self) -> str:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, value = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not value.strip():
            raise InvalidAPIKey()
        return value.strip()

    def authenticated_key(self) -> tuple[str, QuotaSnapshot]:
        api_key = self.bearer_key()
        return api_key, authenticate(DB_PATH, api_key)

    def read_json_body(self) -> tuple[bytes, dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if content_length <= 0:
            raise ValueError("request body is required")
        if content_length > MAX_BODY_BYTES:
            raise OverflowError("request body is too large")
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError("request body is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return body, payload

    def require_authentication(self) -> tuple[str, QuotaSnapshot] | None:
        try:
            return self.authenticated_key()
        except (InvalidAPIKey, DisabledAPIKey):
            self.send_error_json(
                HTTPStatus.UNAUTHORIZED,
                "Invalid or disabled API key",
                "authentication_error",
                "invalid_api_key",
            )
            return None

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            local_enabled = any(route.provider == "local_llama" for route in MODEL_ROUTES.values())
            try:
                status, _, _ = backend_request("/health", timeout=5) if local_enabled else (200, b"", "")
                healthy = status == HTTPStatus.OK
            except Exception:
                healthy = False
            self.send_json(
                HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "status": "ok" if healthy else "unavailable",
                    "gateway": "ok",
                    "local_backend": "ok" if healthy else "unavailable",
                    "models": len(MODEL_ROUTES),
                },
            )
            return

        authenticated = self.require_authentication()
        if authenticated is None:
            return
        _, snapshot = authenticated
        if path == "/v1/models":
            self.send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": route.public_id,
                            "object": "model",
                            "created": 0,
                            "owned_by": "model-gateway",
                            "endpoints": sorted(route.endpoints),
                        }
                        for route in MODEL_ROUTES.values()
                    ],
                },
                headers=quota_headers(snapshot),
            )
            return
        model_prefix = "/v1/models/"
        if path.startswith(model_prefix):
            model_id = path[len(model_prefix):]
            route = MODEL_ROUTES.get(model_id)
            if route is not None:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "id": route.public_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "model-gateway",
                        "endpoints": sorted(route.endpoints),
                    },
                    headers=quota_headers(snapshot),
                )
                return
            self.send_error_json(HTTPStatus.NOT_FOUND, "Model not found", "invalid_request_error", "model_not_found", quota_headers(snapshot))
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found", "invalid_request_error", "not_found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        endpoint = path.removeprefix("/v1/") if path.startswith("/v1/") else ""
        if endpoint not in SUPPORTED_ENDPOINTS:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Endpoint not found", "invalid_request_error", "not_found")
            return

        authenticated = self.require_authentication()
        if authenticated is None:
            return
        api_key, _ = authenticated
        try:
            body, payload = self.read_json_body()
            requested_model = payload.get("model")
            if not isinstance(requested_model, str) or not requested_model:
                raise LookupError("A configured model id is required")
            route = MODEL_ROUTES.get(requested_model)
            if route is None:
                raise LookupError(f"Unsupported model '{requested_model}'")
            if endpoint not in route.endpoints:
                raise LookupError(f"Model '{requested_model}' does not support /v1/{endpoint}")
        except OverflowError as error:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(error), "invalid_request_error", "request_too_large")
            return
        except LookupError as error:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(error), "invalid_request_error", "model_not_found")
            return
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error), "invalid_request_error", "invalid_request")
            return

        if route.provider == "local_llama":
            self.handle_local_embedding(api_key, route, payload)
        else:
            self.handle_upstream_model(api_key, route, endpoint, payload)

    def handle_local_embedding(self, api_key: str, route: ModelRoute, payload: dict[str, Any]) -> None:
        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            texts = [raw_input]
        elif isinstance(raw_input, list) and all(isinstance(item, str) for item in raw_input):
            texts = raw_input
        else:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "input must be a string or a list of strings", "invalid_request_error", "invalid_request")
            return
        if not texts:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "input cannot be empty", "invalid_request_error", "invalid_request")
            return
        if len(texts) > MAX_INPUTS:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"input contains more than {MAX_INPUTS} texts", "invalid_request_error", "invalid_request")
            return

        request_id = uuid.uuid4().hex
        started = time.monotonic()
        try:
            token_count = count_tokens(texts)
            snapshot = reserve_tokens(DB_PATH, api_key, request_id, len(texts), token_count)
        except QuotaExceeded as error:
            self.send_error_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"Token quota exceeded: requested {error.requested_tokens}, remaining {error.snapshot.remaining_tokens}",
                "quota_error",
                "insufficient_quota",
                quota_headers(error.snapshot),
            )
            return
        except (InvalidAPIKey, DisabledAPIKey):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid or disabled API key", "authentication_error", "invalid_api_key")
            return
        except Exception as error:
            self.log_error("tokenizer error: %r", error)
            self.send_error_json(HTTPStatus.BAD_GATEWAY, "Tokenizer unavailable", "backend_error", "tokenizer_unavailable")
            return

        try:
            backend_payload = dict(payload)
            backend_payload["model"] = route.upstream_model
            backend_body = json.dumps(backend_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            status, response_body, content_type = backend_request("/v1/embeddings", method="POST", body=backend_body)
            success = HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES
            if success:
                backend_response = json.loads(response_body)
                backend_response["model"] = route.public_id
                response_body = json.dumps(backend_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception as error:
            self.log_error("local embedding error for request %s: %r", request_id, error)
            self.finalize_local_error(request_id, snapshot, started, error)
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            updated = finalize_reservation(
                DB_PATH,
                request_id,
                success=success,
                http_status=status,
                latency_ms=latency_ms,
                error=None if success else f"backend HTTP {status}",
            )
        except Exception as error:
            self.log_error("token accounting finalization error for request %s: %r", request_id, error)
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Quota accounting temporarily unavailable", "server_error", "quota_accounting_unavailable")
            return
        self.send_bytes(status, response_body, content_type, headers=quota_headers(updated))

    def finalize_local_error(self, request_id: str, snapshot: QuotaSnapshot, started: float, error: Exception) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            updated = finalize_reservation(
                DB_PATH,
                request_id,
                success=False,
                http_status=HTTPStatus.BAD_GATEWAY,
                latency_ms=latency_ms,
                error=str(error)[:500],
            )
            headers = quota_headers(updated)
        except Exception:
            headers = quota_headers(snapshot)
        self.send_error_json(HTTPStatus.BAD_GATEWAY, "Local embedding backend unavailable", "backend_error", "backend_unavailable", headers)

    def handle_upstream_model(
        self, api_key: str, route: ModelRoute, endpoint: str, payload: dict[str, Any]
    ) -> None:
        if payload.get("stream") is True:
            self.send_error_json(
                HTTPStatus.BAD_REQUEST,
                "stream=true is not supported for billable gateway models; use stream=false so usage can be settled exactly",
                "invalid_request_error",
                "streaming_not_supported",
            )
            return
        upstream_payload = dict(payload)
        upstream_payload["model"] = route.upstream_model
        try:
            output_limit, _ = max_output_for_request(endpoint, route, upstream_payload)
        except ValueError as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error), "invalid_request_error", "invalid_request")
            return
        upstream_body = json.dumps(upstream_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # A UTF-8 byte is a conservative upper-bound token estimate for text.
        # The response cap is enforced above, making reservations safe under
        # concurrent calls without importing a provider tokenizer.
        estimated_input_tokens = len(upstream_body)
        estimated_charge = cost_microusd(route, estimated_input_tokens, output_limit)
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        try:
            snapshot = reserve_credit(
                DB_PATH,
                api_key,
                request_id,
                route.public_id,
                estimated_input_tokens,
                output_limit,
                estimated_charge,
            )
        except CreditNotEnabled as error:
            self.send_error_json(
                HTTPStatus.FORBIDDEN,
                "Dollar credit is not configured for this API key. Set a credit balance before using billable models.",
                "billing_error",
                "billing_not_configured",
                quota_headers(error.snapshot),
            )
            return
        except CreditExceeded as error:
            self.send_error_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"Credit balance exceeded: request may cost ${format_usd(error.requested_microusd)}, remaining ${format_usd(error.snapshot.remaining_microusd)}",
                "insufficient_quota",
                "insufficient_quota",
                quota_headers(error.snapshot),
            )
            return
        except (InvalidAPIKey, DisabledAPIKey):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid or disabled API key", "authentication_error", "invalid_api_key")
            return

        try:
            status, response_body, content_type = upstream_request(route, endpoint, upstream_body)
            success = HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES
        except Exception as error:
            self.log_error("upstream error for request %s: %r", request_id, error)
            self.finalize_credit_error(request_id, snapshot, started, error)
            return

        input_tokens = estimated_input_tokens
        output_tokens = output_limit
        actual_charge = estimated_charge
        if success:
            try:
                response_payload = json.loads(response_body)
                if isinstance(response_payload, dict):
                    usage = response_usage(response_payload)
                    if usage is not None:
                        input_tokens, output_tokens = usage
                        actual_charge = cost_microusd(route, input_tokens, output_tokens)
                    response_payload["model"] = route.public_id
                    response_body = json.dumps(response_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (ValueError, json.JSONDecodeError) as error:
                # A 2xx response without parsable usage is charged at the
                # pre-reserved ceiling, not silently given away.
                self.log_error("upstream response had no usable usage data for %s: %r", request_id, error)

        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            updated = finalize_credit_reservation(
                DB_PATH,
                request_id,
                success=success,
                http_status=status,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                charged_microusd=actual_charge,
                error=None if success else f"upstream HTTP {status}",
            )
        except Exception as error:
            self.log_error("credit finalization error for request %s: %r", request_id, error)
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Credit accounting temporarily unavailable", "server_error", "credit_accounting_unavailable")
            return

        headers = quota_headers(updated)
        headers["X-Model-Cost-USD"] = format_usd(actual_charge if success else 0)
        headers["X-Request-ID"] = request_id
        self.send_bytes(status, response_body, content_type, headers=headers)

    def finalize_credit_error(self, request_id: str, snapshot: QuotaSnapshot, started: float, error: Exception) -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            updated = finalize_credit_reservation(
                DB_PATH,
                request_id,
                success=False,
                http_status=HTTPStatus.BAD_GATEWAY,
                latency_ms=latency_ms,
                error=str(error)[:500],
            )
            headers = quota_headers(updated)
        except Exception:
            headers = quota_headers(snapshot)
        self.send_error_json(HTTPStatus.BAD_GATEWAY, "Configured upstream is unavailable", "backend_error", "backend_unavailable", headers)


def main() -> int:
    init_database(DB_PATH)
    recovered = recover_abandoned_reservations(DB_PATH)
    if recovered:
        print(f"Recovered {recovered} abandoned reservations", file=sys.stderr)
    address = (CONFIG["EMBED_HOST"], int(CONFIG["EMBED_PORT"]))
    server = ModelGateway(address, Handler)

    def shutdown(_signum: int, _frame: Any) -> None:
        import threading

        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Model Gateway listening on http://{address[0]}:{address[1]}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
