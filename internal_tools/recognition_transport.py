"""Consume real billed vision SSE with a total deadline, without POST replay."""
import json
import math
import time
from email.utils import parsedate_to_datetime
from http.client import HTTPException

from image_hub.agent_bridge import Stream
from image_hub.gateway_transport import GatewayTransport
from image_hub.server import HubError
from image_hub.sse_terminal import SSETerminal

TRANSPORT = GatewayTransport(capacity=64)


def retry_delay(value):
    """Interpret a provider Retry-After without trusting arbitrary delays."""
    if not value or len(value) > 128:
        return None
    try:
        delay = float(value) if value.strip().isdigit() else parsedate_to_datetime(value).timestamp()-time.time()
        return min(300, max(0, delay)) if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


def recognition_request(url, credential, body, timeout=65, headers=None, on_text=None):
    deadline = time.monotonic() + timeout
    semantic_started = False
    visible_started = False
    stream_failed = False
    completed_only_billing = False
    terminal = SSETerminal(complete_on_usage=True)
    text_tail = ""
    payload = {**body, "stream": True, "stream_options": {"include_usage": True}}
    try:
        with TRANSPORT.open(url, json.dumps(payload, ensure_ascii=False).encode(),
                            {**(headers or {}), "Authorization":"Bearer "+credential,
                             "Content-Type":"application/json", "Accept":"text/event-stream"}, timeout=timeout) as response:
            completed_only_billing = response.headers.get("X-Aerisun-Recognition-Billing") == "completed-only-v1"
            if response.code != 200:
                raw = response.read(8192)
                try:
                    value = json.loads(raw)
                    message = (value.get("error") or {}).get("message") or value.get("message") or "识图上游暂时不可用"
                except (ValueError, TypeError, AttributeError):
                    message = "识图上游暂时不可用"
                error = HubError(response.code, str(message).replace(credential,"[redacted]")[:500])
                error.retry_after = retry_delay(response.headers.get("Retry-After"))
                raise error
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise HubError(502,"识图上游未返回有效的流式响应")

            def lines():
                nonlocal stream_failed
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HubError(504,"识图超时，请稍后重试")
                    connection = response.connection
                    sock = connection.sock if connection else None
                    if sock is None:
                        sock = getattr(getattr(response.response.fp,"raw",None),"_sock",None)
                    if sock:
                        # Image prefill can be silent for longer than 15s.
                        # The total monotonic deadline still bounds keepalives
                        # and every read; a short text idle budget is incorrect.
                        sock.settimeout(remaining)
                    line = response.readline(2*1024*1024+1)
                    if len(line)>2*1024*1024:
                        raise HubError(502,"识图响应过大")
                    if not line:
                        break
                    if line.startswith(b"data:"):
                        try:
                            frame = json.loads(line[5:])
                            stream_failed |= isinstance(frame, dict) and bool(frame.get("error"))
                        except (ValueError, UnicodeError):
                            pass
                    end=terminal.feed(line)
                    yield line if end is None else line[:end]
                    if end is not None:return

            def forward_text(_event, value):
                nonlocal semantic_started, visible_started, text_tail
                if isinstance(value,dict):
                    for choice in value.get("choices",[]):
                        delta=choice.get("delta") or {}
                        semantic_started |= any(bool(delta.get(k)) for k in ("content","reasoning_content","reasoning","tool_calls"))
                if isinstance(value,dict):
                    for choice in value.get("choices",[]):
                        content=(choice.get("delta") or {}).get("content")
                        if isinstance(content,str):
                            visible_started |= bool(content)
                            text_tail=(text_tail+content)[-160:]
                            if "<unk>"*8 in text_tail:
                                raise HubError(502,"识图上游输出异常，请稍后重试")
                if on_text and isinstance(value, dict):
                    for choice in value.get("choices", []):
                        text = choice.get("delta", {}).get("content")
                        if isinstance(text, str) and text:
                            on_text(text)

            result = Stream("chat",forward_text,internal_names=()).read(lines())
            choices = result.get("choices") or []
            if not choices or choices[0].get("finish_reason") not in ("stop","length"):
                raise HubError(502,"图片识别未完整完成，请稍后重试")
            usage=result.get('usage')
            if not isinstance(usage,dict) or not (usage.get('total_tokens',0)>0 or usage.get('prompt_tokens',0)+usage.get('completion_tokens',0)>0):
                raise HubError(502,"识图上游未返回真实用量")
            return 200,result
    except HubError as error:
        error.semantic_started = semantic_started
        error.visible_started = visible_started
        error.finish_seen = terminal.finish_seen
        error.usage_seen = terminal.usage_seen
        # An explicit failed stream from the dedicated gateway is unbilled.
        # Never replay visible partial text, or infer safety from a network
        # timeout / unknown gateway version.
        error.retryable_unbilled = bool(completed_only_billing and (stream_failed or terminal.finished) and not visible_started)
        raise
    except TimeoutError:
        error=HubError(504,"识图超时，请稍后重试");error.semantic_started=semantic_started
        error.visible_started=visible_started;error.finish_seen=terminal.finish_seen;error.usage_seen=terminal.usage_seen
        raise error from None
    except (ValueError,KeyError,TypeError,UnicodeError,HTTPException,OSError):
        error=HubError(502,"识图连接中断，请稍后重试");error.semantic_started=semantic_started
        error.visible_started=visible_started;error.finish_seen=terminal.finish_seen;error.usage_seen=terminal.usage_seen
        raise error from None
