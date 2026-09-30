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
