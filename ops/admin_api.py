"""Local administration helpers. Credentials never belong in source or reports."""
import json
import os
from pathlib import Path
import shlex
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = 'http://127.0.0.1:8080'
EMBEDDING_MODELS = frozenset({'qwen3-embedding-4b'})
RERANK_MODELS = frozenset({'qwen3-reranker-4b'})
IMAGE_MODELS = frozenset({'gpt-image-2', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst'})


def read_env(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        # Split one shell-quoted value without evaluating commands or variables.
        parsed = shlex.split(value)
        result[key.strip()] = parsed[0] if len(parsed) == 1 else value
    return result


def credentials():
    directory = ROOT/'deployment/private/config'
    settings = read_env(directory/'operator.env')
    return settings, os.environ.get('GOAICHAT_API_KEY') or settings.get('GOAICHAT_API_KEY', '')


def request(path, token=None, payload=None, method=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer '+token
    req = Request(API+path, headers=headers, method=method,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
    try:
        with urlopen(req, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'{path}: HTTP {error.code}') from error
    if result.get('code') != 0:
        raise RuntimeError(f'{path}: administrative request rejected')
    return result.get('data')


def login():
    env, _ = credentials()
    return request('/api/v1/auth/login', payload={
        'email': env['ADMIN_EMAIL'], 'password': env['ADMIN_PASSWORD']})['access_token']


def list_items(path, token):
    items, page = [], 1
    while True:
        data = request(path+f'?page={page}&page_size=100', token)
        batch = data.get('items') or []
        items.extend(batch)
        if not batch or len(items) >= data.get('total', len(items)):
            return items
        page += 1
