import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

from image_hub.agent_bridge import Stream, calls, tool_names
from image_hub.agent_tests import BODIES, GENERATORS, frames, NAME as IMAGE_NAME, RESULT
from image_hub.server import HubError
from internal_tools import http_api
from internal_tools.chat_bridge import ToolExecutor, inject_search, search_eligible
from internal_tools.web_search import (FUNCTION_NAME, MODEL, DEFINITION, WebSearchExecutor,
                                       normalize_input, normalize_output, public_url)

RAW = {"output": "Useful extract \ue200cite\ue202turn0search0\ue201", "encrypted_output": "private-opaque",
       "results": [{"ref_id": "turn0search0", "title": "Official docs", "url": "https://example.com/docs", "snippet": "Source extract"}]}


class SearchTests(unittest.TestCase):
    def test_validation_and_native_result_normalization(self):
        args = normalize_input({"queries": [{"query": "  docs  ", "domains": ["Example.COM"]}], "recency_days": 7})
        self.assertEqual(args["queries"], [{"query": "docs", "domains": ["example.com"], "recency_days": 7}])
        self.assertEqual(args["response_length"], "long")
        self.assertEqual(args["max_results"], 20)
        for invalid in ({}, {"query": "a", "queries": []}, {"query": "a", "recency_days": True}, {"query": "a", "fresh": 1},
                        {"query": "a", "domains": ["http://localhost"]}, {"queries": []}, {"query": "a", "model": "bypass"},
                        {"query": "a", "max_results": 51}, {"q": "a", "query": "b"}, {"query": "a", "response_length": []}):
            with self.subTest(invalid=invalid), self.assertRaises(HubError):
                normalize_input(invalid)
        normalized = normalize_output(RAW, args)
        self.assertNotIn("encrypted", json.dumps(normalized))
        self.assertNotIn("\ue200", normalized["summary"])
        value = deepcopy(RAW)
        value["results"] += value["results"] + [{"url": "javascript:alert(1)"}, {"url": "http://127.0.0.1/a"}]
        self.assertEqual(len(normalize_output(value, args)["sources"]), 1)
        for url in ("javascript:a", "https://user:secret@example.com", "http://localhost/a", "http://[::1]/a", "data:text/plain,a", "https://example.com:bad/x", "https://example.com/\nx", "http://127.1/x"):
            self.assertIsNone(public_url(url))
        self.assertEqual(public_url("https://example.com/docs#section"), "https://example.com/docs#section")

    def test_easy_input_aliases_and_legacy_fresh_never_disable_live_search(self):
        for args in ("docs", '"docs"', '{"query":"docs"}', {"q": "docs"}, {"queries": ["docs"]}, {"search_query": [{"q": "docs"}]}):
            self.assertEqual(normalize_input(args)["queries"], [{"query": "docs"}])
        args = normalize_input({"query": "docs", "domains": "https://example.com/docs", "recency": "7", "max_results": "40", "fresh": False})
        self.assertEqual(args["queries"][0], {"query": "docs", "domains": ["example.com"], "recency_days": 7})
        self.assertEqual(args["max_results"], 40)
        self.assertTrue(args["fresh"])
        with self.assertRaises(HubError) as error:
            normalize_input({"query": "docs", "max_results": 0})
        self.assertEqual(error.exception.param, "max_results")
        self.assertTrue(error.exception.suggestion)

    def test_no_cache_or_coalescing_for_identical_sequential_and_concurrent_calls(self):
        nonces, lock = [], threading.Lock()
        barrier = threading.Barrier(4)
        def request(url, key, body=None, **kwargs):
            if url.endswith("/models"):
                return 200, {"data": [{"id": MODEL}]}
            self.assertTrue(body["settings"]["external_web_access"])
            self.assertEqual(kwargs["headers"]["Cache-Control"], "no-cache")
            with lock:
                nonces.append(body["id"])
            if key == "sk-concurrent":
                barrier.wait(timeout=3)
            return 200, RAW
        with patch("internal_tools.web_search.request_json", side_effect=request):
            sequential = WebSearchExecutor("sk-user")
            results = [sequential({"query": "docs", "fresh": False}, str(i)) for i in range(3)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                results += list(pool.map(lambda i: WebSearchExecutor("sk-concurrent")({"query": "docs"}, str(i)), range(4)))
        self.assertEqual(len(nonces), 7)
        self.assertEqual(len(set(nonces)), 7)
        for result in results:
            self.assertFalse(result["cached"])
            self.assertEqual(result["cache_status"], "disabled")
            self.assertEqual(result["usage"]["search_calls"], 1)
            self.assertEqual(result["mode"], "live")
            self.assertTrue(result["searched_at"].endswith("Z"))
            self.assertIn("completed_at", result["output"]["metadata"])

    def test_full_extracts_sources_dates_and_explicit_return_limits(self):
        args = normalize_input({"query": "docs"})
        output = "\n\n".join(f"Docs {i} (https://example.com/{i})\n\ue200cite\ue202turn0search{i}\ue201 [wordlim: 200] Published: 2026-10-20; Crawled: today;\n"+"正文"*2000 for i in range(15))
        raw = {"output": output, "results": [{"ref_id": f"turn0search{i}", "title": f"Docs {i}", "url": f"https://example.com/{i}", "snippet": "摘要"} for i in range(15)]}
        result = normalize_output(raw, args)
        self.assertEqual(len(result["sources"]), 15)
        self.assertGreater(len(result["summary"]), 16000)
        self.assertEqual(result["metadata"]["total_sources"], 15)
        self.assertEqual(result["sources"][0]["published"], "2026-10-20")
        self.assertEqual(result["sources"][0]["crawled"], "today")
        self.assertTrue(result["sources"][0]["content"].startswith("正文"))
        self.assertEqual(len(result["sources"][0]["content"]), 4000)
        self.assertTrue(result["metadata"]["truncated"])
        self.assertIn("source_content", result["metadata"]["truncated_fields"])
        limited = normalize_output(raw, {**args, "max_results": 2})
        self.assertEqual(len(limited["sources"]), 2)
        self.assertEqual(limited["metadata"]["total_sources"], 15)
        self.assertIn("sources", limited["metadata"]["truncated_fields"])
        huge = normalize_output({"output": "字"*200000, "results": []}, args)
        self.assertLessEqual(len(huge["summary"].encode()), 256*1024)
        self.assertIn("summary", huge["metadata"]["truncated_fields"])

    def test_partial_upstream_recovery_and_malformed_result(self):
        args = normalize_input({"query": "docs"})
        for raw in ({"results": RAW["results"]}, {"results": {"turn0search0": RAW["results"][0]}}):
            result = normalize_output(raw, args)
            self.assertIn("Source extract", result["summary"])
            self.assertEqual(len(result["sources"]), 1)
            self.assertTrue(result["metadata"]["warnings"])
        self.assertTrue(normalize_output({"output": "", "results": []}, args)["metadata"]["warnings"])
        with self.assertRaises(HubError):
            normalize_output({"encrypted_output": "opaque-only"}, args)

    def test_gateway_identity_billing_revocation_and_forwarded_ip(self):
        def request(url, key, body=None, **kwargs):
            self.assertEqual(key, "sk-user")
            self.assertEqual(kwargs["headers"]["X-Real-IP"], "203.0.113.1")
            if url.endswith("/models"):
                return 200, {"data": [{"id": MODEL}]}
            self.assertTrue(url.endswith("/v1/alpha/search"))
            self.assertEqual(body["commands"]["search_query"], [{"q": "docs", "domains": ["example.com"], "recency": 7}])
            return 200, RAW
        execute = WebSearchExecutor("sk-user", headers={"X-Real-IP": "203.0.113.1"})
        args = {"query": "docs", "domains": ["example.com"], "recency_days": 7}
        with patch("internal_tools.web_search.request_json", side_effect=request) as req:
            execute(args, "one")
            execute(args, "two")
            self.assertEqual(sum(c.args[0].endswith("/alpha/search") for c in req.call_args_list), 2)
        with patch("internal_tools.web_search.request_json", side_effect=HubError(401, "revoked")), self.assertRaises(HubError):
            execute(args, "three")

    def test_failed_billable_post_is_never_automatically_retried(self):
        with patch("internal_tools.web_search.request_json", side_effect=[(200, {"data": [{"id": MODEL}]}), HubError(504, "timeout")]) as request:
            with self.assertRaises(HubError):
                WebSearchExecutor("sk-user")({"query": "docs"}, "one")
        self.assertEqual(len(request.call_args_list), 2)  # One auth GET and exactly one search POST.
        for key in ("模型名称", "sk-token with spaces", ""):
            with patch("internal_tools.web_search.request_json") as request, self.assertRaises(HubError) as error:
                WebSearchExecutor(key)({"query": "docs"}, "one")
            self.assertEqual(error.exception.status, 401)
            request.assert_not_called()

    def test_only_transient_permission_reads_are_retried(self):
        with patch("internal_tools.web_search.request_json", side_effect=[HubError(503, "cold catalogue"), (200, {"data": [{"id": MODEL}]}), (200, RAW)]) as request:
            result = WebSearchExecutor("sk-user")({"query": "docs"}, "one")
        self.assertEqual(result["usage"]["search_calls"], 1)
        self.assertEqual(sum(call.args[0].endswith("/alpha/search") for call in request.call_args_list), 1)
        with patch("internal_tools.web_search.request_json", side_effect=HubError(403, "forbidden")) as request, self.assertRaises(HubError):
            WebSearchExecutor("sk-user")({"query": "docs"}, "one")
        self.assertEqual(request.call_count, 1)

    def test_jwt_uses_private_authenticated_route(self):
        with patch("internal_tools.web_search.request_json", side_effect=[(200, {"data": [{"id": MODEL}]}), (200, RAW)]) as req:
            WebSearchExecutor("jwt-test")({"query": "docs"}, "one")
            self.assertTrue(req.call_args_list[1].args[0].endswith("/api/v1/chat/alpha/search"))

    def test_search_helper_falls_back_only_within_allowed_models(self):
        with patch("internal_tools.web_search.request_json", side_effect=[(200, {"data": [{"id": "gpt-6.1-sol"}]}), (200, RAW)]) as req:
            WebSearchExecutor("sk-restricted-sol")({"query": "docs"}, "one")
            self.assertEqual(req.call_args_list[1].args[2]["model"], "gpt-6.1-sol")
        with patch("internal_tools.web_search.request_json", return_value=(200, {"data": [{"id": "qwen3.8-flash"}]})), self.assertRaises(HubError) as error:
            WebSearchExecutor("sk-restricted-qwen")({"query": "docs"}, "one")
        self.assertEqual(error.exception.status, 403)

    def test_explicit_client_controls_are_preserved(self):
        body = {**BODIES["chat"], "tool_choice": "none"}
        self.assertFalse(search_eligible(body, "chat", "/v1/chat/completions"))
        body = {**BODIES["chat"], "tools": [{"type": "function", "function": {"name": "web_search"}}]}
        self.assertFalse(search_eligible(body, "chat", "/v1/chat/completions"))
        for kind in GENERATORS:
            path = "/v1beta/models/gemini-test:generateContent" if kind == "gemini" else "/v1/chat/completions"
            self.assertTrue(search_eligible(BODIES[kind], kind, path))
            self.assertIn(FUNCTION_NAME, tool_names(inject_search(BODIES[kind], kind), kind))

    def test_protocol_streams_hide_search_preserve_client_tools_and_usage(self):
        for kind, generate in GENERATORS.items():
            for mixed in (False, True):
                with self.subTest(kind=kind, mixed=mixed):
                    emitted, requests = [], []
                    def upstream(body):
                        requests.append(deepcopy(body))
                        values = generate(call=len(requests) == 1, mixed=mixed and len(requests) == 1)
                        values = json.loads(json.dumps(values).replace(IMAGE_NAME, FUNCTION_NAME).replace("generate_image", "web_search").replace('a circle', 'docs').replace('prompt\\"', 'query\\"').replace('"prompt"', '"query"'))
                        return frames(values)
                    def search(args, call_id):
                        return {"id": call_id, "name": "web_search", "status": "completed", "output": normalize_output(RAW, args), "cached": False}
                    execute = ToolExecutor(search=search)
                    value = execute.run(inject_search(BODIES[kind], kind), kind, upstream, Stream(kind, lambda e,v: emitted.append((e,v)), execute.names))
                    self.assertEqual(len(requests), 1 if mixed else 2)
                    events = [v for e,v in emitted if e == "aerisun.tool"]
                    self.assertEqual([x["status"] for x in events], ["running", "completed"])
                    self.assertEqual(events[1]["sources"][0]["url"], "https://example.com/docs")
                    visible = [v for e,v in emitted if e != "aerisun.tool"]
                    self.assertNotIn(FUNCTION_NAME, json.dumps(visible))
                    self.assertNotIn("Useful extract", json.dumps(visible))
                    self.assertEqual(any(name == "Read" for _,_,name in calls(value,kind)), mixed)
                    if not mixed:
                        self.assertIn('example.com/docs', json.dumps(requests[1]))
                        if kind == "gemini":
                            self.assertEqual(requests[1]["contents"][-1]["parts"][0]["functionResponse"]["name"], FUNCTION_NAME)
                    usage = value["usageMetadata" if kind == "gemini" else "usage"]
                    self.assertEqual(usage["promptTokenCount" if kind == "gemini" else "prompt_tokens" if kind == "chat" else "input_tokens"], 10 if mixed else 20)

    def test_search_limit_and_nonstream_structured_output(self):
        invoked, requests = [], []
        def search(args, call_id):
            invoked.append(1)
            return {"status": "completed", "output": normalize_output(RAW, args), "cached": False}
        def upstream(body):
            requests.append(body)
            tools = tool_names(body, "chat")
            value = {"id": "chat", "choices": [{"message": {"role": "assistant", "content": "", **({"tool_calls": [{"id": str(len(requests)), "function": {"name": FUNCTION_NAME, "arguments": '{"query":"docs"}'}}]} if FUNCTION_NAME in tools else {})}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1}}
            return io.BytesIO(json.dumps(value).encode())
        result = ToolExecutor(search=search).run(inject_search(BODIES["chat"], "chat"), "chat", upstream)
        self.assertEqual(len(invoked), 3)
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(result["aerisun_tools"]), 3)
        self.assertEqual(result["usage"]["prompt_tokens"], 4)


class FakeHandler:
    def __init__(self, path, method="POST", key="sk-user"):
        self.path, self.command, self.headers = path, method, {"Authorization": "Bearer "+key}
        self.wfile, self.response_headers = io.BytesIO(), {}
    def send_response(self, status): self.status = status
    def send_header(self, name, value): self.response_headers[name] = value
    def end_headers(self): pass
    def result(self): return json.loads(self.wfile.getvalue())


class AdapterTests(unittest.TestCase):
    @patch("internal_tools.http_api.registry.catalogue", return_value=[DEFINITION])
    def test_errors_are_actionable_in_rest_and_mcp_and_keep_client_ip(self, auth):
        from internal_tools.errors import invalid
        for path, body in (("/v1/tools/web_search", {"query": "docs", "max_results": 0}),
                           ("/v1/tools/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "docs", "max_results": 0}}})):
            handler = FakeHandler(path)
            handler.headers["X-Real-IP"] = "203.0.113.1"
            with patch("internal_tools.http_api.registry.execute") as execute:
                execute.side_effect = invalid("max_results out of range", "max_results")
                http_api.dispatch(handler, "http://gateway", body)
            self.assertEqual(execute.call_args.args[5], {"X-Real-IP": "203.0.113.1"})
            if path.endswith("mcp"):
                auth.assert_called_with("sk-user", "http://gateway", {"X-Real-IP": "203.0.113.1"})
            else:
                auth.assert_not_called()  # The executor performs REST authentication once.
            result = handler.result()
            error = result["result"]["structuredContent"]["error"] if path.endswith("mcp") else result["error"]
            self.assertEqual(error["code"], "invalid_arguments")
            self.assertEqual(error["param"], "max_results")
            self.assertTrue(error["suggestion"])
            self.assertEqual(handler.response_headers["Cache-Control"], "private, no-store")
            if path.endswith("mcp"):
                self.assertTrue(result["result"]["isError"])
                self.assertEqual(handler.status, 200)
            else:
                self.assertEqual(handler.status, 400)

    def test_invalid_json_uses_standard_mcp_parse_error(self):
        handler = FakeHandler("/v1/tools/mcp")
        http_api.invalid_json(handler)
        self.assertEqual(handler.result()["error"]["code"], -32700)
        self.assertIsNone(handler.result()["id"])
        handler = FakeHandler("/v1/tools/web_search")
        http_api.invalid_json(handler)
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.result()["error"]["code"], "invalid_arguments")

    @patch("internal_tools.http_api.registry.catalogue", return_value=[DEFINITION])
    def test_catalog_mcp_and_http_share_the_same_tool(self, auth):
        handler = FakeHandler("/v1/tools", "GET")
        http_api.dispatch(handler, "http://gateway")
        self.assertEqual(handler.result()["data"][0]["name"], "web_search")
        handler = FakeHandler("/v1/tools/mcp")
        http_api.dispatch(handler, "http://gateway", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(handler.result()["result"]["serverInfo"]["name"], "aerisun-internal-tools")
        handler = FakeHandler("/v1/tools/mcp")
        http_api.dispatch(handler, "http://gateway", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(handler.status, 202)
        with patch("internal_tools.http_api.registry.execute") as execute:
            execute.return_value = {"output": {"summary": "ok", "sources": []}, "cached": False}
            for path, body in [("/tools/web_search", {"query": "docs"}), ("/v1/tools/call", {"name": "web_search", "arguments": {"query": "docs"}}), ("/v1/tools/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "docs"}}})]:
                handler = FakeHandler(path)
                http_api.dispatch(handler, "http://gateway", body)
                self.assertEqual(handler.status, 200)
                self.assertNotIn("encrypted", json.dumps(handler.result()))
                if path.endswith("mcp"):
                    self.assertFalse(handler.result()["result"]["isError"])
                    self.assertIn("structuredContent", handler.result()["result"])
                else:
                    self.assertEqual(handler.response_headers["Cache-Status"], "AerisunSearch; fwd=bypass")

    def test_mcp_auth_failures_are_http_auth_failures(self):
        with patch("internal_tools.http_api.registry.catalogue", side_effect=HubError(401, "invalid")):
            handler = FakeHandler("/v1/tools/mcp")
            http_api.dispatch(handler, "http://gateway", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(handler.status, 401)
            self.assertEqual(handler.response_headers["WWW-Authenticate"], "Bearer")


if __name__ == "__main__":
    unittest.main()
