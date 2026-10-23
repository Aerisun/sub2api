"""Stable, public tool errors shared by REST, MCP and chat adapters."""
from image_hub.server import HubError


class ToolError(HubError):
    def __init__(self, status, message, code="tool_error", param=None, retryable=False, suggestion=None, details=None):
        super().__init__(status, message, code)
        self.param, self.retryable = param, retryable
        self.suggestion, self.details = suggestion, details


def error_details(error):
    fallback = {400: "invalid_arguments", 401: "authentication_error", 402: "insufficient_balance",
                403: "permission_denied", 404: "search_unavailable", 429: "rate_limit_exceeded",
                502: "upstream_error", 503: "service_unavailable", 504: "upstream_timeout"}
    code = getattr(error, "code", "tool_error")
    result = {"code": fallback.get(error.status, "tool_error") if code == "image_hub_error" else code,
              "message": error.message, "status": error.status,
              "retryable": getattr(error, "retryable", error.status in (429, 502, 503, 504))}
    for key in ("param", "suggestion", "details"):
        value = getattr(error, key, None)
        if value is not None:
            result[key] = value
    return result


def invalid(message, param=None, details=None):
    return ToolError(400, message, "invalid_arguments", param=param, details=details,
                     suggestion='最简单的调用格式为 {"query":"需要搜索的内容"}。多条查询使用 queries 数组。')
