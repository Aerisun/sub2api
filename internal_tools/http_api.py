"""Stateless JSON/MCP adapters for the same internal tool implementation."""
import json
import re
import secrets
from urllib.parse import urlsplit

from image_hub.server import HubError
from internal_tools.errors import ToolError, error_details, invalid
from internal_tools import registry
from internal_tools.streaming_http import ToolHTTPStream


def matches(path):
    return bool(re.fullmatch(r"/(?:v1/)?tools(?:/(?:mcp|call|web_search|image_generation|image_recognition))?", urlsplit(path).path))


def credential(handler):
    authorization = handler.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else handler.headers.get("x-api-key", "")


def reply(handler, status, value=None, headers=None):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() if value is not None else b""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    if raw:
        handler.wfile.write(raw)


def invalid_json(handler):
    if urlsplit(handler.path).path.endswith("/mcp"):
        return reply(handler, 200, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error: request body must be valid JSON"}})
    return reply(handler, 400, {"error": error_details(invalid("请求体必须是有效的 JSON"))})


def client_headers(handler):
    return {name: handler.headers[name] for name in ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP") if handler.headers.get(name)}


def dispatch(handler, gateway, body=None):
    path = urlsplit(handler.path).path
    key = credential(handler)
    mcp = path.endswith("/mcp")
    stream = None
    try:
        headers = client_headers(handler)
        # Executors check current authentication/scope, then the billed gateway
        # checks again. REST calls need no second catalogue lookup before that.
        definitions = registry.catalogue(key, gateway, headers) if handler.command == "GET" or mcp else None
        if handler.command == "GET":
            return reply(handler, 405, {"error": {"message": "MCP uses POST"}}, {"Allow": "POST"}) if mcp else reply(handler, 200, {"object": "list", "data": definitions})
        if mcp and (not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) or
                    ("id" in body and (type(body["id"]) not in (str, int)))):
            return reply(handler, 200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
        if path.endswith("/call") and not isinstance(body, dict):
            raise invalid("工具请求必须是 JSON 对象")
        if mcp:
            method = body.get("method")
            if "id" not in body:
                return reply(handler, 202)
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "aerisun-internal-tools", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": definitions}
            elif method == "tools/call":
                params = body.get("params") or {}
                if not isinstance(params, dict) or params.get("name") not in registry.NAMES:
                    return reply(handler, 200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "Unknown tool or invalid parameters"}})
                value = registry.execute(params["name"],params.get("arguments"), "tool_"+secrets.token_hex(12),key,gateway,headers,handler.headers.get("Idempotency-Key"))
                result = {"content": [{"type": "text", "text": json.dumps(value["output"], ensure_ascii=False)}],
                          "structuredContent": value, "isError": value.get("status") in ("failed","interrupted")}
            else:
                return reply(handler, 200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not found"}})
            return reply(handler, 200, {"jsonrpc": "2.0", "id": body["id"], "result": result})
        if path.endswith("/call"):
            if body.get("name") not in registry.NAMES:
                raise ToolError(400, "未知的内部工具；支持 image_generation、image_recognition、web_search", "unknown_tool", param="name")
            name = body["name"]
            arguments = body.get("arguments")
        else:
            name = path.rsplit("/",1)[-1]
            if name not in registry.NAMES:
                raise invalid("请使用 /v1/tools/call 或具体的工具调用地址")
            arguments = body
        if name == "image_recognition" and isinstance(arguments, dict) and (
                arguments.get("stream") is True or "text/event-stream" in handler.headers.get("Accept", "")):
            stream = ToolHTTPStream(handler)
            value = registry.execute(name,arguments,"tool_"+secrets.token_hex(12),key,gateway,headers,
                                     handler.headers.get("Idempotency-Key"),progress=stream.progress)
            stream.emit("result",value)
            return
        value = registry.execute(name,arguments,"tool_"+secrets.token_hex(12),key,gateway,headers,handler.headers.get("Idempotency-Key"))
        return reply(handler, 202 if value.get("status")=="processing" else 200, value,
                     {"Cache-Status": "AerisunSearch; fwd=bypass"} if name=="web_search" else None)
    except HubError as error:
        if stream and stream.started:
            stream.emit("error",{"error":{"type":"tool_error",**error_details(error)}})
            return
        if mcp and isinstance(body, dict) and body.get("jsonrpc") == "2.0" and "id" in body and error.status not in (401, 403):
            return reply(handler, 200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                "isError": True, "content": [{"type": "text", "text": json.dumps(error_details(error), ensure_ascii=False)}],
                "structuredContent": {"name": (body.get("params") or {}).get("name","unknown") if isinstance(body.get("params"),dict) else "unknown", "status": "failed", "error": error_details(error)}}})
        return reply(handler, error.status, {"error": {"type": "tool_error", **error_details(error)}},
                     {"WWW-Authenticate": "Bearer"} if error.status == 401 else {"Retry-After": "1"} if error.status == 429 else None)
    finally:
        if stream:stream.close()
