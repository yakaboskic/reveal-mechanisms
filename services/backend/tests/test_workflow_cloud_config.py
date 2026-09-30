"""Owner-key configuration is isolated by environment and never enters frontend config."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts'))
spec=importlib.util.spec_from_file_location('workflow_cloud_config',ROOT/'scripts/workflow_cloud_config.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
KEY_NAMES=('REVEAL_API_KEY_SHA256','REVEAL_API_KEY_USER_ID')
QA={'REVEAL_API_KEY_SHA256':'a'*64,'REVEAL_API_KEY_USER_ID':'11111111-1111-4111-8111-111111111111'}
PROD={'REVEAL_API_KEY_SHA256':'b'*64,'REVEAL_API_KEY_USER_ID':'22222222-2222-4222-8222-222222222222'}


@pytest.fixture
def cloud(tmp_path,monkeypatch):
    runtime=tmp_path/'.runtime/workflow';runtime.mkdir(parents=True)
    baseline=tmp_path/'.runtime/deployment';baseline.mkdir()
    (baseline/'backend.env').write_text('REVEAL_MYSQL_PASSWORD=synthetic-database\n'
        'REVEAL_API_KEY_SHA256='+('d'*64)+'\nREVEAL_API_KEY_USER_ID=44444444-4444-4444-8444-444444444444\n')
    # Shared local config must never be used to enable cloud keys.
    (tmp_path/'.env').write_text('UPSTASH_VECTOR_REST_TOKEN=synthetic-vector\n'
        'REVEAL_API_KEY_SHA256='+('c'*64)+'\nREVEAL_API_KEY_USER_ID=33333333-3333-4333-8333-333333333333\n')
    directory=tmp_path/'deploy/dig';directory.mkdir(parents=True)
    names=('REVEAL_MYSQL_PASSWORD','REVEAL_GATEWAY_SECRET','REVEAL_GATEWAY_SERVICE_TOKEN',*KEY_NAMES)
    manifest={'env':{'NEXTAUTH_URL':'https://frontend.invalid','REVEAL_GATEWAY_ISSUER':'reveal-nextjs',
        'REVEAL_GATEWAY_AUDIENCE':'reveal-api','REVEAL_S3_PREFIX':'prod/','REVEAL_APPLICATION_TABLE_PREFIX':'reveal'},
        'qa':{'env':{'REVEAL_APPLICATION_TABLE_PREFIX':'reveal_workflow_qa','REVEAL_S3_PREFIX':'qa/'},
            'secrets':dict.fromkeys(names,'configured-qa-secret')},
        'prod':{'secrets':dict.fromkeys(names,'configured-prod-secret')}}
    (directory/'service.yaml').write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(module,'ROOT',tmp_path)
    return runtime


def test_absent_records_disable_keys_despite_shared_local_or_legacy_values(cloud):
    for env in ('qa','prod'):
        module.prepare(env)
        backend=module.read_env(cloud/f'{env}-backend.env')
        secret=json.loads((cloud/f'{env}-backend-secret.json').read_text())
        frontend=module.read_env(cloud/f'{env}-frontend.env')
        assert all(backend[key]==secret[key]=='' for key in KEY_NAMES)
        assert not set(KEY_NAMES)&frontend.keys()
        assert (cloud/f'{env}-backend-secret.json').stat().st_mode & 0o777 == 0o600


def test_qa_only_record_never_enables_production_or_changes_other_credentials(cloud):
    (cloud/'qa-api-key-config.json').write_text(json.dumps(QA))
    qa_summary=module.prepare('qa');module.prepare('prod')
    qa=module.read_env(cloud/'qa-backend.env');prod=module.read_env(cloud/'prod-backend.env')
    assert {name:qa[name] for name in KEY_NAMES}==QA
    assert all(prod[name]=='' for name in KEY_NAMES)
    assert qa['REVEAL_MYSQL_PASSWORD']==prod['REVEAL_MYSQL_PASSWORD']=='synthetic-database'
    assert qa['REVEAL_GATEWAY_SECRET']!=prod['REVEAL_GATEWAY_SECRET']
    assert not any(value in json.dumps(qa_summary) for value in QA.values())
    assert not set(KEY_NAMES)&module.read_env(cloud/'qa-frontend.env').keys()


def test_distinct_environment_records_remain_distinct_and_idempotent(cloud):
    for env,pair in [('qa',QA),('prod',PROD)]:
        (cloud/f'{env}-api-key-config.json').write_text(json.dumps(pair))
        module.prepare(env)
        before=(cloud/f'{env}-backend-secret.json').read_bytes()
        module.prepare(env)
        assert (cloud/f'{env}-backend-secret.json').read_bytes()==before
        secret=json.loads(before)
        assert {key:secret[key] for key in KEY_NAMES}==pair


@pytest.mark.parametrize('record',[None,[],{}, {'REVEAL_API_KEY_SHA256':'a'*64},
    dict(QA,REVEAL_API_KEY_USER_ID=''),dict(QA,REVEAL_API_KEY_SHA256=''),
    dict(QA,REVEAL_API_KEY_SHA256='rvl_RAW_BEARER_IS_NEVER_CONFIGURATION'),
    dict(QA,REVEAL_API_KEY_USER_ID='not-a-user'),dict(QA,REVEAL_API_KEY_USER_ID=None),
    dict(QA,raw_key='never-accepted')])
def test_malformed_record_fails_before_generated_backend_or_frontend_files(cloud,record):
    (cloud/'qa-api-key-config.json').write_text(json.dumps(record))
    with pytest.raises(ValueError,match='API key configuration'):module.prepare('qa')
    assert not (cloud/'qa-backend.env').exists()
    assert not (cloud/'qa-backend-secret.json').exists()
    assert not (cloud/'qa-frontend.env').exists()


def test_bad_json_oversize_and_symlink_records_fail_closed(cloud):
    path=cloud/'qa-api-key-config.json'
    for raw in ['not-json',' '*4097]:
        path.write_text(raw)
        with pytest.raises(ValueError):module.prepare('qa')
    path.unlink();path.symlink_to(cloud/'missing')
    with pytest.raises(ValueError,match='regular private file'):module.prepare('qa')


def test_existing_credential_guard_requires_explicit_pair_reconciliation(cloud):
    module.prepare('qa')
    before=(cloud/'qa-backend-secret.json').read_bytes()
    backend=(cloud/'qa-backend.env').read_bytes()
    (cloud/'qa-api-key-config.json').write_text(json.dumps(QA))
    with pytest.raises(ValueError,match='reconcile'):module.prepare('qa')
    assert (cloud/'qa-backend-secret.json').read_bytes()==before
    assert (cloud/'qa-backend.env').read_bytes()==backend
    # Only an explicit private reconciliation admits the new pair.
    reconciled=json.loads(before);reconciled.update(QA)
    (cloud/'qa-backend-secret.json').write_text(json.dumps(reconciled))
    module.prepare('qa')
    assert {key:module.read_env(cloud/'qa-backend.env')[key] for key in KEY_NAMES}==QA


def test_empty_pair_record_is_explicitly_disabled(cloud):
    (cloud/'qa-api-key-config.json').write_text(json.dumps(dict.fromkeys(KEY_NAMES,'')))
    module.prepare('qa')
    assert all(module.read_env(cloud/'qa-backend.env')[key]=='' for key in KEY_NAMES)
