"""Explicit local bootstrap, health, draining and non-scientific transport probes."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import time

from . import jobs
from .artifact_store import store, retained_file, checksum
from .repository import Repository, now, uid, digest
from .runtime_config import artifacts_root, setting


def local_only(repository):
    if (setting('REVEAL_LOCAL_DEPLOYMENT') != '1' or setting('REVEAL_ENVIRONMENT') not in ('development', 'test')
            or repository.table_prefix == 'reveal'):
        raise RuntimeError('Local verification requires an explicitly isolated application table prefix')


def bootstrap(repository):
    if setting('REVEAL_LOCAL_DEPLOYMENT') != '1' or setting('REVEAL_ENVIRONMENT') not in ('development', 'test'):
        raise RuntimeError('Bootstrap requires the local deployment environment')
    storage = store()
    if setting('REVEAL_S3_ENDPOINT_URL') or setting('REVEAL_S3_PUBLIC_ENDPOINT_URL') or storage.prefix != 'local/':
        raise RuntimeError('Local deployment requires real AWS S3 with the local/ prefix')
    # Provision buckets explicitly outside the application. Startup only checks
    # storage, so restarting local development cannot change AWS bucket policy.
    storage.check()
    blocked = storage.client.get_public_access_block(Bucket=storage.bucket)['PublicAccessBlockConfiguration']
    if not all(blocked.get(key) is True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')):
        raise RuntimeError('The artifact bucket must block all public access')
    if repository.table_prefix == 'reveal':
        # The primary local application adopts its existing users and publications.
        # Never replace that namespace with a fresh test workspace or silently
        # start S3 mode while its retained artifacts still require the old disk.
        with repository.read_transaction() as tx:
            legacy = tx.execute("SELECT COUNT(*) FROM reveal_records WHERE kind=%s "
                "AND JSON_EXTRACT(payload,'$.storage.store') IS NULL", ('artifact',)).fetchone()[0]
        if legacy:
            raise RuntimeError('Migrate existing retained artifacts with scripts/migrate_local_artifacts.py before starting S3 mode')
    else:
        local_only(repository)
        repository.migrate()
    return {'application_tables': repository.table_prefix, 'storage': 'ready', 'database_tls': repository.readiness()['tls']}


def snapshot(repository):
    cutoff = time.time() - 45
    with repository.read_transaction() as tx:
        runtimes = []
        for row in tx.list('runtime'):
            runtime_namespace = row['data'].get('namespace')
            if runtime_namespace is None and row['id'].startswith('dispatcher:'):
                runtime_namespace = row['id'].split(':', 1)[1]
            if (runtime_namespace or 'reveal') != jobs.namespace(): continue
            stamp = row['data'].get('heartbeat_at', '')
            if stamp and datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() >= cutoff:
                runtimes.append({'id': row['id'], **row['data']})
        queues=tx.get_many('queue',[row['id'] for row in tx.list('job') if row['data']['status'] not in jobs.TERMINAL])
        active = [{'id': row['id'], 'kind': row['data']['kind'], 'status': row['data']['status'],
                   'leased': (queues.get(row['id'],{}).get('data',{}).get('lease_until') or '') > now(),
                   'checkpointed':bool(queues.get(row['id'],{}).get('data',{}).get('workspace'))}
                  for row in tx.list('job') if row['data']['status'] not in jobs.TERMINAL
                  and queues.get(row['id'], {}).get('data', {}).get('namespace', 'reveal') == jobs.namespace()]
    return {'runtimes': runtimes, 'active_jobs': active}


def draining(repository, value):
    with repository.transaction() as tx:
        tx.put('worker_control', jobs.namespace(), 'system', {'draining': value, 'updated_at': now()})


def seed_probes(repository, count, delay):
    local_only(repository)
    if not 1 <= count <= 8 or not 1 <= delay <= 120: raise ValueError('Probe limits exceeded')
    from .app import fresh_principal
    me = fresh_principal('anonymous')
    owner, identities = me['user_id'], []
    with repository.transaction() as tx:
        tx.put('principal', owner, owner, {'me': me, 'retired': False, 'created_at': now()})
        for _ in range(count):
            job = jobs.enqueue(tx, owner, 'deployment_probe', inputs={'delay': delay, 'nonce': uid()})
            identities.append(job['id'])
    return {'owner': owner, 'job_ids': identities, 'scientific_execution': False}


async def run_probe(worker, job, queue):
    """Exercise the real delivery/lease/storage path without a scientific claim."""
    async def renew():
        while True:
            await asyncio.sleep(min(20, jobs.lease_duration() / 3))
            if not await asyncio.to_thread(jobs.heartbeat, worker.repository, job['id'], queue['token']): return
    # Real AWS transfers can outlast a short local lease. Cover restore, upload,
    # and final artifact persistence, just as the research worker's lease loop does.
    renewal = asyncio.create_task(renew())
    try:
        return await _run_probe(worker, job, queue)
    finally:
        renewal.cancel()
        try: await renewal
        except asyncio.CancelledError: pass


async def _run_probe(worker, job, queue):
    local_only(worker.repository)
    root = artifacts_root() / job['id']
    root.mkdir(parents=True, exist_ok=True)
    recovered = bool(queue.get('workspace'))
    if recovered:
        await asyncio.to_thread(store().restore, queue['workspace'], root)
    path = root / 'probe-input.json'
    if recovered and not path.exists(): raise RuntimeError('Recovery did not restore the checkpoint file')
    if not path.exists():
        path.write_text(json.dumps({'job_id': job['id'], 'nonce': queue['inputs']['nonce'],
                                    'recovery_only_nonce': uid(), 'payload': 'x' * (5 * 1024 * 1024),
                                    'label': 'Infrastructure probe; not scientific output'}, sort_keys=True))
    frozen = checksum(path.read_bytes())
    await asyncio.to_thread(worker.save_workspace, job, queue['token'], root)
    deadline = time.monotonic() + min(120, max(1, int(queue['inputs']['delay'])))
    while time.monotonic() < deadline:
        if worker.stopping: return
        with worker.repository.read_transaction() as tx:
            state = tx.get('job', job['id'])['data']['status']
        if state == 'cancel_requested':
            jobs.finish(worker.repository, job['id'], queue['token'], 'cancelled'); return
        await asyncio.sleep(min(1, max(0, deadline-time.monotonic())))
    if checksum(path.read_bytes()) != frozen: raise RuntimeError('Probe input changed')
    artifact = await asyncio.to_thread(retained_file, path, frozen)
    with worker.repository.transaction() as tx:
        pair = jobs.fenced(tx, job['id'], queue['token'])
        if not pair or pair[0]['status'] == 'cancel_requested': return
        current, _ = pair
        tx.put('artifact', digest([job['owner_user_id'], frozen]), job['owner_user_id'], {
            'sha256': frozen, 'file': {'filename': 'deployment-probe.json', 'mime_type': 'application/json'},
            'job_id': job['id'], **artifact})
        tx.put('probe_result', job['id'], job['owner_user_id'], {
            'sha256': frozen, 'worker_id': worker.worker_id, 'recovered_from_s3': recovered})
        current.update(status='succeeded', stage='complete', completed_at=now(),
                       result={'kind': 'deployment_probe', 'sha256': frozen}, failure=None)
        jobs.event(tx, current, 'result', 'Infrastructure probe complete; no scientific execution was performed.')


def verify_probes(repository, ids):
    local_only(repository)
    with repository.read_transaction() as tx:
        rows = [tx.get('job', identity) for identity in ids]
        if any(not row or row['data']['kind'] != 'deployment_probe' for row in rows): raise ValueError('Invalid probe IDs')
        if any(row['data']['status'] != 'succeeded' for row in rows):
            return {'complete': False, 'statuses': [row['data']['status'] for row in rows]}
        results = [tx.get('probe_result', identity)['data'] for identity in ids]
        artifacts = [tx.get('artifact', digest([row['owner'], result['sha256']]))['data'] for row, result in zip(rows, results)]
    for artifact in artifacts:
        raw = store().get(artifact['storage'])
        if checksum(raw) != artifact['sha256']: raise RuntimeError('Restored artifact differs')
    return {'complete': True, 'jobs': len(ids), 'workers': sorted({item['worker_id'] for item in results}),
            'recovered_from_s3': sum(bool(item.get('recovered_from_s3')) for item in results),
            'verified_s3_objects': len(artifacts), 'sha256':[artifact['sha256'] for artifact in artifacts], 'scientific_execution': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('bootstrap').add_argument('--apply', required=True, action='store_true')
    commands.add_parser('status')
    health = commands.add_parser('health'); health.add_argument('service', choices=['worker', 'dispatcher'])
    drain = commands.add_parser('drain'); drain.add_argument('--wait', type=int, default=0)
    commands.add_parser('resume')
    seed = commands.add_parser('seed-probes'); seed.add_argument('--count', type=int, default=2); seed.add_argument('--delay', type=int, default=10)
    verify = commands.add_parser('verify-probes'); verify.add_argument('ids', nargs='+')
    args = parser.parse_args(); repository = Repository()
    if args.action == 'bootstrap': result = bootstrap(repository)
    elif args.action == 'status': result = snapshot(repository)
    elif args.action == 'seed-probes': result = seed_probes(repository, args.count, args.delay)
    elif args.action == 'verify-probes': result = verify_probes(repository, args.ids)
    elif args.action == 'health':
        records = snapshot(repository)['runtimes']
        valid = any(row['id'] == 'dispatcher:' + jobs.namespace() if args.service == 'dispatcher'
                    else row['id'].startswith(socket.gethostname() + ':') for row in records)
        if not valid: raise SystemExit(1)
        result = {'status': 'ok'}
    elif args.action == 'resume': draining(repository, False); result = {'draining': False}
    else:
        draining(repository, True)
        deadline = time.monotonic() + args.wait
        while args.wait:
            status = snapshot(repository)
            workers = [row for row in status['runtimes'] if not row['id'].startswith('dispatcher:')]
            if (workers and all(row['draining'] and not row.get('job_id') for row in workers)
                    and not any(row['leased'] for row in status['active_jobs'])): break
            if time.monotonic() >= deadline: raise SystemExit('Workers are still draining; no containers were stopped')
            time.sleep(2)
        result = {'draining': True}
    print(json.dumps(result))


if __name__ == '__main__': main()
