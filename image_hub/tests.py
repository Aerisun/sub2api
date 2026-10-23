import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from image_hub.server import Hub, HubError, MODEL, NATIVE_MODEL, validate_payload
from image_hub.retention import clean, capacity_available, TTL


class ImageHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temp.name, env={"RIGHTAPI_IMAGE_API_KEY": "test-upstream"})

    def tearDown(self):
        self.temp.cleanup()

    def wait(self, key, id_):
        for _ in range(100):
            value = self.hub.get_task(key, id_)
            if value["status"] != "processing":
                return value
            time.sleep(.01)
        self.fail("Task did not finish")

    def test_customer_generation_uses_gateway_and_counts_actual_images(self):
        calls = []
        def request(url, key, body=None, **kwargs):
            calls.append((url, key, body))
            if url.endswith("/models"):
                return 200, {"data": [{"id": NATIVE_MODEL}]}
            return 200, {"data": [{"url": "https://example.com/result.png"}]}
        with patch("image_hub.server.request_json", side_effect=request), patch.object(self.hub, "download_image", side_effect=HubError(502, "test")):
            task = self.hub.submit("sk-customer", {"prompt": "draw", "n": 2})
            result = self.wait("sk-customer", task["task_id"])
        self.assertNotIn("cost", result)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(calls[-1][0], "http://127.0.0.1:8080/v1/images/generations")
        self.assertEqual(calls[-1][1], "sk-customer")
        self.assertNotIn("sk-customer", self.hub.db_path.read_bytes().decode(errors="ignore"))

    def test_browser_jwt_uses_authenticated_chat_routes_without_persisting_token(self):
        calls = []
        token = "jwt.fake-token-for-test.signature"
        def request(url, key, body=None, **kwargs):
            calls.append((url, key))
            if url.endswith("/models"):
                return 200, {"data": [{"id": NATIVE_MODEL}]}
            return 200, {"data": [{"url": "https://example.com/result.png"}]}
        with patch("image_hub.server.request_json", side_effect=request), patch.object(self.hub, "download_image", side_effect=HubError(502, "test")):
            task = self.hub.submit(token, {"prompt": "draw"}, idempotency="one-browser-generation")
            result = self.wait(token, task["task_id"])
        self.assertNotIn("cost", result)
        self.assertEqual([url for url, _ in calls], ["http://127.0.0.1:8080/api/v1/chat/models", "http://127.0.0.1:8080/api/v1/chat/images/generations"])
        self.assertTrue(all(key == token for _, key in calls))
        self.assertNotIn(token.encode(), self.hub.db_path.read_bytes())
        with patch("image_hub.server.request_json", side_effect=HubError(401, "invalid login")):
            with self.assertRaises(HubError):
                self.hub.submit("untrusted-token", {"prompt": "draw"})

    def test_permissions_block_before_task_creation(self):
        with patch("image_hub.server.request_json", return_value=(200, {"data": []})):
            with self.assertRaises(HubError) as error:
                self.hub.submit("sk-text-only", {"prompt": "draw"})
        self.assertEqual(error.exception.status, 403)
        with self.hub.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_owner_isolation(self):
        self.hub.save_task("sk-owner", {"task_id": "id", "status": "completed", "expires_at": time.time() + 60})
        with self.assertRaises(HubError) as error:
            self.hub.get_task("sk-other", "id")
        self.assertEqual(error.exception.status, 404)

    def test_idempotent_submission_does_not_generate_twice(self):
        with patch.object(self.hub, "authenticate"), patch.object(self.hub, "execute") as execute:
            one = self.hub.submit("sk-owner", {"prompt": "draw"}, idempotency="request-1")
            two = self.hub.submit("sk-owner", {"prompt": "draw"}, idempotency="request-1")
            self.assertEqual(one["task_id"], two["task_id"])
            self.assertEqual(execute.call_count, 1)
            with self.assertRaises(HubError) as error:
                self.hub.submit("sk-owner", {"prompt": "different"}, idempotency="request-1")
            self.assertEqual(error.exception.status, 409)

    def test_failed_gateway_request_has_no_fake_image_or_cost(self):
        with patch.object(self.hub, "authenticate"), patch("image_hub.server.request_json", side_effect=HubError(403, "Insufficient balance")):
            task = self.hub.submit("sk-owner", {"prompt": "draw"})
            result = self.wait("sk-owner", task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("cost", result)
        self.assertNotIn("image_url", result)

    def test_restart_preserves_results_and_never_resubmits_pending_tasks(self):
        self.hub.save_task("sk-owner", {"task_id": "pending", "status": "processing", "expires_at": time.time() + 60})
        self.hub.save_task("sk-owner", {"task_id": "done", "status": "completed", "expires_at": time.time() + 60})
        restarted = Hub(self.temp.name, env={"RIGHTAPI_IMAGE_API_KEY": "test"})
        self.assertEqual(restarted.get_task("sk-owner", "pending")["status"], "interrupted")
        self.assertEqual(restarted.get_task("sk-owner", "done")["status"], "completed")

    def test_retired_adapter_never_contacts_rightapi(self):
        with patch("image_hub.server.request_json") as request:
            with self.assertRaises(HubError) as error:
                self.hub.generate_upstream({"model": MODEL, "prompt": "draw"})
            self.assertEqual(error.exception.status, 410)
            request.assert_not_called()

    def test_rpc_lists_tools_and_failed_calls_are_protocol_results(self):
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        result = self.hub.rpc("sk-owner", request)
        self.assertEqual([tool["name"] for tool in result["result"]["tools"]], ["generate_image", "get_image_task", "wait_for_image"])
        result = self.hub.rpc("sk-owner", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_image_task", "arguments": {"task_id": "missing"}}})
        self.assertTrue(result["result"]["isError"])

    def test_invalid_count_and_external_reference_rejected(self):
        for body in ({"prompt": "draw", "n": True}, {"prompt": "draw", "n": 0},
                     {"prompt": "draw", "image": ["http://127.0.0.1/private"]}):
            with self.assertRaises(HubError):
                validate_payload(body)

    def test_retention_removes_expired_results_and_tasks_but_preserves_recent_and_symlinks(self):
        old = self.hub.images / ("a" * 48 + ".png")
        old.write_bytes(b"old image")
        os.utime(old, (time.time() - TTL - 60,) * 2)
        recent = self.hub.images / ("b" * 48 + ".png")
        recent.write_bytes(b"recent image")
        linked = self.hub.images / ("c" * 48 + ".png")
        linked.symlink_to(recent)
        self.hub.save_task("sk-owner", {"task_id": "expired", "status": "completed", "expires_at": time.time() - 1})
        self.hub.save_task("sk-owner", {"task_id": "recent", "status": "completed", "expires_at": time.time() + 60})
        preview = clean(self.hub.directory)
        self.assertEqual(preview["generated_files"], 1)
        self.assertTrue(old.exists())
        clean(self.hub.directory, apply=True)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(linked.is_symlink())
        self.assertEqual(self.hub.get_task("sk-owner", "recent")["status"], "completed")

    def test_storage_limit_blocks_new_task_without_generating(self):
        with patch("image_hub.server.capacity_available", return_value=False), patch.object(self.hub, "authenticate"), patch.object(self.hub, "execute") as execute:
            with self.assertRaises(HubError) as error:
                self.hub.submit("sk-owner", {"prompt": "draw"})
            self.assertEqual(error.exception.status, 503)
            execute.assert_not_called()

    def test_abandoned_processing_and_partial_outputs_expire_without_touching_unknown_files(self):
        partial = self.hub.images / ("d" * 48 + ".part")
        unknown = self.hub.images / "unrelated.part"
        for path in (partial, unknown):
            path.write_bytes(b"unfinished")
            os.utime(path, (time.time() - 7200,) * 2)
        self.hub.save_task("sk-owner", {"task_id": "abandoned", "status": "processing", "expires_at": time.time() - 60})
        report = clean(self.hub.directory, apply=True)
        self.assertEqual(report["expired_tasks"], 1)
        self.assertFalse(partial.exists())
        self.assertTrue(unknown.exists())
        with self.assertRaises(HubError):
            self.hub.get_task("sk-owner", "abandoned")


if __name__ == "__main__":
    unittest.main()
