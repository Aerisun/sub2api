"""Build for the configured public mount and publish assets before entry pages."""
import argparse
import fcntl
import gzip
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urljoin, urlsplit

from ops.admin_api import ROOT


class EntryAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == 'script' and attributes.get('src'):
            self.paths.append(attributes['src'])
        elif tag == 'link' and attributes.get('rel') in ('modulepreload', 'stylesheet'):
            self.paths.append(attributes.get('href', ''))


def site_mount(public_url):
    url = urlsplit(public_url)
    if url.scheme not in ('http', 'https') or not url.netloc or url.query or url.fragment:
        raise ValueError('A public HTTP(S) URL without a query or fragment is required')
    path = url.path.rstrip('/')
    if '..' in unquote(path).split('/') or '//' in path:
        raise ValueError('Invalid public mount')
    return path + '/'


def entry_assets(html, entry_url, public_url):
    parser = EntryAssets()
    parser.feed(html)
    origin = urlsplit(public_url)
    mount = site_mount(public_url)
    result = []
    for reference in parser.paths:
        resolved = urlsplit(urljoin(entry_url, reference))
        path = unquote(resolved.path)
        if ((resolved.scheme, resolved.netloc) != (origin.scheme, origin.netloc)
                or not path.startswith(mount) or '..' in path.split('/')):
            raise ValueError('Entry asset escapes the configured public mount')
        result.append(path[len(mount):])
    if not result:
        raise ValueError('Entry document contains no application assets')
    return result


def validate_bundle(directory, public_url):
    root = Path(directory).resolve()
    files = []
    for entry in ('index.html', 'chat-ui/index.html'):
        url = public_url.rstrip('/') + '/' + entry
        paths = entry_assets((root/entry).read_text(), url, public_url)
        for relative in paths:
            path = (root/relative).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
                raise ValueError('Entry asset is missing or empty')
        files.extend(paths)
    return sorted(set(files))


def publish(directory, destination, public_url):
    directory, destination = Path(directory), Path(destination)
    assets = validate_bundle(directory, public_url)
    destination.mkdir(parents=True, exist_ok=True)
    files = [p for p in directory.rglob('*') if p.is_file()]
    # Keep previously published hashed chunks: open tabs can still import them.
    files.sort(key=lambda p: (p.suffix == '.html', p.as_posix()))
    for source in files:
        target = destination/source.relative_to(directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix='.publish-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as output, source.open('rb') as data:
                shutil.copyfileobj(data, output)
            os.chmod(pending, 0o644)
            os.replace(pending, target)
            if source.suffix in ('.js', '.css') and 'assets' in source.relative_to(directory).parts:
                compressed = gzip.compress(source.read_bytes(), compresslevel=6, mtime=0)
                with tempfile.NamedTemporaryFile(prefix='.publish-', dir=target.parent, delete=False) as stream:
                    compressed_pending = Path(stream.name)
                    stream.write(compressed)
                try:
                    compressed_pending.chmod(0o644)
                    os.replace(compressed_pending, target.with_name(target.name+'.gz'))
                finally:
                    compressed_pending.unlink(missing_ok=True)
        finally:
            Path(pending).unlink(missing_ok=True)
    validate_bundle(destination, public_url)
    return {'public_mount': site_mount(public_url), 'entry_assets_checked': len(assets),
            'published_files': len(files), 'old_hashed_assets_retained': True}


def build(public_url):
    env = dict(os.environ)
    env['PATH'] = str(ROOT/'runtime/bin') + os.pathsep + env['PATH']
    env['VITE_APP_BASE_PATH'] = site_mount(public_url)
    env['VITE_API_BASE_URL'] = site_mount(public_url).rstrip('/') + '/api/v1'
    for project in ('chat-ui', 'frontend'):
        if not (ROOT/project/'node_modules/.bin/tsc').exists():
            subprocess.run(['pnpm', '--dir', str(ROOT/project), 'install', '--frozen-lockfile', '--prefer-offline'],
                           env=env, check=True, timeout=600)
        subprocess.run(['pnpm', '--dir', str(ROOT/project), 'run', 'build'], env=env,
                       check=True, timeout=600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'check', 'publish'))
    args = parser.parse_args()
    config = json.loads((ROOT/'deployment/site.json').read_text())
    public_url = config['public_url']
    directory = ROOT/'backend/internal/web/dist'
    destination = ROOT/config['public_directory']
    lock = ROOT/'var/run/web-publish.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        if args.command == 'build':
            build(public_url)
        if args.command == 'check':
            result = {'entry_assets_checked': len(validate_bundle(directory, public_url))}
        else:
            result = publish(directory, destination, public_url)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
