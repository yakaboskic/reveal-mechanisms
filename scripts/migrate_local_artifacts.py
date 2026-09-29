#!/usr/bin/env python3
"""Move the original local application's retained files to S3, preserving records.

Dry-run by default. Stop the original dev stack first. Originals remain on disk;
an S3 rollback snapshot precedes one fenced, version-checked database commit.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from dotenv import dotenv_values
from reveal_backend.artifact_store import S3Store, checksum
from reveal_backend.repository import Repository, canonical, now
from reveal_backend.jobs import TERMINAL


def local_path(value, root):
    path = Path(value)
    container_root = Path('/app/.runtime/artifacts')
    if path.is_relative_to(container_root): path = root / path.relative_to(container_root)
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError(f'Retained artifact is missing or outside the source directory: {path}')
    return path


def references(value):
    if isinstance(value, dict):
        if value.get('path') and value.get('sha256') and isinstance(value.get('file'), dict):
            yield value
        else:
            for child in value.values(): yield from references(child)
    elif isinstance(value, list):
        for child in value: yield from references(child)


def verify_idle(tx):
    jobs = {identity: json.loads(payload) for identity, payload in
            tx.execute('SELECT id,payload FROM reveal_records WHERE kind=%s', ('job',)).fetchall()}
    if any(job.get('status') not in TERMINAL for job in jobs.values()):
        raise RuntimeError('All original application jobs must be terminal before migration')
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    runtime = ROOT / '.runtime/deployment'
    config = dict(line.split('=', 1) for line in (runtime / 'backend.env').read_text().splitlines()
                  if line and not line.startswith('#'))
    root_env = dotenv_values(ROOT / '.env')
    config['REVEAL_MYSQL_CA_FILE'] = str((ROOT / root_env['REVEAL_MYSQL_CA_FILE']).resolve())
    os.environ.update(config)
    if config['REVEAL_S3_PREFIX'] != 'local/' or config.get('REVEAL_S3_ENDPOINT_URL'):
        raise RuntimeError('Migration requires the real local AWS S3 configuration')
    source = (ROOT / '.runtime/artifacts').resolve()
    repo = Repository(table_prefix='reveal')
    storage = S3Store(config['REVEAL_S3_BUCKET'], config['REVEAL_S3_PREFIX'])
    with repo.read_transaction() as tx:
        jobs = verify_idle(tx)
        rows = [{'kind': kind, 'id': identity, 'owner': owner, 'version': version,
                 'data': json.loads(payload), 'updated_at': updated_at}
                for kind, identity, owner, version, payload, updated_at in tx.execute(
                    'SELECT kind,id,owner_id,version,payload,updated_at FROM reveal_records '
                    'WHERE kind NOT IN (%s,%s)', ('event', 'remote_event')).fetchall()]

    files, workspaces, captures = {}, {}, {}
    def include(path, expected=None):
        if path.is_symlink(): raise RuntimeError('Retained files must not be symbolic links')
        data = path.read_bytes()
        sha = checksum(data)
        if expected and sha != expected: raise RuntimeError(f'Artifact checksum mismatch: {path}')
        files.setdefault(sha, path)
        return sha

    for row in rows:
        for ref in references(row['data']):
            include(local_path(ref['path'], source), ref['sha256'])
    queues = {row['id']: row for row in rows if row['kind'] == 'queue'}
    for identity, row in queues.items():
        if row['data'].get('workspace'): continue
        root = source / identity
        if not root.is_dir():
            if row['data'].get('dispatch_input'): raise RuntimeError(f'Missing dispatched job workspace: {identity}')
            continue
        entries = []
        total = 0
        for path in sorted(root.rglob('*')):
            if path.is_symlink(): raise RuntimeError('Workspace contains a symbolic link')
            if not path.is_file(): continue
            total += path.stat().st_size
            if total > 256 * 1024 * 1024 or len(entries) >= 10000:
                raise RuntimeError('Workspace exceeds restoration limits')
            entries.append({'path': path.relative_to(root).as_posix(), 'sha256': include(path)})
        if entries: workspaces[identity] = entries
        dispatch = row['data'].get('dispatch_input')
        if dispatch and checksum((root / dispatch['path']).read_bytes()) != dispatch['sha256']:
            raise RuntimeError(f'Frozen dispatch changed: {identity}')
        for marker_path in sorted(root.glob('attempt-*/output/.box-capture-complete.json')):
            marker = json.loads(marker_path.read_bytes())
            if marker.get('cleanup_complete') and marker.get('state', {}).get('status') == 'succeeded':
                binding = marker['binding']
                if binding['job_id'] != identity: raise RuntimeError('Capture job identity mismatch')
                if identity not in captures or binding['attempt'] > captures[identity]['attempt']:
                    captures[identity] = {'attempt': binding['attempt'], 'box_id': binding['box_id'],
                                          'capture_sha256': checksum(marker_path.read_bytes())}

    report = {'apply': args.apply, 'table_prefix': 'reveal', 'jobs': len(jobs),
              'artifact_records': sum(row['kind'] == 'artifact' for row in rows),
              'publication_snapshots': sum(row['kind'] == 'publication_snapshot' for row in rows),
              'workspace_count': len(workspaces), 'unique_files': len(files),
              'unique_bytes': sum(path.stat().st_size for path in files.values())}
    print(json.dumps(report), flush=True)
    if not args.apply: return
    storage.check()
    mapped = {}
    def upload(item):
        sha, path = item
        data = path.read_bytes()
        if checksum(data) != sha: raise RuntimeError('Source changed during upload')
        reference = storage.put(data)
        if storage.get(reference) != data: raise RuntimeError('S3 read-back verification failed')
        return sha, reference
    with ThreadPoolExecutor(max_workers=12) as pool:
        pending = [pool.submit(upload, item) for item in files.items()]
        for future in as_completed(pending):
            sha, ref = future.result(); mapped[sha] = ref
            if len(mapped) % 100 == 0: print(f'Verified {len(mapped)}/{len(files)} S3 objects', flush=True)

    manifests = {}
    for identity, entries in workspaces.items():
        manifest = {'format': 'reveal.workspace/1', 'files': [
            {'path': item['path'], 'storage': mapped[item['sha256']]} for item in entries]}
        data = canonical(manifest).encode()
        ref = storage.put(data, 'application/json')
        if storage.get(ref) != data: raise RuntimeError('Workspace read-back verification failed')
        manifests[identity] = ref

    changes = []
    for row in rows:
        data = deepcopy(row['data'])
        for ref in references(data):
            ref['storage'] = mapped[ref['sha256']]
            del ref['path']
        if row['kind'] == 'queue' and row['id'] in manifests:
            data['workspace'] = manifests[row['id']]
            if row['id'] in captures: data['review_capture'] = captures[row['id']]
        if data != row['data']: changes.append((row, data))
    backup = canonical({'format': 'reveal.artifact-migration-backup/1', 'created_at': now(),
                        'table_prefix': 'reveal', 'rows': [row for row, _ in changes]}).encode()
    backup_ref = storage.put(backup, 'application/json')
    if storage.get(backup_ref) != backup: raise RuntimeError('Rollback snapshot verification failed')
    report.update(rollback_snapshot=backup_ref, updated_rows=len(changes))
    report_path = runtime / 'original-data-migration.json'
    report_path.write_text(json.dumps(report, indent=2)+'\n'); report_path.chmod(0o600)
    with repo.transaction() as tx:
        verify_idle(tx)
        for original, data in changes:
            tx.put(original['kind'], original['id'], original['owner'], data, expected=original['version'])
    report.update(committed=True, completed_at=now(), source_files_preserved=True)
    report_path.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'committed': True, 'updated_rows': len(changes),
                      'source_files_preserved': True, 'rollback_snapshot_verified': True}), flush=True)


if __name__ == '__main__': main()
