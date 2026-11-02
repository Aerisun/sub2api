"""Independent GPT failover using qualified exits and real generation checks."""
import argparse
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from image_hub.egress_control import route_lock
from image_hub.egress_watchdog import APIError, AccountRecovery, Controller, Watchdog
from image_hub.proxy_qualifier import (ROOT, REGIONS, codex_generation, credential_expiry,
    fingerprint, isolated_proxy, publish_pool_generation, read_yaml)
from ops.quota_policy import probe_pause

LOG = logging.getLogger('codex-watchdog')


class ProviderUnavailable(APIError):
    """Authentication and quota failures must not cause network route churn."""


class SchedulingPaused(ProviderUnavailable):
    """Local quota protection must apply to diagnostic generations too."""


def recent_transport_failures(account_id, since):
    from ops.admin_api import read_env
    values = read_env(ROOT/'deployment/private/config/runtime.env')
    # Only consecutive transport errors after the same model's latest success
    # count. No prompts, keys, response bodies or client IPs leave the database.
    query = """WITH successes AS (
      SELECT model,MAX(created_at) AS last_success FROM usage_logs
      WHERE account_id=%s AND created_at>NOW()-INTERVAL '90 seconds'
        AND output_tokens>0 GROUP BY model)
      SELECT COALESCE((SELECT row_to_json(b) FROM (
        SELECT e.model,COUNT(*) AS failures,MAX(e.id) AS latest_id
        FROM ops_error_logs e LEFT JOIN successes s ON s.model=e.model
        WHERE e.account_id=%s AND e.created_at>NOW()-INTERVAL '90 seconds'
          AND e.created_at>to_timestamp(%s) AND e.created_at>COALESCE(s.last_success,to_timestamp(0))
          AND e.upstream_status_code IS NULL
          AND e.upstream_error_message ~* '(EOF|tls handshake|connection reset|connection refused|i/o timeout|dial tcp)'
        GROUP BY e.model HAVING COUNT(*)>=2 ORDER BY MAX(e.id) DESC LIMIT 1) b),'{}')""" % (
            int(account_id), int(account_id), float(since))
    result = subprocess.run([str(ROOT/'runtime/bin/psql'), '-X', '-qAt',
        '-h', values['DATABASE_HOST'], '-p', values['DATABASE_PORT'], '-U', values['DATABASE_USER'],
        '-d', values['DATABASE_DBNAME'], '-c', query],
        env=dict(os.environ, PGPASSWORD=values['DATABASE_PASSWORD'], PGCONNECT_TIMEOUT='2',
                 PGOPTIONS='-c statement_timeout=1000'),
        check=True, capture_output=True, text=True, timeout=3)
    return json.loads(result.stdout)


def probe_config(config):
    # Reserve both ephemeral ports together. A collision after release merely
    # fails this isolated check; production listeners are never reconfigured.
    with socket.socket() as mixed, socket.socket() as controller:
        mixed.bind(('127.0.0.1', 0)); controller.bind(('127.0.0.1', 0))
        return dict(config, probe_port=mixed.getsockname()[1],
                    probe_controller_port=controller.getsockname()[1])


def load_config(path):
    config = json.loads(Path(path).read_text())
    for name, default, minimum, maximum in [
        ('interval', 5, 1, 60), ('timeout_ms', 4000, 100, 5000),
        ('failure_threshold', 2, 2, 5), ('cooldown_seconds', 180, 30, 3600),
        ('model_probe_seconds', 60, 30, 600), ('error_poll_seconds', 10, 5, 60)]:
        value = config.setdefault(name, default)
        if type(value) not in (int, float) or not minimum <= value <= maximum:
            raise ValueError('Invalid GPT watchdog timing: '+name)
    qualifier = json.loads(Path(config['qualifier_config']).read_text())
    models = config.setdefault('probe_models', ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-sol'])
    if (not isinstance(models, list) or not 1 <= len(models) <= 4
            or any(not isinstance(model, str) or not model.startswith('gpt-')
                   or any(word in model for word in ('image', 'audio', 'realtime')) for model in models)):
        raise ValueError('Invalid GPT diagnostic models')
    role = next(r for r in qualifier['roles'] if r['name'] == 'codex')
    if type(role['account_id']) is not int or role['account_id'] <= 0:
        raise ValueError('Invalid GPT account identifier')
    mihomo_path = Path(role['mihomo_config'])
    mihomo = read_yaml(mihomo_path)
    providers = []
    for name, provider in mihomo.get('proxy-providers', {}).items():
        source = Path(provider.get('path', ''))
        if not source.is_absolute():
            source = mihomo_path.parent/source
        if provider.get('type') == 'file' and source.resolve() == Path(role['provider_file']).resolve():
            providers.append(name)
    if not providers:
        raise ValueError('GPT watchdog requires the installed qualified file provider')
    config.update(mihomo_config=role['mihomo_config'], group=role['group'],
        providers=providers, node_pattern=REGIONS, recovery_interval=60, first_verified_spare=True,
        max_healthy_delay_ms=0, latency_probe_interval=300,
        probes=[{'url': 'https://chatgpt.com/cdn-cgi/trace', 'expected': '200'}])
    return config, qualifier, role


class CodexController(Controller):
    def __init__(self, config, qualifier, role, clock=time.monotonic):
        super().__init__(config)
        self.qualifier, self.role, self.clock = qualifier, role, clock
        self.mutex = threading.Lock()
        self.credential_mutex = threading.Lock()
        self.policy_mutex = threading.Lock()
        self.admin = AccountRecovery({'base_url': 'http://127.0.0.1:8080', 'timeout': 3,
            'env_file': str(ROOT/'deployment/private/config/operator.env')})
        self.policy_checked = float('-inf')
        self.pause = None
        self.cache, self.node_locks = {}, {}
        self.selected = None
        self.selected_since = time.time()
        self.last_error_poll = self.last_version_check = float('-inf')
        self.latest_error_id = 0
        self.fault_node, self.fault_until = None, 0
        self.hint_model, self.allowed_models = '', set()
        self.version = '0.163.0'
        self.details = {}

    def snapshot(self):
        selected, nodes = super().snapshot()
        if selected != self.selected:
            self.selected, self.selected_since = selected, time.time()
        return selected, nodes

    def observe_errors(self):
        now = self.clock()
        with self.mutex:
            if now-self.last_error_poll < self.config['error_poll_seconds']:
                return
            self.last_error_poll = now
        try:
            burst = recent_transport_failures(self.role['account_id'], self.selected_since)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Database observation is supplementary: HTTPS and real generation
            # checks continue during a database/control-plane outage.
            self.details['error_observation'] = 'unavailable'
            return
        with self.mutex:
            self.details['error_observation'] = 'ok'
            if burst.get('latest_id', 0) > self.latest_error_id:
                self.latest_error_id = burst['latest_id']
                self.fault_node = self.selected
                self.fault_until = now+self.config['cooldown_seconds']
                self.hint_model = burst.get('model', '')
                self.cache.pop(self.selected, None)
                self.details['consecutive_transport_errors'] = burst['failures']

    def credentials(self):
        with self.credential_mutex:
            return self.read_credentials()

    def ensure_scheduling(self):
        with self.policy_mutex:
            if self.clock()-self.policy_checked >= 15:
                account = self.admin.request('/api/v1/admin/accounts/'+str(self.role['account_id']))
                settings = self.admin.request('/api/v1/admin/ops/advanced-settings')
                self.pause = probe_pause(account, settings.get('openai_account_quota_auto_pause'))
                self.policy_checked = self.clock()
                self.details['scheduling'] = self.pause or {'reason': 'eligible'}
            if self.pause:
                raise SchedulingPaused('GPT diagnostic deferred by account scheduling protection')

    def read_credentials(self):
        from ops import admin_api as api
        from ops.credentials import accounts
        self.ensure_scheduling()
        try:
            credentials = accounts([self.role['account_id']])[self.role['account_id']]
            expiry = credential_expiry(credentials)
            if expiry and expiry <= time.time()+120:
                token = api.login()
                api.request('/api/v1/admin/accounts/'+str(self.role['account_id'])+'/refresh', token, {}, 'POST')
                credentials = accounts([self.role['account_id']])[self.role['account_id']]
        except Exception:
            raise APIError('GPT credential lookup or refresh unavailable') from None
        self.allowed_models = set((credentials.get('model_mapping') or {}).keys())
        if self.clock()-self.last_version_check >= 300:
            self.last_version_check = self.clock()
            try:
                settings = api.request('/api/v1/admin/settings', api.login())
                self.version = (settings.get('openai_codex_client_version') or
                                settings.get('openai_codex_client_version_synced') or self.version)
            except Exception:
                pass  # A settings outage does not discard the last usable version.
        return credentials

    def probe_model(self):
        if self.hint_model in self.allowed_models and self.clock() < self.fault_until:
            return self.hint_model
        models = self.config.get('probe_models', ['gpt-6-sol'])
        model = models[int(self.clock()/self.config['model_probe_seconds']) % len(models)]
        if self.allowed_models and model not in self.allowed_models:
            model = next((m for m in models if m in self.allowed_models), 'gpt-6-sol')
        return model

    def measure(self, node_config, model, confirm):
        credentials = self.credentials()
        if model not in self.allowed_models or any(word in model for word in ('image', 'audio', 'realtime')):
            model = next((m for m in self.config.get('probe_models', ['gpt-6-sol'])
                          if m in self.allowed_models), None)
            if not model:
                raise ProviderUnavailable('No configured GPT diagnostic model is available')
        with isolated_proxy([node_config], probe_config(self.qualifier)) as (_, proxy):
            checks = []
            for _ in range(2 if confirm else 1):
                result = codex_generation(credentials, proxy, self.version, model=model)
                checks.append(result)
                if result.get('http_status') in (400, 401, 402, 404, 429):
                    raise ProviderUnavailable('GPT authentication, model access or quota unavailable')
                if not result.get('ok'):
                    return {'ok': False, 'model': model, 'checks': checks}
        return {'ok': True, 'model': model, 'checks': checks}

    def probe(self, node):
        if node is None:
            return None
        self.ensure_scheduling()
        if node.name == self.selected:
            self.observe_errors()
        with self.mutex:
            if node.name == self.fault_node and self.clock() < self.fault_until:
                return None
            lock = self.node_locks.setdefault(node.name, threading.Lock())
        delay = super().probe(node)
        if delay is None:
            return None
        with lock:
            configs = read_yaml(self.role['provider_file']).get('proxies', [])
            config = next((n for n in configs if n['name'] == node.name), None)
            if config is None:
                raise APIError('Qualified candidate changed during check')
            generation = fingerprint([config])
            model = self.probe_model()
            with self.mutex:
                previous = self.cache.get(node.name)
            confirmations = 2 if node.name != self.selected else 1
            if (previous and previous['generation'] == generation and previous['model'] == model
                    and previous['confirmations'] >= confirmations
                    and self.clock()-previous['at'] < self.config['model_probe_seconds']):
                return delay
            try:
                result = self.measure(config, model, confirm=node.name != self.selected)
            except (ProviderUnavailable, APIError):
                raise
            except Exception as error:
                result = {'ok': False, 'model': model, 'error_type': type(error).__name__}
            with self.mutex:
                self.details.setdefault('models', {})[node.name] = {
                    'ok': result['ok'], 'model': result['model'], 'checked_at': time.time()}
                if not result['ok']:
                    self.cache.pop(node.name, None)
                    return None
                self.cache[node.name] = {'generation': generation, 'model': result['model'],
                                         'at': self.clock(), 'confirmations': len(result['checks'])}
            return delay

    def select(self, node):
        with route_lock(self.role):
            group = self.api.request(self.group_path)
            # Preserve a newer decision made by native fallback or qualification.
            if group.get('now') != self.selected or node.name not in group.get('all', []):
                raise APIError('Route changed during GPT verification')
            config = next((n for n in read_yaml(self.role['provider_file']).get('proxies', [])
                           if n['name'] == node.name), None)
            with self.mutex:
                verified = self.cache.get(node.name)
            if (not config or not verified or fingerprint([config]) != verified['generation']
                    or verified['confirmations'] < (2 if node.name != self.selected else 1)
                    or self.clock()-verified['at'] >= self.config['model_probe_seconds']):
                raise APIError('Candidate needs a fresh real generation check')
            self.api.request(self.group_path, 'PUT', {'name': node.name})
            publish_pool_generation(self.qualifier, self.role, self.api)


class CodexWatchdog(Watchdog):
    def publish(self, **fields):
        now = self.clock()
        excluded = {name: time.time()+until-now for name, until in self.cooldown.items() if until > now}
        with self.controller.mutex:
            if self.controller.fault_node and self.controller.fault_until > now:
                excluded[self.controller.fault_node] = time.time()+self.controller.fault_until-now
            details = deepcopy(self.controller.details)
        return super().publish(group=self.controller.role['group'],
            account_id=self.controller.role['account_id'], excluded_nodes=excluded,
            generation_checks=details, **fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'deployment/private/config/codex-watchdog.json')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        config, qualifier, role = load_config(args.config)
    except Exception as error:
        LOG.error('gpt_watchdog_configuration_unavailable error_type=%s', type(error).__name__)
        return 2
    destination = Path(config['state_file']);destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    import fcntl
    with destination.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        controller = CodexController(config, qualifier, role)
        watcher = CodexWatchdog(controller, config)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    status = watcher.tick()
                except SchedulingPaused:
                    status = watcher.publish(state='account_paused', node=controller.selected)
                except ProviderUnavailable:
                    status = watcher.publish(state='provider_unavailable', node=controller.selected)
                except (APIError, OSError, KeyError, ValueError):
                    status = watcher.publish(state='check_unavailable', node=controller.selected)
                except Exception as error:
                    LOG.error('gpt_watchdog_check_unavailable error_type=%s', type(error).__name__)
                    status = watcher.publish(state='check_unavailable', node=controller.selected)
                if args.once:
                    print(json.dumps(status, ensure_ascii=False))
                    return 0 if status['state'] == 'healthy' else 1
                stop.wait(max(.1, config['interval']-(time.monotonic()-started)))
        finally:
            watcher.close()


if __name__ == '__main__':
    raise SystemExit(main())
