import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import httpx

from nvidia_pool.assets import ImageAssets,AssetCapacityError
from nvidia_pool.server import NvidiaServer,sanitized
from nvidia_pool import tests as relay_tests


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.raw=Path(__file__).with_name('fixtures').joinpath('vision-health.png').read_bytes()
        self.url='data:image/png;base64,'+base64.b64encode(self.raw).decode()
        self.body={'model':'model','messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':self.url,'detail':'high'}} for _ in range(4)]}]}
        self.assets=ImageAssets('https://example.com/_vision-assets/',threshold=1)

    def test_request_deduplicates_pixels_only_within_its_own_lifetime(self):
        original=deepcopy(self.body)
        with self.assets.request(self.body) as first:
            urls=[p['image_url']['url'] for p in first['messages'][0]['content']]
            self.assertEqual(len(set(urls)),1)
            self.assertEqual(self.assets.snapshot()['active_bytes'],len(self.raw))
            self.assertEqual(self.body,original)
            self.assertTrue(all(p['image_url']['detail']=='high' for p in first['messages'][0]['content']))
            with self.assets.request(self.body) as second:
                self.assertNotEqual(urls[0],second['messages'][0]['content'][0]['image_url']['url'])
                self.assertEqual(self.assets.snapshot()['active_images'],2)
        self.assertEqual(self.assets.snapshot()['active_bytes'],0)

    def test_provider_error_cannot_expose_an_image_capability(self):
        token='a'*43
        self.assertNotIn(token,sanitized('Unable to load https://example.com/_vision-assets/'+token))
        self.assertNotIn(token,sanitized('Unable to load /v1/vision-assets/'+token))

    def test_step_keeps_exact_inline_pixels_and_only_glm_uses_asset_urls(self):
        server=object.__new__(NvidiaServer);server.image_assets=self.assets
        step={**self.body,'model':'stepfun-ai/step-5-preview'}
        with server.upload_context(step) as body:
            self.assertIs(body,step);self.assertEqual(self.assets.snapshot()['active_images'],0)
        glm={**self.body,'model':'z-ai/glm-5.3-flash'}
        with server.upload_context(glm) as body:
            self.assertTrue(body['messages'][0]['content'][0]['image_url']['url'].startswith(self.assets.origin))
            self.assertEqual(self.assets.snapshot()['active_images'],1)
        self.assertEqual(self.assets.snapshot()['active_bytes'],0)

    def test_http_fetch_preserves_exact_bytes_head_and_revocation(self):
        assets=self.assets
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):assets.serve(self)
            def do_HEAD(self):assets.serve(self,head=True)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with httpx.Client(trust_env=False) as c:
                with assets.request(self.body) as body:
                    token=body['messages'][0]['content'][0]['image_url']['url'].rsplit('/',1)[1]
                    url=f'http://127.0.0.1:{server.server_port}/v1/vision-assets/'+token
                    r=c.get(url);self.assertEqual(r.status_code,200);self.assertEqual(r.content,self.raw)
                    self.assertIn('no-store',r.headers['cache-control'])
                    self.assertEqual(c.head(url).headers['content-length'],str(len(self.raw)))
                    self.assertEqual(c.get(url+'?guess').status_code,404)
                self.assertEqual(c.get(url).status_code,404)
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_exception_revokes_all_capabilities(self):
        with self.assertRaises(RuntimeError):
            with self.assets.request(self.body):raise RuntimeError('fixture')
        self.assertEqual(self.assets.snapshot()['active_images'],0)

    def test_deadline_expires_an_abandoned_owner_without_retaining_pixels(self):
        now=[0.];assets=ImageAssets('https://example.com/',threshold=1,ttl=3,clock=lambda:now[0])
        with assets.request(self.body):
            now[0]=3.1
            self.assertEqual(assets.snapshot()['active_bytes'],0)
        self.assertEqual(assets.bytes,0)

    def test_capacity_is_bounded_and_rejection_keeps_existing_owner_intact(self):
        assets=ImageAssets('https://example.com/',threshold=1,max_bytes=len(self.raw))
        with assets.request(self.body):
            with self.assertRaises(AssetCapacityError):
                with assets.request(self.body):pass
            self.assertEqual(assets.snapshot()['active_bytes'],len(self.raw))
        self.assertEqual(assets.bytes,0)

    def test_small_requests_and_remote_urls_keep_the_original_path(self):
        assets=ImageAssets('https://example.com/',threshold=1000000)
        with assets.request(self.body) as body:self.assertIs(body,self.body)
        body={'messages':[{'content':[{'type':'image_url','image_url':{'url':'https://example.com/image.png'}}]}]}
        with self.assets.request(body) as result:self.assertIs(body,result)

    def test_invalid_image_urls_do_not_crash_or_publish_capabilities(self):
        # An otherwise valid large inline image activates the URL conversion;
        # malformed sibling fields must survive for normal input validation.
        for value in [{},[],None,'data:image/png;base64,====']:
            body=deepcopy(self.body)
            body['messages'][0]['content'].append({'type':'image_url','image_url':{'url':value}})
            with self.assets.request(body) as result:
                self.assertEqual(result['messages'][0]['content'][-1]['image_url']['url'],value)
                self.assertEqual(self.assets.snapshot()['active_images'],1)
            self.assertEqual(self.assets.snapshot()['active_bytes'],0)


class AssetRelayTests(relay_tests.NvidiaRelayTests):
    def test_nvidia_accepts_image_envelopes_above_the_old_32_mib_limit(self):
        self.enable_nvidia_policy()
        self.relay.max_request_body=NvidiaServer.max_request_body
        raw=json.dumps({'model':'model','fixture_padding':'a'*(33*1024*1024)}).encode()
        status,value=self.send(raw)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(value)['choices'][0]['message']['content'],'OK')
        self.assertEqual(len(self.received),1)

    def setup_assets(self,max_bytes=1024):
        self.enable_nvidia_policy()
        self.assets=ImageAssets('https://example.com/_vision-assets/',threshold=1,max_bytes=max_bytes)
        self.relay.upload_context=self.assets.request
        self.relay.local_request_error=NvidiaServer.local_request_error.__get__(self.relay)
        self.body={'model':'model','stream':True,'messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(b'fixture').decode()}}]}]}

    def test_provider_receives_only_request_owned_urls_and_cleanup_follows_completion(self):
        self.setup_assets()
        self.assertEqual(self.send(json.dumps(self.body).encode())[0],200)
        native=json.loads(self.received[0]['raw'])
        self.assertTrue(native['messages'][0]['content'][0]['image_url']['url'].startswith('https://example.com/_vision-assets/'))
        self.assertEqual(self.assets.snapshot()['active_images'],0)

    def test_local_memory_limit_does_not_quarantine_provider_keys_or_retry(self):
        self.setup_assets(max_bytes=1)
        self.assertEqual(self.send(json.dumps(self.body).encode())[0],429)
        self.assertEqual(len(self.received),0)
        self.assertFalse(self.relay.pool.failure_status_counts)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_partial_provider_stream_revokes_urls_without_replaying(self):
        self.setup_assets()
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadTimeout('fixture')
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Broken())
            try:yield response
            finally:response.close()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,raw=self.send(json.dumps(self.body).encode())
        self.assertEqual(status,200);self.assertIn(b'partial',raw);self.assertIn(b'"error"',raw)
        self.assertEqual(calls.call_count,1);self.assertEqual(self.assets.snapshot()['active_images'],0)

    def setup_json_stream(self):
        self.enable_nvidia_policy()
        self.relay.models.add('z-ai/glm-5.3-flash')
        self.relay.prepare_request=NvidiaServer.prepare_request.__get__(self.relay)
        self.relay.read_nonstream=NvidiaServer.read_nonstream.__get__(self.relay)
        self.json_body=b'{"model":"z-ai/glm-5.3-flash","stream":false,"messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.com/image.png"}}]}]}'

    def test_text_multi_choice_and_logprob_json_shapes_are_not_rewritten(self):
        self.setup_json_stream()
        body=json.loads(self.json_body)
        for change in [{'n':2},{'logprobs':True},{'messages':[{'role':'user','content':'text'}]}]:
            prepared=self.relay.prepare_request({**body,**change})
            self.assertIs(prepared['stream'],False)

    def test_json_clients_receive_complete_json_from_real_provider_deltas(self):
        self.setup_json_stream()
        status,raw=self.send(self.json_body)
        self.assertEqual(status,200)
        value=json.loads(raw)
        self.assertEqual(value['choices'][0]['message']['content'],'OK')
        self.assertEqual(value['choices'][0]['finish_reason'],'stop')
        self.assertEqual(value['usage']['prompt_tokens'],3)
        self.assertIs(json.loads(self.received[0]['raw'])['stream'],True)
        self.assertEqual(len(self.received),1)

    def test_json_reasoning_can_continue_beyond_the_first_output_deadline(self):
        import time
        self.setup_json_stream();self.relay.pre_output_timeout=.05
        class Thinking(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"real reasoning"}}]}\n\n'
                time.sleep(.12)
                yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Thinking())
            try:yield response
            finally:response.close()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,raw=self.send(self.json_body)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['choices'][0]['message']['content'],'answer')
        self.assertEqual(json.loads(raw)['choices'][0]['message']['reasoning_content'],'real reasoning')
        self.assertEqual(calls.call_count,1)

    def test_incomplete_json_after_reasoning_is_not_replayed(self):
        self.setup_json_stream()
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"real reasoning"}}]}\n\n'
                raise httpx.ReadTimeout('fixture')
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Broken())
            try:yield response
            finally:response.close()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            self.assertEqual(self.send(self.json_body)[0],502)
        self.assertEqual(calls.call_count,1);self.assertEqual(self.relay.pool.inflight,0)

    def test_explicit_token_budget_finishing_in_reasoning_is_not_replayed(self):
        self.setup_json_stream()
        class Limited(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"budget used"}}]}\n\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        @contextmanager
        def stream(*args,**kwargs):
            response=httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Limited())
            try:yield response
            finally:response.close()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,raw=self.send(self.json_body)
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['choices'][0]['finish_reason'],'length')
        self.assertEqual(calls.call_count,1)
