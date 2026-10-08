"""The durable pilot is isolated and cannot accidentally launch queue workers."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('durable_deployment', ROOT/'scripts/durable_deployment.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_default_stack_has_no_queue_consumers_or_self_hosted_redis():
    stack = yaml.safe_load((ROOT/'deploy/compose.yaml').read_text())
    assert set(stack['services']) == {'api', 'tools', 'bootstrap'}
    assert 'depends_on' not in stack['services']['api']
    assert 'redis' not in json.dumps(stack['services']['api'].get('healthcheck', {})).lower()
    assert stack['services']['api']['mem_limit'] == '4g'
    assert stack['services']['api']['stop_grace_period'] == '120s'
    assert stack['services']['api']['tmpfs'] == [
        '/work:rw,nosuid,nodev,size=1073741824,mode=1777',
        '/tmp:rw,nosuid,nodev,size=67108864,mode=1777',
    ]


@pytest.mark.parametrize('local_api_key', [False, True])
@pytest.mark.parametrize('frontend_port', [3000, 3100])
def test_prepare_separates_application_state_and_backend_secrets(tmp_path, monkeypatch, local_api_key, frontend_port):
    root = tmp_path
    runtime = root/'.runtime/workflow'
    runtime.mkdir(parents=True)
    baseline = root/'.runtime/deployment'; baseline.mkdir()
    (baseline/'backend.env').write_text('REVEAL_MYSQL_PASSWORD=db-secret\nREVEAL_REDIS_URL=redis://old-queue\nTYPESAFE_API_KEY=old-fixture-key\n'
        'REVEAL_API_KEY_SHA256='+('c'*64)+'\nREVEAL_API_KEY_USER_ID=33333333-3333-4333-8333-333333333333\n')
    assets=root/'.deployment-assets'; assets.mkdir()
    for name in ('dapper','dismech','rds-ca.pem'): (assets/name).touch()
    (root/'.env').write_text('UPSTASH_REDIS_REST_URL=https://redis.example\nUPSTASH_REDIS_REST_TOKEN=redis-secret\n'
        'UPSTASH_VECTOR_REST_URL=https://vector.example\nUPSTASH_VECTOR_REST_TOKEN=vector-secret\n'
        'QSTASH_TOKEN=live-token\nQSTASH_CURRENT_SIGNING_KEY=live-current\nQSTASH_NEXT_SIGNING_KEY=live-next\n'
        'TYPESAFE_API_KEY=current-fixture-key\n')
    if local_api_key:
        with (root/'.env').open('a') as output:
            output.write('REVEAL_API_KEY_SHA256='+('a'*64)+'\nREVEAL_API_KEY_USER_ID=11111111-1111-4111-8111-111111111111\n')
    (runtime/'qstash.log').write_text('QSTASH_URL=http://127.0.0.1:18080\nQSTASH_TOKEN=local-token\n'
        'QSTASH_CURRENT_SIGNING_KEY=local-current\nQSTASH_NEXT_SIGNING_KEY=local-next\n')
    monkeypatch.setattr(module,'ROOT',root); monkeypatch.setattr(module,'RUNTIME',runtime)
    monkeypatch.setattr(module,'storage_config',lambda path:({'REVEAL_S3_BUCKET':'test','REVEAL_S3_PREFIX':'local/'},'https://test.example/local/'))
    result=module.prepare(frontend_port=frontend_port)
    backend=module.read_env(runtime/'backend.env'); frontend=module.read_env(runtime/'frontend.env')
    assert backend['REVEAL_APPLICATION_TABLE_PREFIX']=='reveal_workflow_local'
    assert backend['REVEAL_PUBLIC_WEB_URL'] == backend['REVEAL_CANONICAL_URL'] == frontend['NEXTAUTH_URL'] == f'http://localhost:{frontend_port}'
    assert backend['REVEAL_JOB_TRANSPORT']=='workflow'
    assert backend['REVEAL_RETRIEVAL_BACKEND']=='upstash'
    assert backend['TMPDIR'] == backend['REVEAL_WORK_DIR'] == '/work'
    assert backend['REVEAL_MAX_SCRATCH_STEPS'] == '2'
    assert backend['REVEAL_WORKSPACE_MAX_BYTES'] == '268435456'
    assert backend['QSTASH_TOKEN']=='local-token'
    assert backend['QSTASH_URL']=='http://host.docker.internal:18080'
    assert backend['TYPESAFE_API_KEY'] == 'current-fixture-key'
    assert 'TYPESAFE_API_KEY' not in frontend
    assert 'current-fixture-key' not in json.dumps(result)
    assert 'REVEAL_REDIS_URL' not in backend
    assert backend['REVEAL_API_KEY_SHA256'] == ('a'*64 if local_api_key else '')
    assert backend['REVEAL_API_KEY_USER_ID'] == ('11111111-1111-4111-8111-111111111111' if local_api_key else '')
    assert not {'REVEAL_API_KEY_SHA256','REVEAL_API_KEY_USER_ID'} & frontend.keys()
    assert not any(k.startswith(('UPSTASH_','QSTASH_','AWS_','REVEAL_MYSQL')) for k in frontend)
    assert result['worker_services']==0
    assert (runtime/'backend.env').stat().st_mode & 0o777 == 0o600
    assert 'redis-secret' not in json.dumps(result)


def test_malformed_local_api_key_fails_before_config_preparation(tmp_path, monkeypatch):
    (tmp_path/'.env').write_text('REVEAL_API_KEY_SHA256='+('a'*64)+'\n')
    runtime=tmp_path/'.runtime/workflow'
    monkeypatch.setattr(module,'ROOT',tmp_path); monkeypatch.setattr(module,'RUNTIME',runtime)
    with pytest.raises(ValueError,match='Invalid local API key configuration pair'):
        module.prepare()
    assert not runtime.exists()


def test_platform_qa_isolates_authoritative_state_and_callbacks():
    config=yaml.safe_load((ROOT/'deploy/dig/service.yaml').read_text())
    assert config['env']['REVEAL_JOB_TRANSPORT']=='workflow'
    assert config['memory'] == 4096
    assert config['runtime']['stop_timeout_seconds'] == 120
    assert config['runtime']['deregistration_delay_seconds'] == 300
    assert {item['container_path']: item['size_mib'] for item in config['runtime']['tmpfs']} == {'/work': 1024, '/tmp': 64}
    assert config['env']['TMPDIR'] == config['env']['REVEAL_WORK_DIR'] == '/work'
    assert config['env']['REVEAL_MAX_SCRATCH_STEPS'] == '2'
    assert config['env']['REVEAL_WORKSPACE_MAX_BYTES'] == '268435456'
    assert config['qa']['env']['REVEAL_APPLICATION_TABLE_PREFIX']!='reveal'
    assert config['qa']['env']['REVEAL_MAX_ACTIVE_JOBS']=='5'
    assert config['qa']['env']['REVEAL_MAX_ACTIVE_BOXES']=='25'
    assert config['qa']['env']['REVEAL_MAX_RUNNING_JOBS']=='25'
    assert config['qa']['env']['REVEAL_WORKFLOW_STEPS_PER_RUN']=='6'
    assert config['qa']['env']['REVEAL_WORKFLOW_OBSERVE_INTERVAL_SECONDS']=='10'
    assert config['qa']['env']['REVEAL_AGENT_TIMEOUT_SECONDS']=='1800'
    assert 'REVEAL_MAX_ACTIVE_JOBS' not in config['env']
    assert 'REVEAL_MAX_ACTIVE_JOBS' not in config['prod'].get('env',{})
    assert config['env']['REVEAL_MAX_ACTIVE_BOXES']=='2'
    assert config['qa']['env']['REVEAL_S3_PREFIX']=='qa/'
    assert 'api-qa.' in config['qa']['env']['REVEAL_WORKFLOW_URL']
    assert config['qa']['env']['REVEAL_PUBLIC_API_URL'] == 'https://api-qa.hugeampkpnbi.org/api/reveal'
    required = {'REVEAL_MYSQL_PASSWORD', 'REVEAL_GATEWAY_SECRET', 'REVEAL_GATEWAY_SERVICE_TOKEN',
        'REVEAL_API_KEY_SHA256', 'REVEAL_API_KEY_USER_ID',
        'UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN',
        'UPSTASH_VECTOR_REST_URL', 'UPSTASH_VECTOR_REST_TOKEN', 'UPSTASH_VECTOR_WRITE_TOKEN',
        'QSTASH_TOKEN', 'QSTASH_CURRENT_SIGNING_KEY', 'QSTASH_NEXT_SIGNING_KEY'}
    for environment in ('qa', 'prod'):
        public_settings = {**config['env'], **config[environment].get('env', {})}
        assert public_settings['REVEAL_PUBLIC_WEB_URL'] == public_settings['REVEAL_CANONICAL_URL'] == public_settings['NEXTAUTH_URL']
        assert public_settings['REVEAL_PUBLIC_WEB_URL'].startswith('https://')
        expected_web = 'https://reveal-mechanisms-qa.vercel.app' if environment == 'qa' else 'https://reveal-mechanisms.vercel.app'
        assert public_settings['REVEAL_PUBLIC_WEB_URL'] == expected_web
        secrets = config[environment]['secrets']
        assert 'REVEAL_REDIS_URL' not in secrets
        assert required <= secrets.keys()
        for key in required:
            assert secrets[key].startswith('arn:aws:secretsmanager:')
            assert secrets[key].endswith(f':{key}::')
        assert 'REVEAL_REDIS_URL' not in {**config['env'], **config[environment].get('env', {})}
    assert config['qa']['secrets']['REVEAL_GATEWAY_SECRET'] != config['prod']['secrets']['REVEAL_GATEWAY_SECRET']
    for key in ('REVEAL_API_KEY_SHA256', 'REVEAL_API_KEY_USER_ID'):
        assert config['qa']['secrets'][key] != config['prod']['secrets'][key]
