#!/usr/bin/env python3
"""Fetch and verify public, pinned image assets without runtime credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile

from local_sources import ROOT, DISMECH, RDS_CA, clone_release, verify_release
from local_deployment import prepare_dismech


# Official global-bundle.pem reviewed with the existing deployment CA. A trust
# store rotation must be reviewed and this pin updated before builds proceed.
RDS_CA_SHA256 = 'e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3'


def run(*command):
    return subprocess.run([str(part) for part in command], check=True, capture_output=True, text=True, timeout=600)


def validate_ca(path):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != RDS_CA_SHA256:
        raise RuntimeError('RDS CA differs from the reviewed public certificate bundle')
    if b'PRIVATE KEY' in data:
        raise RuntimeError('RDS CA must contain public certificates only')
    ssl.create_default_context(cafile=str(path))


def prepare(output, *, root=ROOT, dapper=None, dismech=None, ca=None):
    """Build a fresh asset directory; supplied local sources undergo the same pins."""
    root, output = Path(root).resolve(), Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    try:
        lock_path = root / 'services/backend/agent-runtime/dapper-release.json'
        lock = json.loads(lock_path.read_text())
        if dapper is None:
            release = clone_release(output / 'dapper', lock_path)
        else:
            verify_release(dapper, lock_path)
            # Copy the pinned Git tree, never untracked files, local hooks or
            # credential-bearing Git configuration from a developer checkout.
            run('git', '-c', 'init.templateDir=', 'clone', '--quiet', '--no-local', '--no-hardlinks',
                '--single-branch', '--branch', lock['tag'], '--depth', '1', '--', Path(dapper).resolve(), output / 'dapper')
            run('git', '-C', output / 'dapper', 'remote', 'set-url', 'origin', lock['repository'])
            release = verify_release(output / 'dapper', lock_path)
        index = root / 'data/dismech-gaps/2026-09-24'
        commit = json.loads((index / 'manifest.json').read_text())['source_commit']
        with tempfile.TemporaryDirectory(prefix='reveal-dismech-') as temporary:
            source = Path(dismech).resolve() if dismech is not None else Path(temporary) / 'source'
            if dismech is None:
                run('git', 'init', '-q', source)
                run('git', '-C', source, 'remote', 'add', 'origin', DISMECH)
                run('git', '-C', source, 'sparse-checkout', 'init', '--cone')
                run('git', '-C', source, 'sparse-checkout', 'set', 'kb', 'src/dismech/schema')
                run('git', '-C', source, 'fetch', '--filter=blob:none', '--depth', '1', 'origin', commit)
                run('git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD')
            prepare_dismech(source, index, output / 'dismech')
        target = output / 'rds-ca.pem'
        if ca is None:
            run('curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                '--tlsv1.2', RDS_CA, '--output', target)
        else:
            shutil.copyfile(ca, target)
        validate_ca(target)
        return {'dapper': release, 'dismech_commit': commit, 'rds_ca_sha256': RDS_CA_SHA256}
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), sort_keys=True))


if __name__ == '__main__':
    main()
