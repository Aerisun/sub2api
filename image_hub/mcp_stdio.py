#!/usr/bin/env python3
"""Portable stdio MCP relay. No dependencies; uses a site API key, never RightAPI's key."""
import json
import os
import secrets
from pathlib import Path
import sys
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError


def main():
    key = os.environ.get("SUB2API_API_KEY", "")
    local = Path(__file__).resolve().parents[1] / "deployment/private/config/image-hub-agent.env"
    if not key and local.exists():
        env = dict(line.split("=", 1) for line in local.read_text().splitlines() if "=" in line)
        key = env.get("SUB2API_API_KEY", "")
    base = os.environ.get("SUB2API_IMAGE_MCP_URL", "https://aerisun.top/sub2api/image-hub/mcp")
    if not key:
        sys.stderr.write("Set SUB2API_API_KEY to a site API key with gpt-image-2.5 access.\n")
        return 1
    opener = build_opener(ProxyHandler({}) if base.startswith("http://127.0.0.1") else ProxyHandler())
    session = secrets.token_hex(16)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
            if message.get("method") == "tools/call" and message.get("params", {}).get("name") == "generate_image":
                headers["Idempotency-Key"] = "mcp-" + session + "-" + str(message.get("id"))
            with opener.open(Request(base, data=json.dumps(message).encode(), headers=headers), timeout=65) as response:
                if response.status != 202:
                    print(response.read().decode(), flush=True)
        except (HTTPError, URLError, ValueError, TimeoutError):
            if isinstance(locals().get("message"), dict) and "id" in message:
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "Image hub request failed; verify your site API key and connectivity"}}), flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
