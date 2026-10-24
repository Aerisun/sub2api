"""Inspect the stack and drain accepted inference before controlled restarts."""
import argparse
from contextlib import ExitStack
import json
import os
import socket
import sqlite3
import subprocess
import time
from urllib.request import urlopen

from image_hub.concurrency import SlotLimitReached
from internal_tools.image_tools import RECOGNITION_SLOTS
from ops import admin_api
from ops.admin_api import ROOT, read_env

PORTS = {'gateway': 8080, 'web': 8081, 'tools': 8094, 'qiniu': 8095, 'nvidia': 8096}
RESTARTABLE = {'sub2api-nvidia-pool', 'sub2api-qiniu-pool', 'sub2api-agent-images', 'sub2api-image-hub',
    'sub2api-gemini-egress', 'sub2api-codex-egress', 'sub2api-nvidia-egress', 'sub2api-inference-link'}


def systemctl(*arguments):
    subprocess.run(['systemctl', '--user', *arguments], check=True, timeout=90)


def health(port):
    with urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
        return json.load(response)


def redis_active(exclude=()):
    env = read_env(ROOT/'deployment/private/config/runtime.env')
    with socket.create_connection((env['REDIS_HOST'], int(env['REDIS_PORT'])), timeout=3) as connection:
        stream = connection.makefile('rb')
        def command(*items):
            values = [str(item).encode() for item in items]
            connection.sendall(('*'+str(len(values))+'\r\n').encode()+b''.join(
                ('$'+str(len(value))+'\r\n').encode()+value+b'\r\n' for value in values))
            result = stream.readline().strip()
            if result.startswith(b'-'):
                raise RuntimeError('Redis drain inspection failed')
            return int(result[1:]) if result.startswith(b':') else result
        if env.get('REDIS_PASSWORD'):
            command('AUTH', env['REDIS_PASSWORD'])
        return command('EVAL', """local n=0
local excluded={} for _,id in ipairs(ARGV) do excluded[id]=true end
for _,pattern in ipairs({'concurrency:account:*','concurrency:live:account:*'}) do
 for _,key in ipairs(redis.call('KEYS',pattern)) do
  local id=key:match(':(%d+)$')
  if id and not excluded[id] then n=n+redis.call('ZCARD',key) end
 end
end
return n""", 0, *exclude)


def active(exclude=()):
    database = ROOT/'data/image-hub/tasks.sqlite3'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        generating = connection.execute("SELECT COUNT(*) FROM tasks WHERE "
            "json_extract(record,'$.status') IN ('processing','queued','pending')").fetchone()[0]
    return {'gateway': redis_active(exclude), 'nvidia': health(8096)['inflight'],
            'qiniu': health(8095)['inflight'], 'generating': generating,
            'chat_generations': durable_chat_active()}


def durable_chat_active():
    # A UI turn may be between paid model/tool legs with no active HTTP call.
    # Its durable lifecycle, rather than a quiet socket, determines completion.
    values = read_env(ROOT/'deployment/private/config/runtime.env')
    result = subprocess.run([str(ROOT/'runtime/bin/psql'), '-X', '-qAt',
        '-h', values['DATABASE_HOST'], '-p', values['DATABASE_PORT'],
        '-U', values['DATABASE_USER'], '-d', values['DATABASE_DBNAME'],
        '-c', "SELECT COUNT(*) FROM chat_generations WHERE status='running'"],
        env=dict(os.environ, PGPASSWORD=values['DATABASE_PASSWORD'], PGCONNECT_TIMEOUT='3',
                 PGOPTIONS='-c statement_timeout=2000'),
        check=True, capture_output=True, text=True, timeout=5)
    return int(result.stdout.strip())


def status():
    result = {}
    for name, port in PORTS.items():
        try:
            value = health(port)
            result[name] = {'healthy': value.get('status') == 'ok'}
        except Exception as error:
            result[name] = {'healthy': False, 'error_type': type(error).__name__}
    result['active'] = active()
    return result


def restart(names, timeout=180):
    if not names or set(names)-RESTARTABLE:
        raise ValueError('Choose only reviewed add-on services; database/gateway restarts are separate operations')
    # The independent vector gateway is never restarted here. Its long CPU
    # batches must not prevent maintenance of unrelated chat/image add-ons.
    token=admin_api.login()
    vector_accounts=[str(account['id']) for account in admin_api.list_items('/api/v1/admin/accounts',token)
        if set((account.get('credentials') or {}).get('openai_capabilities',[])) & {'embeddings','rerank'}
        and not set((account.get('credentials') or {}).get('openai_capabilities',[])) & {'chat_completions','responses'}]
    monitor_enabled = subprocess.run(['systemctl', '--user', 'is-active', '--quiet',
        'sub2api-vision-monitor.timer'], check=False, timeout=5).returncode == 0
    systemctl('stop', 'sub2api-vision-monitor.timer', 'sub2api-vision-monitor.service')
    try:
        print('Waiting for accepted paid work to drain', flush=True)
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if any(active(vector_accounts).values()):
                time.sleep(.3);continue
            with ExitStack() as stack:
                try:
                    for _ in range(RECOGNITION_SLOTS.limit):
                        stack.enter_context(RECOGNITION_SLOTS.acquire())
                except SlotLimitReached:
                    time.sleep(.2);continue
                if any(active(vector_accounts).values()):
                    continue
                # A quiet window also lets the last SSE response leave the gateway.
                time.sleep(.5)
                if any(active(vector_accounts).values()):
                    continue
                for name in names:
                    systemctl('restart', name+'.service')
                    print('Restarted '+name, flush=True)
                for name, port in PORTS.items():
                    ready = False
                    for _ in range(80):
                        try:
                            if health(port).get('status') == 'ok':
                                ready = True;break
                        except OSError:
                            pass
                        time.sleep(.1)
                    if not ready:
                        raise RuntimeError(name+' did not become healthy')
                return
        raise RuntimeError('Accepted work remains active; services were left running')
    finally:
        if monitor_enabled:
            systemctl('start', 'sub2api-vision-monitor.timer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    change = sub.add_parser('restart');change.add_argument('services', nargs='+')
    change.add_argument('--drain-seconds', type=int, default=180)
    args = parser.parse_args()
    if args.command == 'status':
        print(json.dumps(status()))
    else:
        restart(args.services, args.drain_seconds)


if __name__ == '__main__':
    main()
