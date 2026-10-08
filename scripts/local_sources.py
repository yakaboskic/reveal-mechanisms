#!/usr/bin/env python3
"""Fetch the exact runtime source revisions; never import or migrate RDS data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values
from local_setup import ROOT, RDS_CA, DISMECH, run

sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.dapper_release import clone_release, ensure_release, verified_release, verify_release
from reveal_backend.evidence_package import EvidenceBuildError

MANAGED_DAPPER = ROOT / '.runtime/dapper'
ASSET_DAPPER = ROOT / '.deployment-assets/dapper'


def configured(env, key, default):
    path = Path(env.get(key) or default).expanduser()
    return path if path.is_absolute() else ROOT / path


def provision_dapper(dapper, lock):
    """Clone a missing checkout and upgrade a stale managed one; a custom REVEAL_DAPPER_ROOT must already verify."""
    if dapper.exists() and dapper.resolve() != MANAGED_DAPPER.resolve():
        try: return verify_release(dapper, lock)
        except EvidenceBuildError as error:
            raise RuntimeError(f'REVEAL_DAPPER_ROOT={dapper} is not the locked DAPPER release ({error}). Unset it to use '
                               f'the managed {MANAGED_DAPPER}, which is upgraded automatically, or point it at a '
                               'checkout that verifies; see docs/local-development.md.') from None
    release = verified_release(dapper, lock)
    if release is not None: return release
    # The verified deployment asset avoids a network clone; only its pinned Git tree is copied.
    source = ASSET_DAPPER if verified_release(ASSET_DAPPER, lock) else None
    print(('Upgrading stale' if dapper.exists() else 'Fetching') + ' pinned DAPPER release...', flush=True)
    release = ensure_release(dapper, lock, source)
    if 'previous' in release:
        print(f"Previous checkout kept at {release['previous']}; restart running services to mount the new one.", flush=True)
    return release


def main():
    env = dotenv_values(ROOT / '.env', interpolate=False)
    provision_dapper(configured(env, 'REVEAL_DAPPER_ROOT', '.runtime/dapper'),
                     ROOT / 'services/backend/agent-runtime/dapper-release.json')
    dismech = configured(env, 'REVEAL_DISMECH_SOURCE', '.runtime/dismech')
    index = ROOT / 'data/dismech-gaps/2026-09-24'
    commit = json.loads((index / 'manifest.json').read_text())['source_commit']
    if not dismech.exists():
        print('Fetching pinned DisMech sources...', flush=True)
        dismech.parent.mkdir(parents=True, exist_ok=True)
        # A failed download leaves the final path absent and can be retried.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='dismech-', dir=dismech.parent) as temporary:
            checkout = Path(temporary) / 'source'
            run('git', 'init', '-q', checkout)
            run('git', '-C', checkout, 'remote', 'add', 'origin', DISMECH)
            # The upstream repository also contains large unrelated assets.
            # Fetch only the pinned history and materialize the frozen KB/schema.
            run('git', '-C', checkout, 'sparse-checkout', 'init', '--cone')
            run('git', '-C', checkout, 'sparse-checkout', 'set', 'kb', 'src/dismech/schema')
            run('git', '-C', checkout, 'fetch', '--filter=blob:none', '--depth', '1', 'origin', commit, timeout=600)
            run('git', '-C', checkout, 'checkout', '--detach', 'FETCH_HEAD', timeout=600)
            checkout.rename(dismech)
    for item in json.loads((index / 'source-files.json').read_text()):
        path = (dismech / item['path']).resolve()
        if not path.is_relative_to(dismech.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('DisMech source does not match the frozen manifest; existing files were preserved')
    subprocess.run(['git', '-C', str(dismech), 'cat-file', '-e', f'{commit}:src/dismech/schema/dismech.yaml'], check=True)
    ca = configured(env, 'REVEAL_MYSQL_CA_FILE', '.runtime/certs/rds-ca.pem')
    if not ca.exists():
        ca.parent.mkdir(parents=True, exist_ok=True)
        temporary = ca.with_suffix('.download')
        try:
            run('curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--tlsv1.2',
                RDS_CA, '--output', temporary)
            if b'-----BEGIN CERTIFICATE-----' not in temporary.read_bytes(): raise RuntimeError('Invalid RDS CA download')
            temporary.replace(ca)
        finally: temporary.unlink(missing_ok=True)
    print('Pinned source bytes verified; RDS CA available. No database changes made.', flush=True)


if __name__ == '__main__': main()
