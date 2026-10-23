"""Measure whether this Mihomo build interrupts streams during config reload."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

import httpx
import yaml
from ops.admin_api import ROOT


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def main():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length', '100');self.end_headers()
            for _ in range(100):
                try:self.wfile.write(b'x');self.wfile.flush();time.sleep(.02)
                except (BrokenPipeError,ConnectionResetError):return
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True);worker.start()
    (ROOT/'var').mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='reload-check-', dir=ROOT/'var') as directory:
            root = Path(directory);mixed, control = free_port(), free_port()
            old, new = root/'old.yaml', root/'new.yaml'
            old.write_text('proxies: [{name: local-fixture, type: direct}]\n')
            new.write_bytes(old.read_bytes())
            config = {'mixed-port': mixed, 'external-controller': f'127.0.0.1:{control}',
                'allow-lan': False, 'bind-address': '127.0.0.1', 'log-level': 'silent',
                'proxy-providers': {'fixture': {'type': 'file', 'path': str(old)}},
                'proxy-groups': [{'name': 'Fixture', 'type': 'select', 'use': ['fixture']}],
                'rules': ['MATCH,Fixture']}
            path = root/'config.yaml';path.write_text(yaml.safe_dump(config))
            process = subprocess.Popen([str(ROOT/'bin/mihomo'), '-d', directory, '-f', str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{control}', trust_env=False, timeout=5) as api:
                    for _ in range(100):
                        try:
                            if api.get('/version').status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.05)
                    else:
                        raise RuntimeError('Isolated Mihomo did not start')
                    with httpx.Client(proxy=f'http://127.0.0.1:{mixed}', trust_env=False, timeout=5) as client:
                        with client.stream('GET', f'http://127.0.0.1:{upstream.server_port}/stream') as response:
                            iterator = response.iter_bytes(chunk_size=1)
                            content = next(iterator)
                            config['proxy-providers']['fixture']['path'] = str(new)
                            api.put('/configs', json={'payload': yaml.safe_dump(config)}).raise_for_status()
                            old.unlink()
                            interrupted=False
                            try:content += b''.join(iterator)
                            except httpx.RemoteProtocolError:interrupted=True
                    value = api.get('/providers/proxies/fixture').raise_for_status().json()
                    if not value.get('proxies'):
                        raise RuntimeError('New provider did not load')
                    print(json.dumps({'reload_preserved_stream': not interrupted and content==b'x'*100,
                        'bytes': len(content), 'old_provider_removed': True,
                        'deployment_policy': 'drain accepted requests before any full config reload'}))
            finally:
                process.terminate();process.wait(timeout=5)
    finally:
        upstream.shutdown();upstream.server_close();worker.join(timeout=2)


if __name__ == '__main__':
    main()
