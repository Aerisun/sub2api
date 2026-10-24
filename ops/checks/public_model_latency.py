#!/usr/bin/env python3
"""Measure real public generations; retain failures and exclude SSE heartbeats."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import secrets
import time
from zoneinfo import ZoneInfo

import httpx

from ops import admin_api as api
from ops import admin_api as auth

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = "https://aerisun.top/sub2api"
TOOLS = {"image_generation", "image_recognition", "web_search"}


def chat(client, key, model, endpoint, round_number, target_ms):
    left, right = secrets.randbelow(80) + 11, secrets.randbelow(80) + 11
    expected = str(left + right)
    body = {"model": model, "stream": True, "max_tokens": 512,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content":
                          f"计算 {left}+{right}，只输出结果，不要解释。"}]}
    if model != "step-5-preview":
        body["reasoning_effort"] = "low"
    row = {"model": model, "kind": "chat", "endpoint": endpoint,
           "round": round_number, "ok": False, "first_semantic_ms": None,
           "first_text_ms": None, "first_reasoning_ms": None,
           "target_ms": target_ms, "text_deltas": 0, "reasoning_deltas": 0}
    start = time.monotonic()
    output, last_delta, gaps = [], None, []
    try:
        with client.stream("POST", PUBLIC + endpoint, json=body,
                           headers={"Authorization": "Bearer " + key,
                                    "X-Client-Request-Id": "capacity-public-" + secrets.token_hex(8)}) as response:
            row.update(status=response.status_code, headers_ms=round((time.monotonic()-start)*1000))
            if response.status_code != 200:
                response.read()
                try:
                    error = response.json().get("error", {})
                    row["error_type"] = error.get("type") or error.get("code")
                    row["error_message"] = error.get("message", "")[:300]
                except (ValueError, AttributeError):
                    row["error_type"] = "non_json_http_error"
            else:
                for line in response.iter_lines():
                    if time.monotonic() - start > 90:
                        row["error_type"] = "generation_deadline"
                        break
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        row["done"] = True
                        break
                    value = json.loads(raw)
                    if value.get("error"):
                        row["error_type"] = value["error"].get("type", "stream_error")
                    elapsed = round((time.monotonic()-start)*1000)
                    for choice in value.get("choices", []):
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        thought = delta.get("reasoning_content") or delta.get("reasoning")
                        if text or thought:
                            if row["first_semantic_ms"] is None:
                                row["first_semantic_ms"] = elapsed
                            if last_delta is not None:
                                gaps.append(elapsed-last_delta)
                            last_delta = elapsed
                        for field, content in (("text", text), ("reasoning", thought)):
                            if content:
                                row[field+"_deltas"] += 1
                                if row["first_"+field+"_ms"] is None:
                                    row["first_"+field+"_ms"] = elapsed
                        if text:
                            output.append(text)
                        if choice.get("finish_reason"):
                            row["finish_reason"] = choice["finish_reason"]
                    if value.get("usage"):
                        row["usage"] = value["usage"]
        answer = "".join(output).strip().strip("。.! \n`")
        row["expected_answer"] = expected
        row["answer"] = answer
        row["answer_correct"] = re.findall(r"\d+", answer)[-1:] == [expected]
        row["response_characters"] = len("".join(output))
        row["ok"] = (row["answer_correct"] and row.get("done", False)
                     and row.get("finish_reason") == "stop" and not row.get("error_type")
                     and bool(row.get("usage")))
    except Exception as error:
        row["error_type"] = type(error).__name__
    row["total_ms"] = round((time.monotonic()-start)*1000)
    row["largest_delta_gap_ms"] = max(gaps, default=0)
    row["latency_passed"] = row["ok"] and row["first_semantic_ms"] <= target_ms
    row["text_latency_passed"] = row["ok"] and row["first_text_ms"] is not None and row["first_text_ms"] <= target_ms
    return row


def inference(client, key, model):
    nonce = secrets.token_hex(12)
    embedding = "embedding" in model
    endpoint = "/embeddings" if embedding else "/rerank"
    body = ({"model": model, "input": nonce+"杭州西湖有哪些景点？"} if embedding else
            {"model": model, "query": nonce+"杭州西湖有哪些景点？",
             "documents": ["北京故宫是明清皇宫。", "杭州西湖有断桥和雷峰塔。"], "top_n": 2})
    row = {"model": model, "kind": "embedding" if embedding else "rerank", "ok": False}
    start = time.monotonic()
    try:
        response = client.post(PUBLIC+endpoint, json=body, headers={"Authorization": "Bearer "+key})
        row.update(status=response.status_code, cache_status=response.headers.get("Cache-Status"))
        value = response.json()
        row["usage"] = value.get("usage")
        if response.status_code == 200:
            if embedding:
                vector = value["data"][0]["embedding"]
                row["dimensions"] = len(vector)
                valid = len(vector) == 2560 and all(math.isfinite(x) for x in vector)
                valid = valid and abs(sum(x*x for x in vector)-1) < .001
            else:
                valid = value["results"][0]["index"] == 1
            row["ok"] = valid and value["usage"]["prompt_tokens"] > 0 and "; hit" not in (row["cache_status"] or "")
        else:
            row["error_type"] = value.get("error", {}).get("type")
    except Exception as error:
        row["error_type"] = type(error).__name__
    row["total_ms"] = round((time.monotonic()-start)*1000)
    return row


def tool(client, key, name):
    if name == "web_search":
        body = {"query": "NVIDIA NIM API documentation", "domains": ["docs.api.nvidia.com"],
                "max_results": 2, "response_length": "short"}
    else:
        image = base64.b64encode((ROOT/"nvidia_pool/fixtures/vision-health.png").read_bytes()).decode()
        body = {"image": "data:image/png;base64,"+image,
                "prompt": "Read the printed number and name the color and shape of each object. Answer briefly in English only."}
    row = {"model": name, "kind": "tool", "ok": False}
    start = time.monotonic()
    try:
        response = client.post(PUBLIC+"/tools/"+name, json=body, headers={"Authorization": "Bearer "+key})
        row["status"] = response.status_code
        value = response.json()
        if response.status_code == 200:
            row["usage"] = value.get("usage")
            if name == "web_search":
                row["sources"] = len(value.get("output", {}).get("sources", []))
                row["ok"] = row["sources"] > 0 and value.get("cached") is False
            else:
                row['output'] = value.get('output', {}).get('text', '')
                rendered = json.dumps(value, ensure_ascii=False).lower()
                row["fixture_passed"] = all(s in rendered for s in ("657952", "red", "triangle", "blue", "circle", "green", "square"))
                row["ok"] = row["fixture_passed"]
        else:
            row["error_type"] = value.get("error", {}).get("code")
            row["error_message"] = value.get("error", {}).get("message", "")[:300]
    except Exception as error:
        row["error_type"] = type(error).__name__
    row["total_ms"] = round((time.monotonic()-start)*1000)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--rounds", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--target-ms", type=int, default=5000)
    parser.add_argument("--tools", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Use a new output path to preserve previous evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "public_url": PUBLIC,
              "target_first_semantic_ms": args.target_ms, "concurrency": 3, "checks": [],
              "cloud_load_test": False, "image_generation_calls": 0}
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        args.output.chmod(0o600)
    def record(row):
        report["checks"].append(row)
        save()
        print(json.dumps({k:row.get(k) for k in ("model", "kind", "round", "ok", "status",
              "first_semantic_ms", "first_text_ms", "total_ms", "error_type")}, ensure_ascii=False), flush=True)
    admin = auth.login()
    password = secrets.token_urlsafe(32)
    user = api.request("/api/v1/admin/users", admin, {"email": "public-latency-"+secrets.token_hex(8)+"@example.invalid",
        "password": password, "role": "user", "balance": 2, "concurrency": 3,
        "allowed_groups": [9], "restrict_public_groups": True}, "POST")
    key = token = None
    try:
        token = api.request("/api/v1/auth/login", payload={"email":user["email"], "password":password})["access_token"]
        key = api.request("/api/v1/keys", token, {"name":"temporary-public-model-latency", "group_id":9,
                                               "quota":.5, "expires_in_days":1}, "POST")
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(45, connect=10),
                          limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client:
            report["catalog"] = {}
            for endpoint in ("/models", "/v1/models"):
                response = client.get(PUBLIC+endpoint, headers={"Authorization":"Bearer "+key["key"]})
                response.raise_for_status()
                report["catalog"][endpoint] = sorted(r["id"] for r in response.json()["data"])
            assert report["catalog"]["/models"] == report["catalog"]["/v1/models"]
            models = args.models or [m for m in report["catalog"]["/models"] if m not in TOOLS]
            assert set(models) <= set(report["catalog"]["/models"]), "Requested model absent from public catalog"
            save()
            for round_number in range(1, args.rounds+1):
                chat_models = [m for m in models if "embedding" not in m and "rerank" not in m]
                endpoint = "/chat/completions" if round_number % 2 else "/v1/chat/completions"
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futures = [pool.submit(chat, client, key["key"], m, endpoint, round_number, args.target_ms) for m in chat_models]
                    for future in as_completed(futures):
                        record(future.result())
                for model in models:
                    if "embedding" in model or "rerank" in model:
                        record(inference(client, key["key"], model))
            if args.tools:
                for name in ("web_search", "image_recognition"):
                    record(tool(client, key["key"], name))
        records = api.request(f"/api/v1/admin/usage?api_key_id={key['id']}&page=1&page_size=100", admin)["items"]
        report["usage"] = [{k:r.get(k) for k in ("model", "upstream_model", "account_id", "input_tokens",
            "output_tokens", "actual_cost", "first_token_ms", "duration_ms")} for r in records]
        report["cost_usd"] = str(sum((Decimal(str(r["actual_cost"])) for r in records), Decimal(0)))
        report["image_generation_calls"] = sum(str(row.get('model', '')).startswith('gpt-image-') for row in records)
        report["unexpected_image_generation"] = report["image_generation_calls"] > 0
        report["all_calls_passed"] = all(r["ok"] for r in report["checks"]) and not report["unexpected_image_generation"]
        report["all_chat_latency_passed"] = all(r["latency_passed"] for r in report["checks"] if r["kind"] == "chat")
        report["all_chat_text_latency_passed"] = all(r["text_latency_passed"] for r in report["checks"] if r["kind"] == "chat")
    except Exception as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        try:
            if key:
                api.request("/api/v1/keys/"+str(key["id"]), token, method="DELETE")
        finally:
            api.request("/api/v1/admin/users/"+str(user["id"]), admin, method="DELETE")
        report["temporary_credentials_removed"] = True
        save()
    if not report.get("all_calls_passed") or not report.get("all_chat_latency_passed") or not report.get("all_chat_text_latency_passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
