#!/usr/bin/python3
"""Maintain one restricted reverse SSH connection; verify its entire forwarding path."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

mode = sys.argv[1]
if mode != 'public':
    raise SystemExit('Unknown tunnel mode')
port, host = 18081, '101.42.135.92'
root = Path(__file__).resolve().parent
socket = str(Path(os.environ['XDG_RUNTIME_DIR']) / ('sub2api-' + mode + '.sock'))
Path(socket).unlink(missing_ok=True)
base = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(root / 'keys/tcyun_forward_ed25519'),
        '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=sub2api-tcyun',
        '-o', 'UserKnownHostsFile=' + str(root / 'known_hosts'), '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3', '-o', 'TCPKeepAlive=yes',
        # Avoid serial compression work and cross-channel stalls on the
        # shared transport; image uploads and SSE must not wait for zlib.
        '-o', 'Compression=no', '-o', 'IPQoS=none', '-o', 'LogLevel=ERROR', '-S', socket]
base += ['-o', 'ProxyCommand=none']
target = 'rowan@' + host
child = subprocess.Popen(base + ['-M', '-N', '-o', 'ControlPersist=no',
                          '-o', 'ExitOnForwardFailure=yes', '-R', f'127.0.0.1:{port}:127.0.0.1:8081', target])
stop = False

def shutdown(_signum, _frame):
    global stop
    stop = True

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
failures = 0
next_probe = time.monotonic() + 10
try:
    print(f'{mode}: connecting restricted loopback forward on {port}', flush=True)
    while not stop:
        if child.poll() is not None:
            raise RuntimeError(f'SSH exited with code {child.returncode}')
        if time.monotonic() >= next_probe:
            try:
                if not Path(socket).exists():
                    raise RuntimeError('SSH control connection missing')
                # Reuse the existing SSH transport; the server forces this exact health-only helper.
                probe = subprocess.run(base + ['-o', 'ControlMaster=no', target, 'health ' + mode],
                                       capture_output=True, timeout=9)
                healthy = probe.returncode == 0 and json.loads(probe.stdout).get('status') == 'ok'
            except (subprocess.TimeoutExpired, ValueError, RuntimeError, AttributeError):
                healthy = False
            if healthy:
                if failures:
                    print(f'{mode}: end-to-end health recovered', flush=True)
                failures = 0
            else:
                failures += 1
                print(f'{mode}: end-to-end health failed ({failures}/2)', flush=True)
                if failures >= 2:
                    raise RuntimeError('Forwarding path stalled; reconnecting')
            next_probe = time.monotonic() + 20
        time.sleep(1)
finally:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    Path(socket).unlink(missing_ok=True)
