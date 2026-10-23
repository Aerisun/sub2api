"""Exercise actual worker supervision without sending any model requests."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
from urllib.request import ProxyHandler, build_opener


class WorkerTests(unittest.TestCase):
    def test_health_worker_replacement_and_shutdown(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'image_hub.agent_server'],
            env={**os.environ, 'AGENT_IMAGE_BRIDGE_WORKERS': '4', 'AGENT_IMAGE_BRIDGE_PORT': str(port)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        seen = set()

        def children():
            path = Path(f'/proc/{process.pid}/task/{process.pid}/children')
            return set(map(int, path.read_text().split())) if path.exists() else set()

        def until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                if process.poll() is not None:
                    self.fail('worker supervisor exited unexpectedly')
                time.sleep(.02)
            self.fail('worker supervisor did not become ready')

        try:
            until(lambda: len(children()) == 4)
            before = children()
            seen.update(before)
            opener = build_opener(ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                self.assertEqual(json.load(response)['status'], 'ok')
            victim = max(before)
            os.kill(victim, signal.SIGKILL)
            until(lambda: len(children()) == 4 and victim not in children())
            after = children()
            seen.update(after)
            self.assertEqual(len(before & after), 3)
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                self.assertEqual(response.status, 200)
            process.terminate()
            self.assertEqual(process.wait(timeout=7), 0)
            self.assertTrue(all(not Path(f'/proc/{pid}').exists() for pid in seen))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
