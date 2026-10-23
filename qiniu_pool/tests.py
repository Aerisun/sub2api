from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from urllib.request import Request, build_opener, ProxyHandler

from qiniu_pool.server import Pool, Server, Unavailable


class PoolTests(unittest.TestCase):
    def config(self, **changes):
        return dict(capacity=9, per_key=1, model_capacity=9, global_rpm=0,
                    model_rpm=0, queue_seconds=0, **changes)

    def test_balances_keys_and_releases_once(self):
        p = Pool([str(i) for i in range(9)], self.config())
        leases = [p.acquire("model", str(i)) for i in range(9)]
        self.assertEqual(len({l.index for l in leases}), 9)
        with self.assertRaises(Unavailable):
            p.acquire("model")
        for l in leases:
            p.release(l, "model", True)
            p.release(l, "model", True)
        self.assertEqual(p.snapshot()["inflight"], 0)
        self.assertEqual(sum(m["completed"] for m in p.snapshot()["members"]), 9)

    def test_affinity_is_stable_but_capacity_can_spill(self):
        p = Pool(["one", "two"], self.config())
        first = p.acquire("model", "conversation")
        index = first.index
        p.release(first, "model", True)
        second = p.acquire("model", "conversation")
        self.assertEqual(second.index, index)
        third = p.acquire("model", "conversation")
        self.assertNotEqual(second.index, third.index)
        p.release(second, "model", True)
        p.release(third, "model", True)
        fourth = p.acquire("model", "conversation")
        self.assertEqual(fourth.index,index)
        p.release(fourth,"model",True)

    def test_shared_rpm_is_not_multiplied_by_keys(self):
        now = [100.]
        config = self.config()
        config["model_rpm"] = 5
        p = Pool([str(i) for i in range(9)], config, clock=lambda: now[0])
        for _ in range(5):
            lease = p.acquire("model")
            p.release(lease, "model", True)
        with self.assertRaises(Unavailable):
            p.acquire("model")
        other = p.acquire("other")
        p.release(other, "other", True)
        now[0] += 60.1
        p.release(p.acquire("model"), "model", True)

    def test_429_cools_all_keys_for_affected_model(self):
        p = Pool(["one", "two"], self.config())
        lease = p.acquire("model")
        p.error(lease, "model", 429, 30)
        p.release(lease, "model", False)
        with self.assertRaises(Unavailable):
            p.acquire("model")
        p.release(p.acquire("other"), "other", True)

    def test_policy_denial_does_not_disable_or_rotate_keys(self):
        p = Pool(["one", "two"], self.config())
        lease = p.acquire("model")
        p.error(lease, "model", 403)
        p.release(lease, "model", False)
        self.assertFalse(any(m["disabled"] for m in p.snapshot()["members"]))

    def test_parallel_admission_never_exceeds_limits(self):
        config = self.config()
        config.update(capacity=4, per_key=1, model_capacity=4)
        p = Pool([str(i) for i in range(9)], config)
        barrier = threading.Barrier(25)
        admitted = []
        lock = threading.Lock()
        def run(i):
            barrier.wait()
            try:
                lease = p.acquire("model", str(i))
            except Unavailable:
                return
            with lock:
                admitted.append(lease.index)
            time.sleep(.08)
            p.release(lease, "model", True)
        with ThreadPoolExecutor(max_workers=24) as ex:
            futures = [ex.submit(run, i) for i in range(24)]
            barrier.wait()
            for f in futures:
                f.result()
        self.assertEqual(len(admitted), 4)
        self.assertEqual(p.snapshot()["peak"], 4)
        self.assertEqual(p.snapshot()["inflight"], 0)

    def test_fingerprint_keeps_session_and_does_not_keep_history(self):
        p = Pool(["one"], self.config())
        first = {"model": "model", "metadata": {"session_id": "session"}, "messages": [{"role":"user","content":"first"}]}
        second = {**first, "messages": first["messages"]+[{"role":"assistant","content":"next"}]}
        self.assertEqual(p.fingerprint(first, {}), p.fingerprint(second, {}))
        self.assertNotIn("session", p.fingerprint(first, {}))

    def test_moving_cache_markers_and_protocol_text_shape_keep_the_same_binding(self):
        p=Pool(['one'],self.config())
        first={'model':'model','messages':[{'role':'system','content':'Stable instructions'},
            {'role':'user','content':[{'type':'text','text':'First question','cache_control':{'type':'ephemeral'}}]}]}
        later={'model':'model','messages':[{'role':'system','content':[{'type':'text','text':'Stable instructions'}]},
            {'role':'user','content':'First question'},{'role':'assistant','content':'Answer'},
            {'role':'user','content':[{'type':'text','text':'Second question','cache_control':{'type':'ephemeral'}}]}]}
        self.assertEqual(p.fingerprint(first,{}),p.fingerprint(later,{}))
        native={'model':'model','metadata':{'user_id':json.dumps({'session_id':'stable','account_uuid':'first'})}}
        next_turn={'model':'model','metadata':{'user_id':json.dumps({'account_uuid':'second','session_id':'stable'})}}
        self.assertEqual(p.fingerprint(native,{}),p.fingerprint(next_turn,{}))

    def test_restart_preserves_shared_rate_window(self):
        with TemporaryDirectory() as directory:
            config=self.config()
            config.update(model_rpm=1,state_path=str(Path(directory)/"state.json"))
            first=Pool(["one","two"],config)
            first.release(first.acquire("model"),"model",True)
            restarted=Pool(["one","two"],config)
            with self.assertRaises(Unavailable):
                restarted.acquire("model")

    def test_routes_balance_conversations_and_restore_cache_affinity(self):
        with TemporaryDirectory() as directory:
            config = self.config()
            config.update(models=["paid", "free"], routes={"paid":[{"model":"paid"},{"model":"free"}]},
                          state_path=str(Path(directory)/"state.json"))
            p = Pool(["one", "two"], config)
            counts = {"paid":0,"free":0}
            for i in range(1000):
                fp = p.fingerprint({"model":"paid","conversation_id":str(i)}, {})
                counts[p.route_plan("paid", fp)[0]] += 1
            self.assertTrue(420 < counts["free"] < 580, counts)
            body = {"model":"paid","conversation_id":"stable"}
            fp = p.fingerprint(body, {})
            fallback = p.route_plan("paid",fp)[1]
            lease = p.acquire(fallback, fp)
            p.release(lease, fallback, True)
            p.remember_route("paid", fp, fallback)
            restarted = Pool(["one", "two"],config)
            self.assertEqual(restarted.fingerprint(body,{}),fp)
            self.assertEqual(restarted.route_plan("paid",fp)[0],fallback)
            again = restarted.acquire(fallback,fp)
            self.assertEqual(again.index,lease.index)
            restarted.release(again,fallback,True)
            self.assertEqual(restarted.route_plan("free",fp),["free"])


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        cls.upstream_error = None
        cls.upstream_sequence = []
        cls.thinking_gate = None
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                cls.received.append({"raw":raw,"authorization":self.headers.get("Authorization")})
                body = json.loads(raw)
                error=cls.upstream_sequence.pop(0) if cls.upstream_sequence else cls.upstream_error
                if error:
                    status, content = error
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if cls.thinking_gate is not None:
                        self.wfile.write(b'data: {"choices":[{"delta":{"reasoning_content":"first thought"},"finish_reason":null}]}\n\n')
                        self.wfile.flush()
                        cls.thinking_gate.wait(4)
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n')
                    self.wfile.flush()
                    time.sleep(.03)
                    self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n')
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')
        cls.upstream = ThreadingHTTPServer(("127.0.0.1",0),Upstream)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        type(self).upstream_error = None
        type(self).upstream_sequence = []
        type(self).thinking_gate = None
        self.received.clear()
        self.relay = Server(("127.0.0.1",0),{"models":["model"],"capacity":4,"per_key":1,"model_rpm":0,"global_rpm":0},["provider-one","provider-two"],"local-only",f"http://127.0.0.1:{self.upstream.server_port}")
        self.worker = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.worker.start()
        self.port = self.relay.server_port

    def tearDown(self):
        self.relay.shutdown()
        self.relay.server_close()

    def send(self, body, chunked=False, auth="local-only"):
        conn = http.client.HTTPConnection("127.0.0.1",self.port, timeout=5)
        headers={"Authorization":"Bearer "+auth,"Content-Type":"application/json"}
        conn.request("POST","/v1/chat/completions",iter([body[:15],body[15:]]) if chunked else body,headers,encode_chunked=chunked)
        response=conn.getresponse()
        self.response_headers = dict(response.getheaders())
        status, raw=response.status,response.read()
        conn.close()
        return status,raw

    def test_raw_fields_chunked_input_and_usage_survive(self):
        raw=b'{ "model":"model", "stream":true, "reasoning_effort":"max", "thinking":{"type":"enabled","budget_tokens":1234}, "tools":[],"metadata":{"session_id":"unique"} }'
        status,data=self.send(raw,chunked=True)
        self.assertEqual(status,200)
        self.assertEqual(self.received[0]["raw"],raw)
        self.assertEqual(self.received[0]["authorization"],"Bearer provider-one")
        self.assertIn(b'"prompt_tokens":3',data)
        self.assertIn(b'data: [DONE]',data)

    def test_kimi_keeps_local_affinity_without_sending_private_routing_fields(self):
        self.check_kimi_fields("moonshotai/kimi-k3")

    def test_kimi_free_keeps_native_cache_and_reasoning(self):
        self.check_kimi_fields("moonshotai/kimi-k3-free")

    def check_kimi_fields(self, model):
        self.relay.models.add(model)
        body={"model":model,"stream":False,
              "messages":[{"role":"user","content":"hello"}],
              "prompt_cache_key":"same-conversation","metadata":{"user_id":"local-session"},
              "conversation_id":"local-conversation","reasoning_effort":"max",
              "max_completion_tokens":1024,"prompt_cache_options":{"mode":"implicit","ttl":"1h"}}
        for _ in range(2):
            status,_=self.send(json.dumps(body).encode())
            self.assertEqual(status,200)
        native=json.loads(self.received[0]["raw"])
        self.assertNotIn("prompt_cache_key",native)
        self.assertNotIn("metadata",native)
        self.assertNotIn("conversation_id",native)
        self.assertEqual(native["reasoning_effort"],"max")
        self.assertEqual(native["prompt_cache_options"],{"mode":"implicit","ttl":"1h"})
        self.assertEqual(native["max_tokens"],1024)
        self.assertNotIn("max_completion_tokens",native)
        self.assertEqual(self.received[0]["authorization"],self.received[1]["authorization"])
        self.assertEqual(self.relay.pool.snapshot()["affinity_hits"],1)
        self.assertEqual(self.relay.pool.snapshot()["inflight"],0)

    def enable_routes(self):
        self.relay.models.add("free")
        self.relay.pool.routes = {"model":[{"model":"model"},{"model":"free"}]}

    def test_fallback_is_sticky_and_reports_physical_model(self):
        self.enable_routes()
        body = {"model":"model","conversation_id":"conversation"}
        fp = self.relay.pool.fingerprint(body,{})
        first, fallback = self.relay.pool.route_plan("model",fp)
        type(self).upstream_sequence = [(503,b'{"error":{"message":"unavailable"}}'),None]
        self.assertEqual(self.send(json.dumps(body).encode())[0],200)
        self.assertEqual([json.loads(row["raw"])["model"] for row in self.received],[first,fallback])
        self.assertEqual(self.response_headers["X-Sub2api-Upstream-Model"],fallback)
        self.assertEqual(self.relay.pool.snapshot()["inflight"],0)
        self.assertTrue(all(n == 0 for n in self.relay.pool.model_inflight.values()))
        self.assertEqual(self.send(json.dumps(body).encode())[0],200)
        self.assertEqual(json.loads(self.received[-1]["raw"])["model"],fallback)

    def test_explicit_free_limit_never_falls_back_to_paid(self):
        self.enable_routes()
        type(self).upstream_error=(429,b'{"error":{"message":"rate limit"}}')
        self.assertEqual(self.send(b'{"model":"free"}')[0],429)
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.send(b'{"model":"free"}')[0],429)
        self.assertEqual(len(self.received),1)

    def test_policy_error_never_falls_back_to_another_model(self):
        self.enable_routes()
        type(self).upstream_error=(403,b'{"error":{"message":"policy"}}')
        self.assertEqual(self.send(b'{"model":"model"}')[0],403)
        self.assertEqual(len(self.received),1)

    def test_thought_is_received_before_final_answer_is_released(self):
        gate = type(self).thinking_gate = threading.Event()
        conn = http.client.HTTPConnection("127.0.0.1",self.port,timeout=2)
        try:
            conn.request("POST","/v1/chat/completions",b'{"model":"model","stream":true}',
                         {"Authorization":"Bearer local-only","Content-Type":"application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status,200)
            self.assertIn(b'"reasoning_content":"first thought"',response.readline())
            self.assertFalse(gate.is_set())
            gate.set()
            self.assertIn(b'data: [DONE]',response.read())
        finally:
            gate.set()
            conn.close()

    def test_auth_does_not_reach_provider(self):
        self.assertEqual(self.send(b'{"model":"model"}',auth="wrong")[0],401)
        self.assertFalse(self.received)

    def test_removed_or_unknown_model_does_not_reach_provider(self):
        self.assertEqual(self.send(b'{"model":"unknown"}')[0],404)
        self.assertFalse(self.received)

    def test_empty_200_stream_is_a_real_502(self):
        type(self).upstream_error=(200,b'')
        self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],502)
        self.assertEqual(self.relay.pool.snapshot()["inflight"],0)

    def test_comment_only_stream_is_not_a_success(self):
        type(self).upstream_error=(200,b': keepalive\n\n')
        self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],502)

    def test_policy_error_is_not_retried(self):
        type(self).upstream_error=(403,b'{"error":{"message":"Safety policy rejected request"}}')
        self.assertEqual(self.send(b'{"model":"model"}')[0],403)
        self.assertEqual(len(self.received),1)

    def test_secrets_are_redacted_in_provider_errors(self):
        type(self).upstream_error=(400,b'{"error":{"message":"bad sk-supersecret01234567890123456789"}}')
        status,data=self.send(b'{"model":"model"}')
        self.assertEqual(status,400)
        self.assertNotIn(b'sk-supersecret',data)

    def test_bounded_failover_before_stream_only(self):
        type(self).upstream_error=(503,b'{"error":{"message":"unavailable"}}')
        self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],503)
        self.assertEqual(len(self.received),2)
        self.assertEqual(self.relay.pool.snapshot()["inflight"],0)

    def test_expired_provider_key_does_not_reject_customer_auth(self):
        type(self).upstream_sequence=[(401,b'{"error":{"message":"Invalid provider key"}}'),None]
        self.assertEqual(self.send(b'{"model":"model"}')[0],200)
        self.assertEqual(len(self.received),2)
        self.assertEqual(self.relay.pool.snapshot()["available_keys"],1)
        self.assertEqual(self.relay.pool.snapshot()["inflight"],0)


if __name__ == "__main__":
    unittest.main()
