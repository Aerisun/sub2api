"""Merge private subscriptions; only isolated qualification can promote nodes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import httpx
import yaml

from image_hub.proxy_qualifier import atomic_write, eligible_nodes, read_yaml

ROOT = Path(__file__).resolve().parents[1]


def subscription_nodes(raw):
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Subscription exceeds the size limit')
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise ValueError('Subscription is not valid Clash YAML') from None
    if not isinstance(value, dict) or not isinstance(value.get('proxies'), list):
        raise ValueError('Subscription has no Clash proxy list')
    nodes = [n for n in value['proxies'] if isinstance(n, dict) and n.get('server')
             and n.get('port') and n.get('type') not in ('direct', 'reject', 'compatible')]
    if not nodes:
        raise ValueError('Subscription has no usable proxy nodes')
    return nodes


def normalized_nodes(nodes, namespace):
    result = []
    regions = ((r'🇺🇸|(?:^|[^A-Z])US[-\s]|美国|UnitedStates', 'NA-UnitedStates-'),
               (r'🇯🇵|(?:^|[^A-Z])JP[-\s]|日本|Japan', 'ASIA-Japan-'),
               (r'🇸🇬|(?:^|[^A-Z])SG[-\s]|新加坡|Singapore', 'ASIA-Singapore-'))
    for node in nodes:
        name = str(node.get('name', ''))
        if not eligible_nodes({'proxies': [node]}):
            prefix = next((prefix for pattern, prefix in regions if re.search(pattern, name)), None)
            if prefix is None:
                continue
            name = prefix + name
        result.append({**node, 'name': name + (' [' + namespace + ']' if namespace else '')})
    return result


def merge_nodes(sources):
    result, seen, names = [], set(), set()
    for namespace, nodes in sources:
        for node in normalized_nodes(nodes, namespace):
            identity = hashlib.sha256(json.dumps({k:v for k,v in node.items() if k!='name'},
                                                 sort_keys=True).encode()).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            if node['name'] in names:
                node = {**node, 'name': node['name'] + '-' + identity[:8]}
            names.add(node['name'])
            result.append(node)
    return result


def fetch_source(source, proxies):
    url = Path(source['url_file']).read_text().strip()
    if not url.startswith('https://'):
        raise ValueError('Subscriptions must use HTTPS')
    failure = None
    for proxy in proxies:
        try:
            with httpx.Client(proxy=proxy, trust_env=False, timeout=httpx.Timeout(12, connect=5),
                              follow_redirects=True) as client:
                response = client.get(url, headers={'User-Agent': 'clash.meta'})
                response.raise_for_status()
                nodes = subscription_nodes(response.content)
                if not normalized_nodes(nodes, source['name']):
                    raise ValueError('Subscription contains no eligible regions')
                atomic_write(source['cache_file'], response.content)
                return nodes
        except Exception as error:
            failure = type(error).__name__
    # URLs, subscription tokens and provider credentials never enter logs.
    raise RuntimeError(failure or 'SubscriptionFetchFailed') from None


def refresh(config):
    reports, sources = [], []
    primary = config['primary_file']
    try:
        raw = Path(primary).read_bytes()
        nodes = subscription_nodes(raw)
        atomic_write(config['primary_cache_file'], raw)
        reports.append({'name':'primary','state':'loaded','nodes':len(nodes)})
    except Exception as error:
        nodes = subscription_nodes(Path(config['primary_cache_file']).read_bytes())
        reports.append({'name':'primary','state':'cached','error_type':type(error).__name__,'nodes':len(nodes)})
    sources.append(('', nodes))
    for source in config['sources']:
        try:
            nodes = fetch_source(source, config.get('fetch_proxies', [None]))
            reports.append({'name':source['name'],'state':'refreshed','nodes':len(nodes)})
        except Exception as error:
            try:
                nodes = subscription_nodes(Path(source['cache_file']).read_bytes())
            except Exception:
                reports.append({'name':source['name'],'state':'retained_without_cache',
                                'error_type':type(error).__name__,'nodes':0})
                # An unknown/empty replacement cannot remove existing candidates.
                return {'state':'retained','checked_at':time.time(),'sources':reports}
            reports.append({'name':source['name'],'state':'cached','error_type':type(error).__name__,
                            'nodes':len(nodes)})
        sources.append((source['name'], nodes))
    combined = merge_nodes(sources)
    if not combined:
        return {'state':'retained','checked_at':time.time(),'sources':reports}
    path = Path(config['output_file'])
    previous = read_yaml(path).get('proxies',[]) if path.exists() else []
    if previous != combined:
        atomic_write(path, yaml.safe_dump({'proxies':combined}, allow_unicode=True, sort_keys=False).encode())
    return {'state':'updated' if previous!=combined else 'unchanged', 'checked_at':time.time(),
            'eligible_nodes':len(combined),'sources':reports}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'deployment/private/config/egress-subscriptions.json')
    args=parser.parse_args()
    config=json.loads(args.config.read_text())
    result=refresh(config)
    atomic_write(config['state_file'],(json.dumps(result,indent=2)+'\n').encode())
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
