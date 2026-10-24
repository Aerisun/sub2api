"""Wait for a configured outbound group to load its approved provider nodes."""
import argparse
import json
from pathlib import Path
import re
import time
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path(__file__).resolve().parents[1]/'deployment/private/egress/mihomo-sub2api/config.yaml')
    parser.add_argument('--group', default='Sub2API egress')
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    group = next(group for group in config['proxy-groups'] if group['name'] == args.group)
    allowed = re.compile(group['filter'])
    request = Request('http://' + config['external-controller'] + '/proxies/' + quote(args.group, safe=''),
                      headers={'Authorization': 'Bearer ' + config.get('secret', '')})
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=2) as response:
                value = json.load(response)
            candidates = value.get('all') or []
            if (candidates and value.get('now') in candidates and
                    all(allowed.fullmatch(name) for name in candidates)):
                return
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise SystemExit('Outbound group did not load its approved provider nodes: ' + args.group)


if __name__ == '__main__':
    main()
