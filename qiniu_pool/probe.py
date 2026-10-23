"""Small, auditable provider checks; never print credentials or request headers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.qnaigc.com"
SELECTED = {
    "deepseek-v4.1-flash": "deepseek/deepseek-v4.1-flash-1017",
    "qwen3.8-flash": "qwen/qwen3.8-flash-next",
    "qwen3.8-max": "qwen/qwen3.8-max",
    "kimi-k3": "moonshotai/kimi-k3",
}
# Internal physical targets are deliberately absent from the public catalog.
INTERNAL_MODELS = {"moonshotai/kimi-k3-free": "moonshotai/kimi-k3-free"}
ACCOUNT_MODELS = {**SELECTED, **INTERNAL_MODELS}
RETIRED_PUBLIC_MODELS = {"kimi-k3-free"}


def sanitized(value):
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[redacted]", str(value))[:500]


def check(client, key, label, model, body=None, base=BASE):
    body = body or {
        "model": model, "stream": True, "stream_options": {"include_usage": True},
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
    }
    started = time.monotonic()
    result = {"key_label": label, "model": model, "ok": False}
    text, reasoning, done, finished = "", 0, False, False
    calls = {}
    try:
        with client.stream("POST", base + "/v1/chat/completions",
                           headers={"Authorization": "Bearer " + key}, json=body) as response:
            result["http_status"] = response.status_code
            result["headers_ms"] = round((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                result["error"] = sanitized(response.read().decode(errors="replace"))
            elif not body.get("stream"):
                response.read()
                data = response.json()
                result["response_model"] = data.get("model")
                result["usage"] = data.get("usage")
                message = data.get("choices", [{}])[0].get("message", {})
                text = message.get("content") or ""
                calls = {i: c for i, c in enumerate(message.get("tool_calls") or [])}
                done = finished = True
            else:
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    if not data:
                        continue
                    event = json.loads(data)
                    if event.get("model"):
                        result["response_model"] = event["model"]
                    if event.get("error"):
                        result["error"] = sanitized(event["error"])
                    if event.get("usage"):
                        result["usage"] = event["usage"]
                    for choice in event.get("choices", []):
                        delta = choice.get("delta") or {}
                        part = delta.get("content") or ""
                        reasoning += len(delta.get("reasoning_content") or "")
                        if delta.get("reasoning_content"):
                            stamp = round((time.monotonic()-started)*1000)
                            result.setdefault("first_reasoning_ms", stamp)
                            result["last_reasoning_ms"] = stamp
                            result["reasoning_delta_count"] = result.get("reasoning_delta_count", 0)+1
                        if part and "first_text_ms" not in result:
                            result["first_text_ms"] = round((time.monotonic() - started) * 1000)
                        text += part
                        finished |= bool(choice.get("finish_reason"))
                        for call in delta.get("tool_calls") or []:
                            target = calls.setdefault(call.get("index", 0), {"function": {"name": "", "arguments": ""}})
                            if call.get("id"):
                                target["id"] = call["id"]
                            for field in ("name", "arguments"):
                                target["function"][field] += call.get("function", {}).get(field) or ""
        result.update(reply=text[:300], reasoning_chars=reasoning, stream_done=done,
                      finish_received=finished, tool_calls=list(calls.values()))
        result["ok"] = (done and finished and not result.get("error") and
                        (bool(calls) if body.get("tools") else text.strip().upper().rstrip(".!") == "OK"))
    except Exception as error:
        result["error"] = sanitized(error)
    result["total_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--output", default="var/reports/qiniu-key-model-preflight.json")
    args = parser.parse_args()
    keys = json.loads((ROOT / "secrets/qiniu/api_keys.json").read_text())["keys"]
    report = {"checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "checks": []}
    output = ROOT / args.output
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(25, connect=4),
                      limits=httpx.Limits(max_connections=12, max_keepalive_connections=12)) as client:
        for model in args.models or dict.fromkeys(ACCOUNT_MODELS.values()):
            tasks = [(key, f"K{i+1:02d}", model) for i, key in enumerate(keys)]
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                rows = list(pool.map(lambda args: check(client, *args), tasks))
            report["checks"].extend(rows)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            output.chmod(0o600)
            print(json.dumps({"model": model, "successful": sum(r["ok"] for r in rows),
                              "total": len(rows), "max_ms": max(r["total_ms"] for r in rows),
                              "failures": [r for r in rows if not r["ok"]]}, ensure_ascii=False), flush=True)
            if any(r.get("http_status") in (401, 402, 403, 429) for r in rows):
                print("Stopped provider preflight after an authentication, quota or rate-limit response.", flush=True)
                break


if __name__ == "__main__":
    main()
