"""Check privacy, duplicate counting and unchanged native HTTP responses."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

import test_cache_status
from workload_profile import WorkloadProfile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.profile = WorkloadProfile(Path(self.directory.name) / "stats.json", seconds=1)

    def tearDown(self):
        self.profile.deadline = 0
        deadline = time.monotonic() + 4
        while not self.profile.finished:
            self.assertLess(time.monotonic(), deadline, 'profile writer did not finish before cleanup')
            time.sleep(.01)
        self.directory.cleanup()

    def complete(self, value):
        ticket = self.profile.start(json.dumps(value).encode())
        self.profile.finish(ticket, 200, b'{"data":[{"embedding":[0.6,0.8]}],"usage":{"prompt_tokens":12}}',
                            "AerisunEmbedding; fwd=miss", 0, 1)
        return self.profile.snapshot()["rows"][-1]

    def test_only_counts_retained_and_cross_batch_repeats_are_distinct(self):
        self.complete({"input": ["PRIVATE_TEXT_A", "PRIVATE_TEXT_B"], "user": "PRIVATE_USER"})
        second = self.complete({"input": ["PRIVATE_TEXT_B", "PRIVATE_TEXT_C"], "user": "different"})
        third = self.complete({"input": ["PRIVATE_TEXT_A", "PRIVATE_TEXT_A"]})
        self.assertEqual(second["items_seen_in_prior_requests"], 1)
        self.assertEqual(third["items_seen_in_prior_requests"], 2)
        self.assertEqual(third["duplicate_items_within_batch"], 1)
        encoded = json.dumps(self.profile.snapshot())
        self.assertNotIn("PRIVATE_TEXT", encoded)
        self.assertNotIn("PRIVATE_USER", encoded)
        self.assertNotIn("embedding", self.profile.snapshot()["rows"][0])

    def test_bounds_and_invalid_metadata_do_not_capture_values(self):
        self.profile.limit = 1
        self.complete({"input": "PRIVATE_TEXT", "encoding_format": "PRIVATE_VALUE", "dimensions": "PRIVATE_VALUE"})
        self.assertIsNone(self.profile.start(b'{"input":"second"}'))
        self.assertNotIn("PRIVATE", json.dumps(self.profile.snapshot()))
        self.profile.deadline = 0
        self.assertIsNone(self.profile.start(b'{"input":"after deadline"}'))

    def test_private_output_is_written_and_sampling_stops(self):
        self.complete({"input": "PRIVATE_TEXT"})
        self.profile.deadline = 0
        deadline = time.monotonic() + 4
        while not self.profile.finished:
            self.assertLess(time.monotonic(), deadline, "profile writer did not finish")
            time.sleep(.01)
        data = json.loads(self.profile.output.read_text())
        self.assertEqual(data["completed_samples"], 1)
        self.assertFalse(data["accepting_samples"])
        self.assertEqual(self.profile.output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE_TEXT", self.profile.output.read_text())

    def test_disconnected_cache_waiter_releases_profile_sample(self):
        from concurrent.futures import ThreadPoolExecutor
        import socket
        fixture = test_cache_status.CacheStatusTests()
        fixture.setUp()
        fixture.native.gate.clear()
        fixture.proxy.profile = self.profile
        self.profile.deadline = time.monotonic() + 4
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                owner = executor.submit(fixture.call)
                deadline = time.monotonic() + 2
                while fixture.native.calls != 1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                payload = b'{"input":"complete input"}'
                sock = socket.create_connection(("127.0.0.1", fixture.proxy.server_port))
                sock.sendall(b'POST /v1/embeddings HTTP/1.0\r\nAuthorization: Bearer private\r\nContent-Length: '
                             + str(len(payload)).encode() + b'\r\n\r\n' + payload)
                while fixture.proxy.cache.snapshot()["coalesced"] != 1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                sock.close()
                while self.profile.snapshot()["active_samples"] != 1:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                fixture.native.gate.set()
                self.assertEqual(owner.result(timeout=3)[0], 200)
                while self.profile.snapshot()["active_samples"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertEqual(sorted(r["status"] for r in self.profile.snapshot()["rows"]), [200, 499])
        finally:
            fixture.tearDown()

    def test_observation_preserves_vectors_usage_and_cache_status(self):
        fixture = test_cache_status.CacheStatusTests()
        fixture.setUp()
        try:
            fixture.proxy.profile = self.profile
            first, hit = fixture.call(), fixture.call()
            self.assertEqual(first[:2], hit[:2])
            self.assertEqual(first[2], "AerisunEmbedding; fwd=miss")
            self.assertEqual(hit[2], "AerisunEmbedding; hit")
            deadline = time.monotonic() + 2
            while self.profile.snapshot()["completed_samples"] < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            rows = self.profile.snapshot()["rows"]
            self.assertEqual([r["prompt_tokens"] for r in rows], [12, 12])
            self.assertEqual(rows[1]["items_seen_in_prior_requests"], 1)
            self.assertEqual(rows[0]["vector_dimensions"], [2])
            self.assertEqual(fixture.native.calls, 1)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
