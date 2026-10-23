import http.client
import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

from image_hub.server import HubError
from internal_tools import http_api
from internal_tools.test_image_tools import IMAGE
from internal_tools.test_recognition_transport import Handler as Upstream
from internal_tools.vision_routing import VisionRouter
from image_hub.concurrency import FileSlotPool


class PublicHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*_):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        http_api.dispatch(self,self.server.gateway,body)


class RecognitionHTTPStreamTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        routing=patch('internal_tools.image_tools.ROUTER',VisionRouter(folder.name+'/routes.db'))
        routing.start();self.addCleanup(routing.stop)
        for name,limit,directory in [('RECOGNITION_SLOTS',64,'accepted'),('RECOGNITION_UPSTREAM_SLOTS',16,'upstream')]:
            slots=patch('internal_tools.image_tools.'+name,FileSlotPool(limit,folder.name+'/'+directory))
            slots.start();self.addCleanup(slots.stop)
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        self.upstream.calls=[];self.upstream.first_sent=threading.Event();self.upstream.release=threading.Event()
        self.public=ThreadingHTTPServer(('127.0.0.1',0),PublicHandler)
        # Route all recognized images to the held real-SSE fixture.
        self.public.gateway=f'http://127.0.0.1:{self.upstream.server_port}'
        self.workers=[]
        for server in [self.upstream,self.public]:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.workers.append(thread)
        self.auth=patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'});self.auth.start()
        self.route=patch('internal_tools.image_tools.recognition_request',side_effect=self.native);self.route.start()

    def native(self,url,*args,**kwargs):
        from internal_tools.recognition_transport import recognition_request
        return recognition_request(f'http://127.0.0.1:{self.upstream.server_port}'+self.upstream_path,*args,**kwargs)

    def tearDown(self):
        self.upstream.release.set();self.route.stop();self.auth.stop()
        for server in [self.public,self.upstream]:server.shutdown();server.server_close()
        for thread in self.workers:thread.join(2)

    def request(self,args,accept='application/json'):
        connection=http.client.HTTPConnection('127.0.0.1',self.public.server_port,timeout=3)
        connection.request('POST','/v1/tools/image_recognition',json.dumps(args),{'Content-Type':'application/json','Authorization':'Bearer sk-user','Accept':accept})
        return connection,connection.getresponse()

    def test_real_rest_delta_precedes_completion_without_private_reasoning_or_duplicate_post(self):
        self.upstream_path='/held'
        connection,response=self.request({'image':IMAGE,'stream':True})
        try:
            self.assertEqual(response.status,200);self.assertIn('text/event-stream',response.headers['Content-Type'])
            self.assertEqual(response.headers['X-Accel-Buffering'],'no')
            received=[]
            while True:
                line=response.readline().decode()
                self.assertTrue(line,'stream ended before the first real text delta')
                if line.startswith('data:'):
                    event=json.loads(line[5:]);received.append(event)
                    if event.get('delta'):
                        self.assertEqual(event['delta'],'4826');break
            self.assertFalse(self.upstream.release.is_set())
            self.upstream.release.set()
            rest=response.read().decode()
            self.assertIn('event: result',rest);self.assertIn('4826，红色方块',rest)
            self.assertNotIn('private model reasoning',json.dumps(received)+rest)
            self.assertEqual(len(self.upstream.calls),1)
            self.assertIn('"prompt_tokens":540',rest)
        finally:connection.close()

    def test_interrupted_stream_has_error_and_never_emits_final_success(self):
        self.upstream_path='/broken'
        connection,response=self.request({'image':IMAGE,'stream':True})
        try:
            raw=response.read().decode()
            self.assertIn('"delta":"4826"',raw);self.assertIn('event: error',raw)
            self.assertNotIn('event: result',raw);self.assertEqual(len(self.upstream.calls),1)
        finally:connection.close()

    def test_default_json_and_accept_stream_are_compatible(self):
        self.upstream_path='/ok'
        for args,accept,stream in [({'image':IMAGE},'application/json',False),({'image':IMAGE},'text/event-stream',True)]:
            with self.subTest(stream=stream):
                connection,response=self.request(args,accept)
                try:
                    raw=response.read().decode()
                    if stream:self.assertIn('event: result',raw)
                    else:self.assertEqual(json.loads(raw)['output']['text'],'4826')
                finally:connection.close()

    def test_auth_and_invalid_stream_fail_before_sse_headers_or_paid_call(self):
        self.upstream_path='/ok'
        for args,status in [({'image':IMAGE,'stream':'true'},400),({'images':[IMAGE,IMAGE],'stream':True},400),({'image':IMAGE,'stream':True},401)]:
            with self.subTest(status=status),patch('internal_tools.image_tools.authorized_models',side_effect=HubError(401,'Authentication required')):
                connection,response=self.request(args)
                try:
                    self.assertEqual(response.status,status);self.assertEqual(response.headers['Content-Type'],'application/json');response.read()
                finally:connection.close()
        self.assertEqual(self.upstream.calls,[])
