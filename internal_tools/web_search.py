"""Live Codex native search through the regular scoped, billed gateway.

Every invocation makes its own native POST. There is deliberately no result
cache, stale fallback, in-flight deduplication or automatic billable retry.
"""
from datetime import datetime, timezone
import json
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from image_hub.server import HubError, request_json
from internal_tools.errors import ToolError, error_details
from internal_tools.search_contract import (NAME, FUNCTION_NAME, DESCRIPTION, QUERY_SCHEMA,
                                           SCHEMA, DEFINITION, normalize_input, public_url)

MODEL = os.environ.get("AERISUN_WEB_SEARCH_MODEL", "gpt-6-luna")
SEARCH_MODELS = tuple(dict.fromkeys((MODEL, "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra")))
SUMMARY_BYTES = 256*1024
SOURCE_CONTENT_BYTES = 128*1024
CITATION = re.compile(r"\ue200(?:cite|filecite)\ue202.*?\ue201", re.S)
SECTION = re.compile(r"(?m)^([^\n]{1,1000}) \((https?://[^\s]+)\)\n(\ue200cite\ue202([^\ue201]+)\ue201[^\n]*)\n?")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clip(value, limit):
    raw = value.encode("utf-8")
    return (raw[:limit].decode("utf-8", errors="ignore"), True) if len(raw) > limit else (value, False)


def sections(text):
    matches = list(SECTION.finditer(text))
    result = []
    for index, match in enumerate(matches):
        metadata = match[3]
        item = {"title": match[1], "url": match[2], "ref_id": match[4].split("")[-1],
                "content": text[match.end():matches[index+1].start() if index+1 < len(matches) else len(text)].strip()}
        for label in ("Published", "Crawled"):
            value = re.search(r"(?:^|[;\s])"+label+r":\s*([^;\n]+)", metadata)
            if value:
                item[label.lower()] = value[1].strip()[:100]
        result.append(item)
    return result


def normalize_output(value, arguments):
    if not isinstance(value, dict) or not (isinstance(value.get("output"), str) or isinstance(value.get("results"), (list, dict))):
        raise ToolError(502, "搜索服务未返回有效结果", "malformed_upstream_response", retryable=True)
    raw_text = value.get("output") if isinstance(value.get("output"), str) else ""
    scan_text, scan_cut = clip(raw_text, 2*1024*1024)
    extracted = sections(scan_text)
    by_url = {item["url"]: item for item in extracted}
    raw_results = value.get("results") or []
    if isinstance(raw_results, dict):
        raw_results = [{**item, "ref_id": item.get("ref_id", key)} for key, item in raw_results.items() if isinstance(item, dict)]
    if not isinstance(raw_results, list):
        raw_results = []
    sources, seen, filtered = [], {}, 0
    for raw in raw_results[:1000] + extracted:
        if not isinstance(raw, dict):
            filtered += 1
            continue
        url = public_url(raw.get("url"))
        if not url:
            filtered += 1
            continue
        extra = by_url.get(url, {})
        ref = str(raw.get("ref_id") or raw.get("id") or extra.get("ref_id") or f"source-{len(sources)+1}")[:120]
        content = next((item for item in (raw.get("content"), raw.get("text"), extra.get("content"), raw.get("snippet")) if isinstance(item, str) and item), "")
        content = CITATION.sub("", content).strip()
        snippet = raw.get("snippet") if isinstance(raw.get("snippet"), str) else content
        source = {"id": ref, "reference_ids": [ref], "title": str(raw.get("title") or extra.get("title") or urlsplit(url).hostname)[:500],
                  "url": url, "domain": urlsplit(url).hostname, "snippet": CITATION.sub("", snippet)[:2000], "content": content}
        for key in ("type", "published_at", "published", "crawled"):
            field = raw.get(key) or extra.get(key)
            if isinstance(field, str):
                source[key] = field[:100]
        previous = seen.get(url)
        if previous is not None:
            if ref not in previous["reference_ids"] and len(previous["reference_ids"]) < 50:
                previous["reference_ids"].append(ref)
            for key in ("content", "snippet"):
                if len(source[key]) > len(previous[key]):
                    previous[key] = source[key]
            for key in ("published_at", "published", "crawled", "type"):
                if key not in previous and key in source:
                    previous[key] = source[key]
            continue
        seen[url] = source
        sources.append(source)
    total = len(sources)
    sources = sources[:arguments["max_results"]]
    warnings, truncated_fields, clipped_sources = [], [], []
    remaining = SOURCE_CONTENT_BYTES
    for source in sources:
        source["content"], cut = clip(source["content"], min(32*1024, remaining))
        remaining -= len(source["content"].encode())
        if cut:
            source["content_truncated"] = True
            clipped_sources.append(source["id"])
    if not raw_text and sources:
        raw_text = "\n\n".join(source["title"]+" ("+source["url"]+")\n"+source["content"] for source in sources)
        warnings.append("上游未提供合并正文，已由真实来源记录整理检索内容。")
    text, summary_cut = clip(CITATION.sub("", raw_text).strip(), SUMMARY_BYTES)
    if summary_cut or scan_cut:
        truncated_fields.append("summary")
    if len(sources) < total:
        truncated_fields.append("sources")
    if clipped_sources:
        truncated_fields.append("source_content")
    if len(raw_results) > 1000:
        truncated_fields.append("upstream_results")
    if not text and not sources:
        warnings.append("本次实时搜索未返回可用网页；请尝试更具体的搜索词或放宽筛选条件。")
    metadata = {"query_count": len(arguments["queries"]), "total_sources": total, "returned_sources": len(sources),
                "received_results": len(raw_results), "filtered_sources": filtered, "response_length": arguments["response_length"],
                "summary_bytes": len(text.encode()), "original_summary_bytes": len(raw_text.encode()),
                "truncated": bool(truncated_fields), "truncated_fields": truncated_fields,
                "content_truncated_source_ids": clipped_sources, "warnings": warnings}
    return {"summary": text, "sources": sources, "metadata": metadata}


def authorized_models(credential, gateway, headers=None):
    if not isinstance(credential, str) or not credential or len(credential) > 8192 or not credential.isascii() or re.search(r"[\x00-\x20\x7f]", credential):
        raise ToolError(401, "请提供有效的站点 API Key 或登录凭据", "authentication_error",
                        suggestion="使用 Authorization: Bearer <站点 API Key>，不要填模型名或带中文的占位文字。")
    path = "/v1/models" if credential.startswith("sk-") else "/api/v1/chat/models"
    # A cold catalogue or brief loopback interruption can fail a read without
    # charging anything. Retry only this GET, never the billable search POST.
    for attempt in range(2):
        try:
            _, response = request_json(gateway+path, credential, timeout=3, headers=headers)
            break
        except HubError as error:
            if attempt or error.status not in (502, 503, 504):
                raise
            time.sleep(.08)
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ToolError(502, "暂时无法确认网页搜索权限", "permission_check_failed", retryable=True)
    available = {item["id"] for item in response["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return available


def authorize_search(credential, gateway, headers=None):
    available = authorized_models(credential, gateway, headers)
    model = search_model(available)
    if model is None:
        raise ToolError(403, "当前凭据未开通网页搜索权限", "permission_denied",
                        suggestion="请检查当前分组和 API Key 的模型权限，需允许站点提供的 Codex 搜索模型。")
    return model


def search_model(available):
    if NAME in available:
        return MODEL
    return next((model for model in SEARCH_MODELS if model in available), None)


class WebSearchExecutor:
    def __init__(self, credential, gateway="http://127.0.0.1:8080", headers=None):
        self.credential, self.gateway, self.headers = credential, gateway, headers or {}
        self.calls = 0

    def __call__(self, arguments, call_id):
        params = normalize_input(arguments)
        if self.calls >= 3:
            raise ToolError(429, "本轮网页搜索已达到次数上限，请使用已有来源回答", "search_limit_exceeded")
        model = authorize_search(self.credential, self.gateway, self.headers)
        self.calls += 1
        queries = [{"q": q["query"], **({"domains": q["domains"]} if q.get("domains") else {}),
                    **({"recency": q["recency_days"]} if q.get("recency_days") else {})} for q in params["queries"]]
        body = {"model": model, "id": "search-"+secrets.token_hex(16),
                "commands": {"search_query": queries, "response_length": params["response_length"]},
                "settings": {"external_web_access": True, "search_context_size": "high" if params["response_length"] == "long" else "medium"}}
        path = "/v1/alpha/search" if self.credential.startswith("sk-") else "/api/v1/chat/alpha/search"
        started, stamp = time.monotonic(), now()
        # Never retry this billable POST, even if its response is lost.
        _, value = request_json(self.gateway+path, self.credential, body, timeout=60,
                                headers={**self.headers, "Cache-Control": "no-cache", "Pragma": "no-cache"})
        output = normalize_output(value, params)
        output["metadata"].update(mode="live", searched_at=stamp, completed_at=now(), duration_ms=round((time.monotonic()-started)*1000))
        return {"id": call_id, "name": NAME, "status": "completed", "input": params,
                "mode": "live", "searched_at": stamp, "output": output, "cached": False,
                "cache_status": "disabled", "usage": {"search_calls": 1}}


def tool_event(call_id, params, result=None, error=None):
    event = {"type": "tool_call", "id": call_id, "name": NAME,
             "status": "failed" if error else "completed" if result else "running", "input": params}
    if result:
        event.update(output=result["output"]["summary"], sources=result["output"]["sources"], cached=False,
                     metadata=result["output"].get("metadata", {}))
    if error:
        event["error"] = error.message if isinstance(error, HubError) else str(error)
        if isinstance(error, HubError):
            event["metadata"] = {"error": error_details(error)}
    return event
