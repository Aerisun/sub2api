"""Real Antigravity generation used by the network qualifier."""
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4
from ops.admin_api import ROOT

def probe(account_id, credentials, proxy_url, model, base_url='https://daily-cloudcode-pa.googleapis.com'):
    assert model.startswith(('gemini-', 'claude-')) and 'image' not in model.lower()
    started = time.monotonic()
    result = {'account_id': account_id, 'model': model, 'base_url': base_url, 'ok': False,
              'first_text_ms': None}
    source = ROOT / 'backend/internal/pkg/antigravity/request_transformer.go'
    identity = re.search(r'const antigravityIdentity = `([\s\S]*?)`', source.read_text()).group(1)
    upstream = 'gemini-pro-agent' if model == 'gemini-3.1-pro-high' else model
    result['upstream_model'] = upstream
    config = {'maxOutputTokens': 2048}
    if model == 'claude-opus-4-6-thinking':
        config['thinkingConfig'] = {'thinkingBudget': 1024, 'includeThoughts': True}
        config['temperature'] = 1
    body = {'project': credentials['project_id'], 'model': upstream,
            'requestId': 'agent-' + str(uuid4()), 'userAgent': 'antigravity',
            'requestType': 'agent', 'request': {
                'contents': [{'role': 'user', 'parts': [{'text': 'Reply with OK only.'}]}],
                'systemInstruction': {'parts': [{'text': identity}]},
                'sessionId': str(uuid4()),
                'toolConfig': {'functionCallingConfig': {'mode': 'VALIDATED'}},
                'generationConfig': config}}
    req = Request(base_url + '/v1internal:streamGenerateContent?alt=sse',
                  data=json.dumps(body).encode(), method='POST', headers={
                      'Authorization': 'Bearer ' + credentials['access_token'],
                      'Content-Type': 'application/json', 'User-Agent': 'antigravity/2.9.1 linux/amd64'})
    opener = build_opener(ProxyHandler({'https': proxy_url}))
    reply, completed = '', False
    try:
        with opener.open(req, timeout=35) as response:
            result['http_status'] = response.status
            for line in response:
                if time.monotonic() - started > 45:
                    result['error'] = 'Generation exceeded 45 seconds'
                    break
                if not line.startswith(b'data:'):
                    continue
                data = json.loads(line[5:].strip())
                data = data.get('response', data)
                if data.get('modelVersion'):
                    result['response_model'] = data['modelVersion']
                if data.get('error'):
                    result['error'] = data['error']
                if data.get('usageMetadata'):
                    result['usage'] = data['usageMetadata']
                for candidate in data.get('candidates', []):
                    completed |= bool(candidate.get('finishReason'))
                    result['finish_reason'] = candidate.get('finishReason') or result.get('finish_reason')
                    for part in candidate.get('content', {}).get('parts', []):
                        if part.get('text') and not part.get('thought'):
                            reply += part['text']
                            if result['first_text_ms'] is None:
                                result['first_text_ms'] = round((time.monotonic() - started) * 1000)
        result['reply'] = reply[:200]
        result['ok'] = reply.strip().upper().rstrip('.!') == 'OK' and completed and not result.get('error')
    except HTTPError as error:
        result['http_status'] = error.code
        result['error'] = error.read(1600).decode(errors='replace')
    except Exception as error:
        result['error'] = str(error)[:500]
    result['total_ms'] = round((time.monotonic() - started) * 1000)
    return result
