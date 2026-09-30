"""Shared-platform routing must preserve the local API's security and contract."""
from contextlib import asynccontextmanager
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt
import pytest

from reveal_backend import app as api
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.service_routing import mount_service


PREFIX = '/api/reveal'


@pytest.fixture
def service(monkeypatch, tmp_path):
    repository = Repository(str(tmp_path / 'application.sqlite'))
    repository.migrate()
    monkeypatch.setattr(api, 'repo', repository)
    monkeypatch.setattr(api, 'catalog', SimpleNamespace(load=lambda: None, dismech_import='dismech',
        gaps={}, mapping_run='mapping', factors={}, embedding_run='embedding'))
    monkeypatch.setenv('REVEAL_GATEWAY_SECRET', 's' * 40)
    monkeypatch.setenv('REVEAL_GATEWAY_SERVICE_TOKEN', 't' * 40)
    monkeypatch.setenv('REVEAL_GATEWAY_ISSUER', 'reveal-nextjs')
    monkeypatch.setenv('REVEAL_GATEWAY_AUDIENCE', 'reveal-api')
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem')
    monkeypatch.setenv('REVEAL_JOB_TRANSPORT', 'database')
    with TestClient(mount_service(api.app, PREFIX)) as client:
        response = client.post(PREFIX + '/internal/v1/principals/anonymous', json={},
            headers={'Authorization': 'Bearer ' + 't' * 40, 'Idempotency-Key': uid()})
        assert response.status_code == 201, response.text
        owner = response.json()['user_id']
        token = jwt.encode({'sub': owner, 'principal_kind': 'anonymous', 'iss': 'reveal-nextjs',
            'aud': 'reveal-api', 'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid()},
            's' * 40, algorithm='HS256')
        yield SimpleNamespace(client=client, repo=repository, owner=owner,
            headers={'Authorization': 'Bearer ' + token})


def test_platform_health_readiness_and_local_routes(service):
    for path in ('/health', '/healthz', '/readyz', '/health/ready'):
        assert service.client.get(PREFIX + path).status_code == 200
        assert service.client.get(path).status_code == 404
    assert service.client.get('/api/reveal-other/health').status_code == 404
    assert mount_service(api.app, '') is api.app
    with TestClient(api.app) as local:
        assert local.get('/healthz').json() == {'status': 'ok'}
        assert local.get('/readyz').json()['status'] == 'ready'


def test_prefixed_gateway_and_principal_routes_keep_separate_credentials(service):
    client = service.client
    assert client.get(PREFIX + '/v1/me').status_code == 401
    me = client.get(PREFIX + '/v1/me', headers=service.headers)
    assert me.status_code == 200, me.text
    assert me.json()['user_id'] == service.owner
    assert client.post(PREFIX + '/internal/v1/principals/anonymous', json={},
        headers=service.headers).status_code == 403
    assert client.get('/v1/me', headers=service.headers).status_code == 404


def test_prefixed_contract_validation_and_publication_cache_policy(service):
    for suffix in ('?limit=0', '?limit=101', '?gap_id=not-a-dapper-id'):
        response = service.client.get(PREFIX + '/v1/accounts' + suffix, headers=service.headers)
        assert response.status_code == 422, response.text
        assert response.json()['code'] == 'INVALID_QUERY'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert 'Authorization' in response.headers['Vary']
    response = service.client.get(PREFIX + '/v1/accounts?limit=1', headers=service.headers)
    assert response.status_code == 200, response.text
    assert response.json()['items'] == []
    assert response.headers['Cache-Control'] == 'private, no-store'


def test_prefixed_docs_and_openapi_use_platform_origin_without_changing_contract(service):
    for path in ('/docs', '/redoc'):
        response = service.client.get(PREFIX + path)
        assert response.status_code == 200
        assert PREFIX + '/openapi.json' in response.text
        assert service.client.get(path).status_code == 404
    schema = service.client.get(PREFIX + '/openapi.json').json()
    assert schema['servers'] == [{'url': PREFIX}]
    assert '/v1/me' in schema['paths']
    assert PREFIX + '/v1/me' not in schema['paths']
    assert api.CONTRACT['servers'][0]['url'] != PREFIX
    assert service.client.get('/openapi.json').status_code == 404
    redirect = service.client.get(PREFIX + '/health/', follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers['location'] == 'http://testserver' + PREFIX + '/health'


def test_prefixed_sse_still_authorizes_and_replays_the_requested_job(service):
    job_id = uid()
    with service.repo.transaction() as tx:
        tx.put('job', job_id, service.owner, {'id': job_id, 'status': 'succeeded'})
        tx.put('event', job_id + ':000000000001', service.owner,
            {'id': '1', 'job_id': job_id, 'event_type': 'completed'})
    path = PREFIX + '/v1/jobs/' + job_id + '/events'
    assert service.client.get(path, headers={'Accept': 'text/event-stream'}).status_code == 401
    response = service.client.get(path, headers={**service.headers, 'Accept': 'text/event-stream'})
    assert response.status_code == 200, response.text
    assert response.headers['Content-Type'].startswith('text/event-stream')
    assert response.headers['X-Accel-Buffering'] == 'no'
    assert 'id: 1\nevent: completed\n' in response.text
    replay = service.client.get(path, headers={**service.headers, 'Accept': 'text/event-stream', 'Last-Event-ID': '1'})
    assert replay.status_code == 200 and not replay.content


def test_prefixed_artifact_redirect_keeps_signed_s3_url_and_ownership(service, monkeypatch):
    checksum = 'a' * 64
    reference = {'sha256': checksum, 'key': 'prod/artifacts/' + checksum}
    with service.repo.transaction() as tx:
        tx.put('artifact', digest([service.owner, checksum]), service.owner,
            {'storage': reference, 'file': {'filename': 'evidence.json', 'mime_type': 'application/json'}})
    signed_url = 'https://artifacts.s3.us-east-1.amazonaws.com/prod/artifacts/' + checksum + '?signature=test'
    calls = []
    def download(*args):
        calls.append(args)
        return signed_url
    monkeypatch.setattr('reveal_backend.artifact_store.store', lambda: SimpleNamespace(download_url=download))
    path = PREFIX + '/v1/artifacts/' + checksum
    denied = service.client.get(path, follow_redirects=False)
    assert denied.status_code == 404
    assert not calls
    response = service.client.get(path, headers=service.headers, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'] == signed_url
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert calls == [(reference, 'evidence.json', 'application/json')]


def test_mount_retains_lifespan_and_normalizes_one_trailing_slash():
    events = []
    @asynccontextmanager
    async def lifespan(application):
        assert application is inner
        events.append('start')
        yield
        events.append('stop')
    inner = FastAPI(lifespan=lifespan)
    with TestClient(mount_service(inner, PREFIX + '/')) as client:
        assert client.get(PREFIX + '/openapi.json').json()['servers'] == [{'url': PREFIX}]
        assert events == ['start']
    assert events == ['start', 'stop']


def test_configured_application_starts_with_only_prefixed_routes():
    result = subprocess.run([sys.executable, '-c', """
from fastapi.testclient import TestClient
from reveal_backend.app import app
with TestClient(app) as client:
    assert client.get('/api/reveal/health').json() == {'status': 'ok'}
    assert client.get('/health').status_code == 404
    assert client.get('/api/reveal/openapi.json').json()['servers'] == [{'url': '/api/reveal'}]
"""], env={**os.environ, 'SERVICE_PATH_PREFIX': PREFIX}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('prefix', ['api/reveal', '/', '//api/reveal', '/api/../reveal', '/api/reveal?x=1', '/api/%72eveal'])
def test_invalid_service_prefix_fails_startup(prefix):
    with pytest.raises(ValueError, match='SERVICE_PATH_PREFIX'):
        mount_service(api.app, prefix)
