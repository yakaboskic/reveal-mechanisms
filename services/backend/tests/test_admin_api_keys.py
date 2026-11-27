"""API keys minted in the admin console: stored as a SHA-256, usable at once, refused once revoked, no deploy."""
import hashlib
import json
import time

import jwt
import pytest

from reveal_backend import api_keys
from reveal_backend.repository import uid
from test_agent_jobs import service  # noqa: F401  (the agent-job API fixture: workflow transport, two principals)

ADMIN = 'admin@example.org'


def admin_headers(purpose='admin_api_keys', email=ADMIN):
    claims = {'sub': 'admin-console', 'principal_kind': 'registered', 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
              'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid(), 'purpose': purpose}
    if email is not None: claims['admin_email'] = email
    return {'Authorization': 'Bearer ' + 't' * 40, 'X-Reveal-Admin-Assertion': jwt.encode(claims, 's' * 40, algorithm='HS256')}


def mint(service, **body):
    return service.client.post('/internal/v1/admin/api-keys', json=body, headers=admin_headers())


def bearer(key):
    return {'Authorization': 'Bearer ' + key}


def agent_job(service, key):
    return service.client.post('/v1/jobs', json={'kind': 'agent', 'prompt': 'Say hello.'},
                               headers={**bearer(key), 'Idempotency-Key': uid()})


def test_a_minted_key_works_at_once_and_only_its_hash_is_stored(service):
    created = mint(service, label='reveal-claims demo')
    assert created.status_code == 201, created.text
    assert created.headers['cache-control'] == 'private, no-store'
    key, summary = created.json()['api_key'], created.json()['key']
    assert api_keys.well_formed(key) and summary['prefix'] == key[:8] and summary['created_by'] == ADMIN
    me = service.client.get('/v1/me', headers=bearer(key))
    assert me.status_code == 200 and me.json()['user_id'] == summary['user_id']
    # A new workspace: registered, named by the label, with no expiry.
    assert me.json()['principal_kind'] == 'registered' and me.json()['workspace_expires_at'] is None
    assert me.json()['display_name'] == 'reveal-claims demo'
    with service.repo.read_transaction() as tx:
        rows = tx.list(api_keys.KIND)
    assert [row['id'] for row in rows] == [hashlib.sha256(key.encode()).hexdigest()]
    assert key not in json.dumps(rows)
    listed = service.client.get('/internal/v1/admin/api-keys', headers=admin_headers()).json()
    assert [entry['key_id'] for entry in listed['keys']] == [summary['key_id']]
    assert key not in json.dumps(listed) and rows[0]['id'] not in json.dumps(listed)


def test_a_key_for_an_existing_workspace_acts_as_it(service):
    owner = service.users['registered']
    key = mint(service, label='owner', user_id=owner).json()['api_key']
    assert service.client.get('/v1/me', headers=bearer(key)).json()['user_id'] == owner
    assert mint(service, label='nobody', user_id=uid()).status_code == 422
    assert mint(service, label='bad id', user_id='not-a-uuid').status_code == 422


def test_revoking_a_key_refuses_it_and_ends_its_agent_job_grant(service):
    created = mint(service, label='agent', agent_jobs=True).json()
    key, key_id = created['api_key'], created['key']['key_id']
    assert agent_job(service, key).status_code == 202
    revoked = service.client.post(f'/internal/v1/admin/api-keys/{key_id}/revoke', headers=admin_headers())
    assert revoked.status_code == 200 and revoked.json()['key']['revoked_by'] == ADMIN
    assert service.client.get('/v1/me', headers=bearer(key)).status_code == 401
    assert agent_job(service, key).status_code == 401
    # Revoking again changes nothing; an unknown id is 404.
    again = service.client.post(f'/internal/v1/admin/api-keys/{key_id}/revoke', headers=admin_headers())
    assert again.json()['key']['revoked_at'] == revoked.json()['key']['revoked_at']
    assert service.client.post(f'/internal/v1/admin/api-keys/{uid()}/revoke', headers=admin_headers()).status_code == 404
    with service.repo.read_transaction() as tx:
        assert api_keys.agent_job_users(tx) == set()


def test_agent_jobs_need_the_grant(service):
    plain = mint(service, label='plain').json()['api_key']
    refused = agent_job(service, plain)
    assert refused.status_code == 403 and refused.json()['code'] == 'AGENT_JOBS_NOT_ENABLED'
    granted = mint(service, label='granted', agent_jobs=True).json()['api_key']
    assert agent_job(service, granted).status_code == 202


def test_only_an_admin_assertion_for_this_purpose_naming_an_admin_may_mint(service):
    body = {'label': 'x'}
    assert service.client.post('/internal/v1/admin/api-keys', json=body).status_code == 403
    for headers in (admin_headers('admin_settings'), admin_headers(email=None)):
        assert service.client.post('/internal/v1/admin/api-keys', json=body, headers=headers).status_code == 401
    assert service.client.get('/internal/v1/admin/api-keys', headers=admin_headers('admin_telemetry')).status_code == 401
    with service.repo.read_transaction() as tx:
        assert tx.list(api_keys.KIND) == []


@pytest.mark.parametrize('body', [{}, {'label': ''}, {'label': ' padded'}, {'label': 'x' * 65}, {'label': 'ok', 'agent_jobs': 'yes'},
                                  {'label': 'ok', 'extra': 1}, ['label']])
def test_malformed_requests_are_refused_and_mint_nothing(service, body):
    assert mint(service, **body).status_code == 422 if isinstance(body, dict) else \
        service.client.post('/internal/v1/admin/api-keys', json=body, headers=admin_headers()).status_code == 422
    with service.repo.read_transaction() as tx:
        assert tx.list(api_keys.KIND) == []


def test_an_unknown_or_malformed_key_is_401(service):
    assert service.client.get('/v1/me', headers=bearer('rvl_' + 'Z' * 43)).status_code == 401
    assert service.client.get('/v1/me', headers=bearer('rvl_short')).status_code == 401
