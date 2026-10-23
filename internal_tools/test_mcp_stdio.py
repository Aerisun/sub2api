"""A real stdio process must submit 64 distinct MCP calls concurrently."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import unittest


class MCPStdioTests(unittest.TestCase):
    def test_64_image_calls_complete_with_distinct_ids_and_serialized_stdout(self):
        lock=threading.Lock();arrived=threading.Event();state={'active':0,'peak':0,'calls':0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                message=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.headers.get('Authorization')!='Bearer local-fixture':
                    self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
                if 'id' not in message:
                    self.send_response(202);self.send_header('Content-Length','0');self.end_headers();return
                if message['method']=='tools/call':
                    with lock:
                        state['active']+=1;state['calls']+=1;state['peak']=max(state['peak'],state['active'])
                        if state['calls']==64:arrived.set()
                    all_arrived=arrived.wait(8)
                    with lock:state['active']-=1
                    result={'index':message['id'],'all_arrived':all_arrived}
                else:result={'protocolVersion':'2025-06-18'}
                raw=json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        class Server(ThreadingHTTPServer):request_queue_size=128
        server=Server(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            messages=[{'jsonrpc':'2.0','id':'init','method':'initialize'},
                      {'jsonrpc':'2.0','method':'notifications/initialized'}]+[
                {'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'image_recognition','arguments':{'image':'fixture-'+str(i)}}}
                for i in range(64)]
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('mcp_stdio.py'))],
                input=''.join(json.dumps(m)+'\n' for m in messages),text=True,capture_output=True,timeout=15,
                env={**os.environ,'SUB2API_API_KEY':'local-fixture','SUB2API_TOOLS_MCP_URL':f'http://127.0.0.1:{server.server_port}/v1/tools/mcp'})
            self.assertEqual(result.returncode,0,result.stderr)
            rows=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(rows[0]['id'],'init');self.assertEqual(len(rows),65)
            self.assertEqual({r['id'] for r in rows[1:]},set(range(64)))
            self.assertTrue(all(r['result']['all_arrived'] for r in rows[1:]))
            self.assertEqual(state,{'active':0,'peak':64,'calls':64})
        finally:
            arrived.set();server.shutdown();server.server_close();worker.join(2)
