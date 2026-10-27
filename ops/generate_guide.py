#!/usr/bin/env python3
"""Refresh public guide model IDs and OpenCode config from the live allowlist."""

from html import escape
import json
from pathlib import Path
import re

from ops import admin_api as api
from ops import admin_api as unified

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'frontend/public/api-guide.html'
CUSTOMER_GROUP_ID = 9
TOOL_IDS = {'image_generation', 'image_recognition', 'web_search'}
FAMILIES = (
    ('gpt-image-', '图片生成'),
    ('gpt-6', 'Codex'), ('gpt-5.6', 'Codex'),
    ('claude-', 'Claude'), ('kimi-', 'Kimi'), ('glm-', 'GLM'),
    ('deepseek-', 'DeepSeek'), ('qwen', 'Qwen'), ('minimax-', 'MiniMax'),
    ('gemini-', 'Gemini'),
    ('gte-', 'GTE'),
    ('nemotron-', 'NVIDIA'), ('step-', '阶跃星辰'),
)
# Image understanding confirmed through routed endpoints; text-only Nemotron uses internal vision tools.
VERIFIED_IMAGE_MODELS = {'claude-sonnet-4-6', 'claude-opus-4-6-thinking', 'gemini-3.8-flash', 'kimi-k3', 'kimi-k3-free', 'deepseek-v4.1-flash', 'qwen3.8-flash', 'qwen3.8-max', 'Nemotron-3.5'}
# Endpoint constraints, checked against Qiniu's model_constraints and NVIDIA model cards.
# These describe the active routing pool, rather than Claude's assumed alias window.
MODEL_LIMITS = {
    'gpt-6.1-terra': (872000,128000),
    # Account 54's current Antigravity catalog caps these Claude routes.
    'claude-sonnet-4-6': (250000,64000),
    'claude-opus-4-6-thinking': (250000,64000),
    'gemini-3.8-flash': (1048576,65536),
    'kimi-k3': (1048576,1048576), 'kimi-k3-free': (1048576,1048576),
    'deepseek-v4.1-flash': (1000000,384000),
    'qwen3.8-flash': (1000000,128000), 'qwen3.8-max': (1000000,128000),
    'Nemotron-3.5': (1000000,128000),
    **{model: (1050000, 128000) for model in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                                            'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')},
}


def family(model):
    return next((label for prefix, label in FAMILIES if model.lower().startswith(prefix)), '其他')


def ordered(models):
    labels = [label for _, label in FAMILIES] + ['其他']
    return sorted(set(models), key=lambda model: (labels.index(family(model)), model))


def render_catalog(models):
    groups = {}
    for model in ordered(models):
        if model in TOOL_IDS or model.startswith('gpt-image-'):
            continue
        groups.setdefault(family(model), []).append(model)
    output = []
    for label, entries in groups.items():
        buttons = ''.join(
            f'<button class="docs-model-id docs-copy btn btn-secondary" type="button" '
            f'data-model-id="{escape(model)}" data-model-search="{escape((label + " " + model).lower())}" '
            f'data-copy="{escape(model)}" aria-label="复制 {escape(model)}">{escape(model)}</button>'
            for model in entries
        )
        notice = ''
        if label == '图片生成' and entries == ['gpt-image-2.5']:
            notice = '<p>每张 $0.20 · <a href="/sub2api/image-studio.html">生图中枢</a></p>'
        output.append(f'<div class="docs-model-group"><h3>{label}</h3>{notice}'
                      f'<div class="docs-model-ids">{buttons}</div></div>')
    return '\n'.join(output)


def opencode_config(models):
    entries = {}
    for model in ordered(models):
        if model in unified.EMBEDDING_MODELS | unified.RERANK_MODELS | unified.IMAGE_MODELS | TOOL_IDS or model.startswith('gpt-image-'):
            continue
        entry = {'name': model}
        if model in MODEL_LIMITS:
            context, output = MODEL_LIMITS[model]
            entry['limit'] = {'context': context, 'output': min(output, context)}
        if model in {'kimi-k3', 'kimi-k3-free', 'deepseek-v4.1-flash'} or model.lower().startswith(('nemotron-', 'step-')):
            entry['reasoning'] = True
            entry['options'] = {'reasoningEffort': 'low'}
            entry['variants'] = {effort: {'reasoningEffort': effort}
                                 for effort in ('auto', 'low', 'medium', 'high', 'xhigh', 'max')}
            if model.lower().startswith(('deepseek-', 'nemotron-', 'step-')):
                entry['variants']['off'] = {'reasoningEffort': 'none'}
        if model.lower().startswith('gemini-') and 'image' not in model:
            entry['reasoning'] = True
            entry['options'] = {'reasoningEffort': 'low'}
            entry['variants'] = {effort: {'reasoningEffort': effort} for effort in ('low', 'medium', 'high', 'auto')}
        if model in {'claude-sonnet-4-6', 'claude-opus-4-6-thinking'}:
            entry['reasoning'] = True
            entry['options'] = {'reasoningEffort': 'low'}
            entry['variants'] = {effort: {'reasoningEffort': effort}
                                 for effort in ('auto', 'low', 'medium', 'high', 'xhigh', 'max')}
            if model == 'claude-sonnet-4-6':
                entry['variants']['off'] = {'reasoningEffort': 'none'}
        if model.startswith(('gpt-6', 'gpt-5.6')):
            entry['reasoning'] = True
            entry['options'] = {'reasoningEffort': 'low' if model == 'gpt-6.1-terra' else 'medium'}
            entry['variants'] = {effort: {'reasoningEffort': effort}
                                 for effort in ('low', 'medium', 'high', 'xhigh', 'max')}
            if model not in ('gpt-6-astra', 'gpt-6.1-sol'):
                entry['variants']['off'] = {'reasoningEffort': 'none'}
            entry['modalities'] = {'input': ['text', 'image'], 'output': ['text']}
        if model in VERIFIED_IMAGE_MODELS:
            entry['modalities'] = {'input': ['text', 'image'], 'output': ['text']}
        entries[model] = entry
    default = ('gpt-6-sol' if 'gpt-6-sol' in entries else
               'claude-sonnet-4-6' if 'claude-sonnet-4-6' in entries else next(iter(entries)))
    lines = '\n'.join('        ' + json.dumps(model) + ': ' + json.dumps(entry, ensure_ascii=False)
                      + (',' if index < len(entries) - 1 else '')
                      for index, (model, entry) in enumerate(entries.items()))
    return '''{
  "$schema": "https://opencode.ai/config.json",
  "model": "aerisun/''' + default + '''",
  "provider": {
    "aerisun": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Aerisun-API",
      "options": {
        "baseURL": "https://aerisun.top/sub2api",
        "apiKey": "{env:SUB2API_API_KEY}"
      },
      "models": {
''' + lines + '''
      }
    }
  }
}'''


def live_models():
    group = api.request(f'/api/v1/admin/groups/{CUSTOMER_GROUP_ID}', unified.login())
    if group['status'] != 'active' or not group['model_allowlist']['enabled']:
        raise RuntimeError('Guide requires the active customer model allowlist')
    models = {model for model in group['model_allowlist']['models'] if model not in TOOL_IDS and not model.startswith('gpt-image-')}
    priced = {model for entry in group['model_pricing'] for model in entry['models']}
    if not models or models - priced:
        raise RuntimeError('Guide models must have configured prices')
    return ordered(models)


def main():
    models = live_models()
    content = GUIDE.read_text()
    # Both CLI and extension exports use these same source-backed limits.
    encoded_limits = json.dumps({m:v[0] for m,v in MODEL_LIMITS.items() if m in models})
    content = re.sub(r'(<script id="model-context-limits" type="application/json">).*?(</script>)',
                     lambda match:match[1]+encoded_limits+match[2],content,flags=re.S)
    if 'id="model-context-limits"' not in content:
        content = content.replace('</head>', '<script id="model-context-limits" type="application/json">'+encoded_limits+'</script>\n</head>')
    start, end = '<!-- MODEL_IDS_START -->', '<!-- MODEL_IDS_END -->'
    if content.count(start) != 1 or content.count(end) != 1:
        raise RuntimeError('Guide model catalog markers missing or duplicated')
    before, remainder = content.split(start)
    _, after = remainder.split(end)
    content = before + start + '\n' + render_catalog(models) + '\n' + end + after
    content, count = re.subn(r'(<span\b[^>]*\bid="models-count"[^>]*>)[^<]*(</span>)',
                            lambda match: match[1] + str(len(models)) + ' 个模型' + match[2], content)
    if count != 1:
        raise RuntimeError('Guide model count missing or duplicated')
    content, count = re.subn(r'(<code id="opencode-config-code">).*?(</code>)',
                            lambda match: match[1] + escape(opencode_config(models), quote=False) + match[2],
                            content, flags=re.S)
    if count != 1:
        raise RuntimeError('Guide OpenCode config missing or duplicated')
    GUIDE.write_text(content)
    catalog_path = ROOT / 'frontend/src/config/aerisunModels.json'
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = json.loads(opencode_config(models))['provider']['aerisun']['models']
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
    print(f'Published {len(models)} guide model IDs and {len(catalog)} OpenCode chat models.')


if __name__ == '__main__':
    main()
