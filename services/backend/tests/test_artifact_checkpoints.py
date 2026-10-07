"""Checkpoint speed must preserve exact durable bytes and bounded resources."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import threading

import pytest

from reveal_backend import artifact_store
from reveal_backend.artifact_store import S3Store, StorageUnavailable
from test_deployment import MemoryS3


class CountingS3(MemoryS3):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.calls = []
        self.fail_writes = 0
        self.invalid_version = False

    def head_object(self, **kwargs):
        with self.lock:
            self.calls.append(('head', kwargs['Key']))
            return super().head_object(**kwargs)

    def put_object(self, **kwargs):
        with self.lock:
            self.calls.append(('put', kwargs['Key']))
            if self.fail_writes:
                self.fail_writes -= 1
                raise OSError('Simulated upload failure')
            result = super().put_object(**kwargs)
            return {'VersionId': 'null'} if self.invalid_version else result


def test_repeated_snapshot_reuses_verified_versions_without_network_calls(tmp_path):
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    (tmp_path / 'output').mkdir()
    (tmp_path / 'output/account.json').write_bytes(b'original scientific bytes')
    (tmp_path / 'ledger.json').write_bytes(b'captured evidence')
    first = storage.snapshot(tmp_path)
    calls = list(client.calls)
    assert storage.snapshot(tmp_path) == first
    assert client.calls == calls
    manifest = json.loads(storage.get(first))
    before = {item['path']: item['storage'] for item in manifest['files']}
    assert list(before) == sorted(before)

    (tmp_path / 'output/account.json').write_bytes(b'changed scientific bytes')
    changed = storage.snapshot(tmp_path)
    after = {item['path']: item['storage'] for item in json.loads(storage.get(changed))['files']}
    assert changed != first
    assert after['ledger.json'] == before['ledger.json']
    assert after['output/account.json'] != before['output/account.json']
    assert len(client.calls) == len(calls) + 4  # Changed file + changed manifest, HEAD and PUT each.
    assert storage.get(before['output/account.json']) == b'original scientific bytes'
    assert storage.get(after['output/account.json']) == b'changed scientific bytes'


def test_cache_keeps_exact_version_after_latest_overwrite_and_cannot_be_mutated():
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    first = storage.put(b'original')
    original = dict(first)
    first['version_id'] = 'caller-mutated'
    first['key'] = 'caller-mutated'
    client.put_object(Key=original['key'], Body=b'overwritten latest version')
    calls = list(client.calls)
    assert storage.put(b'original') == original
    assert storage.get(original) == b'original'
    assert client.calls == calls
    # Return the requested media type without mutating cached reference metadata.
    assert storage.put(b'original', 'application/json')['content_type'] == 'application/json'
    assert storage.put(b'original')['content_type'] == 'application/octet-stream'


def test_failed_uploads_and_invalid_versions_never_enter_cache():
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    client.fail_writes = 1
    with pytest.raises(StorageUnavailable):
        storage.put(b'retry me')
    assert not storage._verified_puts
    assert storage.get(storage.put(b'retry me')) == b'retry me'
    assert len(client.calls) == 4

    client.invalid_version = True
    with pytest.raises(StorageUnavailable, match='reference'):
        storage.put(b'invalid version')
    count = len(client.calls)
    client.invalid_version = False
    assert storage.get(storage.put(b'invalid version')) == b'invalid version'
    assert len(client.calls) == count + 1  # Re-HEAD verifies the successful remote write.


def test_reference_cache_is_bounded_and_eviction_reverifies_without_reupload(monkeypatch):
    monkeypatch.setattr(artifact_store, 'VERIFIED_PUT_CACHE_SIZE', 3)
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    first = storage.put(b'first')
    for data in (b'second', b'third', b'fourth'):
        storage.put(data)
    assert len(storage._verified_puts) == 3
    calls = len(client.calls)
    assert storage.put(b'first') == first
    assert client.calls[calls:] == [('head', first['key'])]
    assert len(storage._verified_puts) == 3


def test_cache_isolated_by_write_prefix_bucket_and_store_context():
    client = CountingS3()
    local = S3Store('reveal-test-artifacts', 'local/', client=client)
    old = local.put(b'same bytes')
    prod = S3Store('reveal-test-artifacts', 'prod/', client=client, read_prefixes=('local/',))
    assert prod.get(old) == b'same bytes'
    current = prod.put(b'same bytes')
    assert current['key'].startswith('prod/') and current['key'] != old['key']
    calls = len(client.calls)
    fresh = S3Store('reveal-test-artifacts', 'prod/', client=client)
    assert fresh.put(b'same bytes') == current
    assert client.calls[calls:] == [('head', current['key'])]
    with pytest.raises(StorageUnavailable, match='reference'):
        fresh.get(old)
    other = S3Store('reveal-other-artifacts', 'local/', client=CountingS3())
    assert other.put(b'same bytes')['bucket'] == 'reveal-other-artifacts'
    assert len(other.client.calls) == 2


def test_parallel_identical_puts_share_one_verified_write():
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(storage.put, [b'identical'] * 16))
    assert all(ref == refs[0] for ref in refs)
    assert client.calls == [('head', refs[0]['key']), ('put', refs[0]['key'])]


@pytest.mark.parametrize(('window', 'expected_pending'), [(100, 3), (6, 1), (2, 1)])
def test_snapshot_bounds_concurrency_and_buffered_file_reads(tmp_path, monkeypatch, window, expected_pending):
    monkeypatch.setattr(artifact_store, 'SNAPSHOT_CONCURRENCY', 3)
    monkeypatch.setattr(artifact_store, 'SNAPSHOT_PENDING_BYTES', window)
    for index in range(8):
        (tmp_path / f'{index}.txt').write_bytes(str(index).encode() * 4)
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    original_put, original_open = storage.put, Path.open
    lock, filled, release = threading.Lock(), threading.Event(), threading.Event()
    observed = {'active': 0, 'peak': 0, 'reads': 0}

    def open_file(path, *args, **kwargs):
        if path.parent == tmp_path:
            with lock:
                observed['reads'] += 1
        return original_open(path, *args, **kwargs)

    def blocked_put(data, content_type='application/octet-stream'):
        if content_type == 'application/json':
            return original_put(data, content_type)
        with lock:
            observed['active'] += 1
            observed['peak'] = max(observed['peak'], observed['active'])
            if observed['active'] == expected_pending:
                filled.set()
        try:
            assert release.wait(5)
            return original_put(data, content_type)
        finally:
            with lock:
                observed['active'] -= 1

    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(storage, 'put', blocked_put)
    with ThreadPoolExecutor(max_workers=1) as pool:
        snapshot = pool.submit(storage.snapshot, tmp_path)
        try:
            assert filled.wait(3), 'Snapshot did not fill its allowed transfer window'
            with lock:
                assert observed['reads'] == expected_pending
        finally:
            release.set()
        reference = snapshot.result(timeout=5)
    assert observed['peak'] == expected_pending
    assert [row['path'] for row in json.loads(storage.get(reference))['files']] == [f'{i}.txt' for i in range(8)]


def test_failed_snapshot_never_publishes_manifest_and_retry_captures_every_file(tmp_path):
    client = CountingS3()
    storage = S3Store('reveal-test-artifacts', client=client)
    for index in range(4):
        (tmp_path / f'{index}.txt').write_bytes(str(index).encode())
    client.fail_writes = 1
    with pytest.raises(StorageUnavailable):
        storage.snapshot(tmp_path)
    assert not any(data.startswith(b'{"files":') for data in client.objects.values())
    reference = storage.snapshot(tmp_path)
    restored = tmp_path / 'restored'
    storage.restore(reference, restored)
    assert [path.read_bytes() for path in sorted(restored.iterdir())] == [str(i).encode() for i in range(4)]


def test_snapshot_limits_and_changed_file_fail_before_manifest(tmp_path, monkeypatch):
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    (tmp_path / 'large').write_bytes(b'four')
    monkeypatch.setenv('REVEAL_WORKSPACE_MAX_BYTES', '3')
    with pytest.raises(StorageUnavailable, match='workspace limit'):
        storage.snapshot(tmp_path)
    assert not storage.client.calls
    monkeypatch.setenv('REVEAL_WORKSPACE_MAX_BYTES', '100')
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: io.BytesIO(b'grew since stat'))
    with pytest.raises(StorageUnavailable, match='changed during capture'):
        storage.snapshot(tmp_path)
    assert not storage.client.calls


@pytest.mark.parametrize(('window', 'expected_pending'), [(100, 4), (6, 1), (2, 1)])
def test_restore_bounds_downloads_and_bytes_and_preserves_exact_files(tmp_path, monkeypatch, window, expected_pending):
    monkeypatch.setattr(artifact_store, 'RESTORE_CONCURRENCY', 4)
    monkeypatch.setattr(artifact_store, 'RESTORE_PENDING_BYTES', window)
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    for index in range(9):
        (source / f'{index}.txt').write_bytes(str(index).encode() * 4)
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    reference = storage.snapshot(source)
    original_get = storage.get
    lock, filled, release = threading.Lock(), threading.Event(), threading.Event()
    observed = {'active': 0, 'peak': 0, 'started': 0}

    def blocked_get(ref):
        if ref == reference:
            return original_get(ref)
        with lock:
            observed['active'] += 1
            observed['started'] += 1
            observed['peak'] = max(observed['peak'], observed['active'])
            if observed['active'] == expected_pending:
                filled.set()
        try:
            assert release.wait(5)
            return original_get(ref)
        finally:
            with lock:
                observed['active'] -= 1

    monkeypatch.setattr(storage, 'get', blocked_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        restore = pool.submit(storage.restore, reference, target)
        try:
            assert filled.wait(3), 'Restore did not fill its allowed download window'
            assert observed['started'] == expected_pending
            assert not target.exists(), 'Download threads must not write the workspace'
        finally:
            release.set()
        restore.result(timeout=5)
    assert observed['peak'] == expected_pending
    assert [p.read_bytes() for p in sorted(target.iterdir())] == [str(i).encode() * 4 for i in range(9)]


@pytest.mark.parametrize('failure', ['cancel', 'checksum'])
def test_restore_failure_drains_downloads_and_never_writes_unverified_bytes(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(artifact_store, 'RESTORE_CONCURRENCY', 4)
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    for index in range(8):
        (source / f'{index}.txt').write_bytes(str(index).encode())
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    reference = storage.snapshot(source)
    manifest = json.loads(storage.get(reference))
    first = manifest['files'][0]['storage']
    original_get = storage.get
    lock = threading.Lock()
    filled, release_first, release_others, cancelling = (threading.Event() for _ in range(4))
    observed = {'active': 0, 'started': 0}

    def blocked_get(ref):
        if ref == reference:
            return original_get(ref)
        with lock:
            observed['active'] += 1
            observed['started'] += 1
            if observed['active'] == 4:
                filled.set()
        try:
            assert (release_first if ref == first else release_others).wait(5)
            if failure == 'checksum' and ref == first:
                raise StorageUnavailable('Artifact checksum mismatch')
            return original_get(ref)
        finally:
            with lock:
                observed['active'] -= 1

    monkeypatch.setattr(storage, 'get', blocked_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        restored = pool.submit(storage.restore, reference, target, cancelled=cancelling.is_set)
        try:
            assert filled.wait(3)
            if failure == 'cancel':
                cancelling.set()
            release_first.set()
            # Other responses remain live: restore must not return and permit
            # its caller to release scratch while these requests still run.
            with pytest.raises(TimeoutError):
                restored.result(timeout=0.05)
        finally:
            release_first.set()
            release_others.set()
        with pytest.raises(StorageUnavailable, match='cancelled|checksum'):
            restored.result(timeout=5)
    assert observed == {'active': 0, 'started': 4}
    assert not target.exists()


def test_transfer_concurrency_defaults_to_sixteen_and_rejects_unbounded_values(monkeypatch):
    monkeypatch.delenv('REVEAL_S3_TRANSFER_CONCURRENCY', raising=False)
    assert artifact_store.transfer_concurrency() == 16
    monkeypatch.setenv('REVEAL_S3_TRANSFER_CONCURRENCY', '4')
    assert artifact_store.transfer_concurrency() == 4
    for value in ('0', '65'):
        monkeypatch.setenv('REVEAL_S3_TRANSFER_CONCURRENCY', value)
        with pytest.raises(ValueError):
            artifact_store.transfer_concurrency()


class GetCountingS3(CountingS3):
    def get_object(self, **kwargs):
        with self.lock:
            self.calls.append(('get', kwargs['Key']))
        return super().get_object(**kwargs)


def test_restore_downloads_each_object_version_once_and_writes_every_path(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    (source / 'ledger').mkdir(parents=True)
    for name in ('a.json', 'ledger/b.json', 'ledger/c.json'):
        (source / name).write_bytes(b'{}')   # identical bytes: one immutable object
    (source / 'other.txt').write_bytes(b'distinct')
    client = GetCountingS3()
    reference = S3Store('reveal-test-artifacts', client=client).snapshot(source)
    client.calls.clear()
    S3Store('reveal-test-artifacts', client=client).restore(reference, target)
    gets = [key for kind, key in client.calls if kind == 'get']
    assert len(gets) == 3 and len(set(gets)) == 3   # manifest, the shared '{}' object, the distinct object
    assert {path.relative_to(target).as_posix(): path.read_bytes() for path in target.rglob('*') if path.is_file()} == {
        'a.json': b'{}', 'ledger/b.json': b'{}', 'ledger/c.json': b'{}', 'other.txt': b'distinct'}


def test_restored_versions_seed_the_put_cache_so_an_unchanged_checkpoint_sends_nothing(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    for index in range(5):
        (source / f'{index}.json').write_bytes(str(index).encode())
    client = CountingS3()
    reference = S3Store('reveal-test-artifacts', client=client).snapshot(source)
    fresh = S3Store('reveal-test-artifacts', client=client)   # another task: a cold cache
    fresh.restore(reference, target)
    client.calls.clear()
    assert fresh.snapshot(target) == reference
    assert client.calls == []   # no HEAD per restored file, none for the unchanged manifest
    (target / 'validated.json').write_bytes(b'new')
    fresh.snapshot(target)
    assert sorted(kind for kind, _ in client.calls) == ['head', 'head', 'put', 'put']   # only new bytes and the manifest


def test_legacy_prefix_reads_never_enter_the_put_cache(tmp_path):
    client = CountingS3()
    old = S3Store('reveal-test-artifacts', 'local/', client=client).put(b'legacy bytes')
    manifest = S3Store('reveal-test-artifacts', 'local/', client=client).put(json.dumps(
        {'format': 'reveal.workspace/1', 'files': [{'path': 'a.txt', 'storage': old}]}).encode())
    prod = S3Store('reveal-test-artifacts', 'prod/', client=client, read_prefixes=('local/',))
    prod.restore(manifest, tmp_path / 'restore')
    assert (tmp_path / 'restore/a.txt').read_bytes() == b'legacy bytes'
    assert not prod._verified_puts
    calls = len(client.calls)
    assert prod.put(b'legacy bytes')['key'].startswith('prod/')
    assert [kind for kind, _ in client.calls[calls:]] == ['head', 'put']


def test_restore_preflights_all_paths_before_parallel_downloads(tmp_path, monkeypatch):
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    artifact = storage.put(b'data')
    ref = storage.put(json.dumps({'format': 'reveal.workspace/1', 'files': [
        {'path': 'safe.txt', 'storage': artifact}, {'path': '../escape', 'storage': artifact},
    ]}).encode())
    original_get = storage.get
    reads = []
    def recorded_get(value):
        reads.append(value)
        return original_get(value)
    monkeypatch.setattr(storage, 'get', recorded_get)
    with pytest.raises(StorageUnavailable, match='Unsafe'):
        storage.restore(ref, tmp_path / 'restore')
    assert reads == [ref]


def test_metadata_checkpoint_reads_only_requested_file_and_retains_sibling_versions(tmp_path, monkeypatch):
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    (tmp_path / 'evidence.txt').write_bytes(b'frozen evidence')
    (tmp_path / 'review.json').write_bytes(b'{"turn":1}')
    original = storage.snapshot(tmp_path)
    before = {item['path']: item['storage'] for item in storage.workspace_manifest(original)['files']}
    get = storage.get
    reads = []
    def observed_get(ref):
        reads.append(ref)
        return get(ref)
    monkeypatch.setattr(storage, 'get', observed_get)
    assert storage.read_workspace_file(original, 'review.json') == b'{"turn":1}'
    assert reads == [original, before['review.json']]
    reads.clear()
    replacement = storage.replace_workspace_files(original, {'review.json': b'{"turn":2}'})
    assert reads == [original]  # No existing artifact body was downloaded.
    after = {item['path']: item['storage'] for item in storage.workspace_manifest(replacement)['files']}
    assert after['evidence.txt'] == before['evidence.txt']
    assert after['review.json'] != before['review.json']
    assert storage.read_workspace_file(original, 'review.json') == b'{"turn":1}'
    assert storage.read_workspace_file(replacement, 'review.json') == b'{"turn":2}'
    calls = list(storage.client.calls)
    assert storage.replace_workspace_files(replacement, {'review.json': b'{"turn":2}'}) == replacement
    assert storage.client.calls == calls


@pytest.mark.parametrize('updates', [{'../escape': b'x'}, {'review.json/child': b'x'}, {'large': b'x' * 100}])
def test_metadata_checkpoint_preflights_all_updates_before_upload(tmp_path, monkeypatch, updates):
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    (tmp_path / 'review.json').write_bytes(b'{}')
    original = storage.snapshot(tmp_path)
    calls = list(storage.client.calls)
    monkeypatch.setenv('REVEAL_WORKSPACE_MAX_BYTES', '32')
    with pytest.raises(StorageUnavailable):
        storage.replace_workspace_files(original, {'new.json': b'{}', **updates})
    assert storage.client.calls == calls


def test_metadata_checkpoint_failure_leaves_original_readable(tmp_path):
    storage = S3Store('reveal-test-artifacts', client=CountingS3())
    (tmp_path / 'review.json').write_bytes(b'original')
    original = storage.snapshot(tmp_path)
    storage.client.fail_writes = 1
    with pytest.raises(StorageUnavailable):
        storage.replace_workspace_files(original, {'review.json': b'changed'})
    assert storage.read_workspace_file(original, 'review.json') == b'original'
    replacement = storage.replace_workspace_files(original, {'review.json': b'changed'})
    assert storage.read_workspace_file(replacement, 'review.json') == b'changed'
