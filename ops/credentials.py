"""Read only the requested account credentials from the local database."""
import json
import os
import subprocess

from ops.admin_api import ROOT, credentials


def accounts(account_ids):
    if not account_ids or any(type(ident) is not int or ident <= 0 for ident in account_ids):
        raise ValueError('Account IDs must be positive integers')
    settings = credentials()
    environment = dict(os.environ, PGPASSWORD=settings['POSTGRES_PASSWORD'])
    sql = ("SELECT json_build_object('id',id,'credentials',credentials) FROM accounts WHERE id IN ("
           + ','.join(str(ident) for ident in account_ids)+") AND deleted_at IS NULL")
    result = subprocess.run([str(ROOT/'runtime/bin/psql'), '-h', '127.0.0.1', '-p', '55432',
        '-U', 'sub2api', '-d', 'sub2api', '-At', '-c', sql], env=environment,
        text=True, capture_output=True, check=True)
    return {row['id']: row['credentials'] for row in map(json.loads, result.stdout.splitlines())}
