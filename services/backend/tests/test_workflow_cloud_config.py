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
ADMIN_KEY_NAMES=('REVEAL_ADMIN_READ_API_KEY_SHA256','REVEAL_ADMIN_READ_API_KEY_ID')
ADMIN_QA=dict(zip(ADMIN_KEY_NAMES, ('e'*64,'55555555-5555-4555-8555-555555555555')))
QA={'REVEAL_API_KEY_SHA256':'a'*64,'REVEAL_API_KEY_USER_ID':'11111111-1111-4111-8111-111111111111'}
PROD={'REVEAL_API_KEY_SHA256':'b'*64,'REVEAL_API_KEY_USER_ID':'22222222-2222-4222-8222-222222222222'}


@pytest.fixture
def cloud(tmp_path,monkeypatch):
    runtime=tmp_path/'.runtime/workflow';runtime.mkdir(parents=True)
    baseline=tmp_path/'.runtime/deployment';baseline.mkdir()
    (baseline/'backend.env').write_text('REVEAL_MYSQL_PASSWORD=synthetic-database\n'
        'REVEAL_API_KEY_SHA256='+('d'*64)+'\nREVEAL_API_KEY_USER_ID=44444444-4444-4444-8444-444444444444\n'
        'REVEAL_ADMIN_READ_API_KEY_SHA256='+('e'*64)+'\nREVEAL_ADMIN_READ_API_KEY_ID=55555555-5555-4555-8555-555555555555\n')
    # Shared local config must never be used to enable cloud keys.
    (tmp_path/'.env').write_text('UPSTASH_VECTOR_REST_TOKEN=synthetic-vector\n'
        'REVEAL_API_KEY_SHA256='+('c'*64)+'\nREVEAL_API_KEY_USER_ID=33333333-3333-4333-8333-333333333333\n'
        'REVEAL_ADMIN_READ_API_KEY_SHA256='+('f'*64)+'\nREVEAL_ADMIN_READ_API_KEY_ID=66666666-6666-4666-8666-666666666666\n')
    directory=tmp_path/'deploy/dig';directory.mkdir(parents=True)
    names=('REVEAL_MYSQL_PASSWORD','REVEAL_GATEWAY_SECRET','REVEAL_GATEWAY_SERVICE_TOKEN',*KEY_NAMES)
    manifest={'env':{'NEXTAUTH_URL':'https://frontend.invalid','REVEAL_GATEWAY_ISSUER':'reveal-nextjs',
        'REVEAL_GATEWAY_AUDIENCE':'reveal-api','REVEAL_S3_PREFIX':'prod/','REVEAL_APPLICATION_TABLE_PREFIX':'reveal'},
        'qa':{'env':{'REVEAL_APPLICATION_TABLE_PREFIX':'reveal_workflow_qa','REVEAL_S3_PREFIX':'qa/'},
            'secrets':dict.fromkeys((*names,*ADMIN_KEY_NAMES),'configured-qa-secret')},
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
        if env=='qa': assert all(backend[key]==secret[key]=='' for key in ADMIN_KEY_NAMES)
        else: assert not set(ADMIN_KEY_NAMES)&backend.keys() and not set(ADMIN_KEY_NAMES)&secret.keys()
        assert not set(ADMIN_KEY_NAMES)&frontend.keys()
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


def write_admin_record(cloud, values, environment='qa'):
    path=cloud/f'{environment}-admin-read-api-key-config.json'
    path.write_text(json.dumps(values));path.chmod(0o600)
    return path


def test_admin_qa_record_is_separate_from_owner_key_and_never_enables_production(cloud):
    write_admin_record(cloud,ADMIN_QA)
    (cloud/'qa-api-key-config.json').write_text(json.dumps(QA))
    summary=module.prepare('qa');module.prepare('prod')
    qa=module.read_env(cloud/'qa-backend.env');prod=module.read_env(cloud/'prod-backend.env')
    assert {key:qa[key] for key in ADMIN_KEY_NAMES}==ADMIN_QA
    assert {key:qa[key] for key in KEY_NAMES}==QA
    assert not set(ADMIN_KEY_NAMES)&prod.keys()
    assert not set(ADMIN_KEY_NAMES)&module.read_env(cloud/'qa-frontend.env').keys()
    assert not any(value in json.dumps(summary) for value in ADMIN_QA.values())
    before=(cloud/'qa-backend-secret.json').read_bytes();module.prepare('qa')
    assert (cloud/'qa-backend-secret.json').read_bytes()==before


def test_admin_activation_requires_explicit_saved_secret_reconciliation(cloud):
    module.prepare('qa')
    before=(cloud/'qa-backend-secret.json').read_bytes()
    backend=(cloud/'qa-backend.env').read_bytes()
    write_admin_record(cloud,ADMIN_QA)
    with pytest.raises(ValueError,match='reconcile'):module.prepare('qa')
    assert (cloud/'qa-backend-secret.json').read_bytes()==before
    assert (cloud/'qa-backend.env').read_bytes()==backend
    reconciled=json.loads(before);reconciled.update(ADMIN_QA)
    (cloud/'qa-backend-secret.json').write_text(json.dumps(reconciled))
    module.prepare('qa')
    assert {key:module.read_env(cloud/'qa-backend.env')[key] for key in ADMIN_KEY_NAMES}==ADMIN_QA


@pytest.mark.parametrize('record',[None,[],{}, {ADMIN_KEY_NAMES[0]:'a'*64},
    dict(ADMIN_QA,REVEAL_ADMIN_READ_API_KEY_ID=''),dict(ADMIN_QA,REVEAL_ADMIN_READ_API_KEY_SHA256=''),
    dict(ADMIN_QA,REVEAL_ADMIN_READ_API_KEY_SHA256='rvl_admin_RAW_BEARER'),
    dict(ADMIN_QA,REVEAL_ADMIN_READ_API_KEY_ID='not-a-uuid'),dict(ADMIN_QA,REVEAL_ADMIN_READ_API_KEY_ID=None),
    dict(ADMIN_QA,scope='admin:*'),dict(ADMIN_QA,raw_key='never-accepted')])
def test_admin_malformed_config_cannot_generate_deployment_files(cloud,record):
    write_admin_record(cloud,record)
    with pytest.raises(ValueError,match='admin read API key configuration'):module.prepare('qa')
    assert not (cloud/'qa-backend.env').exists()
    assert not (cloud/'qa-backend-secret.json').exists()
    assert not (cloud/'qa-frontend.env').exists()
    assert not (cloud/'qa-session-keys.json').exists()


def test_admin_nonprivate_invalid_json_oversize_and_symlink_files_refused(cloud):
    path=write_admin_record(cloud,ADMIN_QA)
    path.chmod(0o644)
    with pytest.raises(ValueError,match='private'):module.prepare('qa')
    path.chmod(0o600)
    for raw in ['not-json',' '*4097]:
        path.write_text(raw)
        with pytest.raises(ValueError,match='private'):module.prepare('qa')
    path.unlink();path.symlink_to(cloud/'missing')
    with pytest.raises(ValueError,match='regular private file'):module.prepare('qa')


def test_admin_empty_config_disables_and_production_requires_manifest_references(cloud):
    write_admin_record(cloud,dict.fromkeys(ADMIN_KEY_NAMES,''))
    module.prepare('qa')
    assert all(module.read_env(cloud/'qa-backend.env')[key]=='' for key in ADMIN_KEY_NAMES)
    write_admin_record(cloud,ADMIN_QA,environment='prod')
    with pytest.raises(ValueError,match='both environment secret references'):module.prepare('prod')
    assert not (cloud/'prod-backend-secret.json').exists()
    assert not (cloud/'prod-session-keys.json').exists()


def test_admin_production_configuration_requires_its_own_record_even_when_references_exist(cloud):
    path=cloud.parents[1]/'deploy/dig/service.yaml'
    manifest=yaml.safe_load(path.read_text())
    manifest['prod']['secrets'].update(dict.fromkeys(ADMIN_KEY_NAMES,'configured-prod-secret'))
    path.write_text(yaml.safe_dump(manifest))
    production=dict(zip(ADMIN_KEY_NAMES,('f'*64,'66666666-6666-4666-8666-666666666666')))
    write_admin_record(cloud,ADMIN_QA);write_admin_record(cloud,production,environment='prod')
    module.prepare('qa');module.prepare('prod')
    assert {key:module.read_env(cloud/'qa-backend.env')[key] for key in ADMIN_KEY_NAMES}==ADMIN_QA
    assert {key:module.read_env(cloud/'prod-backend.env')[key] for key in ADMIN_KEY_NAMES}==production
    assert not set(ADMIN_KEY_NAMES)&module.read_env(cloud/'prod-frontend.env').keys()


def test_admin_cannot_use_only_one_secret_reference_or_plaintext_manifest_environment(cloud):
    path=cloud.parents[1]/'deploy/dig/service.yaml'
    manifest=yaml.safe_load(path.read_text())
    manifest['qa']['secrets'].pop(ADMIN_KEY_NAMES[1]);path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError,match='both environment secret references'):module.prepare('qa')
    manifest['qa']['secrets'][ADMIN_KEY_NAMES[1]]='qa-secret'
    manifest['qa']['env'].update(ADMIN_QA);path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError,match='only in environment secrets'):module.prepare('qa')


def test_deployment_manifest_enables_only_qa_admin_read_secret_plumbing():
    manifest=yaml.safe_load((ROOT/'deploy/dig/service.yaml').read_text())
    assert set(ADMIN_KEY_NAMES)<=manifest['qa']['secrets'].keys()
    assert not set(ADMIN_KEY_NAMES)&manifest['prod']['secrets'].keys()
    assert all(':cyaka/reveal/workflow-qa-' in manifest['qa']['secrets'][name] for name in ADMIN_KEY_NAMES)
