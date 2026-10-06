"""Encrypted handoff failures must not leak or overwrite working configuration."""
import json
from pathlib import Path
import shutil
import sys

from dotenv import dotenv_values
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import local_handoff as handoff


def bundle_document():
    return {'format': handoff.FORMAT, 'environment': {'REVEAL_MYSQL_PASSWORD': "quoted ' # \\ ${value} $secret"},
            'storage': {'REVEAL_S3_PREFIX': 'local/', 'AWS_ACCESS_KEY_ID': 'test-key',
                        'AWS_SECRET_ACCESS_KEY': 'test-secret'}}


def test_import_preserves_secret_bytes_and_isolates_only_queue(tmp_path, monkeypatch):
    bundle = tmp_path / 'input.gpg'; bundle.write_bytes(b'encrypted')
    monkeypatch.setattr(handoff, 'gpg', lambda *args, **kwargs: json.dumps(bundle_document()).encode())
    first, second = tmp_path / 'first', tmp_path / 'second'
    handoff.import_bundle(bundle, 'password', root=first)
    handoff.import_bundle(bundle, 'password', root=second)
    env = dotenv_values(first / '.env', interpolate=False)
    assert env['REVEAL_MYSQL_PASSWORD'] == bundle_document()['environment']['REVEAL_MYSQL_PASSWORD']
    assert env['REVEAL_JOB_NAMESPACE'] != dotenv_values(second / '.env')['REVEAL_JOB_NAMESPACE']
    assert env['REVEAL_JOB_NAMESPACE'].startswith('reveal-local-')
    assert 'REVEAL_APPLICATION_TABLE_PREFIX' not in env
    assert 'AUTH_SECRET' not in env
    assert env['REVEAL_PUBLIC_WEB_URL'] == env['REVEAL_CANONICAL_URL'] == env['NEXTAUTH_URL'] == 'http://localhost:3000'
    assert (first / '.env').stat().st_mode & 0o777 == 0o600
    original = (first / '.env').read_bytes()
    with pytest.raises(RuntimeError, match='overwrite'): handoff.import_bundle(bundle, 'password', root=first)
    assert (first / '.env').read_bytes() == original


@pytest.mark.skipif(not shutil.which('gpg'), reason='GnuPG is needed for actual encryption round-trip')
def test_real_gpg_roundtrip_wrong_password_and_tampering():
    plaintext = json.dumps(bundle_document()).encode()
    encrypted = handoff.gpg(plaintext, 'correct-handoff-password')
    assert b'test-secret' not in encrypted
    assert handoff.gpg(encrypted, 'correct-handoff-password', decrypt=True) == plaintext
    with pytest.raises(RuntimeError, match='GnuPG failed'):
        handoff.gpg(encrypted, 'wrong-password', decrypt=True)
    broken = bytearray(encrypted); broken[-8] ^= 1
    with pytest.raises(RuntimeError, match='GnuPG failed'):
        handoff.gpg(bytes(broken), 'correct-handoff-password', decrypt=True)


def test_invalid_bundle_writes_nothing(tmp_path, monkeypatch):
    bundle = tmp_path / 'input.gpg'; bundle.write_bytes(b'encrypted')
    document = bundle_document(); document['storage']['REVEAL_S3_PREFIX'] = 'prod/'
    monkeypatch.setattr(handoff, 'gpg', lambda *args, **kwargs: json.dumps(document).encode())
    with pytest.raises(ValueError, match='local/'):
        handoff.import_bundle(bundle, 'password', root=tmp_path / 'clone')
    assert not (tmp_path / 'clone/.env').exists()


def test_private_write_refuses_symlinks(tmp_path):
    original = tmp_path / 'original'; original.write_text('preserve')
    target = tmp_path / 'link'; target.symlink_to(original)
    with pytest.raises(FileExistsError): handoff.private_write(target, 'secret')
    assert original.read_text() == 'preserve'
