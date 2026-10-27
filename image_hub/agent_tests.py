"""Protocol boundary tests: private tool consumption, continuations and billing limits."""
from copy import deepcopy
import io
import json
import unittest
import threading
import socket
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from unittest.mock import patch

from image_hub.agent_bridge import (NAME, ImageExecutor, Stream, calls, continue_body,
                                    eligible, inject, merge_usage, run, already_generated, protocol, normalized_path)
from image_hub.server import HubError

URL = "https://aerisun.top/sub2api/image-hub/results/" + "a" * 48 + ".png"
RESULT = {"status": "completed", "task_id": "imgtask_test", "result": {"data": [{"url": URL}]}, "cost": 0.2}


def frames(values):
    return io.BytesIO(b"".join(("data: " + json.dumps(value) + "\n\n").encode() for value in values))


def anthropic(call=False, mixed=False):
    block = {"type": "tool_use", "id": "internal-1", "name": NAME, "input": {}} if call else {"type": "text", "text": ""}
    delta = {"type": "input_json_delta", "partial_json": '{"prompt":"a circle"}'} if call else {"type": "text_delta", "text": URL}
    values = [{"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning"}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIGNED"}},
              {"type": "content_block_stop", "index": 0},
              {"type": "content_block_start", "index": 1, "content_block": block},
              {"type": "content_block_delta", "index": 1, "delta": delta},
              {"type": "content_block_stop", "index": 1}]
    if mixed:
        values += [{"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "client-1", "name": "Read", "input": {}}},
                   {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"file_path":"/tmp/a"}'}},
                   {"type": "content_block_stop", "index": 2}]
    return values + [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if call else "end_turn"}, "usage": {"output_tokens": 5}}, {"type": "message_stop"}]


def chat(call=False, mixed=False):
    base = {"id": "chat_test", "object": "chat.completion.chunk", "created": 1, "model": "kimi-k3"}
    chunks = [{"role": "assistant", "reasoning_content": "signed reasoning"}]
    if call:
        chunks += [{"tool_calls": [{"index": 0, "id": "internal-1", "type": "function", "function": {"name": "aerisun_", "arguments": ""}}]},
                   {"tool_calls": [{"index": 0, "function": {"name": "generate_image", "arguments": '{"prompt":'}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": '"a circle"}'}}]}]
        if mixed:
            chunks.append({"tool_calls": [{"index": 1, "id": "client-1", "type": "function", "function": {"name": "Read", "arguments": '{}'}}]})
    else:
        chunks += [{"content": URL}]
    return [{**base, "choices": [{"index": 0, "delta": chunk, "finish_reason": None}]} for chunk in chunks] + [{**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if call else "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]


def responses(call=False, mixed=False):
    item = {"type": "function_call", "id": "fc_test", "call_id": "internal-1", "name": NAME, "arguments": '{"prompt":"a circle"}'} if call else {"id": "msg_output", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": URL, "annotations": []}]}
    response = {"id": "resp_test", "object": "response", "status": "completed", "output": [item], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    values = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}, "sequence_number": 0},
              {"type": "response.in_progress", "response": {**response, "status": "in_progress", "output": []}, "sequence_number": 1},
              {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""} if call else item, "sequence_number": 2}]
    if call:
        values += [{"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_test", "delta": item["arguments"], "sequence_number": 3},
                   {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc_test", "arguments": item["arguments"], "sequence_number": 4}]
    else:
        values.append({"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"], "content_index": 0, "delta": URL, "sequence_number": 3})
    values.append({"type": "response.output_item.done", "output_index": 0, "item": item, "sequence_number": 5})
    if mixed:
        client = {"type": "function_call", "id": "fc_client", "call_id": "client-1", "name": "Read", "arguments": "{}"}
        response["output"].append(client)
        values += [{"type": "response.output_item.added", "output_index": 1, "item": client}, {"type": "response.output_item.done", "output_index": 1, "item": client}]
    return values + [{"type": "response.completed", "response": response, "sequence_number": 6}]


def gemini(call=False, mixed=False):
    part = {"functionCall": {"name": NAME, "args": {"prompt": "a circle"}}, "thoughtSignature": "SIGNED"} if call else {"text": URL}
    parts = [part]
    if mixed:
        parts.append({"functionCall": {"name": "Read", "args": {}}})
    return [{"candidates": [{"index": 0, "content": {"role": "model", "parts": parts}}]},
            {"candidates": [{"index": 0, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}]


BODIES = {"anthropic": {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "draw"}]},
          "chat": {"model": "kimi-k3", "messages": [{"role": "user", "content": "draw"}]},
          "responses": {"model": "gemini-3.8-flash", "input": "draw"},
          "gemini": {"contents": [{"role": "user", "parts": [{"text": "draw"}]}]}}
GENERATORS = {"anthropic": anthropic, "chat": chat, "responses": responses, "gemini": gemini}


class AgentTests(unittest.TestCase):
    def test_failed_image_is_terminal_without_model_retry_or_raw_error_object(self):
        for kind, generate in GENERATORS.items():
            with self.subTest(kind=kind):
                requests, emitted = [], []
                def upstream(body):
                    requests.append(body)
                    return frames(generate(call=True))
                result = run(inject(BODIES[kind],kind),kind,upstream,
                    lambda *_: {"status":"failed","error":{"type":"image_hub_error","message":"get image url failed"}},
                    Stream(kind,lambda e,v: emitted.append((e,v))))
                self.assertEqual(len(requests),1)
                self.assertIn("图片生成失败：get image url failed",json.dumps(result,ensure_ascii=False))
                self.assertNotIn("'type': 'image_hub_error'",json.dumps(result))
                self.assertNotIn("<tool_call>",json.dumps(emitted))

    def test_hidden_tool_streaming_all_protocols(self):
        for kind, generate in GENERATORS.items():
            with self.subTest(kind=kind):
                emitted, requests, executed = [], [], []
                def upstream(body):
                    requests.append(deepcopy(body))
                    return frames(generate(call=len(requests) == 1))
                def execute(args, id_):
                    executed.append((args, id_))
                    return RESULT
                result = run(inject(BODIES[kind], kind), kind, upstream, execute, Stream(kind, lambda e, v: emitted.append((e, v))))
                self.assertEqual(len(executed), 1)
                self.assertEqual(len(requests), 2)
                self.assertIn(URL, json.dumps(result))
                self.assertNotIn(NAME, json.dumps(emitted))
                self.assertNotIn(NAME, json.dumps(requests[1].get("tools", [])))
                self.assertIn(NAME, json.dumps(requests[1]))  # Call/result history retained.
                usage = result["usageMetadata" if kind == "gemini" else "usage"]
                self.assertEqual(usage["promptTokenCount" if kind == "gemini" else "prompt_tokens" if kind == "chat" else "input_tokens"], 20)
                if kind == "anthropic":
                    self.assertIn("SIGNED", json.dumps(requests[1]))
                    self.assertEqual(sum(v.get("type") == "message_start" for _, v in emitted), 1)
                    self.assertEqual(sum(v.get("type") == "message_stop" for _, v in emitted), 1)
                    starts = [v["index"] for _, v in emitted if v.get("type") == "content_block_start"]
                    self.assertEqual(starts, list(range(len(starts))))
                elif kind == "chat":
                    self.assertIn("reasoning_content", json.dumps(requests[1]))
                    self.assertEqual(sum(v == "[DONE]" for _, v in emitted), 1)
                elif kind == "responses":
                    self.assertEqual(sum(v.get("type") == "response.created" for _, v in emitted), 1)
                    self.assertEqual(sum(v.get("type") == "response.in_progress" for _, v in emitted), 1)
                    self.assertEqual(sum(v.get("type") == "response.completed" for _, v in emitted), 1)
                    completed_ids = [v["item"]["id"] for _, v in emitted if v.get("type") == "response.output_item.done"]
                    self.assertEqual(completed_ids, [x["id"] for x in result["output"]])
                else:
                    self.assertIn("SIGNED", json.dumps(requests[1]))

    def test_mixed_client_tools_return_real_image_without_invalid_continuation(self):
        for kind, generate in GENERATORS.items():
            with self.subTest(kind=kind):
                emitted, requests = [], []
                def upstream(body):
                    requests.append(body)
                    return frames(generate(call=True, mixed=True))
                result = run(inject(BODIES[kind], kind), kind, upstream, lambda *_: RESULT, Stream(kind, lambda e, v: emitted.append((e, v))))
                self.assertEqual(len(requests), 1)
                self.assertNotIn(NAME, json.dumps(emitted))
                self.assertIn("Read", json.dumps(emitted))
                self.assertIn(URL, json.dumps(result))
                self.assertTrue(any(name == "Read" for _, _, name in calls(result, kind)))

    def test_truncated_stream_never_submits_image(self):
        for kind, generate in GENERATORS.items():
            with self.subTest(kind=kind):
                executed = []
                with self.assertRaises(HubError):
                    run(inject(BODIES[kind], kind), kind, lambda _: frames(generate(call=True)[:-1]), lambda *_: executed.append(1), Stream(kind, lambda *_: None))
                self.assertEqual(executed, [])

    def test_nonstream_protocols(self):
        for kind, generate in GENERATORS.items():
            with self.subTest(kind=kind):
                replies = []
                for call in (True, False):
                    stream = Stream(kind, lambda *_: None)
                    replies.append(stream.read(frames(generate(call=call))))
                def upstream(_):
                    return io.BytesIO(json.dumps(replies.pop(0)).encode())
                result = run(inject(BODIES[kind], kind), kind, upstream, lambda *_: RESULT)
                self.assertNotIn(NAME, json.dumps(result))
                self.assertIn(URL, json.dumps(result))

    def test_continuation_failure_preserves_paid_result(self):
        emitted, requests = [], []
        def upstream(body):
            requests.append(body)
            if len(requests) == 2:
                raise HubError(503, "model unavailable")
            return frames(anthropic(call=True))
        result = run(inject(BODIES["anthropic"], "anthropic"), "anthropic", upstream, lambda *_: RESULT, Stream("anthropic", lambda e, v: emitted.append((e, v))))
        self.assertIn(URL, json.dumps(result))
        self.assertIn(URL, json.dumps(emitted))
        self.assertNotIn(NAME, json.dumps(emitted))

    def test_single_image_batch_and_poll_retry(self):
        responses_ = [(202, {"task_id": "imgtask_test", "status": "processing"}), HubError(502, "temporary"), (200, RESULT)]
        with patch("image_hub.agent_bridge.request_json", side_effect=responses_) as request, patch("image_hub.agent_bridge.time.sleep"):
            execute = ImageExecutor("sk-customer", identity="stable-request")
            self.assertEqual(execute({"prompt": "circle"}, "id")["status"], "completed")
            self.assertEqual(execute({"prompt": "circle"}, "id2")["status"], "failed")
            self.assertEqual(sum(call.args[0].endswith("/async") for call in request.call_args_list), 1)
            self.assertEqual(request.call_args_list[0].kwargs["headers"]["Idempotency-Key"], "agent-stable-request")
            self.assertEqual(request.call_args_list[1].args[0], request.call_args_list[2].args[0])

    def test_all_live_chat_models_have_the_same_internal_image_tool(self):
        for model in ["Nemotron-3.5", "kimi-k3", "deepseek-v4.1-flash", "qwen3.8-flash", "qwen3.8-max", "step-5-preview"]:
            for kind in ("chat", "anthropic", "responses"):
                with self.subTest(model=model, kind=kind):
                    body = {**BODIES[kind], "model": model}
                    self.assertTrue(eligible(body, kind, "/v1/messages"))
                    self.assertIn(NAME, json.dumps(inject(body, kind)["tools"]))
        for model in ("gpt-image-2.5", "qwen3-embedding-4b", "qwen3-reranker-4b"):
            self.assertFalse(eligible({**BODIES["chat"], "model": model}, "chat", "/v1/chat/completions"))
        self.assertEqual(protocol("/api/v1/chat/completions"), "chat")
        self.assertEqual(normalized_path("/api/v1/chat/completions"), "/api/v1/chat/model/completions")

    def test_nested_vision_events_and_generation_use_one_schema_for_every_protocol(self):
        for kind, generate in GENERATORS.items():
            emitted, requests = [], []
            progress = {"type":"tool_call", "id":"vision-test", "name":"image_recognition", "status":"running", "delta":"red triangle"}
            def upstream(body):
                requests.append(body)
                raw = frames(generate(call=len(requests)==1)).read()
                return io.BytesIO(("event: aerisun.tool\ndata: " + json.dumps(progress) + "\n\n").encode() + raw)
            run(inject(BODIES[kind],kind),kind,upstream,lambda *_:RESULT,Stream(kind,lambda event,value:emitted.append((event,value))))
            events=[value for event,value in emitted if event=="aerisun.tool"]
            self.assertIn(progress,events)
            self.assertTrue(any(v["name"]=="image_generation" and v["status"]=="completed" for v in events))
            self.assertTrue(all(v["type"]=="tool_call" for v in events))

    def test_tool_preferences_and_client_catalogue(self):
        body = BODIES["anthropic"]
        self.assertTrue(eligible(body, "anthropic", "/v1/messages"))
        for update in ({"tool_choice": {"type": "none"}}, {"tool_choice": {"type": "tool", "name": "Read"}}, {"tools": [{"name": "mcp__hub__generate_image"}]}, {"model": "gpt-image-2.5"}):
            self.assertFalse(eligible({**body, **update}, "anthropic", "/v1/messages"))
        original = {**body, "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "thinking": {"type": "enabled", "budget_tokens": 1000}}
        injected = inject(original, "anthropic")
        self.assertEqual(injected["tools"][0], original["tools"][0])
        self.assertEqual(injected["thinking"], original["thinking"])
        self.assertEqual(len(original["tools"]), 1)

    def test_client_tool_continuation_reuses_image_but_new_user_can_generate(self):
        for kind in BODIES:
            with self.subTest(kind=kind):
                if kind == "anthropic":
                    history = [*BODIES[kind]["messages"], {"role": "assistant", "content": [{"type": "tool_use", "name": "Write", "id": "write", "input": {"content": URL}}]},
                               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "write", "content": "retry file write"}]}]
                    field = "messages"
                    human = {"role": "user", "content": "Generate a second image"}
                elif kind == "chat":
                    history = [*BODIES[kind]["messages"], {"role": "assistant", "tool_calls": [{"function": {"name": "Write", "arguments": json.dumps({"content": URL})}}]}, {"role": "tool", "content": "retry write"}]
                    field = "messages"
                    human = {"role": "user", "content": "Generate a second image"}
                elif kind == "responses":
                    history = [{"role": "user", "content": "Generate and write"}, {"type": "function_call", "name": "Write", "arguments": json.dumps({"content": URL})}, {"type": "function_call_output", "output": "retry write"}]
                    field = "input"
                    human = {"role": "user", "content": "Generate a second image"}
                else:
                    history = [*BODIES[kind]["contents"], {"role": "model", "parts": [{"functionCall": {"name": "Write", "args": {"content": URL}}}]},
                               {"role": "user", "parts": [{"functionResponse": {"name": "Write", "response": {"content": "retry write"}}}]}]
                    field = "contents"
                    human = {"role": "user", "parts": [{"text": "Generate a second image"}]}
                body = {**BODIES[kind], field: history}
                self.assertTrue(already_generated(body, kind))
                self.assertFalse(already_generated({**body, field: history + [human]}, kind))
                # Tool prerequisites before an image exists must still allow
                # internal image generation after the client supplies results.
                self.assertFalse(already_generated({**body, field: json.loads(json.dumps(history).replace(URL, "brand reference"))}, kind))

    def test_paid_result_survives_client_read_handoff_without_url_in_arguments(self):
        requests, emitted = [], []
        def upstream(body):
            requests.append(body)
            if len(requests) == 1:
                return frames(anthropic(call=True))
            # A client Read may precede the eventual image insertion. Its
            # arguments do not carry the image result to the next request.
            values = [x for x in anthropic(call=True, mixed=True) if x.get("index") != 1]
            return frames(values)
        result = run(inject(BODIES["anthropic"], "anthropic"), "anthropic", upstream, lambda *_: RESULT,
                     Stream("anthropic", lambda e, v: emitted.append((e, v))))
        self.assertIn(URL, json.dumps(emitted))
        self.assertIn(URL, json.dumps(result["content"]))
        next_body = {**BODIES["anthropic"], "messages": [*BODIES["anthropic"]["messages"],
                    {"role": "assistant", "content": result["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "client-1", "content": "brand guidelines"}]}]}
        self.assertTrue(already_generated(next_body, "anthropic"))
        self.assertFalse(eligible(next_body, "anthropic", "/v1/messages"))


class HTTPBoundaryTests(unittest.TestCase):
    def test_wire_stream_customer_identity_and_text_only_permissions(self):
        from image_hub import agent_server
        requests = []
        failure = {"continuation": False}

        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, status=200, content_type="application/json"):
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                image = self.headers.get("Authorization") == "Bearer sk-customer"
                self.reply({"data": [{"id": "claude-sonnet-4-6"}] + ([{"id": "gpt-image-2.5-flare"}] if image else [])})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                if self.path.endswith("/async"):
                    return self.reply(RESULT, 202)
                if self.headers.get("Authorization") == "Bearer sk-text-only":
                    return self.reply({"id": "text", "content": [{"type": "text", "text": "OK"}]})
                continued = any(x.get("role") == "assistant" for x in body["messages"])
                if continued and failure["continuation"]:
                    return self.reply({"error": {"message": "model unavailable"}}, 503)
                if body.get("stream"):
                    self.reply(frames(anthropic(call=not continued)).getvalue(), content_type="text/event-stream")
                else:
                    self.reply(Stream("anthropic", lambda *_: None).read(frames(anthropic(call=not continued))))

        native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.Handler)
        for server in (native, bridge):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:" + str(native.server_port)
        try:
            with patch.object(agent_server, "GATEWAY", base), patch.object(agent_server, "HUB", base):
                opener = build_opener(ProxyHandler({}))
                body = {**BODIES["anthropic"], "stream": True, "max_tokens": 1024}
                req = Request("http://127.0.0.1:" + str(bridge.server_port) + "/messages", data=json.dumps(body).encode(),
                              headers={"Authorization": "Bearer sk-customer", "Content-Type": "application/json"})
                with opener.open(req, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Transfer-Encoding"], "chunked")
                    wire = response.read().decode()
                self.assertNotIn(NAME, wire)
                self.assertIn(URL, wire)
                self.assertEqual(wire.count('"type":"message_stop"'), 1)
                image_requests = [r for r in requests if r[0].endswith("/async")]
                self.assertEqual(len(image_requests), 1)
                self.assertEqual(image_requests[0][1], "Bearer sk-customer")
                requests.clear()
                req = Request("http://127.0.0.1:" + str(bridge.server_port) + "/v1/messages", data=json.dumps(body).encode(),
                              headers={"Authorization": "Bearer sk-text-only", "Content-Type": "application/json"})
                with opener.open(req, timeout=5) as response:
                    self.assertIn("OK", response.read().decode())
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][2], body)
                # A failed follow-up must also preserve a paid image for JSON
                # clients, before any successful response headers were sent.
                requests.clear()
                failure["continuation"] = True
                req = Request("http://127.0.0.1:" + str(bridge.server_port) + "/v1/messages", data=json.dumps({**body, "stream": False}).encode(),
                              headers={"Authorization": "Bearer sk-customer", "Content-Type": "application/json"})
                with opener.open(req, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    value = json.load(response)
                self.assertIn(URL, json.dumps(value))
                self.assertNotIn(NAME, json.dumps(value))
                self.assertEqual(sum(r[0].endswith("/async") for r in requests), 1)
        finally:
            for server in (bridge, native):
                server.shutdown()
                server.server_close()


class AcceptQueueTests(unittest.TestCase):
    def test_burst_connections_do_not_wait_for_accept_or_retransmit(self):
        from image_hub.agent_server import AgentHTTPServer, Handler
        # Hold off application accepts: all 32 handshakes must fit in the
        # kernel queue. The previous default queue admitted only six.
        server = AgentHTTPServer(("127.0.0.1", 0), Handler)
        sockets = []
        try:
            def connect(_):
                return socket.create_connection(server.server_address, timeout=.5)
            with ThreadPoolExecutor(max_workers=32) as pool:
                futures = [pool.submit(connect, i) for i in range(32)]
                for future in futures:
                    sockets.append(future.result())
            self.assertEqual(len(sockets), 32)
        finally:
            for connection in sockets:
                connection.close()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
