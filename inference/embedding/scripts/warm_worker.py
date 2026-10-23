#!/usr/bin/env python3
"""Warm all transformer layers and verify the complete embedding before routing."""
from http.client import HTTPConnection
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
key = (root / 'secrets/backend_internal_key.txt').read_text().strip()
port = 8110 + int(sys.argv[1])
connection = HTTPConnection('127.0.0.1', port, timeout=180)
try:
    body = json.dumps({'model': 'Qwen3-Embedding-4B', 'input': [
        '这份中文文档记录了项目计划、实施过程和结果，完整保留原始材料以供检索。' * 4]}, ensure_ascii=False).encode()
    connection.request('POST', '/v1/embeddings', body,
                       {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    response = connection.getresponse()
    assert response.status == 200, response.status
    result = json.loads(response.read())
    assert len(result['data']) == 1 and len(result['data'][0]['embedding']) == 2560
    assert result['usage']['prompt_tokens'] > 32
    print('Embedding worker warmed with the original complete 4B model.')
finally:
    connection.close()
