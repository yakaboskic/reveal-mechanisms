"""Deployment boundaries: immutable bytes, queue recovery and fenced ownership."""
import base64
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace

from botocore.exceptions import ClientError
import pytest

from reveal_backend import jobs
from reveal_backend.artifact_store import S3Store, StorageUnavailable, checksum
from reveal_backend.deployment import draining, local_only
from reveal_backend.dispatcher import reconcile
from reveal_backend.repository import Repository


class MemoryS3:
    exceptions = SimpleNamespace(ClientError=ClientError)

    def __init__(self):
        self.objects = {}
        self.latest = {}
        self.versioning = True

    def head_bucket(self, **kwargs): pass
    def get_bucket_versioning(self, **kwargs): return {'Status': 'Enabled' if self.versioning else 'Suspended'}

    def head_object(self, Bucket, Key, VersionId=None, **kwargs):
        version = VersionId or self.latest.get(Key)
        if (Key, version) not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        data = self.objects[Key, version]
        return {'ContentLength': len(data), 'VersionId': version,
                'ChecksumSHA256': base64.b64encode(bytes.fromhex(checksum(data))).decode()}

    def put_object(self, Key, Body, **kwargs):
        version = str(len(self.objects) + 1)
        self.objects[Key, version] = Body
        self.latest[Key] = version
        return {'VersionId': version}

    def get_object(self, Key, VersionId, **kwargs):
        return {'Body': io.BytesIO(self.objects[Key, VersionId])}


@pytest.fixture
def storage():
    return S3Store('reveal-test-artifacts', client=MemoryS3())


def test_exact_version_survives_overwrite_and_detects_corruption(storage):
    reference = storage.put(b'original captured evidence')
    assert storage.put(b'original captured evidence') == reference
    storage.client.put_object(Key=reference['key'], Body=b'overwritten latest version')
    assert storage.get(reference) == b'original captured evidence'
    storage.client.objects[reference['key'], reference['version_id']] = b'corrupt'
    with pytest.raises(StorageUnavailable, match='checksum'):
        storage.get(reference)


def test_requires_versioning_and_rejects_foreign_bucket(storage):
    storage.client.versioning = False
    with pytest.raises(StorageUnavailable, match='versioning'): storage.check()
    reference = storage.put(b'bytes')
    with pytest.raises(StorageUnavailable, match='reference'):
        storage.get(dict(reference, bucket='another-bucket'))
    with pytest.raises(StorageUnavailable, match='reference'):
        storage.get(dict(reference, version_id='null'))


def test_cloud_cutover_reads_explicit_legacy_prefix_but_only_writes_prod():
    client = MemoryS3()
    local = S3Store('reveal-test-artifacts', 'local/', client=client)
    old = local.put(b'preserved publication evidence')
    cloud = S3Store('reveal-test-artifacts', 'prod/', client=client, read_prefixes=('local/',))
    assert cloud.get(old) == b'preserved publication evidence'
    new = cloud.put(b'new deployed evidence')
    assert new['key'].startswith('prod/artifacts/')
    with pytest.raises(StorageUnavailable, match='reference'):
        cloud.get(dict(old, key=old['key'].replace('local/', 'another/')))
    with pytest.raises(StorageUnavailable, match='reference'):
        S3Store('reveal-test-artifacts', 'prod/', client=client).get(old)
    for prefix in ('../local', '/local', '', 'local/../prod', 'local//prod'):
        with pytest.raises(ValueError, match='read prefix'):
            S3Store('reveal-test-artifacts', 'prod/', client=client, read_prefixes=(prefix,))


def test_checkpoint_restores_to_empty_worker_and_rejects_path_escape(storage, tmp_path):
    original = tmp_path / 'old-worker'
    (original / 'attempt-1').mkdir(parents=True)
    (original / 'attempt-1/output.json').write_bytes(b'unchanged scientific output')
    manifest = storage.snapshot(original)
    restored = tmp_path / 'replacement-worker'
    storage.restore(manifest, restored)
    assert (restored / 'attempt-1/output.json').read_bytes() == b'unchanged scientific output'
    invalid = storage.put(json.dumps({'format': 'reveal.workspace/1', 'files': [
        {'path': '../escape', 'storage': storage.put(b'escaped')}
    ]}).encode())
    with pytest.raises(StorageUnavailable, match='Unsafe'): storage.restore(invalid, restored)
    assert not (tmp_path / 'escape').exists()


def test_checkpoint_rejects_symlinks(storage, tmp_path):
    source, root = tmp_path / 'private', tmp_path / 'work'
    source.write_text('secret'); root.mkdir(); (root / 'link').symlink_to(source)
    with pytest.raises(StorageUnavailable, match='symbolic'): storage.snapshot(root)


def test_s3_admin_diagnostics_do_not_need_worker_disk_or_expose_other_files(storage, tmp_path, monkeypatch):
    from reveal_backend.admin_jobs import s3_diagnostics
    root = tmp_path / 'work'
    (root / 'attempt-1').mkdir(parents=True)
    (root / 'attempt-1/failure.json').write_text('{"message":"retained failure","token":"private"}')
    (root / 'attempt-1/private.json').write_text('{"private":"must not appear"}')
    (root / 'evidence').mkdir()
    (root / 'evidence/collection-error.json').write_text('{"error":"Missing schema", "token":"private"}')
    reference = storage.snapshot(root)
    monkeypatch.setattr('reveal_backend.artifact_store.store', lambda: storage)
    values, limited = s3_diagnostics(reference, [{'attempt': 1}])
    assert not limited and len(values) == 2
    assert values[0]['data'] == {'message': 'retained failure', 'token': '[redacted]'}
    assert values[1]['data'] == {'error': 'Missing schema', 'token': '[redacted]'}


def test_local_bootstrap_checks_aws_without_modifying_bucket_policy(monkeypatch):
    from reveal_backend.deployment import bootstrap
    monkeypatch.setenv('REVEAL_LOCAL_DEPLOYMENT', '1')
    monkeypatch.setenv('REVEAL_ENVIRONMENT', 'development')
    monkeypatch.delenv('REVEAL_S3_ENDPOINT_URL', raising=False)
    monkeypatch.delenv('REVEAL_S3_PUBLIC_ENDPOINT_URL', raising=False)
    calls = []
    blocked = dict.fromkeys(('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets'), True)
    # No bucket write methods exist on this client: startup must only inspect it.
    storage = SimpleNamespace(prefix='local/', bucket='test-artifacts', check=lambda: calls.append('check'),
        client=SimpleNamespace(get_public_access_block=lambda **kwargs: {'PublicAccessBlockConfiguration': blocked}))
    repo = SimpleNamespace(table_prefix='reveal_test', migrate=lambda: calls.append('migrate'),
                           readiness=lambda: {'tls': True})
    monkeypatch.setattr('reveal_backend.deployment.store', lambda: storage)
    assert bootstrap(repo)['storage'] == 'ready'
    assert calls == ['check', 'migrate']
    blocked['BlockPublicPolicy'] = False
    with pytest.raises(RuntimeError, match='block all public access'): bootstrap(repo)
    assert calls.count('migrate') == 1
    storage.prefix = 'prod/'
    with pytest.raises(RuntimeError, match='local/'): bootstrap(repo)


def test_probe_renews_lease_during_slow_storage_operations(monkeypatch):
    import asyncio
    import threading
    from reveal_backend import deployment
    heartbeat = threading.Event()
    def renew(*args):
        heartbeat.set()
        return True
    async def slow_storage(*args):
        assert await asyncio.to_thread(heartbeat.wait, 2), 'Storage must not block lease renewal'
    monkeypatch.setattr(deployment.jobs, 'lease_duration', lambda: .03)
    monkeypatch.setattr(deployment.jobs, 'heartbeat', renew)
    monkeypatch.setattr(deployment, '_run_probe', slow_storage)
    asyncio.run(deployment.run_probe(SimpleNamespace(repository=object()), {'id': 'probe'}, {'token': 'lease'}))


def test_primary_bootstrap_keeps_original_records_and_requires_migrated_artifacts(tmp_path, monkeypatch):
    from reveal_backend.deployment import bootstrap
    monkeypatch.setenv('REVEAL_LOCAL_DEPLOYMENT', '1')
    monkeypatch.setenv('REVEAL_ENVIRONMENT', 'development')
    monkeypatch.delenv('REVEAL_S3_ENDPOINT_URL', raising=False)
    monkeypatch.delenv('REVEAL_S3_PUBLIC_ENDPOINT_URL', raising=False)
    storage = SimpleNamespace(prefix='local/', bucket='test-artifacts', check=lambda: None,
        client=SimpleNamespace(get_public_access_block=lambda **kwargs: {'PublicAccessBlockConfiguration':
            dict.fromkeys(('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets'), True)}))
    monkeypatch.setattr('reveal_backend.deployment.store', lambda: storage)
    repo = Repository(str(tmp_path / 'original.sqlite'), table_prefix='reveal')
    repo.migrate()
    with repo.transaction() as tx:
        tx.put('publication', 'published', 'original-owner', {'visibility': 'public'})
        tx.put('artifact', 'original-file', 'original-owner', {'path': '/old/file'})
    def no_migration(): raise AssertionError('Bootstrap must use the existing tables')
    monkeypatch.setattr(repo, 'migrate', no_migration)
    with pytest.raises(RuntimeError, match='Migrate existing retained artifacts'): bootstrap(repo)
    with repo.transaction() as tx:
        tx.put('artifact', 'original-file', 'original-owner', {'storage': {'store': 's3'}})
    assert bootstrap(repo)['application_tables'] == 'reveal'
    with repo.read_transaction() as tx:
        assert tx.get('publication', 'published')['owner'] == 'original-owner'
    with pytest.raises(RuntimeError, match='isolated'): local_only(repo)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv('REVEAL_JOB_TRANSPORT', 'redis')
    monkeypatch.setenv('REVEAL_JOB_NAMESPACE', 'deployment-test')
    monkeypatch.setenv('REVEAL_MAX_RUNNING_JOBS', '2')
    repo = Repository(str(tmp_path / 'app.sqlite'), table_prefix='reveal_test')
    repo.migrate()
    return repo


def enqueue(repo):
    with repo.transaction() as tx:
        job = jobs.enqueue(tx, 'owner', 'analysis')
        return job['id'], tx.get('queue', job['id'])['data']['dispatch_id']


def claim(repo, pair, worker='worker'):
    return jobs.claim(repo, worker, job_id=pair[0], dispatch_id=pair[1])


def test_outbox_is_atomic_and_default_application_tables_are_isolated(repository):
    with pytest.raises(RuntimeError), repository.transaction() as tx:
        jobs.enqueue(tx, 'owner', 'analysis')
        raise RuntimeError('abort submission')
    with repository.read_transaction() as tx:
        assert tx.list('job') == tx.list('dispatch') == []
    pair = enqueue(repository)
    original = Repository(repository.sqlite_path, table_prefix='reveal')
    original.migrate()
    with original.read_transaction() as tx: assert tx.list('job') == []
    with repository.read_transaction() as tx:
        assert tx.get('dispatch', pair[0])['data']['dispatch_id'] == pair[1]


def test_duplicate_delivery_global_cap_and_drain(repository):
    first, second, third = [enqueue(repository) for _ in range(3)]
    one, two = claim(repository, first), claim(repository, second)
    assert one and two
    assert claim(repository, first, 'duplicate') is None
    assert claim(repository, third, 'overflow') is None
    assert jobs.finish(repository, first[0], one[1]['token'], 'failed')
    draining(repository, True)
    assert claim(repository, third) is None
    draining(repository, False)
    assert claim(repository, third)


def test_expired_worker_cannot_commit_after_s3_recovery_claim(repository):
    pair = enqueue(repository)
    old = claim(repository, pair)
    with repository.transaction() as tx:
        queue = tx.get('queue', pair[0])['data']
        queue.update(workspace={'checkpoint': 'present'}, lease_until=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        tx.put('queue', pair[0], 'owner', queue)
    new = claim(repository, pair, 'replacement')
    assert new and new[1]['attempt'] == old[1]['attempt']
    assert new[1]['token'] != old[1]['token']
    assert not jobs.finish(repository, pair[0], old[1]['token'], 'succeeded')
    assert jobs.finish(repository, pair[0], new[1]['token'], 'failed')


def test_database_worker_cannot_claim_redis_namespace(repository, monkeypatch):
    pair = enqueue(repository)
    monkeypatch.setenv('REVEAL_JOB_TRANSPORT', 'database')
    assert claim(repository, pair) is None


def test_colleague_queue_does_not_claim_or_drain_original_jobs(repository, monkeypatch):
    from reveal_backend.deployment import snapshot
    from reveal_backend.repository import now
    original = enqueue(repository)
    with repository.transaction() as tx:
        tx.put('runtime', 'original-worker', 'system', {'namespace': 'deployment-test', 'heartbeat_at': now(), 'draining': False})
        # Older dispatcher records lack namespace; the ID provides it.
        tx.put('runtime', 'dispatcher:deployment-test', 'system', {'heartbeat_at': now()})
    monkeypatch.setenv('REVEAL_JOB_NAMESPACE', 'colleague')
    colleague = enqueue(repository)
    assert claim(repository, original) is None
    with repository.transaction() as tx:
        tx.put('runtime', 'colleague-worker', 'system', {'namespace': 'colleague', 'heartbeat_at': now(), 'draining': True})
    state = snapshot(repository)
    assert [row['id'] for row in state['active_jobs']] == [colleague[0]]
    assert [row['id'] for row in state['runtimes']] == ['colleague-worker']
    draining(repository, True)
    assert claim(repository, colleague) is None
    monkeypatch.setenv('REVEAL_JOB_NAMESPACE', 'deployment-test')
    assert claim(repository, original) is not None


class MemoryBroker:
    def __init__(self): self.messages = {}
    def ensure_group(self): pass
    def exists(self, identity): return identity in self.messages
    def publish(self, intent):
        identity = str(len(self.messages)+1)
        self.messages[identity] = dict(intent)
        return identity


def test_broker_loss_replays_outbox_without_resetting_live_lease(repository):
    pair = enqueue(repository)
    broker = MemoryBroker()
    assert reconcile(repository, broker) == 1
    current = claim(repository, pair)
    assert reconcile(repository, broker) == 0
    broker.messages.clear()
    assert reconcile(repository, broker) == 1
    assert claim(repository, pair, 'duplicate-after-reset') is None
    assert jobs.finish(repository, pair[0], current[1]['token'], 'failed')
    broker.messages.clear()
    assert reconcile(repository, broker) == 0


def test_local_probes_refuse_production_or_default_tables(repository, monkeypatch):
    monkeypatch.setenv('REVEAL_LOCAL_DEPLOYMENT', '1')
    monkeypatch.setenv('REVEAL_ENVIRONMENT', 'production')
    with pytest.raises(RuntimeError): local_only(repository)
    monkeypatch.setenv('REVEAL_ENVIRONMENT', 'development')
    local_only(repository)
    repository.table_prefix = 'reveal'
    with pytest.raises(RuntimeError): local_only(repository)
