import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from nvidia_pool.server import NvidiaPool
from qiniu_pool.checkpoint import Checkpoint, atomic_write
from qiniu_pool.server import Pool, Unavailable


class CheckpointTests(unittest.TestCase):
    def test_expired_bindings_are_pruned_and_recent_homes_are_preserved(self):
        with TemporaryDirectory() as folder:
            now = [100.]
            pool = Pool(["key"], {"capacity":1, "per_key":1, "global_rpm":0,
                "model_rpm":0, "affinity_ttl":10, "state_path":str(Path(folder)/"state")},
                clock=lambda:now[0])
            pool.affinity.update(old=(0, 89), boundary=(0, 90), recent=(0, 99))
            pool.route_affinity.update(old=("a", 89), recent=("b", 99))
            pool._persist()
            self.assertEqual(list(pool.affinity), ["recent"])
            self.assertEqual(list(pool.route_affinity), ["recent"])
            state = json.loads(pool.state_path.read_text())
            self.assertEqual(state["affinity"][0][:2], ["recent", 0])
            now[0] += 10
            pool._persist()
            self.assertFalse(pool.affinity)
            self.assertFalse(pool.route_affinity)

    def test_slow_durable_disk_does_not_block_new_admission_and_latest_copy_wins(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            entered, release = threading.Event(), threading.Event()
            def slow_write(path, raw):
                if path == durable and b'"revision":' in raw:
                    entered.set()
                    self.assertTrue(release.wait(3))
                atomic_write(path, raw)
            with patch("qiniu_pool.checkpoint.atomic_write", side_effect=slow_write):
                store = Checkpoint(durable, runtime, interval=.01)
                try:
                    store.write({"revision": 1})
                    self.assertTrue(entered.wait(2))
                    # A writer is blocked in durable I/O. Both a new checkpoint
                    # and status inspection must still finish independently.
                    completed = threading.Event()
                    def update():
                        store.write({"revision": 2})
                        store.status()
                        completed.set()
                    request = threading.Thread(target=update)
                    request.start()
                    self.assertTrue(completed.wait(.5))
                    self.assertEqual(json.loads(runtime.read_text())["revision"], 2)
                    release.set()
                    request.join()
                    self.assertTrue(store.flush())
                    self.assertEqual(json.loads(durable.read_text())["revision"], 2)
                    self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(durable.stat().st_mode & 0o777, 0o600)
                finally:
                    release.set()
                    store.close()

    def test_both_pools_restart_with_current_rate_window_and_affinity(self):
        for cls in (Pool, NvidiaPool):
            with self.subTest(pool=cls.__name__), TemporaryDirectory() as folder:
                config = {"capacity": 2, "per_key": 1, "model_capacity": 2,
                          "global_rpm": 0, "model_rpm": 1, "key_rpm": 1,
                          "queue_seconds": 0, "state_path": str(Path(folder)/"durable"),
                          "runtime_state_path": str(Path(folder)/"runtime")}
                first = cls(["one"], config)
                second = None
                try:
                    fingerprint = first.fingerprint({"model":"m", "conversation_id":"private-session"}, {})
                    lease = first.acquire("m", fingerprint)
                    first.release(lease, "m", True)
                    first.checkpoint.close()
                    second = cls(["one"], config)
                    self.assertFalse(second.checkpoint.restore_cooldown)
                    self.assertEqual(second.fingerprint({"model":"m", "conversation_id":"private-session"}, {}), fingerprint)
                    self.assertEqual(second.affinity[fingerprint][0], lease.index)
                    with self.assertRaises(Unavailable):
                        second.acquire("m", fingerprint)
                    self.assertNotIn("private-session", Path(config["runtime_state_path"]).read_text())
                finally:
                    first.checkpoint.close()
                    if second:
                        second.checkpoint.close()

    def test_missing_runtime_restores_durable_state_with_a_full_quota_window(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            store = Checkpoint(durable, runtime)
            store.write({"affinity": [], "cooldowns": {}})
            store.close()
            runtime.unlink()
            pool = Pool(["key"], {"capacity":1, "per_key":1, "global_rpm":0,
                "model_rpm":1, "queue_seconds":0, "state_path":str(durable),
                "runtime_state_path":str(runtime)}, clock=lambda:100)
            try:
                self.assertTrue(pool.checkpoint.restore_cooldown)
                with self.assertRaises(Unavailable) as error:
                    pool.acquire("m")
                self.assertEqual(error.exception.retry_after, 60)
                pool.checkpoint.close()
                again = Pool(["key"], {"capacity":1, "per_key":1, "global_rpm":0,
                    "model_rpm":1, "queue_seconds":0, "state_path":str(durable),
                    "runtime_state_path":str(runtime)}, clock=lambda:100)
                try:
                    self.assertFalse(again.checkpoint.restore_cooldown)
                    with self.assertRaises(Unavailable) as repeated:
                        again.acquire("m")
                    self.assertEqual(repeated.exception.retry_after, 60)
                finally:
                    again.checkpoint.close()
            finally:
                pool.checkpoint.close()

    def test_initial_migration_preserves_old_state_without_unnecessary_cooldown(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            atomic_write(durable, b'{"affinity_salt":"abcdef"}')
            store = Checkpoint(durable, runtime)
            try:
                self.assertFalse(store.restore_cooldown)
                self.assertEqual(json.loads(runtime.read_text())["affinity_salt"], "abcdef")
            finally:
                store.close()

    def test_only_one_process_can_own_a_runtime_checkpoint(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            first = Checkpoint(durable, runtime)
            try:
                with self.assertRaises(OSError):
                    Checkpoint(durable, runtime)
            finally:
                first.close()
            second = Checkpoint(durable, runtime)
            second.close()

    def test_crash_before_durable_copy_recovers_acknowledged_local_commit(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            code = """
import os,sys
from qiniu_pool.checkpoint import Checkpoint
checkpoint=Checkpoint(sys.argv[1],sys.argv[2],interval=60)
checkpoint.write({'starts':{'N01':[123]},'affinity':[['fingerprint',0,456]]})
os._exit(0)
"""
            subprocess.run([sys.executable, "-c", code, str(durable), str(runtime)], check=True, timeout=20)
            self.assertTrue(durable.exists())
            self.assertNotIn("starts", json.loads(durable.read_text()))
            second = Checkpoint(durable, runtime)
            try:
                self.assertFalse(second.restore_cooldown)
                self.assertEqual(json.loads(runtime.read_text())["starts"], {"N01":[123]})
                self.assertTrue(second.flush())
                self.assertEqual(json.loads(durable.read_text())["affinity"], [["fingerprint",0,456]])
            finally:
                second.close()

    def test_local_commit_releases_scheduler_lock_and_reserves_one_recovery_probe(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            pool = NvidiaPool(["one", "two"], {"capacity":2, "per_key":1, "key_rpm":40,
                "global_rpm":0, "model_rpm":0, "queue_seconds":0,
                "state_path":str(durable), "runtime_state_path":str(runtime)})
            pool.transient_circuits["m"] = {"until":0, "probe":False, "backoff":3, "avoid":[]}
            entered, release = threading.Event(), threading.Event()
            def slow_local(path, raw):
                if path == runtime:
                    entered.set()
                    if not release.wait(3):
                        raise OSError("blocked")
                atomic_write(path, raw)
            try:
                with patch("qiniu_pool.checkpoint.atomic_write", side_effect=slow_local), ThreadPoolExecutor(2) as executor:
                    pending = executor.submit(pool.acquire, "m")
                    self.assertTrue(entered.wait(1))
                    snapshot = executor.submit(pool.snapshot).result(timeout=.3)
                    self.assertEqual(snapshot["inflight"], 1)
                    self.assertEqual(snapshot["workload_inflight"]["chat"], 1)
                    with self.assertRaises(Unavailable):
                        pool.acquire("m")
                    release.set()
                    lease = pending.result(timeout=1)
                    pool.release(lease, "m", True)
                    self.assertEqual(pool.inflight, 0)
                    self.assertNotIn("m", pool.transient_circuits)
            finally:
                release.set()
                pool.checkpoint.close()

    def test_concurrent_commits_retain_every_rate_start_and_affinity_after_restart(self):
        with TemporaryDirectory() as folder:
            config={"capacity":20, "per_key":16, "key_rpm":40, "global_rpm":0,
                "model_rpm":0, "queue_seconds":0, "state_path":str(Path(folder)/"durable"),
                "runtime_state_path":str(Path(folder)/"runtime")}
            pool=NvidiaPool(["one", "two"], config)
            try:
                gate=threading.Barrier(20)
                def start(index):
                    gate.wait()
                    return pool.acquire("m", "session-"+str(index))
                with ThreadPoolExecutor(20) as executor:
                    leases=list(executor.map(start, range(20)))
                self.assertEqual(pool.inflight, 20)
                self.assertEqual(pool.workload_inflight["chat"], 20)
                for lease in leases:
                    pool.release(lease, "m", True)
                pool.checkpoint.close()

                again=NvidiaPool(["one", "two"], config)
                try:
                    self.assertEqual(sum(len(starts) for starts in again.starts.values()), 20)
                    self.assertEqual(len(again.affinity), 20)
                    self.assertEqual(again.inflight, 0)
                finally:
                    again.checkpoint.close()
            finally:
                pool.checkpoint.close()

    def test_key_expansion_does_not_hold_scheduler_or_route_uncommitted_keys(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            pool=NvidiaPool(["one", "two"], {"capacity":4, "per_key":1, "key_rpm":40,
                "global_rpm":0, "model_rpm":0, "queue_seconds":0,
                "state_path":str(durable), "runtime_state_path":str(runtime)})
            entered, release=threading.Event(), threading.Event()
            def slow_local(path, raw):
                if path == runtime:
                    entered.set()
                    if not release.wait(3):
                        raise OSError("blocked")
                atomic_write(path, raw)
            try:
                with patch("qiniu_pool.checkpoint.atomic_write", side_effect=slow_local), ThreadPoolExecutor(3) as executor:
                    expansion=executor.submit(pool.expand, ["one", "two", "three"])
                    self.assertTrue(entered.wait(1))
                    state=executor.submit(pool.snapshot).result(timeout=.3)
                    self.assertEqual(state["available_keys"], 2)
                    request=executor.submit(pool.acquire, "m")
                    deadline=time.monotonic()+1
                    while not executor.submit(pool.snapshot).result(timeout=.3)["inflight"]:
                        self.assertLess(time.monotonic(), deadline)
                    self.assertEqual(pool.members[2].inflight, 0)
                    self.assertTrue(pool.members[2].pending)
                    release.set()
                    self.assertEqual(expansion.result(timeout=1), 1)
                    lease=request.result(timeout=1)
                    self.assertLess(lease.index, 2)
                    pool.release(lease, "m", True)
                    self.assertEqual(pool.snapshot()["available_keys"], 3)
            finally:
                release.set()
                pool.checkpoint.close()

    def test_durable_failure_retains_runtime_and_can_recover(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            failed = threading.Event()
            def failure(path, raw):
                if path == durable:
                    failed.set()
                    raise OSError("unavailable")
                atomic_write(path, raw)
            store = Checkpoint(durable, runtime, interval=.01)
            try:
                with patch("qiniu_pool.checkpoint.atomic_write", side_effect=failure):
                    store.write({"revision": 3})
                    self.assertTrue(failed.wait(2))
                    self.assertFalse(store.flush(timeout=.02))
                    self.assertEqual(json.loads(runtime.read_text())["revision"], 3)
                self.assertTrue(store.flush(timeout=3))
                self.assertGreaterEqual(store.status()["durable_write_failures"], 1)
                self.assertFalse(store.status()["durable_pending"])
            finally:
                store.close()

    def test_local_failure_is_returned_to_caller_and_preserves_previous_snapshot(self):
        with TemporaryDirectory() as folder:
            durable, runtime = (Path(folder)/name for name in ("durable", "runtime"))
            store = Checkpoint(durable, runtime)
            try:
                store.write({"revision": 1})
                self.assertTrue(store.flush())
                with patch("qiniu_pool.checkpoint.atomic_write", side_effect=OSError("local unavailable")):
                    with self.assertRaises(OSError):
                        store.write({"revision": 2})
                self.assertEqual(json.loads(runtime.read_text())["revision"], 1)
            finally:
                store.close()
