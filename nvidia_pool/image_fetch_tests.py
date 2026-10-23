import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from nvidia_pool.image_fetch import fetch_image,inline_remote_images,ImageFetchError


class FetchTests(unittest.TestCase):
    def setUp(self):
        raw=Path(__file__).with_name('fixtures').joinpath('vision-health.png').read_bytes()
        self.raw=raw;self.requests=[];calls=self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                calls.append((self.path,self.headers.get('Host'),self.headers.get('Authorization')))
                if self.path.startswith('/redirect-private'):
                    self.send_response(302);self.send_header('Location','http://127.0.0.1/private');self.end_headers();return
                if self.path.startswith('/redirect'):
                    self.send_response(302);self.send_header('Location','http://next.example/ok');self.end_headers();return
                if self.path.startswith('/loop'):
                    self.send_response(302);self.send_header('Location','/loop');self.end_headers();return
                if self.path.startswith('/huge'):
                    self.send_response(200);self.send_header('Content-Length',str(16*1024*1024+1));self.end_headers();return
                payload=b'not an image' if self.path.startswith('/wrong') else raw
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers()
                try:
                    if self.path.startswith('/slow'):
                        for offset in range(0,len(payload),128):
                            self.wfile.write(payload[offset:offset+128]);self.wfile.flush();time.sleep(.025)
                    else:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.destinations=[]
        def resolve(host,port,**_):
            ip='127.0.0.1' if host=='127.0.0.1' else '8.8.8.8'
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port))]
        def connect(address,timeout,source_address=None):
            self.destinations.append(address)
            connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM);connection.settimeout(timeout)
            connection.connect(('127.0.0.1',self.server.server_port));return connection
        self.dns=patch('socket.getaddrinfo',side_effect=resolve);self.dns.start()
        self.connect=patch('socket.create_connection',side_effect=connect);self.connect.start()

    def tearDown(self):
        self.connect.stop();self.dns.stop();self.server.shutdown();self.server.server_close();self.thread.join(2)

    def test_download_pins_vetted_ip_preserves_host_and_exact_pixels_without_credentials(self):
        value=fetch_image('http://image.example/ok')
        self.assertEqual(base64.b64decode(value.split(',',1)[1]),self.raw)
        self.assertEqual(self.destinations,[('8.8.8.8',80)])
        self.assertEqual(self.requests,[('/ok','image.example',None)])

    def test_private_address_and_redirect_are_rejected_before_connecting_to_private_host(self):
        with self.assertRaises(ImageFetchError):fetch_image('http://127.0.0.1/ok')
        self.assertEqual(self.destinations,[])
        with self.assertRaises(ImageFetchError):fetch_image('http://image.example/redirect-private')
        self.assertEqual(self.destinations,[('8.8.8.8',80)])

    def test_public_redirect_is_revalidated_and_uses_new_original_hostname(self):
        fetch_image('http://image.example/redirect')
        self.assertEqual([r[1] for r in self.requests],['image.example','next.example'])

    def test_file_credentials_redirect_loops_and_oversized_nonimages_fail_without_provider_work(self):
        for url in ['file:///etc/passwd','http://user:password@image.example/ok','http://image.example/loop',
                    'http://image.example/huge','http://image.example/wrong']:
            with self.subTest(url=url),self.assertRaises(ImageFetchError):fetch_image(url)

    def test_trickled_image_cannot_extend_download_deadline(self):
        start=time.monotonic()
        with self.assertRaises(ImageFetchError):fetch_image('http://image.example/slow',start+.09)
        self.assertLess(time.monotonic()-start,.6)

    def test_request_deduplicates_remote_fetch_but_has_no_cross_request_cache(self):
        body={'messages':[{'content':[{'type':'image_url','image_url':{'url':'http://image.example/ok','detail':'high'}}]*4}]}
        original=deepcopy(body)
        for _ in range(2):
            value=inline_remote_images(body)
            self.assertTrue(all(p['image_url']['url'].startswith('data:image/png;base64,') for p in value['messages'][0]['content']))
            self.assertTrue(all(p['image_url']['detail']=='high' for p in value['messages'][0]['content']))
        self.assertEqual(body,original);self.assertEqual(len(self.requests),2)
