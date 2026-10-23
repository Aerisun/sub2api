#!/usr/bin/env python3
"""Portable MCP stdio adapter. Only a caller's site key leaves this process."""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, target):
        original, new = urlsplit(request.full_url), urlsplit(target)
        if (original.scheme, original.netloc) != (new.scheme, new.netloc):
            raise URLError("Unexpected tool endpoint redirect")
        return super().redirect_request(request, fp, code, message, headers, target)


def main():
    key = os.environ.get("SUB2API_API_KEY", "")
    base = os.environ.get("SUB2API_TOOLS_MCP_URL", "https://aerisun.top/sub2api/v1/tools/mcp")
    if not key:
        sys.stderr.write("Set SUB2API_API_KEY to your site API key with tool access.\n")
        return 1
    opener = build_opener(ProxyHandler({}) if base.startswith("http://127.0.0.1") else ProxyHandler(), SameOriginRedirect())
    output_lock = threading.Lock()
    slots = threading.BoundedSemaphore(64)

    def emit(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def recognition(message):
        params=message.get('params')
        return (message.get('method')=='tools/call' and isinstance(params,dict)
                and params.get('name')=='image_recognition')

    def forward(message):
        try:
            request = Request(base, data=json.dumps(message).encode(), headers={
                "Authorization": "Bearer "+key, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
            with opener.open(request, timeout=190 if recognition(message) else 70) as response:
                if response.status != 202:
                    raw = response.read(4*1024*1024+1)
                    if len(raw) > 4*1024*1024:
                        raise ValueError("Oversized MCP response")
                    result = json.loads(raw)
                    if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != message.get("id"):
                        raise ValueError("Invalid MCP response")
                    emit(result)
        except (HTTPError, URLError, ValueError, TimeoutError, OSError):
            if isinstance(message, dict) and "id" in message:
                emit({"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32000, "message": "Internal tool request failed; verify your site key and connectivity"}})

    def concurrent_forward(message):
        try:forward(message)
        finally:slots.release()

    # MCP identifies responses by id and permits out-of-order completion.
    # Keep handshake/other methods synchronous, and bound image submissions
    # across both running workers and pending stdin messages. Never retry POSTs.
    with ThreadPoolExecutor(max_workers=64) as executor:
        for line in sys.stdin:
            message=None
            try:
                message=json.loads(line)
                if len(line.encode())>24*1024*1024 or not isinstance(message,dict) or message.get('jsonrpc')!='2.0':
                    raise ValueError('Invalid MCP request')
            except ValueError:
                if isinstance(message,dict) and 'id' in message:
                    emit({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32600,'message':'Invalid MCP request'}})
                continue
            if recognition(message):
                slots.acquire()
                try:executor.submit(concurrent_forward,message)
                except BaseException:slots.release();raise
            else:
                forward(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
