"""Publish verified encrypted project backups using the application's existing S3 destination."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import subprocess

from ops.admin_api import ROOT, read_env

RECOVERY_CONFIG = ROOT/'deployment/private/config/backup-store.json'


def system_config():
    # Use server-side operator access, not a second set of cloud credentials.
    values = read_env(ROOT/'deployment/private/config/runtime.env')
    result = subprocess.run([
        str(ROOT/'runtime/bin/psql'), '-X', '-qAt', '-h', values['DATABASE_HOST'],
        '-p', values['DATABASE_PORT'], '-U', values['DATABASE_USER'], '-d', values['DATABASE_DBNAME'],
        '-c', "SELECT value FROM settings WHERE key='backup_s3_config'"],
        env=dict(os.environ, PGPASSWORD=values['DATABASE_PASSWORD']),
        check=True, capture_output=True, text=True)
    config = json.loads(result.stdout)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        sealed = base64.b64decode(config['secret_access_key'], validate=True)
        key = bytes.fromhex(values['TOTP_ENCRYPTION_KEY'])
        config['secret_access_key'] = AESGCM(key).decrypt(sealed[:12], sealed[12:], None).decode()
    except Exception:
        raise RuntimeError('Cannot decrypt system backup storage credentials with the configured key') from None
    if not config.get('bucket') or not config.get('access_key_id') or not config['secret_access_key']:
        raise RuntimeError('System backup storage is incomplete')
    # An encrypted full-project archive must also carry the credentials needed after DB restore.
    RECOVERY_CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = RECOVERY_CONFIG.with_suffix('.pending')
    fd = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as target:
        json.dump(config, target, indent=2)
        target.write('\n')
    os.replace(pending, RECOVERY_CONFIG)
    return config


def client(config):
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=config['endpoint'],
        region_name=config.get('region') or 'auto', aws_access_key_id=config['access_key_id'],
        aws_secret_access_key=config['secret_access_key'],
        config=Config(connect_timeout=10, read_timeout=180,
            retries={'max_attempts': 3, 'mode': 'standard'},
            s3={'addressing_style': 'path' if config.get('force_path_style') else 'virtual'},
            request_checksum_calculation='when_required', response_checksum_validation='when_required'))


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_object(store, bucket, key, expected_sha256, expected_size):
    head = store.head_object(Bucket=bucket, Key=key)
    if head['ContentLength'] != expected_size:
        raise RuntimeError('Cloud object size mismatch')
    body = store.get_object(Bucket=bucket, Key=key)['Body']
    digest, size = hashlib.sha256(), 0
    try:
        for block in iter(lambda: body.read(1024*1024), b''):
            size += len(block)
            digest.update(block)
    finally:
        body.close()
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise RuntimeError('Cloud download checksum mismatch')


def publish(path, category='current'):
    from boto3.s3.transfer import TransferConfig
    os.umask(0o077)
    path = Path(path).resolve()
    if path.suffix != '.age':
        raise ValueError('Only age-encrypted archives may be published')
    proof = json.loads(path.with_suffix(path.suffix+'.verified.json').read_text())
    expected = path.with_suffix(path.suffix+'.sha256').read_text().split()[0]
    if not proof.get('verified') or file_digest(path) != expected:
        raise RuntimeError('Publish requires a verified archive and matching checksum')
    config = system_config()
    store = client(config)
    # Keep full project archives separate from the application's database retention jobs.
    key = posixpath.join(config.get('prefix', '').strip('/'), 'project-snapshots', category, path.name)
    print('Uploading encrypted archive to configured system storage', flush=True)
    store.upload_file(str(path), config['bucket'], key,
        ExtraArgs={'ContentType': 'application/octet-stream', 'Metadata': {'sha256': expected}},
        Config=TransferConfig(multipart_threshold=16*1024*1024,
                              multipart_chunksize=16*1024*1024, max_concurrency=2))
    print('Verifying cloud object by downloading and hashing every byte', flush=True)
    verify_object(store, config['bucket'], key, expected, path.stat().st_size)
    sidecar = {'verified': True, 'archive': path.name, 'sha256': expected,
        'bytes': path.stat().st_size, 'git_commit': proof.get('git_commit'),
        'postgres_restore_passed': bool(proof.get('postgres_restore')),
        'cloud_download_sha256_verified': True}
    for suffix, body in [('.sha256', (expected+'  '+path.name+'\n').encode()),
                         ('.verification.json', (json.dumps(sidecar, indent=2)+'\n').encode())]:
        store.put_object(Bucket=config['bucket'], Key=key+suffix, Body=body,
                         ContentType='application/json' if suffix.endswith('.json') else 'text/plain')
    receipt = dict(sidecar, category=category, bucket=config['bucket'], key=key, endpoint=config['endpoint'],
                   verified_at=datetime.now(timezone.utc).isoformat())
    path.with_suffix(path.suffix+'.cloud.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({'uploaded': True, 'cloud_download_verified': True,
                      'archive': path.name, 'bytes': path.stat().st_size}), flush=True)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--category', choices=('current', 'retired'), default='current')
    args = parser.parse_args()
    publish(args.archive, args.category)


if __name__ == '__main__':
    main()
