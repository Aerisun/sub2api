"""Public search schema and tolerant, bounded normalization."""
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit

from internal_tools.errors import invalid

NAME = "web_search"
FUNCTION_NAME = "aerisun_web_search"
DESCRIPTION = ("Search the public web live on every call, with no result cache or request coalescing. "
               "Returns source extracts, real URLs and retrieval timestamps. Use query for one search, "
               "or queries for up to four related searches. Optional domains and recency_days filter results. "
               "The server executes and displays this tool; do not ask the user to run it.")
QUERY_SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "Search terms or a natural language question."},
    "domains": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "description": "Optional hostnames, e.g. example.com."},
    "recency_days": {"type": "integer", "minimum": 1, "maximum": 3650, "description": "Optional publication recency filter in days."}},
    "required": ["query"], "additionalProperties": False}
SCHEMA = {"type": "object", "properties": {
    "query": QUERY_SCHEMA["properties"]["query"],
    "queries": {"type": "array", "items": QUERY_SCHEMA, "minItems": 1, "maxItems": 4,
                "description": "Use instead of query to search up to four related questions together."},
    "domains": QUERY_SCHEMA["properties"]["domains"],
    "recency_days": QUERY_SCHEMA["properties"]["recency_days"],
    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20,
                    "description": "Maximum returned sources, not a guarantee of available results."},
    "response_length": {"type": "string", "enum": ["short", "medium", "long"], "default": "long",
                        "description": "Amount of source context requested. long is the default."}},
    "additionalProperties": False}
DEFINITION = {"name": NAME, "description": DESCRIPTION, "inputSchema": SCHEMA,
              "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}}


def aliases(value, names):
    value = dict(value)
    for old, new in names.items():
        if old in value:
            if new in value:
                raise invalid(f"{old} 和 {new} 请只提供一个", new)
            value[new] = value.pop(old)
    return value


def integer(value, lower, upper, param):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value.strip()):
        value = int(value)
    if type(value) is not int or not lower <= value <= upper:
        raise invalid(f"{param} 必须是 {lower} 到 {upper} 的整数", param)
    return value


def public_url(value):
    if not isinstance(value, str) or len(value) > 4000 or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host) or "." not in host:
                return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return None


def domain_list(value, param):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 10:
        raise invalid("最多指定 10 个域名", param)
    result = []
    for domain in value:
        if not isinstance(domain, str):
            raise invalid("域名必须是字符串，例如 example.com", param)
        domain = domain.strip().lower().rstrip(".")
        if "://" in domain:
            link = public_url(domain)
            domain = urlsplit(link).hostname if link else ""
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            domain = ""
        if len(domain) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain) or not public_url("https://"+domain):
            raise invalid("请输入有效的公开域名，例如 example.com", param)
        if domain not in result:
            result.append(domain)
    return result


def normalize_input(arguments):
    if isinstance(arguments, str):
        text = arguments.strip()
        if text.startswith(("{", "[", '"')):
            try:
                arguments = json.loads(text)
            except ValueError:
                raise invalid("搜索参数的 JSON 格式无效") from None
        else:
            arguments = {"query": text}
        if isinstance(arguments, str):
            arguments = {"query": arguments}
    if not isinstance(arguments, dict):
        raise invalid("搜索参数必须是 JSON 对象")
    arguments = aliases(arguments, {"q": "query", "search_query": "queries", "recency": "recency_days"})
    extra = set(arguments) - set(SCHEMA["properties"]) - {"fresh"}
    if extra:
        raise invalid("搜索参数包含未知字段："+", ".join(sorted(extra)), details={"supported_fields": list(SCHEMA["properties"])})
    # Older callers may still send fresh=false. It cannot enable result caching.
    if "fresh" in arguments and type(arguments["fresh"]) is not bool:
        raise invalid("fresh 兼容参数必须是布尔值；所有调用始终实时检索", "fresh")
    if "query" in arguments and "queries" in arguments:
        raise invalid("query 和 queries 请选择一种", "queries")
    queries = arguments.get("queries", [{"query": arguments.get("query")}])
    if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
        raise invalid("每次搜索需要 1 到 4 条查询", "queries")
    normalized = []
    for index, query in enumerate(queries):
        param = f"queries[{index}]"
        if isinstance(query, str):
            query = {"query": query}
        if not isinstance(query, dict):
            raise invalid("每条查询需要 query 字段", param)
        query = aliases(query, {"q": "query", "recency": "recency_days"})
        if set(query) - set(QUERY_SCHEMA["properties"]):
            raise invalid("查询包含未知字段", param)
        text = query.get("query")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise invalid("请输入 1 到 2000 字符的搜索词", param+".query")
        item = {"query": text.strip()}
        domains = domain_list(query.get("domains", arguments.get("domains")), param+".domains")
        if domains:
            item["domains"] = domains
        recency = query.get("recency_days", arguments.get("recency_days"))
        if recency is not None:
            item["recency_days"] = integer(recency, 1, 3650, param+".recency_days")
        normalized.append(item)
    limit = integer(arguments.get("max_results", 20), 1, 50, "max_results")
    length = arguments.get("response_length", "long")
    length = length.strip().lower() if isinstance(length, str) else length
    if length not in ("short", "medium", "long"):
        raise invalid("response_length 必须是 short、medium 或 long", "response_length")
    result = {"queries": normalized, "max_results": limit, "response_length": length, "fresh": True}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 32*1024:
        raise invalid("搜索参数过长，请缩短查询或筛选条件")
    return result
