"""Qualify subscription changes before atomically replacing live proxy pools."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, ProxyHandler

import yaml

from image_hub.egress_watchdog import JSONClient
from image_hub.egress_control import route_lock

ROOT = Path(__file__).resolve().parent.parent
REGIONS = r'^(?:NA-UnitedStates-|ASIA-Japan-|ASIA-Singapore-).+$'
ALLOWED_COUNTRIES = {'US', 'JP', 'SG'}
LOG = logging.getLogger('proxy-qualifier')
LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)


def read_yaml(path):
    return yaml.load(Path(path).read_bytes(), Loader=LOADER)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    with pending.open('wb') as file:
        os.chmod(pending, 0o600)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    pending.replace(path)


def eligible_nodes(document):
    result = {}
    for node in document.get('proxies', []):
        name = node.get('name', '')
        if re.match(REGIONS, name) and node.get('type') not in ('direct', 'reject', 'compatible'):
            result[name] = node
    return list(result.values())


def fingerprint(nodes):
    value = json.dumps(sorted(nodes, key=lambda n: n['name']), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def publish_pool_generation(config, role, client=None):
    """Publish the selected qualified exit; never touch active connections."""
    filename = config.get('upstream_route_state_file')
    if not filename:
        return False
    client = client or controller(role)
    selected = client.request('/proxies/' + quote(role['group'], safe='')).get('now')
    nodes = read_yaml(role['provider_file']).get('proxies', [])
    node = next((node for node in nodes if node.get('name') == selected), None)
    if node is None:
        return False
    proxy = read_yaml(role['mihomo_config'])
    port = proxy.get('mixed-port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid qualified proxy listener')
    path = Path(filename)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Live role checks run concurrently. Serialize the read/merge/rename so
    # publishing Codex cannot erase Gemini's last verified generation.
    with path.with_name(path.name+'.lock').open('a+b') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open('rb') as source:
                raw = source.read(32769)
            if len(raw) > 32768:
                raise ValueError('Oversized upstream route snapshot')
            previous = json.loads(raw)
            if (not isinstance(previous, dict) or previous.get('version') != 1
                    or not isinstance(previous.get('routes'), dict) or len(previous['routes']) > 64
                    or any(not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', key)
                           or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                           for key, value in previous['routes'].items())):
                raise ValueError('Invalid upstream route snapshot')
            previous = {'version': 1, 'routes': previous['routes']}
        except (FileNotFoundError, ValueError):
            # Reconstruct corrupt state from verified live selections; readers
            # keep their last good generations until each role publishes again.
            previous = {'version': 1, 'routes': {}}
        key = 'http://127.0.0.1:'+str(port)
        generation = fingerprint([node])
        if previous['routes'].get(key) == generation:
            return False
        previous['routes'][key] = generation
        atomic_write(path, (json.dumps(previous, sort_keys=True)+'\n').encode())
    return True


def pool_generation_report(config, role, client=None):
    try:
        return {'route_generation_changed': publish_pool_generation(config, role, client)}
    except Exception as error:
        # A marker/storage failure cannot undo a working route or prevent the
        # normal network checks and recovery from continuing.
        return {'route_state_error_type': type(error).__name__}


def watch_pool_generation(config, role, stop):
    """Track automatic fallback even while qualification is probing models."""
    previous_error = None
    while not stop.is_set():
        try:
            report = pool_generation_report(config, role, controller(role, timeout=2))
        except Exception as error:
            report = {'route_state_error_type': type(error).__name__}
        error = report.get('route_state_error_type')
        if error and error != previous_error:
            LOG.warning('route_generation_deferred role=%s error_type=%s', role['name'], error)
        previous_error = error
        stop.wait(2)


def controller(role, timeout=12):
    config = read_yaml(role['mihomo_config'])
    return JSONClient('http://' + config['external-controller'], config.get('secret', ''), timeout=timeout)


def wait_pool(client, group_path, names, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = client.request(group_path)
        if set(value.get('all', [])) == set(names):
            return value
        time.sleep(.15)
    raise RuntimeError('Qualified provider did not load the expected node set')


def install_pool(role, nodes, client=None):
    """Keep the old selected member present until the new winner is selected."""
    if not nodes:
        return False
    with route_lock(role):
        return _install_pool(role, nodes, client)


def _install_pool(role, nodes, client=None):
    path = Path(role['provider_file'])
    old = path.read_bytes()
    old_nodes = read_yaml(path)['proxies']
    client = client or controller(role)
    endpoint = '/proxies/' + quote(role['group'], safe='')
    previous = client.request(endpoint)['now']
    names = [n['name'] for n in nodes]
    if old_nodes == nodes and previous == names[0]:
        return True
    merged = {n['name']: n for n in old_nodes}
    merged.update({n['name']: n for n in nodes})
    try:
        atomic_write(path, yaml.safe_dump({'proxies': list(merged.values())}, sort_keys=False).encode())
        wait_pool(client, endpoint, merged)
        client.request(endpoint, 'PUT', {'name': names[0]})
        atomic_write(path, yaml.safe_dump({'proxies': nodes}, sort_keys=False).encode())
        current = wait_pool(client, endpoint, names)
        if current['now'] != names[0]:
            client.request(endpoint, 'PUT', {'name': names[0]})
        if client.request(endpoint)['now'] != names[0]:
            raise RuntimeError('Qualified route selection was not acknowledged')
        LOG.info('qualified_pool_applied role=%s primary=%s members=%s',
                 role['name'], names[0], ','.join(names))
        return True
    except BaseException:
        atomic_write(path, old)
        wait_pool(client, endpoint, [n['name'] for n in old_nodes])
        if previous in {n['name'] for n in old_nodes}:
            client.request(endpoint, 'PUT', {'name': previous})
        raise


@contextmanager
def isolated_proxy(nodes, config):
    with tempfile.TemporaryDirectory(prefix='sub2api-qualification-') as directory:
        root = Path(directory)
        value = {'mixed-port': config['probe_port'], 'allow-lan': False,
                 'bind-address': '127.0.0.1', 'mode': 'rule', 'ipv6': False,
                 'log-level': 'silent', 'external-controller': '127.0.0.1:' + str(config['probe_controller_port']),
                 'secret': os.urandom(24).hex(), 'profile': {'store-selected': False},
                 'proxies': nodes, 'proxy-groups': [{'name': 'Qualification', 'type': 'select',
                    'proxies': [n['name'] for n in nodes], 'empty-fallback': 'REJECT'}],
                 'rules': ['MATCH,Qualification']}
        path = root / 'config.yaml'
        path.write_text(yaml.safe_dump(value, sort_keys=False))
        process = subprocess.Popen([config['mihomo_binary'], '-d', directory, '-f', str(path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = JSONClient('http://' + value['external-controller'], value['secret'], timeout=7)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Isolated proxy exited')
                try:
                    if set(client.request('/proxies/Qualification').get('all', [])) == {n['name'] for n in nodes}:
                        break
                except Exception:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('Isolated node list did not load')
            yield client, 'http://127.0.0.1:' + str(config['probe_port'])
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def codex_generation(credentials, proxy, version):
    headers = {'Authorization': 'Bearer ' + credentials['access_token'], 'Content-Type': 'application/json',
               'Accept': 'text/event-stream', 'Originator': 'codex_cli_rs', 'Version': version,
               'OpenAI-Beta': 'responses=experimental',
               'User-Agent': 'codex_cli_rs/' + version + ' (Ubuntu 22.4.0; x86_64) xterm-256color'}
    for key in ('access_token', 'id_token'):
        try:
            encoded = credentials[key].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            ident = claims.get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
            if ident:
                headers['ChatGPT-Account-Id'] = ident
                break
        except (KeyError, ValueError, IndexError):
            pass
    payload = {'model': 'gpt-6-sol', 'instructions': 'You are a coding assistant. Reply with OK only.',
               'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Reply with OK only.'}]}],
               'stream': True, 'store': False, 'reasoning': {'effort': 'low'}}
    started = time.monotonic()
    result = {'ok': False}
    request = Request('https://chatgpt.com/backend-api/codex/responses',
                      data=json.dumps(payload).encode(), headers=headers, method='POST')
    opener = build_opener(ProxyHandler({'https': proxy}))
    try:
        reply, done = '', False
        with opener.open(request, timeout=25) as response:
            result['http_status'] = response.status
            for line in response:
                if time.monotonic() - started > 40:
                    break
                if not line.startswith(b'data:') or line[5:].strip() == b'[DONE]':
                    continue
                event = json.loads(line[5:])
                if event.get('type') == 'response.output_text.delta':
                    result.setdefault('first_text_ms', round((time.monotonic() - started) * 1000))
                    reply += event.get('delta', '')
                done |= event.get('type') == 'response.completed'
        result['ok'] = done and reply.strip().upper().rstrip('.!') == 'OK'
    except HTTPError as error:
        result['http_status'] = error.code
    except Exception as error:
        result['error_type'] = type(error).__name__
    result['total_ms'] = round((time.monotonic() - started) * 1000)
    return result


def real_check(role, credentials, proxy, version, confirm=False):
    if role['name'] == 'codex':
        return codex_generation(credentials, proxy, version)
    from ops.antigravity_probe import probe
    model = 'gemini-3.8-flash-high' if confirm else 'gemini-3.8-flash-low'
    result = probe(role['account_id'], credentials, proxy, model)
    return {k: result[k] for k in ('ok', 'http_status', 'first_text_ms', 'total_ms') if k in result}


def choose_pool(results, limit):
    qualified = [r for r in results if r.get('ok') and r.get('country') in ALLOWED_COUNTRIES]
    return sorted(qualified, key=lambda r: (r['score_ms'], r['network_ms']))[:limit]


def stable_pool(rows, selected):
    """Keep a freshly verified primary unless the measured gain is substantial."""
    current = next((row for row in rows if row['node'] == selected), None)
    if not rows or current is None or current is rows[0]:
        return rows
    best_ms, current_ms = rows[0]['score_ms'], current['score_ms']
    if current_ms - best_ms < 500 or best_ms > current_ms * .7:
        return [current] + [row for row in rows if row is not current]
    return rows


def diverse_candidates(rank, limit):
    """A region rejecting the API must not consume the entire bounded scan."""
    buckets = [[name for name in rank if name.startswith(prefix)] for prefix in
               ('NA-UnitedStates-', 'ASIA-Japan-', 'ASIA-Singapore-')]
    candidates = []
    while any(buckets) and len(candidates) < limit:
        for bucket in buckets:
            if bucket and len(candidates) < limit:
                candidates.append(bucket.pop(0))
    return candidates


def probe_live_pools(config):
    """Check every installed spare without selecting it or submitting inference."""
    def check(role):
        report = {'network_ok': False, 'healthy_nodes': [], 'checked_at': time.time()}
        try:
            client = controller(role)
            group = client.request('/proxies/' + quote(role['group'], safe=''))
            report['selected'] = group.get('now')
            providers = client.request('/providers/proxies').get('providers', {})
            owners = {node['name']: name for name, provider in providers.items()
                      for node in provider.get('proxies', [])}
            url, expected = (('https://oauth2.googleapis.com/token', '404') if role['name'] == 'gemini'
                             else ('https://chatgpt.com/backend-api/codex/responses', '405'))
            query = urlencode({'url': url, 'expected': expected,
                               'timeout': config.get('live_probe_timeout_ms', 2500)})
            def probe(name):
                try:
                    path = ('/providers/proxies/' + quote(owners[name], safe='') + '/' + quote(name, safe='') + '/healthcheck?'
                            if name in owners else '/proxies/' + quote(name, safe='') + '/delay?')
                    delay = client.request(path + query).get('delay')
                    return name if isinstance(delay, (int, float)) and delay > 0 else None
                except Exception:
                    return None
            names = group.get('all', [])
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(names)))) as pool:
                report['healthy_nodes'] = [name for name in pool.map(probe, names) if name]
            report['network_ok'] = bool(report['healthy_nodes'])
            report.update(pool_generation_report(config, role, client))
        except Exception as error:
            report['error_type'] = type(error).__name__
        return role['name'], report
    with ThreadPoolExecutor(max_workers=max(1, len(config['roles']))) as pool:
        return dict(pool.map(check, config['roles']))


def probe_live_models(config, health):
    """Small synthetic generations catch an API/region failure behind healthy TLS."""
    from ops.credentials import accounts
    from ops import admin_api as admin
    from ops import admin_api as auth
    roles = [r for r in config['roles'] if health.get(r['name'], {}).get('network_ok')]
    if not roles:
        return {}
    credentials = accounts([r['account_id'] for r in roles])
    token = auth.login()
    settings = admin.request('/api/v1/admin/settings', token)
    version = settings.get('openai_codex_client_version') or settings.get('openai_codex_client_version_synced') or '0.163.0'
    def check(role):
        try:
            proxy = read_yaml(role['mihomo_config'])
            result = real_check(role, credentials[role['account_id']],
                                'http://127.0.0.1:' + str(proxy['mixed-port']), version)
        except Exception as error:
            result = {'ok': False, 'error_type': type(error).__name__}
        result['checked_at'] = time.time()
        return role['name'], result
    with ThreadPoolExecutor(max_workers=len(roles)) as pool:
        return dict(pool.map(check, roles))


def qualification_due(config, previous, signature, now, unhealthy=False):
    retry_at = previous.get('retry_at', 0)
    if retry_at and now < retry_at:
        return signature != previous.get('source_fingerprint')
    return (signature != previous.get('source_fingerprint') or unhealthy or bool(retry_at) or
            now - previous.get('checked_at', 0) >= config.get('revalidate_seconds', 3600))


def reconcile(config, validate=None):
    """A bad/empty subscription never replaces a working qualified pool."""
    source = eligible_nodes(read_yaml(config['subscription_file']))
    if not source:
        return {'state': 'retained', 'reason': 'no_allowed_subscription_nodes', 'roles': {}}
    source_hash = fingerprint(source)
    role_reports = validate(source) if validate else evaluate(config, source)
    report = {'state': 'completed', 'source_fingerprint': source_hash, 'checked_at': time.time(), 'roles': {}}
    for role in config['roles']:
        results = role_reports.get(role['name'], [])
        pool = choose_pool(results, config.get('pool_size', 3))
        if pool:
            if role.get('mihomo_config'):
                selected = controller(role).request('/proxies/' + quote(role['group'], safe=''))['now']
                pool = stable_pool(pool, selected)
            install_pool(role, [r['config'] for r in pool])
        report['roles'][role['name']] = {'state': 'applied' if pool else 'retained',
            'primary': pool[0]['node'] if pool else None,
            'pool': [r['node'] for r in pool],
            'results': [{k: v for k, v in r.items() if k != 'config'} for r in results]}
        if pool:
            report['roles'][role['name']].update(pool_generation_report(config, role))
    return report


def evaluate(config, source):
    from ops.credentials import accounts
    from ops import admin_api as admin
    from ops import admin_api as auth
    token = auth.login()
    for role in config['roles']:
        current = accounts([role['account_id']])[role['account_id']]
        expires = int(current.get('expires_at') or 0)
        if not expires:
            try:
                encoded = current['access_token'].split('.')[1]
                expires = int(json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))).get('exp', 0))
            except (KeyError, ValueError, IndexError):
                pass
        if expires and expires <= time.time() + 120:
            admin.request('/api/v1/admin/accounts/' + str(role['account_id']) + '/refresh', token, {}, 'POST')
    credentials = accounts([r['account_id'] for r in config['roles']])
    settings = admin.request('/api/v1/admin/settings', token)
    version = settings.get('openai_codex_client_version') or settings.get('openai_codex_client_version_synced') or '0.163.0'
    all_nodes = {n['name']: n for n in source}
    for role in config['roles']:
        for node in eligible_nodes(read_yaml(role['provider_file'])):
            all_nodes.setdefault(node['name'], node)
    deadline = time.monotonic() + config.get('qualification_budget_seconds', 180)
    with isolated_proxy(list(all_nodes.values()), config) as (client, proxy):
        def network(node):
            values = {}
            for name, url, expected in (
                ('gemini', 'https://oauth2.googleapis.com/token', '404'),
                ('codex', 'https://chatgpt.com/cdn-cgi/trace', '200')):
                query = urlencode({'url': url, 'expected': expected, 'timeout': 4000})
                try:
                    values[name] = client.request('/proxies/' + quote(node, safe='') + '/delay?' + query).get('delay')
                except Exception:
                    values[name] = None
            return node, values
        with ThreadPoolExecutor(max_workers=6) as pool:
            delays = dict(pool.map(network, all_nodes))
        reports = {}
        for role in config['roles']:
            rank = sorted([n for n in all_nodes if delays[n].get(role['name'])],
                          key=lambda n: delays[n][role['name']])
            candidates = diverse_candidates(rank, config.get('max_candidates_per_role', 12))
            rows = []
            opener = build_opener(ProxyHandler({'https': proxy}))
            for name in candidates:
                if time.monotonic() >= deadline:
                    break
                client.request('/proxies/Qualification', 'PUT', {'name': name})
                row = {'node': name, 'network_ms': delays[name][role['name']],
                       'country': None, 'ok': False, 'config': all_nodes[name]}
                try:
                    req = Request('https://www.cloudflare.com/cdn-cgi/trace')
                    with opener.open(req, timeout=10) as response:
                        trace = dict(line.split('=', 1) for line in response.read(16384).decode().splitlines() if '=' in line)
                    row['country'] = trace.get('loc')
                    if row['country'] in ALLOWED_COUNTRIES:
                        first = real_check(role, credentials[role['account_id']], proxy, version)
                        row['checks'] = [first]
                        if first['ok']:
                            second = real_check(role, credentials[role['account_id']], proxy, version, confirm=True)
                            row['checks'].append(second)
                            row['ok'] = second['ok']
                            if row['ok']:
                                row['score_ms'] = round(statistics.median(r['total_ms'] for r in row['checks']))
                except Exception as error:
                    row['error_type'] = type(error).__name__
                rows.append(row)
                LOG.info('candidate_checked role=%s node=%s country=%s model_ok=%s',
                         role['name'], name, row['country'], row['ok'])
                if sum(row['ok'] for row in rows) >= config.get('pool_size', 3):
                    break
            reports[role['name']] = rows
        return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'deployment/private/config/proxy-qualifier.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(args.config.read_text())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    state_path = Path(config['state_file'])
    lock = state_path.with_suffix('.lock').open('a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A subscription qualification run is already active')
    if config.get('upstream_route_state_file') and not args.once:
        for role in config['roles']:
            threading.Thread(target=watch_pool_generation, args=(config, role, stop),
                             name='proxy-route-'+role['name'], daemon=True).start()
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    last_live = last_model = float('-inf')
    network_failures, model_failures = {}, {}
    live_health = {}
    seen = None
    since = time.monotonic()
    while not stop.is_set():
        try:
            signature = fingerprint(eligible_nodes(read_yaml(config['subscription_file'])))
            if signature != seen:
                seen, since = signature, time.monotonic()
            ready = args.once or time.monotonic() - since >= config.get('debounce_seconds', 5)
            clock = time.monotonic()
            if not args.once and clock - last_live >= config.get('live_probe_seconds', 15):
                live_health = probe_live_pools(config)
                last_live = clock
                for name, health in live_health.items():
                    network_failures[name] = 0 if health['network_ok'] else network_failures.get(name, 0) + 1
                model_interval = config.get('suspect_model_probe_seconds', 30) if any(model_failures.values()) else config.get('model_probe_seconds', 300)
                if clock - last_model >= model_interval:
                    try:
                        model_results = probe_live_models(config, live_health)
                    except Exception as error:
                        # A failed control-plane lookup must not suspend
                        # transport recovery or pretend models were verified.
                        LOG.warning('live_model_check_deferred error_type=%s', type(error).__name__)
                        model_results = {}
                    last_model = clock
                    for name, result in model_results.items():
                        model_failures[name] = 0 if result.get('ok') else model_failures.get(name, 0) + 1
                    previous['model_health'] = model_results
                previous['live_health'] = live_health
                atomic_write(state_path, (json.dumps(previous, ensure_ascii=False, indent=2) + '\n').encode())
            unhealthy = (max(network_failures.values(), default=0) >= config.get('live_failure_threshold', 2) or
                         max(model_failures.values(), default=0) >= config.get('model_failure_threshold', 2))
            if ready and (args.once or qualification_due(config, previous, signature, time.time(), unhealthy)):
                affected = {name for name in network_failures if network_failures[name] >= config.get('live_failure_threshold', 2)}
                affected.update(name for name in model_failures if model_failures[name] >= config.get('model_failure_threshold', 2))
                same_source = signature == previous.get('source_fingerprint')
                scoped = {**config, 'roles': [r for r in config['roles'] if r['name'] in affected]} if unhealthy and same_source and affected else config
                result = reconcile(scoped)
                if scoped is not config:
                    result['roles'] = {**previous.get('roles', {}), **result['roles']}
                result['source_fingerprint'] = result.get('source_fingerprint', signature)
                result['retry_at'] = time.time() + config.get('recovery_retry_seconds', 60) if any(r['state'] == 'retained' for r in result['roles'].values()) or result['state'] == 'retained' else 0
                result['live_health'] = live_health
                result['model_health'] = previous.get('model_health', {})
                atomic_write(state_path, (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
                previous = result
                network_failures.clear(); model_failures.clear()
                LOG.info('subscription_qualification_finished state=%s', result['state'])
                if args.once:
                    print(json.dumps(result, ensure_ascii=False))
                    return 0
        except Exception as error:
            LOG.error('qualification_failed_existing_pools_retained error_type=%s', type(error).__name__)
            if args.once:
                raise
            stop.wait(config.get('recovery_retry_seconds', 60))
        stop.wait(config.get('poll_seconds', 10))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
