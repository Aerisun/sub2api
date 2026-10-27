"""Fault regressions for image prefill, route checks and no partial replay."""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import time
from unittest.mock import patch
import unittest

import httpx

from nvidia_pool.server import NvidiaServer
from nvidia_pool.tests import NvidiaRelayTests
from nvidia_pool import egress


class VisionRelayTests(NvidiaRelayTests):
    def test_completed_images_reuse_healthy_upstream_connection_after_early_return(self):
        self.vision_policy();ports=[];drained=threading.Event()
        payload=(b'data: {"choices":[{"delta":{"content":"657952"}}]}\n\n'
                 b'data: {"choices":[{"finish_reason":"stop"}],"usage":{"total_tokens":12}}\n\n'
                 b'data: [DONE]\n\n')
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));ports.append(self.client_address[1])
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);self.wfile.flush()
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        self.relay.upstream=f'http://127.0.0.1:{upstream.server_port}'
        native_drain=self.relay.drain_completed
        def drain(*args):
            native_drain(*args);drained.set()
        self.relay.drain_completed=drain
        body=json.dumps({'model':'model','stream':True,'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}).encode()
        try:
            for _ in range(2):
                drained.clear();status,raw=self.send(body)
                self.assertEqual(status,200);self.assertIn(b'657952',raw);self.assertTrue(drained.wait(1))
            self.assertEqual(len(ports),2);self.assertEqual(ports[0],ports[1])
            self.assertEqual(self.relay.pool.inflight,0)
        finally:upstream.shutdown();upstream.server_close();thread.join(2)

    def test_completed_image_releases_lease_without_waiting_for_provider_eof_or_done(self):
        self.vision_policy()
        class CompleteButOpen(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"657952"}}]}\n\n'
                yield (b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                       b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n')
                time.sleep(1)
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=CompleteButOpen())
            try:yield response
            finally:response.close()
        body=json.dumps({'model':'model','stream':True,'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}).encode()
        start=time.monotonic()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,raw=self.send(body)
        self.assertEqual(status,200);self.assertIn(b'657952',raw)
        self.assertIn(b'"total_tokens":12',raw);self.assertEqual(raw.count(b'data: [DONE]'),1)
        self.assertLess(time.monotonic()-start,.5);self.assertEqual(calls.call_count,1)
        self.assertEqual(self.relay.pool.inflight,0)

    def vision_policy(self):
        self.enable_nvidia_policy()
        self.relay.first_event_seconds=.04
        self.relay.vision_first_event_seconds=.4
        self.relay.vision_pre_output_timeout=.8
        self.relay.vision_read_timeout=.4
        for name in ('request_policy','request_options','first_event_ready'):
            # These methods use super(), so delegate to an actual policy
            # object while retaining the real HTTP relay and its scheduler.
            policy=object.__new__(NvidiaServer)
            policy.__dict__.update(self.relay.__dict__)
            setattr(self.relay,name,getattr(policy,name))

    def test_reasoning_only_private_image_can_recover_before_any_output(self):
        self.vision_policy()
        self.relay.request_policy.__self__.vision_first_event_seconds=.1
        attempts=[]
        class ReasoningOnly(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"private verification"}}]}\n\n'
                time.sleep(.12)
                yield b'data: {"choices":[{"delta":{"content":"657952"},"finish_reason":"stop"}],"usage":{"total_tokens":12}}\n\n'
                yield b'data: [DONE]\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            attempts.append(1)
            if len(attempts) in (1,3):data=ReasoningOnly()
            else:data=httpx.ByteStream(
                b'data: {"choices":[{"delta":{"content":"657952"},"finish_reason":"stop"}],"usage":{"total_tokens":12}}\n\n'
                b'data: [DONE]\n\n')
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=data)
            try:yield response
            finally:response.close()
        value={'model':'model','stream':True,'metadata':{'aerisun_vision_helper':True},'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}
        with patch('nvidia_pool.server.PRIVATE_VISION_MODELS',frozenset({'model'})),patch.object(self.relay.client,'stream',side_effect=stream):
            status,raw=self.send(json.dumps(value).encode())
            self.assertEqual(status,504);self.assertNotIn(b'private verification',raw);self.assertEqual(len(attempts),1)
            status,raw=self.send(json.dumps(value).encode())
            self.assertEqual(status,200);self.assertIn(b'657952',raw);self.assertEqual(len(attempts),2)
            # A public native caller retains its existing reasoning stream.
            value['metadata']={}
            status,raw=self.send(json.dumps(value).encode())
            self.assertEqual(status,200);self.assertIn(b'private verification',raw);self.assertIn(b'657952',raw)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_silent_image_prefill_longer_than_text_budget_completes_once(self):
        self.vision_policy()
        class SlowImage(httpx.SyncByteStream):
            def __iter__(self):
                time.sleep(.12)
                yield b'data: {"choices":[{"delta":{"content":"657952"}}]}\n\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            self.assertGreaterEqual(kwargs['timeout'].read,.3)
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=SlowImage())
            try:yield response
            finally:response.close()
        body={'model':'model','stream':True,'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,raw=self.send(json.dumps(body).encode())
        self.assertEqual(status,200);self.assertIn(b'657952',raw);self.assertIn(b'[DONE]',raw)
        self.assertEqual(calls.call_count,1);self.assertEqual(self.relay.pool.inflight,0)

    def test_plain_text_keeps_existing_short_budget(self):
        self.vision_policy()
        self.assertEqual(self.relay.request_policy({'messages':[{'content':'text'}]})['first_event_seconds'],.04)
        self.assertEqual(self.relay.request_options(time.monotonic()+1,{'messages':[]})['timeout'].read,.25)

    def test_actual_private_vision_models_use_configured_image_deadlines(self):
        self.vision_policy()
        for model in ('nvidia/nemotron-3.5-super-vl-120b-a12b',
                      'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'):
            body={'model':model,'metadata':{'aerisun_vision_helper':True},'messages':[
                {'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}
            with self.subTest(model=model):
                policy=self.relay.request_policy(body)
                self.assertEqual(policy['pre_output_timeout'],.4)
                self.assertEqual(policy['first_event_seconds'],.4)
                self.assertEqual(self.relay.request_options(time.monotonic()+1,body)['timeout'].read,.4)

    def test_tool_owns_recovery_after_one_silent_first_event_budget(self):
        self.vision_policy()
        self.relay.request_policy.__self__.vision_first_event_seconds=.1
        attempts=[]
        class Silent(httpx.SyncByteStream):
            def __iter__(self):
                time.sleep(.12);yield b': keepalive\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            attempts.append(1)
            if len(attempts)==1:
                response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Silent())
            else:
                response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=httpx.ByteStream(
                    b'data: {"choices":[{"delta":{"content":"657952"}}]}\n\n'
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'))
            try:yield response
            finally:response.close()
        body=json.dumps({'model':'model','stream':True,'metadata':{'aerisun_vision_helper':True},
            'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]}]}).encode()
        with patch('nvidia_pool.server.PRIVATE_VISION_MODELS',frozenset({'model'})),patch.object(self.relay.client,'stream',side_effect=stream):
            first,_=self.send(body)
            self.assertEqual(first,504);self.assertEqual(len(attempts),1)
            second,raw=self.send(body)
            self.assertEqual(second,200);self.assertIn(b'657952',raw);self.assertEqual(len(attempts),2)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_vision_deadline_still_bounds_comment_only_stream(self):
        self.vision_policy()
        self.relay.request_policy=lambda _:dict(pre_output_timeout=.09,response_timeout=1,first_event_seconds=.07)
        class Comments(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(100):
                    time.sleep(.02);yield b': keepalive\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Comments())
            try:yield response
            finally:response.close()
        start=time.monotonic()
        with patch.object(self.relay.client,'stream',side_effect=stream):
            status,_=self.send(b'{"model":"model","stream":true}')
        self.assertEqual(status,504);self.assertLess(time.monotonic()-start,.4)
        self.assertEqual(self.relay.pool.inflight,0)


class VisionEgressTests(unittest.TestCase):
    def test_model_failure_requires_actual_vision_success_before_promoting_backup(self):
        class Client:
            def __init__(self):self.switches=[];self.current='old'
            def request(self,path,method='GET',body=None):
                if method=='PUT':self.switches.append(body['name']);self.current=body['name'];return {}
                return {'now':self.current,'all':['old','fast-but-broken','vision-good']}
        @contextmanager
        def isolated(nodes,config):yield None,nodes[0]['name']
        client=Client()
        cfg={'role':{'group':'NVIDIA egress','provider_file':'fixture'}}
        with patch.object(egress,'delay',side_effect=lambda _,name:1 if name=='fast-but-broken' else 2), \
             patch.object(egress,'read_yaml',return_value={'proxies':[{'name':'fast-but-broken'},{'name':'vision-good'}]}), \
             patch.object(egress,'isolated_proxy',side_effect=isolated), \
             patch.object(egress.Path,'read_text',return_value='{"keys":["fixture"]}'), \
             patch.object(egress,'generation',side_effect=lambda proxy,*_,**kw:{'ok':proxy=='vision-good'}) as generation:
            self.assertTrue(egress.failover(cfg,client,model_failure=True))
        self.assertEqual(client.switches,['vision-good'])
        self.assertTrue(all(call.kwargs.get('vision') for call in generation.call_args_list))
