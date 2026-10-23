#!/usr/bin/env python3
"""Check mixed-size scheduling, bounded overtaking and prepared process activation."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

from embedding_pool import Disconnected, PoolServer, WorkerPool, request_work, start_secondary
from reload_pool import activate, prepare, stage


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Condition not met')
        time.sleep(.005)


class WorkSchedulerTest(unittest.TestCase):
    def queued_call(self, pool, name, size, items=1):
        acquired = threading.Event()
        release = threading.Event()
        result = {}

        def run():
            try:
                ticket, port = pool.acquire(size, items=items)
                result.update(ticket=ticket, port=port)
                acquired.set()
                release.wait(5)
                pool.release(ticket)
            except BaseException as exc:
                result['error'] = exc
                acquired.set()

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(release.set)
        return acquired, release, result, thread

    def test_estimate_does_not_confuse_token_ids_with_a_batch(self):
        self.assertEqual(request_work(b'{"input":[11,22,33]}'), (1, 12))
        self.assertEqual(request_work(b'{"input":[[11,22],[33]]}'), (2, 12))
        self.assertEqual(request_work(json.dumps({'input': ['a', '中文']}).encode()), (2, 7))
        for body in (b'null', b'[]', b'{', b'{"input":null}', b'{"input":[{}]}'):
            self.assertEqual(request_work(body), (1, len(body)))

    def test_batches_use_sequence_budget_and_single_requests_share_spare_slots(self):
        pool = WorkerPool([1, 2], concurrency=4, per_worker=2, scheduler='bounded-shortest')
        a, pa = pool.acquire(1000, items=6)
        b, pb = pool.acquire(2000, items=2)
        self.assertNotEqual(pa, pb)
        ready, release, result, thread = self.queued_call(pool, 'small', 10)
        wait_for(lambda: pool.snapshot()['waiting'] == 1)
        self.assertFalse(ready.is_set())
        self.assertEqual(sum(pool.snapshot()['workers_reserved_slots'].values()), 4)
        pool.release(a)
        self.assertTrue(ready.wait(2))
        self.assertEqual(result['port'], pa)
        c, pc = pool.acquire(20)
        self.assertEqual(pc, pa)
        self.assertEqual(pool.snapshot()['workers_reserved_slots'], {pa: 2, pb: 2})
        with self.assertRaises(Disconnected):
            pool.acquire(30, lambda: True, items=2)
        pool.release(c)
        pool.release(b)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(pool.snapshot()['workers_reserved_slots'], {1: 0, 2: 0})

    def test_short_request_overtakes_burst_but_long_request_cannot_starve(self):
        pool = WorkerPool([1], concurrency=1, per_worker=1, scheduler='bounded-shortest', max_bypass=1)
        first, _ = pool.acquire(1)
        large = self.queued_call(pool, 'large', 9000)
        wait_for(lambda: pool.snapshot()['waiting'] == 1)
        medium = self.queued_call(pool, 'medium', 3000)
        wait_for(lambda: pool.snapshot()['waiting'] == 2)
        small = self.queued_call(pool, 'small', 20)
        wait_for(lambda: pool.snapshot()['waiting'] == 3)
        pool.release(first)
        self.assertTrue(small[0].wait(2))
        self.assertFalse(large[0].is_set())
        small[1].set()
        self.assertTrue(large[0].wait(2))
        self.assertFalse(medium[0].is_set())
        large[1].set()
        self.assertTrue(medium[0].wait(2))
        medium[1].set()
        for call in (large, medium, small):
            call[3].join(2)
            self.assertNotIn('error', call[2])
        self.assertEqual(pool.snapshot()['scheduler']['priority_dispatches'], 1)

    def test_aged_batch_reserves_next_idle_worker(self):
        pool = WorkerPool([1], concurrency=2, per_worker=2, scheduler='bounded-shortest', aging_seconds=.03)
        a, _ = pool.acquire(10)
        b, _ = pool.acquire(10)
        batch = self.queued_call(pool, 'aged-batch', 9000, items=6)
        wait_for(lambda: pool.snapshot()['waiting'] == 1)
        time.sleep(.04)
        pool.release(a)
        small = self.queued_call(pool, 'new-small', 1)
        wait_for(lambda: pool.snapshot()['waiting'] == 2)
        self.assertFalse(small[0].is_set())
        pool.release(b)
        self.assertTrue(batch[0].wait(2))
        self.assertFalse(small[0].is_set())
        batch[1].set()
        self.assertTrue(small[0].wait(2))
        small[1].set()

    def test_old_document_burst_does_not_block_every_new_query(self):
        pool = WorkerPool([1], concurrency=1, per_worker=1, scheduler='fair-shortest',
                          aging_seconds=.02, max_bypass=2)
        first, _ = pool.acquire(1000)
        calls = {}
        for i in range(4):
            calls['b' + str(i)] = self.queued_call(pool, 'background-' + str(i), 9000 + i)
            wait_for(lambda: pool.snapshot()['waiting'] == len(calls))
        time.sleep(.03)
        for i in range(4):
            calls['s' + str(i)] = self.queued_call(pool, 'short-' + str(i), 10 + i)
            wait_for(lambda: pool.snapshot()['waiting'] == len(calls))
        pool.release(first)
        # Both classes progress even when every document is already aged.
        expected = ['s0', 's1', 'b0', 's2', 's3', 'b1', 'b2', 'b3']
        remaining = set(calls)
        for name in expected:
            self.assertTrue(calls[name][0].wait(2), name)
            self.assertTrue(all(not calls[other][0].is_set() for other in remaining - {name}))
            remaining.remove(name)
            calls[name][1].set()
            calls[name][3].join(2)
            self.assertNotIn('error', calls[name][2])

    def test_aged_batch_protects_one_worker_and_uses_other_spare_capacity(self):
        pool = WorkerPool([1, 2], concurrency=4, per_worker=2, scheduler='fair-shortest',
                          aging_seconds=.02, max_bypass=1)
        first, first_port = pool.acquire(1000)
        second, second_port = pool.acquire(2000)
        self.assertNotEqual(first_port, second_port)
        batch = self.queued_call(pool, 'aged-batch', 9000, items=2)
        wait_for(lambda: pool.snapshot()['waiting'] == 1)
        time.sleep(.03)
        pool.short_streak = 1
        short = self.queued_call(pool, 'short', 10)
        self.assertTrue(short[0].wait(2))
        self.assertEqual(short[2]['port'], second_port)
        self.assertFalse(batch[0].is_set())
        pool.release(first)
        self.assertTrue(batch[0].wait(2))
        self.assertEqual(batch[2]['port'], first_port)
        pool.release(second)
        for call in (batch, short):
            call[1].set()
            call[3].join(2)

    def test_live_policy_rollback_keeps_active_reservations(self):
        pool = WorkerPool([1, 2], concurrency=4, per_worker=2, scheduler='bounded-shortest')
        active, _ = pool.acquire(1000, items=2)
        before = pool.snapshot()['workers_reserved_slots']
        pool.configure_scheduler('fair-shortest', 15, 4, 512)
        self.assertEqual(pool.snapshot()['workers_reserved_slots'], before)
        pool.configure_scheduler('bounded-shortest', 15, 4, 512)
        self.assertEqual(pool.snapshot()['workers_reserved_slots'], before)
        with self.assertRaises(RuntimeError):
            pool.configure_scheduler('fifo', 15, 4, 512)
        for settings in [('fair-shortest', True, 4, 512), ('bad', 15, 4, 512),
                         ('fair-shortest', 15, 0, 512), ('fair-shortest', 15, 4, 4097)]:
            with self.assertRaises(ValueError):
                pool.configure_scheduler(*settings)
        pool.release(active)


class PreparedReloadTest(unittest.TestCase):
    def test_migration_listener_retains_old_port_and_shares_cache_and_admission(self):
        class OldHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'old-still-serving')

        old = ThreadingHTTPServer(('127.0.0.1', 0), OldHandler)
        port = old.server_port
        server = PoolServer(('127.0.0.1', 0), WorkerPool([1], concurrency=1, per_worker=1), 'test-only')
        stop = threading.Event()
        for service in (old, server):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        body = b'{"input":"complete cache fixture"}'
        digest, _, flight = server.cache.claim(body)
        expected = b'{"data":[{"index":0,"embedding":[0.6,0.8]}],"usage":{"prompt_tokens":12,"total_tokens":12}}'
        server.cache.finish(digest, flight, (200, expected))
        start_secondary(server, port, stop)
        try:
            connection = HTTPConnection('127.0.0.1', port, timeout=2)
            connection.request('GET', '/health')
            self.assertEqual(connection.getresponse().read(), b'old-still-serving')
            connection.close()
            old.shutdown()
            old.server_close()
            wait_for(lambda: len(server.secondary_servers) == 1)
            self.assertTrue(server.admission_slots.acquire(blocking=False))
            # Consume every primary admission slot; the alias must share them.
            taken = 1
            while server.admission_slots.acquire(blocking=False):
                taken += 1
            try:
                for payload, status in [(body, 200), (b'{"input":"uncached fixture"}', 429)]:
                    connection = HTTPConnection('127.0.0.1', port, timeout=2)
                    connection.request('POST', '/v1/embeddings', payload, {'Authorization': 'Bearer test-only'})
                    response = connection.getresponse()
                    result = response.read()
                    self.assertEqual(response.status, status)
                    if status == 200:
                        self.assertEqual(result, expected)
                        self.assertEqual(response.getheader('Cache-Status'), 'AerisunEmbedding; hit')
                    connection.close()
            finally:
                for _ in range(taken):
                    server.admission_slots.release()
            self.assertEqual(server.pool.snapshot()['active'], 0)
        finally:
            stop.set()
            for alias in server.secondary_servers:
                alias.shutdown()
                alias.server_close()
            for service in (old, server):
                service.shutdown()
                service.server_close()

    def test_stage_readiness_leaves_the_serving_port_untouched(self):
        old = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        old_port = old.server_port
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0))
            port = free.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as log:
            root = Path(directory)
            (root / 'secrets').mkdir()
            (root / 'secrets/backend_internal_key.txt').write_text('test-only')
            command = [sys.executable, '-c',
                       'import sys; from pathlib import Path; import embedding_pool; '
                       'embedding_pool.ROOT=Path(sys.argv.pop(1)); embedding_pool.main()',
                       str(root), '--port', str(old_port), '--workers', '1,2', '--concurrency', '4',
                       '--per-worker', '2', '--scheduler', 'fair-shortest']
            process = None
            try:
                with self.assertRaises(RuntimeError):
                    stage(command, log, 'test-only', old_port, port)
                process, state = stage(command, log, 'test-only', port, old_port)
                self.assertEqual(state['listen_ports'], [port])
                self.assertEqual(state['scheduler']['policy'], 'fair-shortest')
                old.server_close()
                def both_bound():
                    connection = HTTPConnection('127.0.0.1', port, timeout=2)
                    connection.request('GET', '/metrics', headers={'Authorization': 'Bearer test-only'})
                    response = connection.getresponse()
                    state = json.loads(response.read())
                    connection.close()
                    return sorted(state['listen_ports']) == sorted([port, old_port])
                wait_for(both_bound)
            finally:
                old.server_close()
                if process:
                    process.terminate()
                    process.wait(timeout=5)

    def test_scheduler_changes_require_auth_and_preserve_inflight_work(self):
        pool = WorkerPool([1], concurrency=2, per_worker=2, scheduler='bounded-shortest')
        active, _ = pool.acquire(1000, items=2)
        server = PoolServer(('127.0.0.1', 0), pool, 'test-only')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cases = [('wrong', 'fair-shortest', 4, 401), ('test-only', 'fair-shortest', 0, 400),
                     ('test-only', 'fair-shortest', 4, 200), ('test-only', 'fifo', 4, 409),
                     ('test-only', 'bounded-shortest', 4, 200)]
            for token, policy, bypass, expected in cases:
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                connection.request('POST', '/internal/scheduler', json.dumps({'policy': policy,
                                   'aging_seconds': 15, 'max_bypass': bypass, 'short_cost_limit': 512}),
                                   {'Authorization': 'Bearer ' + token})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                self.assertEqual(pool.snapshot()['workers_reserved_slots'], {1: 2})
                connection.close()
        finally:
            pool.release(active)
            server.shutdown()
            server.server_close()

    def test_new_process_prepares_while_port_still_serves_then_activates(self):
        class OldHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'old-still-serving')

        old = ThreadingHTTPServer(('127.0.0.1', 0), OldHandler)
        port = old.server_port
        thread = threading.Thread(target=old.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as log:
            root = Path(directory)
            (root / 'secrets').mkdir()
            (root / 'secrets/backend_internal_key.txt').write_text('test-only')
            command = [sys.executable, '-c',
                       'import sys; from pathlib import Path; import embedding_pool; '
                       'embedding_pool.ROOT=Path(sys.argv.pop(1)); embedding_pool.main()',
                       str(root), '--port', str(port), '--workers', '1,2', '--concurrency', '4',
                       '--per-worker', '2', '--scheduler', 'bounded-shortest']
            process = control = None
            try:
                with self.assertRaises(RuntimeError):
                    prepare(command + ['--scheduler', 'invalid'], log)
                process, control = prepare(command, log)
                connection = HTTPConnection('127.0.0.1', port, timeout=2)
                connection.request('GET', '/health')
                self.assertEqual(connection.getresponse().read(), b'old-still-serving')
                connection.close()
                old.shutdown()
                old.server_close()
                state = activate(process, control, 'test-only', port)
                self.assertEqual(state['scheduler']['policy'], 'bounded-shortest')
                self.assertEqual(state['active'], 0)
            finally:
                old.shutdown()
                old.server_close()
                if control:
                    control.close()
                if process:
                    process.terminate()
                    process.wait(timeout=5)

    def test_maintenance_requires_authentication_and_boolean_state(self):
        pool = WorkerPool([1, 2], concurrency=2, per_worker=1)
        server = PoolServer(('127.0.0.1', 0), pool, 'test-only')
        calls = []

        class Controller:
            def set_maintenance(self, port, paused):
                calls.append((port, paused))
                pool.pause(port)

        server.elastic = Controller()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for token, payload, expected in [('wrong', {'port': 2, 'paused': True}, 401),
                                              ('test-only', {'port': 2, 'paused': 'yes'}, 400),
                                              ('test-only', {'port': 2, 'paused': True}, 200),
                                              ('test-only', {'port': 1, 'paused': True}, 400)]:
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                connection.request('POST', '/internal/worker-maintenance', json.dumps(payload),
                                   {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                connection.close()
            self.assertEqual(calls, [(2, True), (1, True)])
            self.assertEqual(pool.snapshot()['enabled'], [1])
        finally:
            server.shutdown()
            server.server_close()


class MixedHttpTest(unittest.TestCase):
    def test_short_query_bypasses_queued_batch_without_changing_body_or_usage(self):
        received = []
        first_gate, query_gate = threading.Event(), threading.Event()
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer test-only'}
        first_body = json.dumps({'input': ['first-complete-document'] * 6}).encode()
        batch_body = json.dumps({'input': ['second-complete-document'] * 6}).encode()
        query_body = json.dumps({'input': 'short query', 'encoding_format': 'float'}).encode()

        class NativeHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append(body)
                if body == first_body:
                    first_gate.wait(5)
                if body == query_body:
                    query_gate.wait(5)
                values = json.loads(body)['input']
                count = len(values) if isinstance(values, list) else 1
                output = json.dumps({'data': [{'index': i, 'embedding': [.6, .8]} for i in range(count)],
                                     'usage': {'prompt_tokens': 177, 'total_tokens': 177}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(output)))
                self.end_headers()
                self.wfile.write(output)

        native = ThreadingHTTPServer(('127.0.0.1', 0), NativeHandler)
        pool = WorkerPool([native.server_port], concurrency=2, per_worker=2, scheduler='fair-shortest')
        server = PoolServer(('127.0.0.1', 0), pool, 'test-only')
        for service in (native, server):
            threading.Thread(target=service.serve_forever, daemon=True).start()

        def call(body):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                connection.request('POST', '/v1/embeddings', body, headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(result['usage'], {'prompt_tokens': 177, 'total_tokens': 177})
                return result, response.getheader('Cache-Status')
            finally:
                connection.close()

        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                first = executor.submit(call, first_body)
                wait_for(lambda: len(received) == 1)
                batch = executor.submit(call, batch_body)
                wait_for(lambda: pool.snapshot()['waiting'] == 1)
                query = executor.submit(call, query_body)
                wait_for(lambda: pool.snapshot()['waiting'] == 2)
                first_gate.set()
                wait_for(lambda: len(received) == 2)
                self.assertEqual(received, [first_body, query_body])
                self.assertFalse(batch.done())
                query_gate.set()
                self.assertEqual(len(query.result()[0]['data']), 1)
                self.assertEqual(len(batch.result()[0]['data']), 6)
                self.assertEqual(len(first.result()[0]['data']), 6)
            repeated, cache_status = call(batch_body)
            self.assertEqual(cache_status, 'AerisunEmbedding; hit')
            self.assertEqual(len(repeated['data']), 6)
            self.assertEqual(received, [first_body, query_body, batch_body])
            self.assertEqual(pool.snapshot()['scheduler']['priority_dispatches'], 1)
        finally:
            first_gate.set()
            query_gate.set()
            for service in (server, native):
                service.shutdown()
                service.server_close()


if __name__ == '__main__':
    unittest.main()
