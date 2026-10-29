"""Real Responses turns with a growing synthetic context; never replay user content."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets
import socket
import statistics
import time

import httpx

from ops import admin_api as api

PUBLIC = 'https://aerisun.top/sub2api'


def temporary_user_active(user_id):
    """Inspect only this diagnostic user's leases before deleting its counters."""
    env = api.read_env(api.ROOT/'deployment/private/config/runtime.env')
    with socket.create_connection((env['REDIS_HOST'], int(env['REDIS_PORT'])), timeout=3) as connection:
        stream = connection.makefile('rb')
        def command(*items):
            values = [str(item).encode() for item in items]
            connection.sendall(('*'+str(len(values))+'\r\n').encode()+b''.join(
                ('$'+str(len(value))+'\r\n').encode()+value+b'\r\n' for value in values))
            result = stream.readline().strip()
            if result.startswith(b'-') or not result:
                raise RuntimeError('Diagnostic lease inspection failed')
            return int(result[1:]) if result.startswith(b':') else result
        if env.get('REDIS_PASSWORD'):
            command('AUTH', env['REDIS_PASSWORD'])
        return sum(command('ZCARD', prefix+str(user_id))
                   for prefix in ('concurrency:user:', 'concurrency:live:user:'))


def drain_temporary_user(user_id, timeout=180):
    deadline = time.monotonic()+timeout
    try:
        while temporary_user_active(user_id):
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def synthetic_context(kib, seed):
    lines = []
    for index in range(kib * 1024 // 130 + 1):
        digest = hashlib.sha256(f'{seed}-{index}'.encode()).hexdigest()
        lines.append(f'Reference record {index:05d}: synthetic fixture {digest}; this record needs no action.\n')
    return ''.join(lines)[:kib * 1024]


def run_turn(client, key, model, effort, tier, session, turns, index, stream_lines=0):
    expected = str(200 + index)
    instruction = ('List the integers from 1 through '+str(stream_lines)+
                   ', in order, each on a separate line. Return only those lines.'
                   if stream_lines else 'Reply with '+expected+' only.')
    body = {'model': model, 'instructions': 'The reference records are synthetic background data. '
            'Follow only the last user instruction. Reply with the requested number only.',
            'input': turns + [{'role': 'user', 'content': instruction}],
            'stream': True, 'store': False, 'reasoning': {'effort': effort},
            'prompt_cache_key': session}
    if stream_lines:
        body['instructions'] = ('The reference records are synthetic background data. '
                                'Follow only the last user instruction. Return the requested lines only.')
    if tier:
        body['service_tier'] = tier
    request_id = 'sustained-latency-'+secrets.token_hex(10)
    row = {'turn': index+1, 'ok': False, 'client_request_id': request_id,
           'request_bytes': len(json.dumps(body).encode()), 'first_semantic_ms': None,
           'first_text_ms': None, 'max_semantic_gap_ms': 0}
    started = time.monotonic()
    answer, event_names, last = '', set(), None
    try:
        with client.stream('POST', PUBLIC+'/v1/responses', json=body,
                headers={'Authorization': 'Bearer '+key, 'X-Client-Request-Id': request_id,
                         'session_id': session}) as response:
            row.update(status=response.status_code, headers_ms=round((time.monotonic()-started)*1000),
                       gateway_request_id=response.headers.get('X-Request-Id'))
            if response.status_code != 200:
                response.read()
                row['error_type'] = 'http_error'
                try:
                    row['error_message'] = response.json().get('error', {}).get('message', '')[:250]
                except (ValueError, AttributeError):
                    pass
            else:
                for line in response.iter_lines():
                    if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                        continue
                    event = json.loads(line[5:])
                    kind = event.get('type', '')
                    event_names.add(kind)
                    elapsed = round((time.monotonic()-started)*1000)
                    if kind == 'response.output_text.delta':
                        # Preserve standalone newlines/spaces in the answer;
                        # only the latency clock ignores empty semantic chunks.
                        answer += event.get('delta', '')
                    if kind in ('response.output_text.delta', 'response.reasoning_summary_text.delta',
                                'response.reasoning_text.delta') and event.get('delta', '').strip():
                        if row['first_semantic_ms'] is None:
                            row['first_semantic_ms'] = elapsed
                        if last is not None:
                            row['max_semantic_gap_ms'] = max(row['max_semantic_gap_ms'], elapsed-last)
                        last = elapsed
                        if kind == 'response.output_text.delta':
                            if row['first_text_ms'] is None:
                                row['first_text_ms'] = elapsed
                    if kind == 'response.completed':
                        result = event.get('response') or {}
                        usage = result.get('usage') or {}
                        row.update(completed=True, usage={k:usage[k] for k in ('input_tokens',
                            'input_tokens_details','output_tokens','output_tokens_details','total_tokens') if k in usage},
                                   response_model=result.get('model'), response_tier=result.get('service_tier'))
                        break
                    if kind in ('error', 'response.failed', 'response.incomplete'):
                        row['error_type'] = kind
                        break
        correct = (answer.split() == list(map(str, range(1, stream_lines+1)))
                   if stream_lines else answer.strip() == expected)
        row.update(answer_correct=correct,
                   answer_characters=len(answer), answer_lines=len(answer.splitlines()),
                   ok=bool(correct and row.get('completed') and row.get('usage')
                           and row.get('response_model') == model))
        if row.get('completed') and not row['ok'] and 'error_type' not in row:
            row['error_type'] = 'unexpected_output' if not correct else 'unexpected_model_or_usage'
        elif not row.get('completed') and 'error_type' not in row:
            row['error_type'] = 'incomplete_stream'
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
        row['error_type'] = type(error).__name__
    row.update(total_ms=round((time.monotonic()-started)*1000), event_types=sorted(event_names))
    turns.extend([{'role': 'user', 'content': instruction},
                  {'role': 'assistant', 'content': answer}])
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--effort', default='xhigh', choices=('low','medium','high','xhigh','max'))
    parser.add_argument('--tier', default='priority', choices=('', 'default','priority'))
    parser.add_argument('--group-id', type=int, default=103)
    parser.add_argument('--turns', type=int, default=4, choices=range(1, 7))
    parser.add_argument('--context-kib', type=int, default=128, choices=range(16, 513))
    parser.add_argument('--seed', default='sustained-synthetic-20261029')
    parser.add_argument('--interval-seconds', type=float, default=2)
    parser.add_argument('--stream-lines', type=int, default=0, choices=(0, 200, 600))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not 0 <= args.interval_seconds <= 30:
        parser.error('Use a new output path and an interval between zero and 30 seconds')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'model': args.model,
              'effort': args.effort, 'requested_tier': args.tier, 'group_id': args.group_id,
              'context_kib': args.context_kib, 'synthetic_only': True, 'checks': []}
    report['stream_lines'] = args.stream_lines
    def save():
        pending = args.output.with_suffix('.pending')
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        pending.chmod(0o600)
        pending.replace(args.output)
    admin = api.login()
    password = secrets.token_urlsafe(32)
    user = api.request('/api/v1/admin/users', admin, {'email':'sustained-check-'+secrets.token_hex(8)+'@example.invalid',
        'password':password, 'role':'user','balance':3,'concurrency':1,
        'allowed_groups':[args.group_id], 'restrict_public_groups':True}, 'POST')
    token = key = None
    try:
        token = api.request('/api/v1/auth/login', payload={'email':user['email'], 'password':password})['access_token']
        key = api.request('/api/v1/keys', token, {'name':'temporary-synthetic-session-latency',
            'group_id':args.group_id,'quota':3,'expires_in_days':1,'allowed_models':[args.model]}, 'POST')
        turns = [{'role':'user', 'content':synthetic_context(args.context_kib, args.seed)}]
        session = 'sustained-synthetic-'+secrets.token_hex(12)
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(150, connect=10)) as client:
            for index in range(args.turns):
                row = run_turn(client, key['key'], args.model, args.effort, args.tier, session, turns, index, args.stream_lines)
                report['checks'].append(row)
                save()
                print(json.dumps({k:row.get(k) for k in ('turn','ok','status','first_semantic_ms','first_text_ms','total_ms','error_type')}), flush=True)
                if not row['ok']:
                    break
                if index+1 < args.turns:
                    time.sleep(args.interval_seconds)
        usage = api.request(f"/api/v1/admin/usage?api_key_id={key['id']}&page=1&page_size=50", admin)['items']
        report['usage'] = [{k:r.get(k) for k in ('model','upstream_model','account_id','input_tokens',
            'cache_read_tokens','output_tokens','reasoning_effort','service_tier','actual_cost','first_token_ms','duration_ms')} for r in usage]
        report['cost_usd'] = str(sum((Decimal(str(r['actual_cost'])) for r in usage), Decimal(0)))
        report['all_passed'] = len(report['checks']) == args.turns and all(r['ok'] for r in report['checks'])
        for field in ('first_semantic_ms','first_text_ms','total_ms'):
            values = [r[field] for r in report['checks'] if r['ok'] and r[field] is not None]
            report[field+'_p50'] = statistics.median(values) if values else None
    finally:
        report['temporary_credentials_removed'] = False
        if drain_temporary_user(user['id']):
            if key:
                api.request('/api/v1/keys/'+str(key['id']), token, method='DELETE')
            api.request('/api/v1/admin/users/'+str(user['id']), admin, method='DELETE')
            report['temporary_credentials_removed'] = True
        else:
            # Preserve account counters while an accepted inference may finish;
            # only non-secret IDs are needed for subsequent private cleanup.
            report['cleanup_deferred'] = {'user_id': user['id'], 'key_id': key['id'] if key else None}
        save()
    print(json.dumps({k:v for k,v in report.items() if k not in ('checks','usage')}), flush=True)
    if not report.get('all_passed') or not report.get('temporary_credentials_removed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
