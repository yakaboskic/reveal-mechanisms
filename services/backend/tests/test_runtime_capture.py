"""Large trusted input-view provenance survives capture without enlarging authored outputs."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reveal_backend import box_remote, box_upload as upload, direct_capture as capture
from reveal_backend.box_adapter import BoxTransportError, read_capture_marker
from test_direct_capture import BINDING, HANDLE, STATE, descriptor, fixture_bytes, inventory, previous_workspace, staged


@pytest.fixture(scope='module')
def large_runtime():
    # The first KPN pilot retained 108,861 derived-view hashes in its trusted manifest.
    raw = capture.canonical({'job_id': BINDING['job_id'], 'attempt': 1,
        'input_sha256': BINDING['input_sha256'],
        'derived_input_views': {f'input/evidence-records/{index:064x}.json': 'a' * 64 for index in range(108861)}})
    assert 17_000_000 < len(raw) <= upload.MAX_RUNTIME == 20_000_000
    return raw


def remote_tree(root, data):
    (root / 'state').mkdir(parents=True)
    (root / 'request.json').write_bytes(capture.canonical({key: value for key, value in BINDING.items() if key != 'box_id'}))
    (root / 'state/status.json').write_bytes(capture.canonical(STATE))
    for name, raw in data.items():
        target = root / ('state/' + name if name == 'runtime.json' or name.startswith('ledger/') else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return root


def test_large_runtime_freezes_uploads_verifies_and_restores_unchanged(tmp_path, monkeypatch, large_runtime):
    data = fixture_bytes(**{'runtime.json': large_runtime})
    remote = remote_tree(tmp_path / 'box', data)
    frozen = upload.inventory(remote, BINDING)
    capture.validate_inventory(frozen, BINDING, STATE)
    runtime = next(item for item in frozen['files'] if item['path'] == 'runtime.json')
    assert runtime == descriptor('runtime.json', large_runtime)
    assert (remote / 'state/runtime.json').read_bytes() == large_runtime
    assert upload.inventory(remote, BINDING) == frozen  # Same Box/attempt resumes its original bytes.

    storage, files, receipts, refs = staged(data)
    host, tickets = capture.tickets_for(storage, [runtime])
    class Connection:
        def __init__(self, hostname, timeout): assert hostname == host
        def request(self, method, path, body, headers):
            assert method == 'PUT' and body.read() == large_runtime
            assert headers['Content-Length'] == str(len(large_runtime))
        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _count: b'', getheader=lambda _name: refs['runtime.json']['version_id'])
        def close(self): pass
    monkeypatch.setattr(upload.http.client, 'HTTPSConnection', Connection)
    assert upload.upload(remote, {'binding': BINDING, 'host': host, 'tickets': tickets})['receipts'][0]['path'] == 'runtime.json'
    verified = capture.verify_uploads(storage, files, receipts, BINDING, ())
    result = capture.compose_workspace(storage, previous_workspace(storage, **{'input.json': b'input'}), BINDING, HANDLE, files, verified)
    restored = tmp_path / 'restored'
    storage.restore(result['workspace'], restored)
    request = SimpleNamespace(job_id=BINDING['job_id'], attempt=1, kind='research', selected_graphs=BINDING['selected_graphs'],
        input_path=restored / 'input.json', output_dir=restored / 'attempt-1/output')
    marker = read_capture_marker(request, result['box'])
    assert marker['files']['runtime.json']['sha256'] == hashlib.sha256(large_runtime).hexdigest()
    assert (request.output_dir / 'runtime.json').read_bytes() == large_runtime
    (request.output_dir / 'runtime.json').write_bytes(b'x' + large_runtime[1:])
    with pytest.raises(BoxTransportError, match='incomplete or changed'): read_capture_marker(request, result['box'])


@pytest.mark.parametrize('name', ['output/runtime.json', 'ledger/runtime.json', 'output/account.json'])
def test_authored_and_ledger_files_do_not_inherit_runtime_allowance(tmp_path, name):
    remote = remote_tree(tmp_path, fixture_bytes())
    path = remote / ('state/' + name if name.startswith('ledger/') else name)
    with path.open('wb') as stream: stream.truncate(8_000_001)
    with pytest.raises(ValueError, match='bounded regular files'): upload.inventory(remote, BINDING)
    value = inventory([descriptor('ledger/manifest.json', b'{}'), {'path': name, 'size_bytes': 8_000_001, 'sha256': 'a' * 64}])
    with pytest.raises(BoxTransportError, match='size or checksum'): capture.validate_inventory(value, BINDING, STATE)
    assert not (remote / 'state/direct-capture-v1').exists()


def test_runtime_still_has_20mb_limit_and_counts_toward_40mb_total(tmp_path):
    remote = remote_tree(tmp_path, fixture_bytes())
    with (remote / 'state/runtime.json').open('wb') as stream: stream.truncate(20_000_001)
    with pytest.raises(ValueError, match='bounded regular files'): upload.inventory(remote, BINDING)
    runtime = {'path': 'runtime.json', 'size_bytes': 20_000_001, 'sha256': 'a' * 64}
    with pytest.raises(BoxTransportError, match='size or checksum'):
        capture.validate_inventory(inventory([descriptor('ledger/manifest.json', b'{}'), runtime]), BINDING, STATE)
    runtime['size_bytes'] = 20_000_000
    ordinary = [{'path': f'output/part-{index}', 'size_bytes': 8_000_000, 'sha256': 'b' * 64} for index in range(3)]
    with pytest.raises(BoxTransportError, match='byte budget'):
        capture.validate_inventory(inventory([descriptor('ledger/manifest.json', b'{}'), runtime, *ordinary]), BINDING, STATE)
    assert upload.MAX_TOTAL == capture.MAX_TOTAL == 40_000_000


@pytest.mark.parametrize('unsafe', ['writable', 'owner', 'symlink'])
def test_metadata_allowance_requires_trusted_nonlinked_runtime(tmp_path, monkeypatch, unsafe):
    remote = remote_tree(tmp_path, fixture_bytes())
    path = remote / 'state/runtime.json'
    if unsafe == 'writable': path.chmod(0o666)
    elif unsafe == 'owner':
        uid = os.geteuid()
        monkeypatch.setattr(upload.os, 'geteuid', lambda: uid + 1)
    else:
        path.unlink(); path.symlink_to(remote / 'request.json')
    with pytest.raises((ValueError, OSError)): upload.inventory(remote, BINDING)
    assert not (remote / 'state/direct-capture-v1').exists()


def test_large_runtime_keeps_streaming_credential_and_checksum_checks():
    secret = b'synthetic-known-credential'
    raw = b'a' * (8_388_608 - 7) + secret + b'end'
    storage, files, receipts, _ = staged({'runtime.json': raw})
    with pytest.raises(BoxTransportError, match='credential'):
        capture.verify_uploaded(storage, files[0], receipts[0], (secret,))
    storage.client.get_object = lambda **kwargs: {'Body': io.BytesIO(b'x' * len(raw))}
    with pytest.raises(BoxTransportError, match='bytes differ'):
        capture.verify_uploaded(storage, files[0], receipts[0], ())


def test_legacy_collect_accepts_same_metadata_and_enforces_aggregate(tmp_path, monkeypatch, large_runtime):
    remote_tree(tmp_path, fixture_bytes(**{'runtime.json': large_runtime}))
    monkeypatch.setattr(box_remote, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(box_remote, 'OUTPUT', tmp_path / 'output')
    captured = box_remote.collect()
    assert base64.b64decode(captured['files']['runtime.json']) == large_runtime
    monkeypatch.setattr(box_remote, 'MAX_TOTAL', len(large_runtime))
    with pytest.raises(ValueError, match='budget exceeded'): box_remote.collect()
