"""Deployment source packaging and AWS configuration, without remote I/O."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('local_deployment', ROOT / 'scripts/local_deployment.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_packages_schema_from_pinned_commit_even_when_checkout_changes(tmp_path):
    source, index, target = (tmp_path / name for name in ('source', 'index', 'image'))
    source.mkdir(); index.mkdir()
    schema = source / 'src/dismech/schema/dismech.yaml'
    schema.parent.mkdir(parents=True)
    schema.write_text('prefixes: {MONDO: "http://purl.obolibrary.org/obo/MONDO_"}\n')
    original_schema = schema.read_bytes()
    document = source / 'kb/disorders/example.yaml'
    document.parent.mkdir(parents=True); document.write_text('name: Example\n')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    git('init', '-q'); git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'Pinned source')
    commit = git('rev-parse', 'HEAD')
    (index / 'manifest.json').write_text(json.dumps({'source_commit': commit}))
    (index / 'source-files.json').write_text(json.dumps([{'path': 'kb/disorders/example.yaml',
        'sha256': hashlib.sha256(document.read_bytes()).hexdigest()}]))
    schema.write_text('prefixes: {MONDO: "changed"}\n')
    deployment.prepare_dismech(source, index, target)
    assert (target / schema.relative_to(source)).read_bytes() == original_schema
    assert (target / document.relative_to(source)).read_bytes() == document.read_bytes()
    document.write_text('name: Unimported edit\n')
    with pytest.raises(RuntimeError, match='differs from the imported manifest'):
        deployment.prepare_dismech(source, index, target)


def test_real_s3_config_uses_regional_url_and_keeps_credentials_backend_only(tmp_path, monkeypatch):
    config = tmp_path / 'storage.env'
    config.write_text('AWS_PROFILE=project\nAWS_REGION=us-east-1\nREVEAL_S3_BUCKET=test-artifacts\nREVEAL_S3_PREFIX=local/\n')
    calls = []
    def credentials(command, **kwargs):
        calls.append(command)
        return json.dumps({'AccessKeyId': 'test-key', 'SecretAccessKey': 'test-secret', 'SessionToken': 'test-session'})
    monkeypatch.setattr(deployment.subprocess, 'check_output', credentials)
    backend, download = deployment.storage_config(config)
    assert calls == [['aws', '--profile', 'project', 'configure', 'export-credentials', '--format', 'process']]
    assert backend['AWS_SESSION_TOKEN'] == 'test-session'
    assert backend['AWS_S3_US_EAST_1_REGIONAL_ENDPOINT'] == 'regional'
    assert backend['REVEAL_S3_ENDPOINT_URL'] == backend['REVEAL_S3_PUBLIC_ENDPOINT_URL'] == ''
    assert download == 'https://test-artifacts.s3.us-east-1.amazonaws.com/local/'
    config.write_text(config.read_text().replace('local/', 'prod/'))
    with pytest.raises(ValueError, match='local/'):
        deployment.storage_config(config)


def test_verification_refuses_original_tables_before_any_stack_mutation(monkeypatch):
    monkeypatch.setattr(deployment, 'read_env', lambda path: {'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal'})
    def no_tools(*args, **kwargs): raise AssertionError('No stack mutation is allowed')
    monkeypatch.setattr(deployment, 'tool', no_tools)
    monkeypatch.setattr(deployment, 'compose', no_tools)
    with pytest.raises(RuntimeError, match='existing application tables'): deployment.verify()


def test_shared_credentials_need_no_aws_cli_and_reject_expiration(tmp_path, monkeypatch):
    config = tmp_path / 'storage.env'
    config.write_text('AWS_REGION=us-east-1\nREVEAL_S3_BUCKET=test-artifacts\nREVEAL_S3_PREFIX=local/\n'
                      'AWS_ACCESS_KEY_ID=shared-key\nAWS_SECRET_ACCESS_KEY=shared-secret\n')
    def no_cli(*args, **kwargs): raise AssertionError('Shared handoff does not need AWS CLI')
    monkeypatch.setattr(deployment.subprocess, 'check_output', no_cli)
    backend, _ = deployment.storage_config(config)
    assert backend['AWS_ACCESS_KEY_ID'] == 'shared-key'
    config.write_text(config.read_text() + 'AWS_CREDENTIAL_EXPIRATION=2020-01-01T00:00:00Z\n')
    with pytest.raises(RuntimeError, match='expired'): deployment.storage_config(config)
