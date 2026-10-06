"""Offline checks for credential isolation and preservation during cloud handoff."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from cloud_deployment import configuration, https_origin

spec = importlib.util.spec_from_file_location('materialize_runtime', ROOT / 'deploy/aws/materialize-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def prepared():
    local = {'REVEAL_MYSQL_HOST': 'existing-db', 'REVEAL_MYSQL_DATABASE': 'existing-schema',
        'REVEAL_MYSQL_PASSWORD': 'db-secret', 'AWS_ACCESS_KEY_ID': 'never-copy',
        'AWS_SECRET_ACCESS_KEY': 'never-copy', 'AWS_PROFILE': 'never-copy',
        'AWS_EC2_METADATA_DISABLED': 'true', 'REVEAL_S3_PREFIX': 'local/',
        'REVEAL_APPLICATION_TABLE_PREFIX': 'wrong', 'UPSTASH_BOX_API_KEY': 'backend-only'}
    local.update(REVEAL_API_KEY_SHA256='a'*64, REVEAL_API_KEY_USER_ID='11111111-1111-4111-8111-111111111111')
    local.update(REVEAL_ADMIN_READ_API_KEY_SHA256='b'*64,
                 REVEAL_ADMIN_READ_API_KEY_ID='22222222-2222-4222-8222-222222222222')
    frontend = {'AUTH_GOOGLE_ID': 'same-identity-provider', 'AUTH_GOOGLE_SECRET': 'oauth-secret',
        'REVEAL_MYSQL_PASSWORD': 'never-copy', 'DISABLE_ADMIN_LOGIN': 'true'}
    keys = {key: letter * 64 for key, letter in zip(('gateway', 'service', 'session', 'redis'), 'abcd')}
    image = '005901288866.dkr.ecr.us-east-1.amazonaws.com/cyaka-reveal-backend@sha256:' + 'a' * 64
    return configuration(local, frontend, keys, 'https://reveal-mechanisms.vercel.app', 'https://203.0.113.10', image)


def test_cloud_handoff_preserves_database_and_identity_without_copying_aws_credentials():
    secret, frontend, compose = prepared()
    backend = secret['backend']
    assert backend['REVEAL_MYSQL_HOST'] == 'existing-db'
    assert backend['REVEAL_MYSQL_DATABASE'] == 'existing-schema'
    assert backend['REVEAL_APPLICATION_TABLE_PREFIX'] == 'reveal'
    assert backend['REVEAL_S3_PREFIX'] == 'prod/'
    assert backend['REVEAL_S3_READ_PREFIXES'] == 'local/'
    assert not {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_PROFILE', 'AWS_EC2_METADATA_DISABLED'} & backend.keys()
    assert backend['UPSTASH_BOX_API_KEY'] == 'backend-only'
    assert backend['REVEAL_API_KEY_SHA256'] == backend['REVEAL_API_KEY_USER_ID'] == ''
    assert 'REVEAL_API_KEY_SHA256' not in frontend and 'REVEAL_API_KEY_USER_ID' not in frontend
    assert backend['REVEAL_ADMIN_READ_API_KEY_SHA256'] == backend['REVEAL_ADMIN_READ_API_KEY_ID'] == ''
    assert not {'REVEAL_ADMIN_READ_API_KEY_SHA256', 'REVEAL_ADMIN_READ_API_KEY_ID'} & frontend.keys()
    assert 'UPSTASH_BOX_API_KEY' not in frontend and 'REVEAL_MYSQL_PASSWORD' not in frontend
    assert frontend['REVEAL_GATEWAY_SECRET'] == backend['REVEAL_GATEWAY_SECRET']
    assert backend['REVEAL_PUBLIC_WEB_URL'] == backend['REVEAL_CANONICAL_URL'] == frontend['NEXTAUTH_URL'] == 'https://reveal-mechanisms.vercel.app'
    assert frontend['AUTH_GOOGLE_ID'] == 'same-identity-provider'
    assert frontend['DISABLE_ADMIN_LOGIN'] == 'false'
    assert compose['REVEAL_RUNTIME_DIR'] == '/run/reveal'


def test_materialization_is_private_and_rejects_credential_or_storage_overrides(tmp_path):
    secret, _, _ = prepared()
    root = tmp_path / 'runtime'
    runtime.materialize(secret, root)
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == (0o444 if path.name == 'redis.conf' else 0o600) for path in root.iterdir())
    assert 'requirepass ' + secret['redis_password'] in (root / 'redis.conf').read_text()
    for key, value in [('AWS_ACCESS_KEY_ID', 'foreign'), ('REVEAL_S3_PREFIX', 'local/'),
                       ('REVEAL_APPLICATION_TABLE_PREFIX', 'empty_tables'),
                       ('REVEAL_S3_ENDPOINT_URL', 'http://emulator'), ('REVEAL_MYSQL_PASSWORD', 'line\nbreak')]:
        invalid = {**secret, 'backend': {**secret['backend'], key: value}}
        with pytest.raises(ValueError): runtime.materialize(invalid, root)
    assert 'REVEAL_S3_PREFIX=prod/' in (root / 'backend.env').read_text()


@pytest.mark.parametrize('value', ['http://example.org', 'https://user:pass@example.org',
    'https://example.org/path', 'https://example.org?x=y', 'https://example.org:8443'])
def test_origin_rejects_insecure_or_non_origin_values(value):
    with pytest.raises(ValueError): https_origin(value)
