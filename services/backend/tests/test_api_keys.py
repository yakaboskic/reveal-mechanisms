"""Opaque credentials keep the same principal, ownership and durable API rules."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import jwt
import pytest

from reveal_backend import api_keys, app as api, auth, jobs, workspace_events as events
from reveal_backend.repository import Repository, digest, now, uid

KEY = 'rvl_' + 'A' * 43
ROTATED = 'rvl_' + 'B' * 43
HASH = hashlib.sha256(KEY.encode()).hexdigest()
EMPTY = {'source_gap': None, 'eaggl_anchors': [], 'dismissed_source_ids': [],
    'mechanism_subquery': '', 'model': 'cfde-inc-v2', 'selected_kgs': []}


@pytest.fixture
def service(monkeypatch, tmp_path):
    env = {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_SERVICE_TOKEN': 't' * 40,
        'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api',
        'REVEAL_API_KEY_SHA256': HASH, 'REVEAL_API_KEY_USER_ID': uid(),
        'REVEAL_ARTIFACT_STORE': 'filesystem', 'REVEAL_JOB_TRANSPORT': 'database',
        'REVEAL_MAX_ACTIVE_JOBS': '2', 'REVEAL_ANONYMOUS_ANALYSES_PER_DAY': '5',
        'REVEAL_NOTIFICATION_NAMESPACE': 'api-key-test', 'REVEAL_SSE_WINDOW_SECONDS': '240',
        'REVEAL_NOTIFICATION_REDIS_URL': '', 'REVEAL_NOTIFICATION_REDIS_REST_URL': '',
        'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': '', 'REVEAL_NOTIFICATION_REDIS_REST_TOKEN': ''}
    for key, value in env.items(): monkeypatch.setenv(key, value)
    repo = Repository(str(tmp_path / 'app.sqlite')); repo.migrate()
    owner, other = env['REVEAL_API_KEY_USER_ID'], uid()
    with repo.transaction() as tx:
        for identity in (owner, other):
            me = {**api.fresh_principal('anonymous'), 'user_id': identity}
            tx.put('principal', identity, identity, {'me': me, 'retired': False})
    catalog = SimpleNamespace(load=lambda: None, validate_composer=lambda *args, **kwargs: None,
        selected=lambda reference: reference, dismech_import='source', gaps={}, mapping_run='mapping', factors={}, embedding_run='embedding')
    monkeypatch.setattr(api, 'repo', repo); monkeypatch.setattr(api, 'catalog', catalog)
    with TestClient(api.app) as client:
        yield SimpleNamespace(client=client, repo=repo, owner=owner, other=other,
            headers={'Authorization': 'Bearer ' + KEY})


def jwt_headers(owner, kind='anonymous', **extra):
    claims = {'sub': owner, 'principal_kind': kind, 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
        'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid(), **extra}
    return {'Authorization': 'Bearer ' + jwt.encode(claims, 's' * 40, algorithm='HS256')}


def seed_draft(service, owner=None):
    owner = owner or service.owner; identity = uid(); factor = 'factor:test'
    with service.repo.transaction() as tx:
        tx.put('draft', identity, owner, {'id': identity, 'version': 1,
            'composer': {'source_gap': {'source_id': 'dismech:test'}, 'eaggl_anchors': [{'reference': {'source_id': factor}}]},
            'created_at': now(), 'updated_at': now()})
        tx.put('draft_binding', identity, owner, {'dismech_import_id': 'source',
            'source_gap': {'object': {'id': 'dapper:KnowledgeGap.' + 'g' * 32, 'text': 'Bounded test'}, 'attachments': []},
            'selections': {factor: {'record': {'object': {'id': 'dapper:Mechanism.' + 'm' * 32, 'name': 'Test factor'}},
                'binding': {'source_id': factor, 'reference_generation_id': 'a' * 64}}}})
    return identity


def test_key_and_jwt_resolve_same_existing_principal_without_provisioning(service):
    key = service.client.get('/v1/me', headers=service.headers)
    signed = service.client.get('/v1/me', headers=jwt_headers(service.owner))
    assert key.status_code == signed.status_code == 200
    assert key.json() == signed.json()
    assert key.json()['principal_kind'] == 'anonymous'
    with service.repo.read_transaction() as tx: assert len(tx.list('principal')) == 2
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner, exp=int(time.time())-10)).status_code == 401
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner, kind='registered')).status_code == 401


def test_draft_and_analysis_submission_preserve_idempotency_quota_and_owner(service, monkeypatch):
    headers = {**service.headers, 'Idempotency-Key': uid()}
    created = service.client.post('/v1/drafts', json={'composer': EMPTY}, headers=headers)
    assert created.status_code == 201, created.text
    assert service.client.post('/v1/drafts', json={'composer': EMPTY}, headers=headers).json() == created.json()
    identity = created.json()['id']
    edited = service.client.patch('/v1/drafts/' + identity, json={'expected_version': 1, 'name': 'Colleague draft'},
        headers={**service.headers, 'Idempotency-Key': uid()})
    assert edited.status_code == 200, edited.text
    draft = seed_draft(service); headers = {**service.headers, 'Idempotency-Key': uid()}
    body = {'kind': 'analysis', 'draft_id': draft, 'draft_version': 1}
    first = service.client.post('/v1/jobs', json=body, headers=headers)
    assert first.status_code == 202, first.text
    monkeypatch.setenv('REVEAL_MAX_ACTIVE_JOBS', '1')
    assert service.client.post('/v1/jobs', json=body, headers=headers).json() == first.json()
    conflict = service.client.post('/v1/jobs', json={**body, 'draft_version': 2}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    limited = service.client.post('/v1/jobs', json=body, headers={**service.headers, 'Idempotency-Key': uid()})
    assert limited.status_code == 429 and limited.json()['code'] == 'ANONYMOUS_QUOTA_EXCEEDED'
    with service.repo.read_transaction() as tx:
        assert len(tx.list('job')) == len(tx.list('queue')) == len(tx.list('request')) == 1
        assert tx.list('job')[0]['owner'] == service.owner
        assert tx.list('request')[0]['data']['attribution']['principal_kind'] == 'anonymous'
        assert KEY not in json.dumps([row['data'] for kind in ('job', 'queue', 'request', 'idempotency') for row in tx.list(kind)])


def test_anonymous_daily_limit_and_publication_restriction_remain(service, monkeypatch):
    draft = seed_draft(service); monkeypatch.setenv('REVEAL_ANONYMOUS_ANALYSES_PER_DAY', '0')
    response = service.client.post('/v1/jobs', json={'kind': 'analysis', 'draft_id': draft, 'draft_version': 1},
        headers={**service.headers, 'Idempotency-Key': uid()})
    assert response.status_code == 429 and response.json()['code'] == 'ANONYMOUS_QUOTA_EXCEEDED'
    with service.repo.read_transaction() as tx:
        with pytest.raises(auth.Problem) as error: auth.publication_principal(tx, service.headers['Authorization'], 'public')
    assert error.value.status == 403


def test_cross_owner_drafts_jobs_cancel_artifact_and_sse_are_denied(service):
    draft = seed_draft(service, service.other)
    with service.repo.transaction() as tx:
        job = jobs.enqueue(tx, service.other, 'analysis')
        tx.put('artifact', digest([service.other, 'a' * 64]), service.other, {'private': True})
    assert service.client.get('/v1/drafts/' + draft, headers=service.headers).status_code == 404
    assert service.client.post('/v1/jobs', json={'kind': 'analysis', 'draft_id': draft, 'draft_version': 1},
        headers={**service.headers, 'Idempotency-Key': uid()}).status_code == 404
    for path in ('/v1/jobs/' + job['id'], '/v1/jobs/' + job['id'] + '/events', '/v1/artifacts/' + 'a' * 64):
        assert service.client.get(path, headers={**service.headers, 'Accept': 'text/event-stream'}).status_code == 404
    assert service.client.post('/v1/jobs/' + job['id'] + '/cancel', headers=service.headers).status_code == 404
    assert service.client.get('/v1/jobs', headers=service.headers).json()['items'] == []
    cursor = events.encode_cursor(service.other, {'workspace': 0, 'public': 0})
    assert service.client.get('/v1/me/workspace/events', headers={**service.headers, 'Last-Event-ID': cursor}).status_code == 400


@pytest.mark.parametrize('token', [None, '', 'rvl_', 'rvl_'+'A'*42, 'rvl_'+'A'*44, 'rvl_'+'!'*43, 'rvl_'+'é'*43, ROTATED])
def test_invalid_keys_fail_before_any_jwt_fallback(service, token):
    # Non-ASCII bytes are tested at the credential boundary, not HTTP's ASCII header encoder.
    with service.repo.read_transaction() as tx, patch.object(auth, 'decode_assertion', side_effect=AssertionError('key cannot become JWT')):
        with pytest.raises(auth.Problem) as error:
            auth.principal(tx, None if token is None else 'Bearer ' + token)
    assert error.value.status == 401


def test_rotation_disable_and_invalid_public_credential(service, monkeypatch):
    monkeypatch.setenv('REVEAL_API_KEY_SHA256', hashlib.sha256(ROTATED.encode()).hexdigest())
    assert service.client.get('/v1/me', headers=service.headers).status_code == 401
    assert service.client.get('/v1/me', headers={'Authorization': 'Bearer ' + ROTATED}).json()['user_id'] == service.owner
    assert service.client.get('/v1/analysis-outcomes/unknown', headers=service.headers).status_code == 401
    monkeypatch.setenv('REVEAL_API_KEY_SHA256', ''); monkeypatch.setenv('REVEAL_API_KEY_USER_ID', '')
    assert api_keys.configuration() is None
    assert service.client.get('/v1/me', headers={'Authorization': 'Bearer ' + ROTATED}).status_code == 401
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner)).status_code == 200


@pytest.mark.parametrize('fingerprint,owner', [('x'*64, 'owner'), ('A'*64, 'owner'),
    (HASH, ''), ('', '11111111-1111-4111-8111-111111111111'),
    (HASH, '11111111111141118111111111111111'), (HASH, 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'),
    (HASH, ' 11111111-1111-4111-8111-111111111111'), (None, None)])
def test_malformed_configuration_fails_closed_without_values(service, monkeypatch, fingerprint, owner):
    mapping = {'REVEAL_API_KEY_SHA256': fingerprint, 'REVEAL_API_KEY_USER_ID': owner}
    with pytest.raises(auth.Problem) as error: api_keys.configuration(mapping)
    assert error.value.status == 503 and error.value.detail == 'API key authentication is unavailable.'
    monkeypatch.setenv('REVEAL_API_KEY_SHA256', str(fingerprint)); monkeypatch.setenv('REVEAL_API_KEY_USER_ID', str(owner))
    for path in ('/v1/me', '/readyz'):
        response = service.client.get(path, headers=service.headers)
        assert response.status_code == 503 and response.json()['code'] == 'API_KEY_CONFIGURATION_INVALID'
        assert HASH not in response.text and KEY not in response.text and service.owner not in response.text
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner)).status_code == 200


def test_configuration_is_pure_and_accepts_disabled_or_exact_pair(service):
    assert api_keys.configuration({}) is None
    assert api_keys.configuration({'REVEAL_API_KEY_SHA256': ' ', 'REVEAL_API_KEY_USER_ID': '\t'}) is None
    assert api_keys.configuration({'REVEAL_API_KEY_SHA256': HASH, 'REVEAL_API_KEY_USER_ID': service.owner}) == (HASH, service.owner)
    assert service.client.get('/readyz').status_code == 200


@pytest.mark.parametrize('state', ['missing', 'retired', 'expired'])
def test_principal_lifecycle_is_authoritative_without_auto_provision(service, monkeypatch, state):
    if state == 'missing': monkeypatch.setenv('REVEAL_API_KEY_USER_ID', uid())
    else:
        with service.repo.transaction() as tx:
            row = tx.get('principal', service.owner)['data']
            if state == 'retired': row['retired'] = True
            else: row['me']['workspace_expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('principal', service.owner, service.owner, row)
    assert service.client.get('/v1/me', headers=service.headers).status_code == 401
    with service.repo.read_transaction() as tx: assert len(tx.list('principal')) == 2


def test_same_id_upgrade_uses_current_kind_but_retired_transfer_does_not_follow(service):
    with service.repo.transaction() as tx:
        row = tx.get('principal', service.owner)['data']; row['me'].update(principal_kind='registered', workspace_expires_at=None)
        tx.put('principal', service.owner, service.owner, row)
    assert service.client.get('/v1/me', headers=service.headers).json()['principal_kind'] == 'registered'
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner)).status_code == 401
    assert service.client.get('/v1/me', headers=jwt_headers(service.owner, 'registered')).status_code == 200
    with service.repo.transaction() as tx:
        tx.transfer(service.owner, service.other)
        row['retired'] = True; tx.put('principal', service.owner, service.owner, row)
    assert service.client.get('/v1/me', headers=service.headers).status_code == 401


def test_api_key_never_authorizes_internal_admin_or_identity_proofs(service):
    for path in ('/internal/v1/principals/anonymous', '/internal/v1/principals/resolve', '/internal/v1/principals/claim'):
        assert service.client.post(path, json={}, headers=service.headers).status_code == 403
    for path in ('/internal/v1/admin/telemetry', '/internal/v1/admin/jobs/test', '/internal/v1/admin/tables/records'):
        assert service.client.get(path, headers={**service.headers, 'x-reveal-admin-assertion': KEY}).status_code == 403
    with pytest.raises(auth.Problem) as error: auth.decode_assertion(KEY, 'verified_identity')
    assert error.value.status == 401


def test_credential_deadline_preserves_jwt_expiry_and_bounds_key_streams(service, monkeypatch):
    monkeypatch.setenv('REVEAL_SSE_WINDOW_SECONDS', '99999')
    start = time.monotonic(); deadline = events.stream_deadline(service.headers['Authorization'])
    assert 239 <= deadline-start <= 241
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=12)).isoformat()
    assert 10 <= events.stream_deadline(service.headers['Authorization'], expiry)-start <= 13
    token = jwt_headers(service.owner, exp=int(time.time())+20)['Authorization']
    assert 18 <= events.stream_deadline(token)-start <= 21
    assert auth.credential_expiry(service.headers['Authorization']) is None


def test_key_job_sse_replays_authorized_events(service):
    with service.repo.transaction() as tx:
        job = jobs.enqueue(tx, service.owner, 'analysis'); jobs.cancel(tx, job)
    result = service.client.get('/v1/jobs/' + job['id'] + '/events',
        headers={**service.headers, 'Accept': 'text/event-stream', 'Last-Event-ID': '1'})
    assert result.status_code == 200 and 'id: 2' in result.text and 'id: 1' not in result.text


def test_workspace_stream_uses_idle_heartbeats_then_revokes_before_replay(service, monkeypatch):
    class Listener:
        calls = 0
        async def wait(self, timeout):
            self.calls += 1
            if self.calls == 1: raise asyncio.TimeoutError()
            monkeypatch.setenv('REVEAL_API_KEY_SHA256', hashlib.sha256(ROTATED.encode()).hexdigest())
            return 'changed'
    class Hub:
        @asynccontextmanager
        async def subscribe(self, scopes): yield Listener()
    async def run():
        response = await events.workspace_response(service.repo, SimpleNamespace(headers={'authorization': service.headers['Authorization']}))
        output = []; stream = response.body_iterator
        with patch.object(events, 'replay', wraps=events.replay) as replay:
            async for chunk in stream:
                output.append(chunk)
                if chunk == ': heartbeat\n\n': assert replay.call_count == 1
        assert replay.call_count == 2
        assert any('event: ready' in chunk for chunk in output)
        assert output[-1].startswith('event: access_revoked')
        assert KEY not in ''.join(output)
    with patch.object(events.redis_notifications, 'hub', return_value=Hub()): asyncio.run(run())


def test_workspace_stream_does_not_switch_owner_if_configuration_is_rebound(service, monkeypatch):
    class Listener:
        async def wait(self, timeout):
            monkeypatch.setenv('REVEAL_API_KEY_USER_ID', service.other)
            return 'changed'
    class Hub:
        @asynccontextmanager
        async def subscribe(self, scopes): yield Listener()
    async def run():
        response = await events.workspace_response(service.repo, SimpleNamespace(headers={'authorization': service.headers['Authorization']}))
        output = [chunk async for chunk in response.body_iterator]
        assert output[-1].startswith('event: access_revoked')
        assert service.other not in ''.join(output)
    with patch.object(events.redis_notifications, 'hub', return_value=Hub()): asyncio.run(run())
