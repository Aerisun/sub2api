import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from image_hub.server import HubError
from internal_tools.recognition_transport import recognition_request


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((body,self.headers.get('Authorization')))
        if self.path=='/denied':
            raw=b'{"error":{"message":"fixture denied"}}'
            self.send_response(401);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            return
        if self.path=='/quota':
            raw=b'{"error":{"message":"busy"}}'
            self.send_response(429);self.send_header('Retry-After','60');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            return
        self.send_response(200);self.send_header('Content-Type','text/event-stream')
        if self.path in ('/failed-reasoning','/failed-visible'):
            self.send_header('X-Aerisun-Recognition-Billing','completed-only-v1')
        self.end_headers()
        def emit(value):
            self.wfile.write(('data: '+json.dumps(value)+'\n\n').encode());self.wfile.flush()
        try:
            if self.path=='/silent':
                time.sleep(.2)
            if self.path=='/trickle':
                for _ in range(20):
                    self.wfile.write(b': keepalive\n\n');self.wfile.flush();time.sleep(.025)
                return
            emit({'id':'chat-fixture','choices':[{'index':0,'delta':{'reasoning_content':'private model reasoning'}}]})
            if self.path in ('/failed-reasoning','/failed-unverified'):
                emit({'error':{'message':'provider failed'}});return
            emit({'id':'chat-fixture','choices':[{'index':0,'delta':{'content':'4826'}}]})
            if self.path=='/failed-visible':
                emit({'error':{'message':'provider failed'}});return
            if self.path=='/held':
                self.server.first_sent.set()
                self.server.release.wait(3)
                emit({'id':'chat-fixture','choices':[{'index':0,'delta':{'content':'，红色方块'}}]})
            if self.path=='/noisy':
                emit({'choices':[{'index':0,'delta':{'content':'<unk>'*16}}]})
                return
            if self.path=='/broken':return
            emit({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})
            emit({'choices':[],'usage':{'prompt_tokens':540,'completion_tokens':20,'total_tokens':560}})
            if self.path=='/usage-held':
                self.wfile.flush();self.server.release.wait(3);return
            self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
            if self.path=='/done-held':self.server.release.wait(3)
        except (BrokenPipeError,ConnectionResetError):pass


class RecognitionTransportTests(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.calls=[]
        self.server.first_sent=threading.Event();self.server.release=threading.Event()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def call(self,path,**kwargs):
        return recognition_request(f'http://127.0.0.1:{self.server.server_port}'+path,'jwt-fixture',{'model':'glm-5.3-flash'},**kwargs)

    def test_real_stream_returns_final_content_and_actual_usage_without_replaying(self):
        status,value=self.call('/ok')
        self.assertEqual(status,200);self.assertEqual(value['choices'][0]['message']['content'],'4826')
        self.assertEqual(value['usage']['prompt_tokens'],540)
        self.assertEqual(len(self.server.calls),1)
        payload,auth=self.server.calls[0]
        self.assertEqual(auth,'Bearer jwt-fixture');self.assertIs(payload['stream'],True)
        self.assertTrue(payload['stream_options']['include_usage'])

    def test_partial_stream_is_not_fabricated_or_replayed(self):
        with self.assertRaises(HubError):self.call('/broken')
        self.assertEqual(len(self.server.calls),1)

    def test_total_deadline_bounds_trickled_keepalives(self):
        start=time.monotonic()
        with self.assertRaises(HubError):self.call('/trickle',timeout=.09)
        self.assertLess(time.monotonic()-start,1.5)
        self.assertEqual(len(self.server.calls),1)

    def test_auth_error_keeps_status_without_paid_retry(self):
        with self.assertRaises(HubError) as error:self.call('/denied')
        self.assertEqual(error.exception.status,401);self.assertEqual(len(self.server.calls),1)

    def test_quota_error_retains_retry_after_for_bounded_scheduler_wait(self):
        with self.assertRaises(HubError) as error:self.call('/quota')
        self.assertEqual(error.exception.status,429)
        self.assertEqual(error.exception.retry_after,60)
        self.assertFalse(error.exception.semantic_started)

    def test_only_confirmed_unbilled_reasoning_failure_can_be_retried(self):
        for path,safe in [('/failed-reasoning',True),('/failed-unverified',False),('/failed-visible',False)]:
            with self.subTest(path=path),self.assertRaises(HubError) as error:self.call(path)
            self.assertTrue(error.exception.semantic_started)
            self.assertIs(error.exception.retryable_unbilled,safe)

    def test_silent_prefill_timeout_is_504_and_is_never_replayed(self):
        with self.assertRaises(HubError) as error:self.call('/silent',timeout=.05)
        self.assertEqual(error.exception.status,504)
        self.assertEqual(len(self.server.calls),1)

    def test_silent_prefill_within_total_budget_keeps_real_output(self):
        status,value=self.call('/silent',timeout=1)
        self.assertEqual(status,200)
        self.assertEqual(value['choices'][0]['message']['content'],'4826')
        self.assertEqual(len(self.server.calls),1)

    def test_completed_result_and_usage_return_without_waiting_for_http_eof(self):
        start=time.monotonic()
        status,value=self.call('/usage-held',timeout=.5)
        self.assertEqual(status,200);self.assertEqual(value['choices'][0]['message']['content'],'4826')
        self.assertEqual(value['usage']['total_tokens'],560)
        self.assertLess(time.monotonic()-start,.4)
        self.assertFalse(self.server.release.is_set());self.assertEqual(len(self.server.calls),1)

    def test_text_callback_arrives_before_upstream_finishes_and_excludes_reasoning(self):
        from concurrent.futures import ThreadPoolExecutor
        deltas=[];visible=threading.Event()
        def text(value):deltas.append(value);visible.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(self.call,'/held',on_text=text)
            self.assertTrue(visible.wait(2));self.assertFalse(result.done())
            self.assertEqual(deltas,['4826'])
            self.server.release.set()
            self.assertEqual(result.result(2)[1]['choices'][0]['message']['content'],''.join(deltas))
        self.assertEqual(deltas,['4826','，红色方块'])
        self.assertEqual(len(self.server.calls),1)

    def test_partial_real_text_remains_partial_when_stream_is_interrupted(self):
        deltas=[]
        with self.assertRaises(HubError):self.call('/broken',on_text=deltas.append)
        self.assertEqual(deltas,['4826']);self.assertEqual(len(self.server.calls),1)

    def test_repeated_unknown_tokens_fail_without_replaying_or_streaming_noise(self):
        chunks=[]
        with self.assertRaises(HubError) as error:
            recognition_request('http://127.0.0.1:'+str(self.server.server_port)+'/noisy','sk-fixture',{'model':'vision'},on_text=chunks.append)
        self.assertTrue(error.exception.semantic_started)
        self.assertEqual(len(self.server.calls),1)
        self.assertNotIn('<unk>',''.join(chunks))
