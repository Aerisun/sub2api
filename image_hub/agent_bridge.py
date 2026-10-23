"""Private image tool execution for chat protocols; no client MCP required.

Model calls and image tasks use the original customer's key. Tool calls are
consumed by the server; normal text, thinking and client tools remain streamed.
Only one image batch may be submitted during one incoming chat request.
"""
from copy import deepcopy
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from image_hub.server import HubError, NATIVE_MODEL, NATIVE_MODELS, NATIVE_MODEL_CHOICES, TOOLS, request_json, validate_payload

NAME = "aerisun_generate_image"
NATIVE_CHAT_MODELS = frozenset({'gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                              'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'})


def image_model_for_chat(model):
    return NATIVE_MODEL


DESCRIPTION = TOOLS[0]["description"] + " The server executes this tool and returns image URLs; no client setup is needed. Use existing vision support for recognition."
SCHEMA = deepcopy(TOOLS[0]["inputSchema"])
INSTRUCTION = ("Use aerisun_generate_image when the user explicitly asks to create an image. "
               "Choose the image model yourself based on task complexity: Flare for everyday generation and speed, "
               "Sunburst for demanding compositions or precise edits, Image 2 for compatibility. "
               "Preserve their requirements and default to one image. Results are already displayed to the user. "
               "Write the accompanying answer as ordinary prose in the user's requested language "
               "(Chinese for a Chinese request unless another language is requested). "
               "Do not add image Markdown, empty image placeholders, duplicate URLs, tool status or pricing. "
               "Real result URLs may still be used in client tools; never invent links. "
               "Use existing vision support for recognition. No client MCP setup is needed.")


def protocol(path):
    path = urlsplit(path).path
    if re.fullmatch(r"/(?:antigravity/)?(?:v1/)?messages", path):
        return "anthropic"
    if path == "/api/v1/chat/completions" or re.fullmatch(r"/(?:v1/)?chat/completions", path):
        return "chat"
    if re.fullmatch(r"/(?:v1/)?responses", path):
        return "responses"
    if re.fullmatch(r"/(?:antigravity/)?v1beta/models/[^/]+:(?:streamGenerateContent|generateContent)", path):
        return "gemini"
    return None


def normalized_path(path):
    if urlsplit(path).path == "/api/v1/chat/completions":
        return path.replace("/api/v1/chat/completions", "/api/v1/chat/model/completions", 1)
    if re.match(r"^/(messages|chat/completions|responses)(?:\?|$)", path):
        return "/v1" + path
    return path


def tool_names(body, kind):
    names = []
    for tool in body.get("tools", []):
        if not isinstance(tool, dict):
            continue
        if kind == "gemini":
            names.extend(x.get("name", "") for x in tool.get("functionDeclarations", []) if isinstance(x, dict))
        else:
            names.append(tool.get("function", tool).get("name", tool.get("type", "")))
    return names


def already_generated(body, kind):
    """Client tool continuations reuse an image from the same human turn.

    A new human message resets this check, allowing an explicitly requested
    second image even when previous turns contain generated image URLs.
    """
    history = body.get("contents" if kind == "gemini" else "input" if kind == "responses" else "messages", [])
    if not isinstance(history, list):
        return False
    last_user = -1
    for index, message in enumerate(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("parts" if kind == "gemini" else "content", "")
        human = isinstance(content, str) and bool(content.strip())
        if isinstance(content, list):
            human = any(isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
                        and part.get("type", "text") in ("text", "input_text") for part in content)
        if human:
            last_user = index
    if last_user < 0:
        return False
    after_user = json.dumps(history[last_user + 1:], ensure_ascii=False)
    return bool(re.search(r"https://aerisun\.top/sub2api/image-hub/results/[0-9a-f]{48}\.(?:png|jpg|webp)", after_user))


def eligible(body, kind, path, search=False):
    if not isinstance(body, dict) or not kind:
        return False
    model = body.get("model", "") if kind != "gemini" else urlsplit(path).path.split("/models/", 1)[-1].split(":")[0]
    # All chat models share the internal tool; media/embedding endpoints do not.
    if not isinstance(model, str) or not model.strip() or any(
            part in model.lower() for part in ("embedding", "rerank", "gpt-image", "image-generation")):
        return False
    # Responses clients receive the upstream native image_generation events.
    # Anthropic/Chat clients use the native gateway Images adapter below because
    # those protocols cannot carry an OpenAI image_generation output item.
    if not search and kind == 'responses' and model.strip().lower() in NATIVE_CHAT_MODELS:
        return False
    if kind == 'chat' and not isinstance(body.get('messages'), list):
        return False  # Leave Responses-shaped compatibility requests to the gateway.
    if not search and already_generated(body, kind):
        return False
    # Respect a client's explicit tool choice, response schema and candidate count.
    # Multiple completions must never cause multiple paid hidden operations.
    choice = body.get("tool_choice", "auto")
    if choice not in ("auto", None) and choice != {"type": "auto"}:
        return False
    if body.get("n", 1) != 1 or body.get("response_format", {}).get("type", "text") not in ("text",):
        return False
    if kind == "gemini":
        config = body.get("generationConfig", {})
        if config.get("candidateCount", 1) != 1 or config.get("responseMimeType", "text/plain") != "text/plain":
            return False
        call_config = body.get("toolConfig", {}).get("functionCallingConfig", {})
        if call_config.get("mode", "AUTO") not in ("AUTO", "VALIDATED") or call_config.get("allowedFunctionNames"):
            return False
        if body.get("cachedContent"):
            return False  # Cached server contexts cannot have their tools changed.
    if kind == "responses" and (body.get("text", {}).get("format", {}).get("type", "text") != "text"):
        return False
    # A configured client image tool takes precedence; avoid duplicate catalogues.
    if not search and any(NAME == name or "generate_image" in name or "image_generation" in name for name in tool_names(body, kind)):
        return False
    return True


def inject(body, kind, image_model=NATIVE_MODEL, allowed_models=NATIVE_MODEL_CHOICES):
    body = deepcopy(body)
    tool = {"name": NAME, "description": DESCRIPTION}
    schema = deepcopy(SCHEMA)
    schema['properties']['model']['enum'] = [model for model in NATIVE_MODEL_CHOICES if model in allowed_models]
    schema['properties']['model']['default'] = image_model
    if kind == "anthropic":
        tool["input_schema"] = schema
        system = body.get("system", [])
        if isinstance(system, str):
            system = [{"type": "text", "text": system}] if system else []
        body["system"] = system + [{"type": "text", "text": INSTRUCTION}]
    elif kind == "chat":
        tool["parameters"] = schema
        tool = {"type": "function", "function": tool}
        body["messages"] = [{"role": "system", "content": INSTRUCTION}] + body["messages"]
    elif kind == "responses":
        tool.update(type="function", parameters=schema)
        body["instructions"] = (body.get("instructions") or "") + "\n" + INSTRUCTION
    else:
        tool["parameters"] = schema
        # Gemini's schema dialect does not accept these OpenAPI constraints.
        tool["parameters"].pop("additionalProperties", None)
        for prop in tool["parameters"]["properties"].values():
            for key in ("default", "minLength", "minimum", "maximum", "maxItems"):
                prop.pop(key, None)
        tool = {"functionDeclarations": [tool]}
        system = body.get("systemInstruction", {"parts": []})
        body["systemInstruction"] = {**system, "parts": system.get("parts", []) + [{"text": INSTRUCTION}]}
    body["tools"] = body.get("tools", []) + [tool]
    return body


def hide_tool(body, kind, names=None):
    names = {NAME} if names is None else names
    body = deepcopy(body)
    if kind == "gemini":
        tools = []
        for tool in body.get("tools", []):
            if "functionDeclarations" not in tool:
                tools.append(tool)
            else:
                declarations = [x for x in tool["functionDeclarations"] if x.get("name") not in names]
                if declarations:
                    tools.append({**tool, "functionDeclarations": declarations})
        body["tools"] = tools
    else:
        body["tools"] = [t for t in body.get("tools", []) if t.get("function", t).get("name") not in names]
    if not body["tools"]:
        body.pop("tools")
        body.pop("tool_choice", None)
        body.pop("parallel_tool_calls", None)
    return body


class ImageExecutor:
    def __init__(self, key, headers=None, hub="http://127.0.0.1:8092", identity=None, timeout=1500, model=NATIVE_MODEL,
                 allowed_models=NATIVE_MODEL_CHOICES):
        self.key, self.headers, self.hub = key, headers or {}, hub
        self.identity = identity or secrets.token_hex(16)
        self.timeout = timeout
        self.submitted = False
        self.progress = None
        self.model = model
        self.allowed_models = NATIVE_MODELS.intersection(allowed_models)

    def __call__(self, arguments, call_id):
        if self.submitted:
            return {"status": "failed", "error": "Only one image batch is permitted per chat request; use the earlier result."}
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            selected = arguments.get('model', self.model)
            if selected not in self.allowed_models:
                return {"status": "failed", "error": "Image model is unavailable; no image submitted."}
            payload = validate_payload({**arguments, 'model': selected})
            self.submitted = True
            _, task = request_json(self.hub + "/v1/images/generations/async", self.key, payload,
                                   headers={**self.headers, "Idempotency-Key": "agent-" + self.identity})
            if self.progress:
                self.progress({"type":"tool_call", "id":call_id, "name":"image_generation", "status":"running",
                               "delta":"图片任务已提交，正在生成图片。\n", **image_tool_metadata(task)})
            deadline = time.monotonic() + self.timeout
            while task["status"] == "processing" and time.monotonic() < deadline:
                try:
                    wait = min(30, max(1, int(deadline - time.monotonic())))
                    _, task = request_json(self.hub + "/v1/images/tasks/" + task["task_id"] +
                                           "?wait_seconds=" + str(wait), self.key, timeout=wait + 5)
                except HubError as error:
                    if error.status not in (429, 500, 502, 503, 504):
                        raise
                    # Poll the SAME task after a transient failure, never resubmit.
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
            return {k: v for k, v in task.items() if k in ("task_id", "status", "model", "result", "cost", "expires_at", "error")}
        except (ValueError, TypeError):
            return {"status": "failed", "error": "Invalid image tool arguments; no image submitted."}
        except HubError as error:
            return {"status": "failed", "error": error.message}


def image_tool_metadata(task):
    # The task service records the accepted image model. Never substitute the
    # conversational model, the executor default or an unexecuted tool argument.
    model = task.get("model")
    return {"metadata": {"model": model}} if isinstance(model, str) and model in NATIVE_MODELS else {}


def image_error_text(value):
    error = value.get("error") or "图片生成未完成"
    if isinstance(error, dict):
        error = error.get("message") or "图片生成未完成"
    return str(error)


def result_text(results):
    links = []
    failures = []
    for _, value in results:
        if value.get("status") == "completed":
            links.extend(x["url"] for x in value.get("result", {}).get("data", []) if x.get("url"))
        elif value.get("status") == "processing":
            failures.append("图片仍在生成，任务编号：" + value.get("task_id", ""))
        else:
            failures.append("图片生成失败：" + image_error_text(value))
    return "\n\n" + "\n\n".join(["![生成的图片](" + url + ")" for url in links] + failures) + "\n\n"


def continue_body(body, kind, response, results, disable_names=None):
    body = hide_tool(body, kind, disable_names)
    encoded = lambda value: json.dumps(value, ensure_ascii=False)
    if kind == "anthropic":
        body["messages"] += [{"role": "assistant", "content": response["content"]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": id_, "content": encoded(value)} for id_, value in results]}]
    elif kind == "chat":
        message = deepcopy(response["choices"][0]["message"])
        message.pop("refusal", None)
        body["messages"] += [message] + [{"role": "tool", "tool_call_id": id_, "content": encoded(value)} for id_, value in results]
    elif kind == "responses":
        inputs = body.get("input", [])
        if isinstance(inputs, str):
            inputs = [{"role": "user", "content": inputs}]
        body["input"] = inputs + response.get("output", []) + [{"type": "function_call_output", "call_id": id_, "output": encoded(value)} for id_, value in results]
        # Preserve previous_response_id for the original history, adding this turn
        # explicitly rather than relying on a provider's newly allocated response ID.
    else:
        names = {id_: name for id_, _, name in calls(response, kind)}
        body["contents"] += [response["candidates"][0]["content"],
                             {"role": "user", "parts": [{"functionResponse": {"name": names[id_], "response": value}} for id_, value in results]}]
    return body


def calls(response, kind):
    if kind == "anthropic":
        return [(x["id"], x.get("input", {}), x.get("name")) for x in response.get("content", []) if x.get("type") == "tool_use"]
    if kind == "chat":
        return [(x["id"], x["function"].get("arguments", "{}"), x["function"].get("name")) for x in response["choices"][0]["message"].get("tool_calls", [])]
    if kind == "responses":
        return [(x["call_id"], x.get("arguments", "{}"), x.get("name")) for x in response.get("output", []) if x.get("type") == "function_call"]
    return [(str(i), x["functionCall"].get("args", {}), x["functionCall"].get("name")) for i, x in enumerate(response.get("candidates", [{}])[0].get("content", {}).get("parts", [])) if "functionCall" in x]


def merge_usage(one, two):
    result = deepcopy(one)
    for name, value in two.items():
        if isinstance(value, dict):
            result[name] = merge_usage(result.get(name, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result[name] = result.get(name, 0) + value
        else:
            result[name] = value
    return result


def clean_response(response, kind, results=None, text_item=None, internal_names=None):
    internal_names = {NAME} if internal_names is None else internal_names
    response = deepcopy(response)
    text = result_text(results) if results else ""
    if kind == "anthropic":
        response["content"] = [x for x in response.get("content", []) if not (x.get("type") == "tool_use" and x.get("name") in internal_names)]
        if text:
            response["content"].append({"type": "text", "text": text})
        if not any(x.get("type") == "tool_use" for x in response["content"]) and response.get("stop_reason") == "tool_use":
            response["stop_reason"] = "end_turn"
    elif kind == "chat":
        message = response["choices"][0]["message"]
        message["tool_calls"] = [x for x in message.get("tool_calls", []) if x["function"].get("name") not in internal_names]
        if not message["tool_calls"]:
            message.pop("tool_calls")
            if response["choices"][0].get("finish_reason") == "tool_calls":
                response["choices"][0]["finish_reason"] = "stop"
        if text:
            message["content"] = (message.get("content") or "") + text
    elif kind == "responses":
        response["output"] = [x for x in response.get("output", []) if not (x.get("type") == "function_call" and x.get("name") in internal_names)]
        if text:
            response["output"].append(text_item or {"id": "msg_" + secrets.token_hex(12), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    else:
        candidate = response["candidates"][0]
        candidate["content"]["parts"] = [x for x in candidate["content"].get("parts", []) if x.get("functionCall", {}).get("name") not in internal_names]
        if text:
            candidate["content"]["parts"].append({"text": text})
    return response


def merge_response(first, second, kind):
    if first is None:
        return deepcopy(second)
    response = deepcopy(second)
    if kind == "anthropic":
        response["id"] = first["id"]
        response["content"] = first["content"] + second["content"]
    elif kind == "chat":
        response["id"] = first["id"]
        response["choices"][0]["message"]["content"] = (first["choices"][0]["message"].get("content") or "") + (second["choices"][0]["message"].get("content") or "")
    elif kind == "responses":
        response["id"] = first["id"]
        response["output"] = first.get("output", []) + second.get("output", [])
    else:
        response["candidates"][0]["content"]["parts"] = first["candidates"][0]["content"].get("parts", []) + second["candidates"][0]["content"].get("parts", [])
    usage_name = "usageMetadata" if kind == "gemini" else "usage"
    response[usage_name] = merge_usage(first.get(usage_name, {}), second.get(usage_name, {}))
    return response


def sse_frames(source, tool_event=None):
    """Read complete SSE frames, tolerating CRLF and multiline JSON data."""
    event, lines = None, []
    size = 0
    for raw in source:
        size += len(raw)
        if size > 32 * 1024 * 1024:
            raise HubError(502, "Model stream exceeded the supported response size")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                value = "\n".join(lines)
                if value != "[DONE]":
                    parsed = json.loads(value)
                    if event == "aerisun.tool":
                        if tool_event: tool_event(parsed)
                    else:
                        yield event, parsed
            event, lines = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
    if lines:
        raise HubError(502, "Model stream ended before a complete event")


class Stream:
    """One logical outer response, including any internal tool continuation."""
    def __init__(self, kind, emit, internal_names=None):
        self.kind, self.emit = kind, emit
        self.internal_names = {NAME} if internal_names is None else set(internal_names)
        self.started = False
        self.next_index = 0
        self.sequence = 0
        self.outer_id = None
        self.aggregate = None
        self.progress_sent = False
        self.chat_metadata = {}

    def tool(self, value):
        if (isinstance(value, dict) and value.get("type") == "tool_call"
                and value.get("name") in ("image_recognition", "image_generation", "web_search")
                and value.get("status") in ("running", "completed", "failed")):
            self.emit("aerisun.tool", deepcopy(value))

    def send(self, event, value):
        value = deepcopy(value)
        if self.kind == "responses":
            if "sequence_number" in value:
                value["sequence_number"] = self.sequence
                self.sequence += 1
            if "response_id" in value and self.outer_id:
                value["response_id"] = self.outer_id
        elif self.kind == "chat" and self.outer_id:
            value["id"] = self.outer_id
        self.emit(event, value)

    def read(self, source):
        return getattr(self, "read_" + self.kind)(source)

    def read_anthropic(self, source):
        response, blocks, mapping, arguments = None, {}, {}, {}
        fragments = {}
        complete = False
        for event, value in sse_frames(source, self.tool):
            type_ = value.get("type", event)
            if type_ == "message_start":
                response = deepcopy(value["message"])
                response["content"] = []
                if not self.started:
                    self.outer_id = response["id"]
                    self.send(event, value)
                    self.started = True
            elif type_ == "content_block_start":
                index, block = value["index"], deepcopy(value["content_block"])
                blocks[index] = block
                for name in ("text", "thinking", "signature"):
                    if isinstance(block.get(name), str):
                        fragments[index, name] = [block[name]]
                if block.get("type") != "tool_use" or block.get("name") not in self.internal_names:
                    mapping[index] = self.next_index
                    self.next_index += 1
                    self.send(event, {**value, "index": mapping[index]})
            elif type_ == "content_block_delta":
                index, delta = value["index"], value["delta"]
                block = blocks[index]
                for name in ("text", "thinking", "signature"):
                    if name in delta:
                        fragments.setdefault((index, name), []).append(delta[name])
                if "partial_json" in delta:
                    arguments.setdefault(index, []).append(delta["partial_json"])
                if index in mapping:
                    self.send(event, {**value, "index": mapping[index]})
            elif type_ == "content_block_stop":
                index = value["index"]
                if index in arguments:
                    blocks[index]["input"] = json.loads("".join(arguments[index]))
                if index in mapping:
                    self.send(event, {**value, "index": mapping[index]})
            elif type_ == "message_delta":
                response.update(value.get("delta", {}))
                # message_delta usage is cumulative for this upstream round.
                response["usage"] = {**response.get("usage", {}), **value.get("usage", {})}
            elif type_ == "message_stop":
                complete = True
            elif type_ == "error":
                raise HubError(502, "Model returned a streaming error")
            else:
                self.send(event, value)
        if not complete or response is None:
            raise HubError(502, "Model stream ended before completion; no image submitted")
        for (index, name), pieces in fragments.items():
            blocks[index][name] = "".join(pieces)
        response["content"] = [blocks[i] for i in sorted(blocks)]
        return response

    def read_chat(self, source):
        message, tools, buffered, mapping = {"role": "assistant", "content": ""}, {}, {}, {}
        fragments = {}
        response, complete = None, False
        for event, value in sse_frames(source, self.tool):
            if value.get("error"):
                raise HubError(502, "Model returned a streaming error")
            if response is None:
                response = {**value, "object": "chat.completion", "choices": []}
                self.outer_id = self.outer_id or value.get("id")
                if not self.chat_metadata:
                    self.chat_metadata = {k: v for k, v in value.items() if k not in ("choices", "usage")}
            if "usage" in value and value["usage"]:
                response["usage"] = value["usage"]
            for choice in value.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise HubError(502, "Multiple model candidates are unsupported by the internal image bridge")
                delta = deepcopy(choice.get("delta", {}))
                for name in ("content", "reasoning_content", "reasoning"):
                    if isinstance(delta.get(name), str):
                        fragments.setdefault(name, []).append(delta[name])
                visible_calls = []
                for tool in delta.pop("tool_calls", []):
                    index = tool["index"]
                    saved = tools.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tool.get("id"):
                        saved["id"] = tool["id"]
                    for name, fragment in tool.get("function", {}).items():
                        if isinstance(fragment, str):
                            saved["function"][name] = saved["function"].get(name, "") + fragment
                    buffered.setdefault(index, []).append(tool)
                    name = saved["function"]["name"]
                    # A fragmented private name must not escape to the client.
                    if name and not any(private.startswith(name) for private in self.internal_names):
                        if index not in mapping:
                            mapping[index] = len(mapping)
                        visible_calls += [{**x, "index": mapping[index]} for x in buffered.pop(index)]
                if visible_calls:
                    delta["tool_calls"] = visible_calls
                if self.started:
                    delta.pop("role", None)
                elif delta:
                    self.started = True
                if delta:
                    self.send(event, {**value, "choices": [{**choice, "delta": delta, "finish_reason": None}], "usage": None})
                if choice.get("finish_reason") is not None:
                    response["finish_reason"] = choice["finish_reason"]
                    complete = True
        if not complete or response is None:
            raise HubError(502, "Model stream ended before completion; no image submitted")
        # Names that are prefixes of the private name but completed differently
        # are normal client tools, forwarded only after their identity is known.
        for index, tool_fragments in buffered.items():
            if tools[index]["function"]["name"] not in self.internal_names:
                mapping[index] = len(mapping)
                self.send(None, {**response, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"tool_calls": [{**x, "index": mapping[index]} for x in tool_fragments]}, "finish_reason": None}], "usage": None})
        if tools:
            message["tool_calls"] = [tools[i] for i in sorted(tools)]
        message.update({name: "".join(pieces) for name, pieces in fragments.items()})
        response["choices"] = [{"index": 0, "message": message, "finish_reason": response.pop("finish_reason")}]
        return response

    def read_responses(self, source):
        mapping, hidden, response = {}, set(), None
        for event, value in sse_frames(source, self.tool):
            type_ = value.get("type", event)
            if type_ in ("response.created", "response.in_progress"):
                self.outer_id = self.outer_id or value["response"]["id"]
                if not self.started or type_ == "response.in_progress" and not self.progress_sent:
                    if type_ == "response.created":
                        self.started = True
                    else:
                        self.progress_sent = True
                    value = {**value, "response": {**value["response"], "id": self.outer_id}}
                    self.send(event, value)
            elif type_ == "response.output_item.added":
                index, item = value["output_index"], value["item"]
                if item.get("type") == "function_call" and item.get("name") in self.internal_names:
                    hidden.add(index)
                else:
                    mapping[index] = self.next_index
                    self.next_index += 1
                    self.send(event, {**value, "output_index": mapping[index]})
            elif type_ == "response.completed":
                response = value["response"]
            elif type_ in ("response.failed", "response.incomplete", "error"):
                raise HubError(502, "Model response did not complete; no image submitted")
            elif "output_index" in value:
                index = value["output_index"]
                if index not in hidden:
                    self.send(event, {**value, "output_index": mapping.get(index, index)})
            else:
                self.send(event, value)
        if response is None:
            raise HubError(502, "Model stream ended before completion; no image submitted")
        return response

    def read_gemini(self, source):
        response, parts, complete = None, [], False
        text_fragments = {}
        for event, value in sse_frames(source, self.tool):
            if value.get("error"):
                raise HubError(502, "Model returned a streaming error")
            if response is None:
                response = deepcopy(value)
            candidate = value.get("candidates", [{}])[0]
            incoming = candidate.get("content", {}).get("parts", [])
            # Adjacent text/thinking fragments form one signed content part.
            for part in incoming:
                if parts and "text" in part and "text" in parts[-1] and part.get("thought", False) == parts[-1].get("thought", False) and not parts[-1].get("thoughtSignature"):
                    text_fragments.setdefault(len(parts) - 1, [parts[-1]["text"]]).append(part["text"])
                    parts[-1].update({k: v for k, v in part.items() if k != "text"})
                else:
                    parts.append(deepcopy(part))
            if value.get("usageMetadata"):
                response["usageMetadata"] = value["usageMetadata"]
            if candidate.get("finishReason"):
                response["candidates"][0]["finishReason"] = candidate["finishReason"]
                complete = True
            visible = [x for x in incoming if x.get("functionCall", {}).get("name") not in self.internal_names]
            if visible:
                candidate = {**candidate, "content": {**candidate.get("content", {}), "parts": visible}}
                candidate.pop("finishReason", None)
                self.send(event, {**value, "candidates": [candidate], "usageMetadata": {}})
        if not complete or response is None:
            raise HubError(502, "Model stream ended before completion; no image submitted")
        for index, pieces in text_fragments.items():
            parts[index]["text"] = "".join(pieces)
        response["candidates"][0]["content"] = {"role": "model", "parts": parts}
        return response

    def text(self, text):
        if self.kind == "anthropic":
            index = self.next_index
            self.next_index += 1
            self.send("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            self.send("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}})
            self.send("content_block_stop", {"type": "content_block_stop", "index": index})
        elif self.kind == "chat":
            self.send(None, {**self.chat_metadata, "id": self.outer_id, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        elif self.kind == "responses":
            index = self.next_index
            self.next_index += 1
            id_ = "msg_" + secrets.token_hex(12)
            base = {"response_id": self.outer_id, "output_index": index}
            item = {"id": id_, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
            self.send("response.output_item.added", {"type": "response.output_item.added", **base, "item": item})
            part = {"type": "output_text", "text": "", "annotations": []}
            self.send("response.content_part.added", {"type": "response.content_part.added", **base, "item_id": id_, "content_index": 0, "part": part})
            self.send("response.output_text.delta", {"type": "response.output_text.delta", **base, "item_id": id_, "content_index": 0, "delta": text})
            self.send("response.output_text.done", {"type": "response.output_text.done", **base, "item_id": id_, "content_index": 0, "text": text})
            part["text"] = text
            self.send("response.content_part.done", {"type": "response.content_part.done", **base, "item_id": id_, "content_index": 0, "part": part})
            completed = {**item, "status": "completed", "content": [part]}
            self.send("response.output_item.done", {"type": "response.output_item.done", **base, "item": completed})
            return completed
        else:
            self.send(None, {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": text}]}}]})

    def finish(self, response):
        if self.kind == "anthropic":
            self.send("message_delta", {"type": "message_delta", "delta": {"stop_reason": response.get("stop_reason", "end_turn"), "stop_sequence": response.get("stop_sequence")}, "usage": response.get("usage", {})})
            self.send("message_stop", {"type": "message_stop"})
        elif self.kind == "chat":
            self.send(None, {**self.chat_metadata, "id": self.outer_id, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0].get("finish_reason", "stop")}], "usage": response.get("usage", {})})
            self.emit(None, "[DONE]")
        elif self.kind == "responses":
            response["id"] = self.outer_id
            self.send("response.completed", {"type": "response.completed", "response": response, "sequence_number": self.sequence})
        else:
            self.send(None, {"candidates": [{"index": 0, "finishReason": response["candidates"][0].get("finishReason", "STOP")}], "usageMetadata": response.get("usageMetadata", {})})


def run(body, kind, upstream, execute, stream=None):
    """At most two model rounds: initial tool selection, then result continuation."""
    if hasattr(execute, "run"):
        return execute.run(body, kind, upstream, stream)
    aggregate = None
    results = None
    for round_ in range(2):
        try:
            with upstream(body) as response:
                value = stream.read(response) if stream else json.load(response)
        except (HubError, ValueError, KeyError, TypeError, OSError):
            if results is None:
                raise
            # An image may already be charged. Return its real result even when
            # the follow-up model fails; do not turn it into a retryable failure.
            # The real result was already exposed in the outer assistant text.
            if stream:
                stream.finish(aggregate)
            return aggregate
        selected = calls(value, kind)
        internal = [(id_, arguments) for id_, arguments, name in selected if name == NAME]
        if not internal:
            aggregate = merge_response(aggregate, clean_response(value, kind), kind)
            if stream:
                stream.finish(aggregate)
            return aggregate
        results = []
        for id_, arguments in internal:
            if stream:
                stream.tool({"type":"tool_call", "id":id_, "name":"image_generation", "status":"running"})
            if isinstance(execute, ImageExecutor):
                execute.progress = stream.tool if stream else None
            result = execute(arguments, id_)
            results.append((id_, result))
            if stream:
                status = "completed" if result.get("status") == "completed" else "running" if result.get("status") == "processing" else "failed"
                stream.tool({"type":"tool_call", "id":id_, "name":"image_generation", "status":status,
                             "output":result_text([(id_,result)]), **image_tool_metadata(result),
                             **({"error":image_error_text(result)} if status=="failed" else {})})
        visible_tools = any(name != NAME for _, _, name in selected)
        # Preserve the image result in CLIENT history as well as the internal
        # model history. A subsequent Read/Edit call might contain no image URL;
        # without this text the next client turn would lose the paid result.
        item = stream.text(result_text(results)) if stream else None
        if visible_tools or round_ == 1 or not any(result.get("status") == "completed" for _, result in results):
            aggregate = merge_response(aggregate, clean_response(value, kind, results, item), kind)
            if stream:
                stream.finish(aggregate)
            return aggregate
        aggregate = merge_response(aggregate, clean_response(value, kind, results, item), kind)
        body = continue_body(body, kind, value, results)
    raise HubError(502, "Internal image continuation did not finish")
