"""Fast, bounded failover for the dedicated Claude/Gemini outbound route."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import fcntl
import json
import logging
from pathlib import Path
import re
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

import yaml
from image_hub.egress_control import route_lock

LOG = logging.getLogger('egress-watchdog')
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / 'deployment/private/config/egress-watchdog.json'


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Controller and admin endpoints must be local HTTP addresses')
    return value.rstrip('/')


class APIError(RuntimeError):
    pass


class JSONClient:
    def __init__(self, base, token='', timeout=3):
        self.base, self.token, self.timeout = local_url(base), token, timeout
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, method='GET', body=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(self.base + path, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_048_576)
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            # Never include response bodies, credentials, or URLs in logs.
            raise APIError(f'HTTP {error.code}') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise APIError('Connection failed or invalid JSON') from None


@dataclass(frozen=True)
class Node:
    name: str
    provider: str


class Controller:
    def __init__(self, config):
        self.config = config
        mihomo = yaml.safe_load(Path(config['mihomo_config']).read_text())
        self.api = JSONClient('http://' + mihomo['external-controller'],
                              mihomo.get('secret', ''), config['timeout_ms'] / 1000 + .5)
        self.group_path = '/proxies/' + quote(config['group'], safe='')
        self.allowed = re.compile(config['node_pattern'])

    def snapshot(self):
        group = self.api.request(self.group_path)
        if group.get('type') not in ('Fallback', 'Selector'):
            raise APIError('Unsupported outbound group')
        names = {name for name in group.get('all', []) if self.allowed.fullmatch(name)}
        providers = self.api.request('/providers/proxies').get('providers', {})
        nodes = [Node(proxy['name'], provider)
                 for provider, data in providers.items()
                 if provider in self.config['providers']
                 for proxy in data.get('proxies', []) if proxy['name'] in names]
        return group.get('now'), nodes

    def probe(self, node):
        def check(endpoint):
            query = urlencode({'url': endpoint['url'], 'expected': endpoint['expected'],
                               'timeout': self.config['timeout_ms']})
            path = ('/providers/proxies/' + quote(node.provider, safe='') + '/' +
                    quote(node.name, safe='') + '/healthcheck?' + query)
            try:
                value = self.api.request(path).get('delay')
                return value if isinstance(value, (int, float)) and value > 0 else None
            except APIError:
                return None
        # Each route must reach BOTH OAuth and model traffic, without selecting it.
        with ThreadPoolExecutor(max_workers=len(self.config['probes'])) as executor:
            delays = list(executor.map(check, self.config['probes']))
        return max(delays) if all(d is not None for d in delays) else None

    def select(self, node):
        with route_lock(self.config):
            if node.name not in self.api.request(self.group_path).get('all', []):
                raise APIError('Candidate was removed by a newer qualified pool')
            self.api.request(self.group_path, 'PUT', {'name': node.name})


class AccountRecovery:
    """Clear only network-induced holds on accounts bound to this proxy."""
    def __init__(self, config):
        self.config = config
        self.api = JSONClient(config['base_url'], timeout=config.get('timeout', 8))

    def request(self, path, method='GET', body=None):
        if not self.api.token:
            env = {}
            for line in Path(self.config['env_file']).read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    name, value = line.split('=', 1)
                    env[name] = value.strip().strip('\"\'')
            login = self.api.request('/api/v1/auth/login', 'POST', {
                'email': env['ADMIN_EMAIL'], 'password': env['ADMIN_PASSWORD']})
            self.api.token = login['data']['access_token']
        try:
            result = self.api.request(path, method, body)
        except APIError as error:
            if str(error) == 'HTTP 401':
                self.api.token = ''
            raise
        if result.get('code') != 0:
            raise APIError('Admin operation failed')
        return result.get('data')

    def run(self):
        for account_id in self.config['account_ids']:
            try:
                path = f'/api/v1/admin/accounts/{account_id}'
                account = self.request(path)
                reason = (account.get('temp_unschedulable_reason') or '').lower()
                if (account.get('platform') != 'antigravity' or
                        account.get('status') != 'active' or
                        account.get('proxy_id') != self.config['proxy_id'] or
                        not account.get('temp_unschedulable_until') or
                        not any(term in reason for term in ('proxy unavailable',
                                                            'tls handshake timeout')) or
                        any(term in reason for term in ('invalid_grant', 'unauthorized',
                                                        'quota', 'rate limit'))):
                    continue
                self.request(path + '/refresh', 'POST', {})
                self.request(path + '/temp-unschedulable', 'DELETE')
                LOG.info('network_hold_cleared account_id=%s', account_id)
            except (APIError, OSError, KeyError):
                LOG.warning('account_recovery_pending account_id=%s', account_id)


class Watchdog:
    def __init__(self, controller, config, clock=time.monotonic, recover=None):
        self.controller, self.config, self.clock = controller, config, clock
        self.recover = recover
        self.failed = {}
        self.cooldown = {}
        self.status = {}
        self.last_recovery = float('-inf')
        self.last_latency_probe = float('-inf')
        self.recovery_executor = ThreadPoolExecutor(max_workers=1)
        self.recovery_future = None
        self.pinned = None
        self.persistence_failed = False

    def publish(self, **fields):
        before = (self.status.get('state'), self.status.get('node'))
        self.status.update(fields, checked_at=time.time())
        after = (self.status.get('state'), self.status.get('node'))
        if after != before:
            LOG.info('route_state state=%s node=%s', *after)
        path = self.config.get('state_file')
        if path:
            destination = Path(path)
            temp = destination.with_suffix('.tmp')
            try:
                temp.write_text(json.dumps(self.status, ensure_ascii=False, indent=2) + '\n')
                temp.chmod(0o600)
                temp.replace(destination)
            except OSError as error:
                # Status persistence is diagnostic. A full/unwritable disk must
                # not stop route health checks, or fail again in the error path.
                if not self.persistence_failed:
                    LOG.warning('route_status_persistence_failed errno=%s', error.errno)
                self.persistence_failed = True
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                if self.persistence_failed:
                    LOG.info('route_status_persistence_recovered')
                self.persistence_failed = False
        return self.status.copy()

    def healthy(self, node, delay, **fields):
        # Keep a verified healthy route; native fallback still escapes a dead pin.
        if self.pinned != node.name:
            self.controller.select(node)
            self.pinned = node.name
        previous = self.status.get('node')
        if previous and previous != node.name:
            fields.setdefault('previous_node', previous)
            fields.setdefault('last_switch_at', time.time())
        self.failed.clear()
        now = self.clock()
        if (self.recover and now - self.last_recovery >= self.config['recovery_interval']
                and (self.recovery_future is None or self.recovery_future.done())):
            self.last_recovery = now
            self.recovery_future = self.recovery_executor.submit(self.recover.run)
        return self.publish(state='healthy', node=node.name, delay_ms=delay, **fields)

    def tick(self):
        current, nodes = self.controller.snapshot()
        node = next((node for node in nodes if node.name == current), None)
        if not nodes:
            return self.publish(state='no_candidates', node=current)
        delay = self.controller.probe(node) if node else None
        if delay is not None:
            self.cooldown.pop(node.name, None)
            faster = self.prefer_faster(node, delay, nodes)
            if faster is not None:
                return faster
            return self.healthy(node, delay)
        self.failed[current] = self.failed.get(current, 0) + 1
        if node and self.failed[current] < self.config['failure_threshold']:
            return self.publish(state='suspect', node=current)
        started = self.clock()
        if node:
            self.cooldown[node.name] = started + self.config['cooldown_seconds']
        candidates = [node for node in nodes if node.name != current and
                      self.cooldown.get(node.name, 0) <= started]
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(candidates)))) as executor:
            checked = list(executor.map(self.controller.probe, candidates))
        usable = sorted(((delay, node) for node, delay in zip(candidates, checked)
                         if delay is not None), key=lambda item: item[0])
        for latency, alternative in usable:
            self.controller.select(alternative)
            # Re-read the group: a concurrent native fallback/config reload may win.
            selected, refreshed = self.controller.snapshot()
            actual = next((node for node in refreshed if node.name == selected), None)
            verified = self.controller.probe(actual) if actual else None
            if verified is not None:
                elapsed = round((self.clock() - started) * 1000)
                LOG.info('failover_verified old=%s new=%s recovery_ms=%s',
                         current, actual.name, elapsed)
                self.last_recovery = float('-inf')
                return self.healthy(actual, verified, previous_node=current,
                                    last_switch_at=time.time(), recovery_ms=elapsed)
            self.cooldown[alternative.name] = self.clock() + self.config['cooldown_seconds']
        return self.publish(state='no_healthy_nodes', node=current)

    def prefer_faster(self, current, delay, nodes):
        threshold = self.config.get('max_healthy_delay_ms', 0)
        now = self.clock()
        if (not threshold or delay <= threshold or
                now - self.last_latency_probe < self.config.get('latency_probe_interval', 30)):
            return None
        self.last_latency_probe = now
        candidates = [node for node in nodes if node.name != current.name and
                      self.cooldown.get(node.name, 0) <= now]
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(candidates)))) as executor:
            checked = list(executor.map(self.controller.probe, candidates))
        usable = sorted(((latency, node) for node, latency in zip(candidates, checked)
                         if latency is not None and latency <= delay * .7 and delay - latency >= 100),
                        key=lambda item: item[0])
        for _, alternative in usable:
            self.controller.select(alternative)
            selected, refreshed = self.controller.snapshot()
            actual = next((node for node in refreshed if node.name == selected), None)
            verified = self.controller.probe(actual) if actual else None
            if verified is not None and verified <= delay * .7 and delay - verified >= 100:
                LOG.info('faster_route_verified old=%s new=%s old_delay_ms=%s new_delay_ms=%s',
                         current.name, actual.name, delay, verified)
                return self.healthy(actual, verified, previous_node=current.name,
                                    last_switch_at=time.time(), switch_reason='latency')
            # Keep the already verified working route if an alternative failed.
            # Slow healthy routes remain eligible for availability failover.
            self.controller.select(current)
            self.pinned = None
        return None

    def close(self):
        self.recovery_executor.shutdown(wait=True)


def load_config(path):
    config = json.loads(Path(path).read_text())
    if (not 0 < config['interval'] <= 60 or not 0 < config['timeout_ms'] <= 5000 or
            config['failure_threshold'] < 1 or config['cooldown_seconds'] < 0 or
            not config['probes'] or len(config['probes']) > 4):
        raise ValueError('Invalid watchdog timing or probes')
    if (not 0 <= config.get('max_healthy_delay_ms', 0) <= config['timeout_ms'] or
            not 10 <= config.get('latency_probe_interval', 30) <= 3600):
        raise ValueError('Invalid latency preference settings')
    for endpoint in config['probes']:
        parsed = urlsplit(endpoint['url'])
        if parsed.scheme != 'https' or parsed.hostname not in (
                'oauth2.googleapis.com', 'daily-cloudcode-pa.googleapis.com'):
            raise ValueError('Health probes must use the actual Google service hosts')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--once', action='store_true', help='Check once and exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(args.config)
    lock_path = Path(config['state_file']).with_suffix('.lock')
    lock = lock_path.open('a')
    lock_path.chmod(0o600)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.error('A watchdog for this state file is already running')
        return 2
    recover = AccountRecovery(config['account_recovery']) if config.get('account_recovery') else None
    watcher = Watchdog(Controller(config), config, recover=recover)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                status = watcher.tick()
            except (APIError, OSError, KeyError, ValueError):
                status = watcher.publish(state='controller_unavailable', node=None)
            if args.once:
                print(json.dumps(status, ensure_ascii=False))
                return 0 if status['state'] == 'healthy' else 1
            stop.wait(max(.1, config['interval'] - (time.monotonic() - started)))
    finally:
        watcher.close()
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
