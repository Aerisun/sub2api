"""Read-only website/API latency and actual entry-asset checks; no model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit

import httpx

from ops.admin_api import ROOT, login
from ops.web import entry_assets

PUBLIC_PATHS = ('/health', '/api/v1/settings/public', '/setup/status')
USER_PATHS = ('/api/v1/auth/me', '/api/v1/admin/dashboard/snapshot-v2',
              '/api/v1/admin/accounts?page_size=20', '/api/v1/user/chat/conversations')


def probe(client, url, headers=None, method='GET', expected_type=None):
    started = time.perf_counter()
    row = {'path': urlsplit(url).path, 'ok': False}
    try:
        with client.stream(method, url, headers=headers) as response:
            row['headers_ms'] = round((time.perf_counter()-started)*1000, 2)
            response.read()
            row.update(status=response.status_code, protocol=response.http_version,
                       content_type=response.headers.get('content-type', '').split(';')[0],
                       cache_control=response.headers.get('cache-control'),
                       encoding=response.headers.get('content-encoding'),
                       wire_bytes=response.num_bytes_downloaded, bytes=len(response.content))
            row['ok'] = response.status_code == 200
            if expected_type:
                row['ok'] = row['ok'] and row['content_type'] in expected_type
            if method == 'GET' and row['content_type'] == 'application/json':
                value = response.json()
                row['ok'] = row['ok'] and (value.get('code', 0) == 0) and not value.get('error')
                if row['path'].endswith('/health'):
                    row['ok'] = row['ok'] and value.get('status') == 'ok'
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as error:
        row['ok'] = False
        row['error_type'] = type(error).__name__
    row['total_ms'] = round((time.perf_counter()-started)*1000, 2)
    return row


def check_entries(client, public_url, method='GET'):
    rows = []
    for entry in ('index.html', 'chat-ui/index.html'):
        url = public_url.rstrip('/') + '/' + entry
        started = time.perf_counter()
        try:
            response = client.get(url)
            response.raise_for_status()
            if 'text/html' not in response.headers.get('content-type', ''):
                raise ValueError('Entry is not HTML')
            assets = entry_assets(response.text, url, public_url)
            rows.append({'path': urlsplit(url).path, 'ok': True, 'assets': len(assets),
                         'total_ms': round((time.perf_counter()-started)*1000, 2)})
        except (httpx.HTTPError, ValueError) as error:
            rows.append({'path': urlsplit(url).path, 'ok': False, 'error_type': type(error).__name__,
                         'total_ms': round((time.perf_counter()-started)*1000, 2)})
            continue
        for asset in assets:
            types = ('text/css',) if asset.endswith('.css') else ('application/javascript', 'text/javascript')
            rows.append(probe(client, public_url.rstrip('/')+'/'+asset, method=method, expected_type=types))
    return rows


def measure(public_url, samples=5, authenticated=False, head_assets=False, request_timeout=12):
    token = login() if authenticated else None
    headers = {'Authorization': 'Bearer '+token} if token else {}
    origins = {'gateway': 'http://127.0.0.1:8080', 'web': 'http://127.0.0.1:8081', 'public': public_url.rstrip('/')}
    paths = PUBLIC_PATHS + (USER_PATHS if token else ())

    def one_origin(item):
        name, origin = item
        rows = []
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(request_timeout, connect=min(4, request_timeout))) as client:
            for path in paths:
                runs = [probe(client, origin+path, headers=headers, expected_type=('application/json',))
                        for _ in range(samples)]
                ordered = sorted(r['total_ms'] for r in runs)
                rows.append({'origin': name, 'endpoint': path, 'ok': all(r['ok'] for r in runs),
                             'median_ms': statistics.median(ordered),
                             'p95_ms': ordered[math.ceil(.95*len(ordered))-1], 'samples': runs})
        return rows

    with ThreadPoolExecutor(max_workers=3) as executor:
        checks = [row for group in executor.map(one_origin, origins.items()) for row in group]
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(request_timeout, connect=min(4, request_timeout))) as client:
        entries = check_entries(client, public_url, 'HEAD' if head_assets else 'GET')
    return {'at': datetime.now(timezone.utc).isoformat(), 'public_url': public_url,
            'samples_per_endpoint': samples, 'checks': checks, 'entries': entries,
            'all_calls_passed': all(row['ok'] for row in checks+entries), 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, choices=range(1, 31), default=5)
    parser.add_argument('--authenticated', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Choose a new report path to retain previous evidence')
    config = json.loads((ROOT/'deployment/site.json').read_text())
    report = measure(config['public_url'], args.samples, args.authenticated)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    args.output.chmod(0o600)
    print(json.dumps({'all_calls_passed': report['all_calls_passed'],
                      'checked_endpoints': len(report['checks']), 'checked_entry_resources': len(report['entries'])}))
    if not report['all_calls_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
