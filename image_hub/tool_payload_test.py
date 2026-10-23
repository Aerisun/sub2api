"""The tool HTTP boundary accepts the web composer's full four-image payload."""
import json
import threading
import unittest
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

from image_hub.agent_server import AgentHTTPServer, Handler


class ToolPayloadTests(unittest.TestCase):
    def test_four_five_megabyte_attachments_fit_one_recognition_request(self):
        image = "data:image/png;base64," + "A" * (4 * ((5 * 1024 * 1024 + 2) // 3))
        raw = json.dumps({"images": [image] * 4, "prompt": "比较四张图"}).encode()
        self.assertGreater(len(raw), 24 * 1024 * 1024)
        server = AgentHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Image decoding is covered by the executor tests. Here only the HTTP
        # boundary is under test; no upstream model is submitted or billed.
        try:
            with patch("internal_tools.http_api.registry.catalogue", return_value=[]), patch(
                "internal_tools.http_api.registry.execute",
                return_value={"name": "image_recognition", "status": "completed", "output": {"text": "four images"}},
            ) as execute:
                request = Request(f"http://127.0.0.1:{server.server_port}/v1/tools/image_recognition", raw,
                                  {"Authorization": "Bearer user-jwt", "Content-Type": "application/json"})
                with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[1]["images"], [image] * 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
