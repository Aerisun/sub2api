#!/usr/bin/env python3
"""Call the local reranker without putting the API key in shell history."""
import json
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

root = Path(__file__).resolve().parent.parent
request = Request("http://127.0.0.1:8101/v1/rerank",
    data=(root / "example-request.json").read_bytes(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + (root / "secrets/api_key.txt").read_text().strip()})
with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
    print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
