#!/usr/bin/env python3
"""Reload dispatcher settings in its managed cgroup, preserving native models."""
import fcntl
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def command_matches(pid, expected):
    try:
        return expected in (Path('/proc') / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode()
    except FileNotFoundError:
        return False


def idle(key):
    connection = HTTPConnection('127.0.0.1', 8091, timeout=3)
    try:
        connection.request('GET', '/metrics', headers={'Authorization': 'Bearer ' + key})
        response = connection.getresponse()
        data = json.loads(response.read())
        return (response.status == 200 and not data['active'] and not data['waiting']
                and not data['quarantined'] and not data.get('result_cache', {}).get('pending'))
    finally:
        connection.close()


def prepare(command, log):
    control, standby = socket.socketpair()
    control.settimeout(10)
    try:
        process = subprocess.Popen(command + ['--standby-fd', str(standby.fileno())],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   pass_fds=(standby.fileno(),), start_new_session=True)
        standby.close()
        if control.recv(32) != b'ready\n':
            raise RuntimeError('Replacement failed its startup check')
        return process, control
    except BaseException:
        control.close()
        standby.close()
        if 'process' in locals():
            process.terminate()
            process.wait(timeout=10)
        raise


def activate(process, control, key, port=8091):
    control.sendall(b'activate\n')
    if control.recv(32) != b'serving\n':
        raise RuntimeError('Replacement failed to activate')
    connection = HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('GET', '/metrics', headers={'Authorization': 'Bearer ' + key})
        response = connection.getresponse()
        state = json.loads(response.read())
        if response.status != 200 or not state['enabled'] or process.poll() is not None:
            raise RuntimeError('Replacement is unhealthy')
        return state
    finally:
        connection.close()


def stage(command, log, key, port, old_port):
    if not 1024 < port < 65536 or port == old_port:
        raise ValueError('Invalid migration listener')
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise RuntimeError('Migration listener is already in use')
    staged = list(command)
    staged[staged.index('--port') + 1] = str(port)
    process = subprocess.Popen(staged + ['--secondary-port', str(old_port)], stdin=subprocess.DEVNULL,
                               stdout=log, stderr=log, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            connection = HTTPConnection('127.0.0.1', port, timeout=1)
            try:
                connection.request('GET', '/metrics', headers={'Authorization': 'Bearer ' + key})
                response = connection.getresponse()
                state = json.loads(response.read())
                if response.status == 200 and state['enabled']:
                    return process, state
            except (OSError, ValueError):
                pass
            finally:
                connection.close()
            time.sleep(.05)
        raise RuntimeError('Migration dispatcher failed its readiness check')
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise


def main():
    key = (ROOT / 'secrets/backend_internal_key.txt').read_text().strip()
    env = dict(os.environ, EMBED_FROM_SUPERVISOR='1')
    runtime = Path(env.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
    with (ROOT / 'runtime/reload.lock').open('a') as reload_lock:
        fcntl.flock(reload_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Prewarm new minimums while the current dispatcher continues serving.
        with (runtime / 'cpu-inference-model-load.lock').open('a') as load_lock:
            fcntl.flock(load_lock, fcntl.LOCK_EX)
            subprocess.run([str(ROOT / 'scripts/start_pool.sh')], env=env, check=True, timeout=250)
        supervisor = int((ROOT / 'runtime/supervisor.pid').read_text())
        pool = int((ROOT / 'runtime/backend.pid').read_text())
        if not command_matches(supervisor, str(ROOT / 'scripts/supervise.sh')):
            raise RuntimeError('Unexpected supervisor identity')
        if not command_matches(pool, str(ROOT / 'scripts/embedding_pool.py')):
            raise RuntimeError('Unexpected dispatcher identity')
        command = json.loads(subprocess.check_output([str(ROOT / 'scripts/start_pool.sh'), '--print-command'],
                                                    env=env, text=True, timeout=5))
        with (ROOT / 'runtime/embedding-pool.log').open('ab') as log:
            if env.get('EMBED_DISPATCHER_STAGE_PORT'):
                port = int(env['EMBED_DISPATCHER_STAGE_PORT'])
                old_port = int(command[command.index('--port') + 1])
                replacement, state = stage(command, log, key, port, old_port)
                record = {'old_pid': pool, 'new_pid': replacement.pid, 'primary_port': port,
                          'secondary_port': old_port, 'scheduler': state['scheduler']}
                (ROOT / 'runtime/migration-candidate.json').write_text(json.dumps(record, indent=2) + '\n')
                print('Migration dispatcher is ready; current routing and native models retained.')
                return
            replacement, control = prepare(command, log)
            committed = paused = old_stopped = False
            try:
                deadline = time.monotonic() + 30
                quiet_since = None
                while True:
                    now = time.monotonic()
                    quiet_since = (quiet_since if quiet_since is not None else now) if idle(key) else None
                    if quiet_since is not None and now - quiet_since >= .2:
                        break
                    if now >= deadline:
                        raise RuntimeError('Dispatcher reload deferred until current native work finishes')
                    time.sleep(.05)
                # Keep the watchdog from treating the swap as a model failure.
                # The replacement has already imported and validated its code.
                os.kill(supervisor, signal.SIGSTOP)
                paused = True
                if not idle(key):
                    raise RuntimeError('New inference arrived; dispatcher reload deferred')
                started = time.monotonic()
                os.kill(pool, signal.SIGTERM)
                old_stopped = True
                deadline = time.monotonic() + 10
                while command_matches(pool, str(ROOT / 'scripts/embedding_pool.py')):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Previous dispatcher has not stopped')
                    time.sleep(.005)
                state = activate(replacement, control, key)
                pid_file = ROOT / 'runtime/backend.pid'
                temporary = pid_file.with_suffix('.next')
                temporary.write_text(str(replacement.pid) + '\n')
                temporary.replace(pid_file)
                committed = True
                record = {'old_pid': pool, 'new_pid': replacement.pid,
                          'termination_to_healthy_ms': round((time.monotonic() - started) * 1000, 3),
                          'scheduler': state['scheduler'], 'native_models_restarted': False}
                (ROOT / 'runtime/last-reload.json').write_text(json.dumps(record, indent=2) + '\n')
            finally:
                control.close()
                try:
                    if not committed:
                        replacement.terminate()
                        replacement.wait(timeout=10)
                        if old_stopped:
                            subprocess.run([str(ROOT / 'scripts/start_pool.sh')], env=env, check=True, timeout=35)
                finally:
                    if paused and command_matches(supervisor, str(ROOT / 'scripts/supervise.sh')):
                        os.kill(supervisor, signal.SIGCONT)
    print('Embedding dispatcher reloaded; native model processes retained.')


if __name__ == '__main__':
    main()
