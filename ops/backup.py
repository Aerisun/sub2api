"""Encrypted online snapshots and isolated PostgreSQL/SQLite restore verification."""
import argparse
from io import BytesIO
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

from ops.admin_api import ROOT, read_env

IDENTITY = ROOT/'secrets/backup/identity.agekey'


def load_layout():
    layout = json.loads((ROOT/'deployment/backup-layout.json').read_text())
    if layout.get('version') != 1:
        raise RuntimeError('Unsupported backup layout version')
    return layout


def sqlite_snapshot(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as connection, sqlite3.connect(target) as dest:
        connection.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite snapshot failed integrity check')


def add_external(archive, path, name, stage, databases):
    """Capture explicitly registered sibling components without copying live SQLite journals."""
    if path.name in ('__pycache__', '.git', '.venv') or path.name.endswith(('-wal', '-shm', '-journal', '.lock', '.pyc')):
        return
    if path.is_symlink():
        raise RuntimeError('External snapshot paths must not be symlinks')
    if path.is_dir():
        for child in sorted(path.iterdir()):
            add_external(archive, child, name+'/'+child.name, stage, databases)
    elif path.name.endswith('.sqlite3'):
        target = stage/'external-sqlite'/name
        sqlite_snapshot(path, target)
        databases.append((target, name))
    else:
        # Atomic read gives the tar member its actual length even during session refreshes.
        data = path.read_bytes()
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(data), 0o600
        archive.addfile(info, BytesIO(data))


def run(command, **kwargs):
    return subprocess.run([str(part) for part in command], check=True, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def private_inventory():
    destination = ROOT/'deployment/private/INVENTORY.json'
    files = []
    for base in (ROOT/'deployment/private', ROOT/'secrets'):
        for path in sorted(base.rglob('*')):
            if not path.is_file() or path == destination or '__pycache__' in path.parts:
                continue
            files.append({'path': str(path.relative_to(ROOT)), 'sha256': digest(path),
                'mode': oct(path.stat().st_mode & 0o777),
                'backup': 'separate recovery copy' if path.resolve() == IDENTITY.resolve()
                          else 'encrypted project snapshot'})
    destination.write_text(json.dumps({'version': 1, 'contains_values': False, 'files': files}, indent=2)+'\n')
    destination.chmod(0o600)


def identity():
    IDENTITY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not IDENTITY.exists():
        if any((ROOT/'backups').glob('*.age')):
            raise RuntimeError('Restore the saved backup identity before creating further snapshots')
        run([ROOT/'bin/age-keygen', '-o', IDENTITY], capture_output=True)
        IDENTITY.chmod(0o600)
    result = run([ROOT/'bin/age-keygen', '-y', IDENTITY], capture_output=True, text=True)
    return result.stdout.strip()


def database_env():
    values = read_env(ROOT/'deployment/private/config/runtime.env')
    return values, dict(os.environ, PGPASSWORD=values['DATABASE_PASSWORD'])


def pg_dump(destination):
    values, environment = database_env()
    run([ROOT/'runtime/bin/pg_dump', '-h', values['DATABASE_HOST'], '-p', values['DATABASE_PORT'],
         '-U', values['DATABASE_USER'], '-d', values['DATABASE_DBNAME'], '-Fc',
         '--no-owner', '--no-privileges', '-f', destination], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def skip(path):
    relative = path.relative_to(ROOT)
    parts = relative.parts
    if any(part in ('__pycache__', 'node_modules', '.gocache', '.pytest_cache') for part in parts):
        return True
    if parts[:2] == ('runtime', 'frontend-node_modules'):
        return True  # Reinstall from the pinned pnpm lockfile when building.
    if parts[0] == 'data':
        if len(parts) > 1 and parts[1] in ('task-temp-20261020', 'nginx-client-body', 'logs',
                'tool-concurrency', 'egress-control-locks', 'image-io'):
            return True
        if path.suffix == '.log' or '.sqlite3' in path.name:
            return True
    # The decryption key must have a separate recovery copy, never accompany its archive.
    return path.resolve() == IDENTITY.resolve() or path.is_socket()


def add_tree(archive, path):
    if skip(path):
        return
    archive.add(path, arcname='project/'+str(path.relative_to(ROOT)), recursive=False)
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.iterdir()):
            add_tree(archive, child)


def snapshot(output=None):
    os.umask(0o077)
    from ops.backup_store import system_config
    system_config()  # Export the existing encrypted-in-DB storage credentials into private recovery config.
    private_inventory()
    layout = load_layout()
    for name in layout['required_files']:
        if not (ROOT/name).is_file():
            raise RuntimeError('Required project component is missing: '+name)
    recipient = identity()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = Path(output or ROOT/'backups'/f'sub2api-{stamp}.tar.gz.age').resolve()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('Backup already exists')
    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=ROOT/'backups') as directory:
        stage = Path(directory)
        database = stage/'database.dump'
        print('Creating consistent PostgreSQL snapshot', flush=True)
        pg_dump(database)
        redis = stage/'redis.rdb'
        values, environment = database_env()
        run([ROOT/'bin/redis-cli', '-h', values['REDIS_HOST'], '-p', values['REDIS_PORT'],
             '--rdb', redis], env=dict(environment, REDISCLI_AUTH=values.get('REDIS_PASSWORD', '')),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        databases = []
        for path in (ROOT/'data').rglob('*.sqlite3'):
            target = stage/'sqlite'/path.relative_to(ROOT/'data')
            sqlite_snapshot(path, target)
            databases.append((target, 'project/data/'+str(path.relative_to(ROOT/'data'))))
        commit = run(['git', '-C', ROOT, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        manifest = {'format': 1, 'created_at': stamp, 'installed_root': str(ROOT), 'git_commit': commit,
            'database_sha256': digest(database), 'redis_sha256': digest(redis),
            'sqlite': {name: digest(path) for path, name in databases},
            'encryption': 'age X25519; identity stored separately',
            'restore': 'python3 -m ops.backup verify ARCHIVE --restore-database',
            'backup_layout_version': layout['version'],
            'required_members': ['project/'+name for name in layout['required_files']],
            'external_components': [c['name'] for c in layout.get('external_components', [])]}
        plain = stage/'snapshot.tar.gz'
        print('Packing current source, private configuration, runtime and state', flush=True)
        with tarfile.open(plain, 'w:gz', compresslevel=3) as archive:
            # Use the repository file inventory; ignored production files are added explicitly below.
            inventory = run(['git', '-C', ROOT, 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            capture_output=True).stdout.split(b'\0')
            source_members = []
            for name in sorted(set(name.decode() for name in inventory if name)):
                path = ROOT/name
                if path.is_file() or path.is_symlink():
                    archive.add(path, arcname='project/'+name, recursive=False)
                    source_members.append('project/'+name)
            manifest['source_members'] = source_members
            for name in layout['project_trees']:
                path = ROOT/name
                if path.exists():
                    add_tree(archive, path)
            for name in layout['runtime_files']:
                add_tree(archive, ROOT/'bin'/name)
            for component in layout.get('external_components', []):
                directory = (ROOT/component['path']).resolve()
                if directory.parent != ROOT.parent or directory == ROOT:
                    raise RuntimeError('External component must be an explicitly registered sibling directory')
                for name in component['include']:
                    add_external(archive, directory/name, 'external/'+component['name']+'/'+name, stage, databases)
            for entry in layout.get('host_files', []):
                path = Path(entry['path']).expanduser()
                data = path.read_bytes()
                info = tarfile.TarInfo(entry['archive'])
                info.size, info.mode = len(data), 0o600
                archive.addfile(info, BytesIO(data))
                manifest['required_members'].append(entry['archive'])
            for path, name in databases:
                archive.add(path, arcname=name)
            manifest['sqlite'] = {name: digest(path) for path, name in databases}
            (stage/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
            archive.add(database, arcname='database.dump')
            archive.add(redis, arcname='redis.rdb')
            archive.add(stage/'manifest.json', arcname='manifest.json')
        partial = output.with_suffix(output.suffix+'.pending')
        try:
            run([ROOT/'bin/age', '-r', recipient, '-o', partial, plain], capture_output=True)
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
    sums = output.with_suffix(output.suffix+'.sha256')
    sums.write_text(digest(output)+'  '+output.name+'\n')
    print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size,
                      'key_file': str(IDENTITY), 'sha256': digest(output)}), flush=True)
    return output


def restore_database(dump, directory):
    cluster, socket = directory/'pgdata', directory/'socket'
    socket.mkdir(mode=0o700)
    # Unix socket in a private directory, TCP disabled, no production connections.
    run([ROOT/'runtime/bin/initdb', '-D', cluster, '-U', 'sub2api', '--auth=trust', '--no-locale'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    run([ROOT/'runtime/bin/pg_ctl', '-D', cluster, '-l', directory/'postgres.log', '-w', 'start',
         '-o', f"-h '' -p 55439 -k {socket}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        run([ROOT/'runtime/bin/createdb', '-h', socket, '-p', '55439', '-U', 'sub2api', 'restored'],
            capture_output=True)
        run([ROOT/'runtime/bin/pg_restore', '-h', socket, '-p', '55439', '-U', 'sub2api',
             '-d', 'restored', '--no-owner', '--no-privileges', '--exit-on-error', dump], capture_output=True)
        result = run([ROOT/'runtime/bin/psql', '-h', socket, '-p', '55439', '-U', 'sub2api',
            '-d', 'restored', '-Atc', "SELECT json_build_object('accounts',(SELECT count(*) FROM accounts),"
            "'users',(SELECT count(*) FROM users),'usage_logs',(SELECT count(*) FROM usage_logs))"],
            capture_output=True, text=True)
        return json.loads(result.stdout)
    finally:
        run([ROOT/'runtime/bin/pg_ctl', '-D', cluster, '-m', 'immediate', '-w', 'stop'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def verify(path, restore=False):
    os.umask(0o077)
    path = Path(path).resolve()
    expected = path.with_suffix(path.suffix+'.sha256').read_text().split()[0]
    if digest(path) != expected:
        raise RuntimeError('Encrypted archive checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=ROOT/'backups') as directory:
        stage = Path(directory);plain = stage/'snapshot.tar.gz'
        run([ROOT/'bin/age', '-d', '-i', IDENTITY, '-o', plain, path], capture_output=True)
        with tarfile.open(plain, 'r:gz') as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            names = archive.getnames()
            missing = set(manifest.get('required_members', [])+manifest.get('source_members', []))-set(names)
            if missing:
                raise RuntimeError('Backup is missing required source or configuration components')
            if 'project/secrets/backup/identity.agekey' in names:
                raise RuntimeError('Recovery identity must not be uploaded with the archive')
            for name in ('database.dump', 'redis.rdb'):
                dest = stage/name
                with archive.extractfile(name) as source, dest.open('wb') as target:
                    shutil.copyfileobj(source, target)
                if digest(dest) != manifest[name.split('.')[0]+'_sha256']:
                    raise RuntimeError('Snapshot component checksum mismatch')
            for index, (name, expected) in enumerate(manifest['sqlite'].items()):
                dest = stage/f'sqlite-{index}.db'
                with archive.extractfile(name) as source, dest.open('wb') as target:
                    shutil.copyfileobj(source, target)
                if digest(dest) != expected:
                    raise RuntimeError('SQLite component checksum mismatch')
                with sqlite3.connect(dest) as connection:
                    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Restored SQLite is corrupt')
        run([ROOT/'runtime/bin/pg_restore', '--list', stage/'database.dump'], capture_output=True)
        result = {'verified': True, 'archive': path.name, 'entries': len(names),
                  'sqlite_databases': len(manifest['sqlite']), 'git_commit': manifest['git_commit'],
                  'source_coverage_verified': True,
                  'external_components': manifest.get('external_components', [])}
        if restore:
            result['postgres_restore'] = restore_database(stage/'database.dump', stage)
    report = path.with_suffix(path.suffix+'.verified.json')
    report.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create');create.add_argument('--output', type=Path)
    create.add_argument('--publish', action='store_true', help='Verify a real DB restore, then publish to system S3')
    check = sub.add_parser('verify');check.add_argument('archive', type=Path)
    check.add_argument('--restore-database', action='store_true')
    args = parser.parse_args()
    if args.command == 'create':
        path = snapshot(args.output)
        if args.publish:
            verify(path, restore=True)
            from ops.backup_store import publish
            publish(path)
    else:
        verify(args.archive, args.restore_database)


if __name__ == '__main__':
    main()
