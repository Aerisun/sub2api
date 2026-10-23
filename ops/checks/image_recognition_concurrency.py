#!/usr/bin/env python3
"""Real paid image recognition through public REST, SSE and MCP at fixed load."""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import secrets
import sqlite3
import statistics
import threading
import time

import httpx
from PIL import Image, ImageDraw, ImageFont
from ops import admin_api as api
from ops import admin_api as auth
from internal_tools.vision_quality import QUALITY_PROMPT, accepts
from internal_tools.vision_routing import ROOT, VisionRouter

PUBLIC = 'https://aerisun.top/sub2api'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temp.chmod(0o600)
    temp.replace(path)


def fixture(index, quality=False):
    code = ''.join(str(secrets.randbelow(10)) for _ in range(6))
    if quality:
        original = Image.open(ROOT/'nvidia_pool/fixtures/vision-quality.png').convert('RGB')
        image = Image.new('RGB', (original.width, original.height+70), 'white')
        image.paste(original, (0, 0))
        ImageDraw.Draw(image).text((24, original.height+10), 'TEST '+code,
            font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 32), fill='black')
        expected = None
        prompt = QUALITY_PROMPT+'bar_colors 使用中文颜色名，不要使用 RGB 或 HEX 颜色代码。'
    else:
        image = Image.new('RGB', (960, 600), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 48)
        a = f'{100+index}.{secrets.randbelow(100):02d}'
        b = f'{200+index}.{secrets.randbelow(100):02d}'
        palette = [('red', '#ed2929'), ('blue', '#235fed'), ('green', '#17994b')]
        palette = palette[index % 3:] + palette[:index % 3]
        for y, text in [(30, 'IMAGE CAPACITY TEST'), (125, 'CODE: '+code), (235, 'A: '+a), (325, 'B: '+b)]:
            draw.text((45, y), text, font=font, fill='black')
        for i, (_, color) in enumerate(palette):
            draw.rectangle((50+i*290, 440, 280+i*290, 555), fill=color)
        expected = {'code': code, 'a': a, 'b': b, 'colors': [name for name, _ in palette]}
        prompt = ('只输出 JSON 对象，不要 Markdown。code 是图中六位编号（字符串），'
                  'a 和 b 是 A 行和 B 行数值（字符串，保留小数），'
                  'colors 是从左到右三个色块的英文颜色数组。')
    buf = io.BytesIO(); image.save(buf, format='PNG')
    return {'image': 'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode(),
            'prompt': prompt}, expected


def content_ok(text, expected):
    if expected is None:
        return accepts(text)
    try:
        raw = text.strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0]
        value = json.loads(raw)
        return (str(value['code']) == expected['code'] and
            all(Decimal(str(value[k])) == Decimal(expected[k]) for k in ('a', 'b')) and
            [str(c).lower() for c in value['colors']] == expected['colors'])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return False


def summary(rows):
    values = sorted(r['total_ms'] for r in rows)
    first = sorted(r['first_text_ms'] for r in rows if r.get('first_text_ms') is not None)
    pct = lambda data, p: data[max(0, math.ceil(len(data)*p)-1)] if data else None
    return {'requests': len(rows), 'successes': sum(r['ok'] for r in rows),
            'http_statuses': dict(Counter(str(r.get('http_status')) for r in rows)),
            'total_p50_ms': round(statistics.median(values)) if values else None,
            'total_p95_ms': pct(values, .95), 'total_max_ms': max(values, default=None),
            'stream_first_text_p50_ms': round(statistics.median(first)) if first else None,
            'stream_first_text_p95_ms': pct(first, .95)}


def wave(client, key, count, number, case):
    trace_run=secrets.token_hex(6)
    fixtures = [fixture(number*count+i, quality=(case == 'quality' or case == 'mixed' and i % 8 == 0)) for i in range(count)]
    gate = threading.Barrier(count)
    lock = threading.Lock(); activity = {'inflight': 0, 'peak': 0}
    stopped = threading.Event(); samples = []
    router = VisionRouter()

    def sample():
        while not stopped.wait(.05):
            try:
                with sqlite3.connect(router.path, timeout=.2) as c:
                    rows = c.execute('SELECT model,COUNT(*) FROM leases WHERE expires>? GROUP BY model', (time.time(),)).fetchall()
                h = client.get('http://127.0.0.1:8096/health', timeout=2).json()
                samples.append({'time': time.time(), 'router_leases': dict(rows),
                    'upstream_inflight': h['inflight'], 'vision_inflight': h['workload_inflight']['vision'],
                    'model_inflight': h['model_inflight']})
            except (sqlite3.Error, httpx.HTTPError, ValueError, KeyError):
                pass

    def one(i):
        arguments, expected = fixtures[i]
        protocol = ('rest_json', 'rest_sse', 'mcp', 'tools_call')[i % 4]
        path = '/v1/tools/image_recognition'
        body = arguments
        if protocol == 'rest_sse':
            body = {**arguments, 'stream': True}
        elif protocol == 'mcp':
            path = '/v1/tools/mcp'
            body = {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                    'params': {'name': 'image_recognition', 'arguments': arguments}}
        elif protocol == 'tools_call':
            path = '/v1/tools/call'; body = {'name': 'image_recognition', 'arguments': arguments}
        request_id=f'capacity-recognition64-{trace_run}-{number}-{i}'
        row = {'index': i, 'protocol': protocol, 'case': 'quality' if expected is None else 'unique_ocr',
               'client_request_id':request_id}
        gate.wait(timeout=30); start = time.monotonic()
        with lock:
            activity['inflight'] += 1; activity['peak'] = max(activity['peak'], activity['inflight'])
        try:
            headers = {'Authorization': 'Bearer '+key,'X-Client-Request-ID':request_id}
            value = None
            with client.stream('POST', PUBLIC+path, headers=headers, json=body) as response:
                row.update(http_status=response.status_code, headers_ms=round((time.monotonic()-start)*1000))
                row['response_headers']={name:response.headers[name] for name in
                    ['server','via','x-request-id','content-type','content-length','retry-after','connection'] if name in response.headers}
                if 'text/event-stream' in response.headers.get('content-type', ''):
                    event = ''
                    for line in response.iter_lines():
                        if line.startswith('event:'): event = line[6:].strip()
                        if not line.startswith('data:'): continue
                        data = json.loads(line[5:])
                        if event == 'aerisun.tool' and data.get('delta'):
                            row.setdefault('first_text_ms', round((time.monotonic()-start)*1000))
                        elif event == 'result': value = data
                        elif event == 'error': row['error'] = data.get('error')
                else:
                    raw = response.read()
                    try: value = json.loads(raw)
                    except ValueError: row['error'] = raw.decode(errors='replace')[:300]
                if protocol == 'mcp' and isinstance(value, dict):
                    value = (value.get('result') or {}).get('structuredContent', value)
            value = value if isinstance(value, dict) else {}
            text = (value.get('output') or {}).get('text', '')
            row.update(tool_status=value.get('status'), output=text, usage=value.get('usage'),
                       content_verified=content_ok(text, expected), cached=value.get('cached'))
            if value.get('error'): row['error'] = value['error']
            row['ok'] = bool(row['http_status'] == 200 and value.get('status') == 'completed'
                and row['content_verified'] and (value.get('usage') or {}).get('total_tokens', 0) > 0
                and value.get('cached') is False and (value.get('billing') or {}).get('images') == 1)
        except Exception as error:
            row.update(ok=False, error=type(error).__name__)
        finally:
            row['total_ms'] = round((time.monotonic()-start)*1000)
            with lock: activity['inflight'] -= 1
        return row

    poller = threading.Thread(target=sample, daemon=True); poller.start()
    try:
        with ThreadPoolExecutor(max_workers=count) as executor: rows = list(executor.map(one, range(count)))
    finally:
        stopped.set(); poller.join(3)
    return {'wave': number, 'client_peak': activity['peak'], 'checks': rows, **summary(rows),
        'sampled_router_peak': max((sum(s['router_leases'].values()) for s in samples), default=0),
        'sampled_vision_peak': max((s['vision_inflight'] for s in samples), default=0),
        'samples': samples}


def billing(admin, token, user_id, key_id, expected):
    deadline = time.monotonic()+15
    while True:
        rows = []; page = 1
        while True:
            data = api.request(f'/api/v1/admin/usage?api_key_id={key_id}&page_size=100&page={page}', admin)
            batch = data.get('items') or []; rows.extend(batch)
            if len(rows) >= data.get('total', len(rows)) or not batch: break
            page += 1
        cost = sum((Decimal(str(r['actual_cost'])) for r in rows), Decimal(0))
        balance = Decimal(str(api.request(f'/api/v1/admin/users/{user_id}', admin)['balance']))
        quota = Decimal(str(api.request(f'/api/v1/keys/{key_id}', token)['quota_used']))
        images = sum(r.get('image_count') or 0 for r in rows)
        charged=[r for r in rows if r.get('image_count')]
        unbilled=[r for r in rows if not r.get('image_count')]
        verified = (len(charged) == images == expected and cost == Decimal('.002')*expected
                    and all(Decimal(str(r['actual_cost']))==0 for r in unbilled)
                    and abs(Decimal(2)-balance-cost) < Decimal('.000001') and abs(quota-cost) < Decimal('.000001'))
        if verified or time.monotonic() >= deadline:
            return {'verified': verified, 'images': images, 'usage_rows': len(rows), 'unbilled_rows':len(unbilled), 'cost_usd': str(cost),
                'quota_used': str(quota), 'balance_debit': str(Decimal(2)-balance),
                'models': dict(Counter(r.get('upstream_model') for r in rows)),
                'routes': [{'model':r.get('upstream_model'), 'account_id':r.get('account_id'),
                    'image_count':r.get('image_count'), 'cost':r.get('actual_cost')} for r in rows]}
        time.sleep(.25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--concurrency', type=int, default=64)
    parser.add_argument('--waves', type=int, default=2)
    parser.add_argument('--case', choices=['ocr', 'quality', 'mixed'], default='mixed')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 64 or not 1 <= args.waves <= 4: parser.error('load exceeds bounded test budget')
    admin = auth.login(); password = secrets.token_urlsafe(32)
    user = api.request('/api/v1/admin/users', admin, {'email':'vision-64-'+secrets.token_hex(8)+'@example.invalid',
        'password': password, 'role': 'user', 'balance': 2, 'concurrency': 64,
        'allowed_groups': [9], 'restrict_public_groups': True}, 'POST')
    token = key = None
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'concurrency': args.concurrency,
              'case': args.case, 'endpoint': PUBLIC, 'http2_requested':False,'waves': [], 'all_passed': False}
    def wait_ready():
        router=VisionRouter();names={m['id'] for m in router.models};deadline=time.monotonic()+90
        while True:
            if any(m['id'] in names and m['ready'] and m['cooldown']<=time.time() for m in router.snapshot()):return
            if time.monotonic()>=deadline:raise RuntimeError('No verified healthy vision route; do not submit a paid load test')
            time.sleep(.25)
    try:
        token = api.request('/api/v1/auth/login', payload={'email':user['email'], 'password':password})['access_token']
        key = api.request('/api/v1/keys', token, {'name':'temporary-image-recognition-64-validation', 'group_id':9,
            'quota':1, 'expires_in_days':1, 'allowed_models':['image_recognition']}, 'POST')
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(195, connect=30),
            limits=httpx.Limits(max_connections=140, max_keepalive_connections=140)) as client:
            # Establish public sockets without submitting any paid inference.
            warm_gate = threading.Barrier(args.concurrency)
            def warm(_):
                warm_gate.wait(timeout=30)
                try: return client.get(PUBLIC+'/health').status_code
                except httpx.HTTPError as error: return type(error).__name__
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                report['warmup_statuses'] = dict(Counter(executor.map(warm, range(args.concurrency))))
            for i in range(args.waves):
                wait_ready()
                result = wave(client, key['key'], args.concurrency, i+1, args.case)
                report['waves'].append(result); save(args.output, report)
                print(json.dumps({k:v for k,v in result.items() if k not in ('checks', 'samples')}), flush=True)
        expected = sum(r.get('tool_status') == 'completed' for w in report['waves'] for r in w['checks'])
        report['billing'] = billing(admin, token, user['id'], key['id'], expected)
        report['all_calls_passed'] = all(w['successes'] == args.concurrency for w in report['waves'])
        report['capacity_verified'] = all(w['client_peak'] == args.concurrency for w in report['waves'])
        report['capacity_scope'] = 'concurrent real tool requests; upstream execution is bounded separately'
        report['all_passed'] = (report['all_calls_passed'] and report['capacity_verified']
                                and report['billing']['verified'])
    finally:
        try:
            if key: api.request('/api/v1/keys/'+str(key['id']), token, method='DELETE')
        finally:
            api.request('/api/v1/admin/users/'+str(user['id']), admin, method='DELETE')
            report['temporary_credentials_removed'] = True; save(args.output, report)
    print(json.dumps({'all_passed':report['all_passed'], 'cost_usd':report.get('billing',{}).get('cost_usd'),
                      'output':str(args.output)}), flush=True)
    if not report['all_passed']: raise SystemExit(1)


if __name__ == '__main__':
    main()
