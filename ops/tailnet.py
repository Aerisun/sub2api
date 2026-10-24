"""Adopt the existing user VPN, restoring it only when no daemon owns its state."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def load_config(path):
    config = json.loads(Path(path).read_text())
    for name in ('state_file', 'socket_file'):
        config[name] = str(Path(config[name]).expanduser())
    config['daemon'] = str(ROOT/'bin/tailscaled')
    return config


def available(config):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(config['socket_file'])
        with socket.create_connection(('127.0.0.1', config['socks_port']), timeout=1):
            pass
        return True
    except OSError:
        return False


def daemon_running():
    return subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', 'tailscaled'],
                          stdout=subprocess.DEVNULL, check=False).returncode == 0


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0'+address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(b'READY=1', address)


def supervise(config, stop):
    child, announced, last_start = None, False, 0
    try:
        while not stop.is_set():
            if available(config):
                if not announced:
                    notify_ready()
                    announced = True
                    print('Existing user Tailscale connection adopted.', flush=True)
            elif (child is None or child.poll() is not None) and time.monotonic()-last_start >= 5:
                # Do not create a second owner, even if a live daemon is temporarily unready.
                if not daemon_running():
                    if not Path(config['state_file']).is_file():
                        raise RuntimeError('Existing Tailscale state is missing')
                    last_start = time.monotonic()
                    child = subprocess.Popen([
                        config['daemon'], '--tun=userspace-networking',
                        '--state='+config['state_file'], '--socket='+config['socket_file'],
                        '--socks5-server=127.0.0.1:'+str(config['socks_port']),
                        '--outbound-http-proxy-listen=127.0.0.1:'+str(config['http_port'])])
                    print('Restoring the existing user Tailscale state.', flush=True)
            stop.wait(2)
    finally:
        # An adopted daemon belongs to the host and must survive supervisor restarts.
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'deployment/private/config/tailnet.json')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    if args.check:
        healthy = available(config)
        print(json.dumps({'healthy': healthy, 'daemon_running': daemon_running()}))
        if not healthy:
            raise SystemExit(1)
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    supervise(config, stop)


if __name__ == '__main__':
    main()
