import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from nvidia_pool.egress_transport import EgressTransport


class FakeTransport:
    instances=[]
    def __init__(self,**_):self.closed=False;self.calls=0;self.instances.append(self)
    def handle_request(self,request):
        self.calls+=1
        return httpx.Response(200,stream=httpx.ByteStream(b'real upstream bytes'))
    def close(self):self.closed=True


class EgressTransportTests(unittest.TestCase):
    @patch('nvidia_pool.egress_transport.ProxySafeTransport',FakeTransport)
    def test_new_requests_adopt_route_while_existing_response_drains_without_replay(self):
        FakeTransport.instances=[]
        with TemporaryDirectory() as directory:
            path=Path(directory)/'route.json';path.write_text(json.dumps({'generation':'a'*64}))
            transport=EgressTransport(path)
            request=httpx.Request('POST','https://integrate.api.nvidia.com/v1/chat/completions')
            first=transport.handle_request(request);old=FakeTransport.instances[0]
            path.write_text(json.dumps({'generation':'b'*64}))
            transport.refresh()
            second=transport.handle_request(request);new=FakeTransport.instances[1]
            self.assertFalse(old.closed)
            self.assertEqual(first.read(),b'real upstream bytes');first.close()
            self.assertTrue(old.closed);self.assertFalse(new.closed)
            second.close();self.assertEqual((old.calls,new.calls),(1,1))
            transport.close();self.assertTrue(new.closed)

    @patch('nvidia_pool.egress_transport.ProxySafeTransport',FakeTransport)
    def test_incomplete_marker_retains_healthy_warm_transport(self):
        FakeTransport.instances=[]
        with TemporaryDirectory() as directory:
            path=Path(directory)/'route.json';path.write_text(json.dumps({'generation':'a'*64}))
            transport=EgressTransport(path)
            request=httpx.Request('POST','https://integrate.api.nvidia.com/v1/chat/completions')
            first=transport.handle_request(request);first.close()
            path.write_text('{incomplete')
            transport.refresh()
            second=transport.handle_request(request);second.close()
            self.assertEqual(len(FakeTransport.instances),1);transport.close()

    @patch('nvidia_pool.egress_transport.ProxySafeTransport',FakeTransport)
    def test_slow_route_file_does_not_hold_the_request_lock(self):
        FakeTransport.instances=[]
        with TemporaryDirectory() as directory:
            path=Path(directory)/'route.json';path.write_text(json.dumps({'generation':'a'*64}))
            transport=EgressTransport(path)
            entered,release=threading.Event(),threading.Event()
            def slow_read():
                entered.set()
                if not release.wait(3):raise OSError('blocked route file')
                return json.dumps({'generation':'b'*64})
            try:
                with patch.object(transport,'_epoch',side_effect=slow_read),ThreadPoolExecutor(2) as executor:
                    pending=executor.submit(transport.refresh)
                    self.assertTrue(entered.wait(1))
                    request=httpx.Request('POST','https://integrate.api.nvidia.com/v1/chat/completions')
                    response=executor.submit(transport.handle_request,request).result(timeout=.3)
                    self.assertEqual(response.read(),b'real upstream bytes');response.close()
                    release.set();pending.result(timeout=1)
                self.assertEqual(len(FakeTransport.instances),2)
            finally:
                release.set();transport.close()
