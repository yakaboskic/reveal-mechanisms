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


# Budgets ----------------------------------------------------------------------------------------------------------

from reveal_backend import api_key_budget, job_metrics  # noqa: E402


def budgeted(service, monkeypatch, limit, period='total'):
    monkeypatch.setenv('REVEAL_MAX_ACTIVE_JOBS', '20')
    created = mint(service, label='budgeted', agent_jobs=True, budget={'limit_usd': limit, 'period': period}).json()
    return created['api_key'], created['key']['key_id']


def spend(service, key, budget_usd):
    return service.client.post('/v1/jobs', json={'kind': 'agent', 'prompt': 'Say hello.', 'limits': {'max_budget_usd': budget_usd}},
                               headers={**bearer(key), 'Idempotency-Key': uid()})


def finish(service, job_id, *costs):
    """The job's runs, each recorded as the runner records it, then the job finished."""
    for cost in costs:
        job_metrics.record(service.repo, {'id': job_id}, agent={'cost_usd': cost, 'estimated_cost_usd': None, 'budget_usd': 2.0})
    with service.repo.transaction() as tx:
        row = tx.get('job', job_id); tx.put('job', job_id, row['owner'], {**row['data'], 'status': 'succeeded'})


def listed(service, key_id):
    keys = service.client.get('/internal/v1/admin/api-keys', headers=admin_headers()).json()['keys']
    return next(entry for entry in keys if entry['key_id'] == key_id)


def test_a_budget_refuses_a_job_its_remainder_cannot_cover_and_writes_nothing(service, monkeypatch):
    key, key_id = budgeted(service, monkeypatch, 5)
    first, second = spend(service, key, 2), spend(service, key, 2)
    assert first.status_code == second.status_code == 202
    with service.repo.read_transaction() as tx: jobs_before = len(tx.list('job'))
    refused = spend(service, key, 2)
    assert refused.status_code == 402, refused.text
    body = refused.json()
    assert body['code'] == 'API_KEY_BUDGET_EXHAUSTED' and body['remaining_usd'] == 1.0 and body['job_reserve_usd'] == 2.0
    with service.repo.read_transaction() as tx: assert len(tx.list('job')) == jobs_before
    # The remainder still covers a smaller run.
    assert spend(service, key, 1).status_code == 202
    usage = listed(service, key_id)['usage']
    assert usage['spent_usd'] == 5.0 and usage['in_progress_usd'] == 5.0 and usage['remaining_usd'] == 0.0 and usage['jobs'] == 3


def test_a_finished_job_is_charged_what_its_runs_cost_retries_included(service, monkeypatch):
    key, key_id = budgeted(service, monkeypatch, 3)
    job = spend(service, key, 2).json()['id']
    finish(service, job, 0.25, 0.5)   # two runs: a retry
    usage = listed(service, key_id)['usage']
    assert usage['spent_usd'] == 0.75 and usage['in_progress_usd'] == 0 and usage['remaining_usd'] == 2.25
    assert spend(service, key, 2).status_code == 202
    assert spend(service, key, 0.5).status_code == 402


def test_a_run_that_reports_no_cost_is_charged_its_cap(service):
    job_id = uid()
    with service.repo.transaction() as tx: tx.put('job', job_id, service.users['registered'], {'id': job_id, 'kind': 'agent', 'status': 'running'})
    job_metrics.record(service.repo, {'id': job_id}, agent={'cost_usd': None, 'estimated_cost_usd': None, 'budget_usd': 2.0})
    with service.repo.read_transaction() as tx:
        assert api_key_budget.charged(tx.get('job_metrics', job_id)) == 2.0


def test_a_monthly_budget_counts_only_this_calendar_month(service, monkeypatch):
    key, key_id = budgeted(service, monkeypatch, 3, 'month')
    job = spend(service, key, 2).json()['id']
    finish(service, job, 2.0)
    assert spend(service, key, 2).status_code == 402
    with service.repo.transaction() as tx:   # the job was last month
        row = tx.get(api_key_budget.USAGE_KIND, key_id)
        row['data']['jobs'][job]['created_at'] = '2000-01-15T00:00:00Z'
        tx.put(api_key_budget.USAGE_KIND, key_id, row['owner'], row['data'])
    assert listed(service, key_id)['usage']['spent_usd'] == 0
    assert spend(service, key, 2).status_code == 202


def test_an_admin_can_change_or_remove_a_budget(service, monkeypatch):
    key, key_id = budgeted(service, monkeypatch, 1)
    assert spend(service, key, 2).status_code == 402
    path = f'/internal/v1/admin/api-keys/{key_id}/budget'
    raised = service.client.put(path, json={'budget': {'limit_usd': 10, 'period': 'month'}}, headers=admin_headers())
    assert raised.status_code == 200 and raised.json()['key']['budget'] == {'limit_usd': 10.0, 'period': 'month'}
    assert raised.json()['key']['budget_changed_by'] == ADMIN and raised.json()['key']['usage']['remaining_usd'] == 10.0
    assert spend(service, key, 2).status_code == 202
    removed = service.client.put(path, json={'budget': None}, headers=admin_headers())
    assert removed.json()['key']['budget'] is None and removed.json()['key']['usage']['remaining_usd'] is None
    for bad in ({'budget': {'limit_usd': 0, 'period': 'month'}}, {'budget': {'limit_usd': 5, 'period': 'week'}},
                {'budget': {'limit_usd': True, 'period': 'total'}}, {}):
        assert service.client.put(path, json=bad, headers=admin_headers()).status_code == 422
    assert service.client.put(path, json={'budget': None}, headers=admin_headers('admin_settings')).status_code == 401


def test_jobs_from_a_key_without_a_budget_are_still_counted(service, monkeypatch):
    monkeypatch.setenv('REVEAL_MAX_ACTIVE_JOBS', '20')
    created = mint(service, label='open', agent_jobs=True).json()
    assert spend(service, created['api_key'], 2).status_code == 202
    usage = listed(service, created['key']['key_id'])['usage']
    assert usage['spent_usd'] == 2.0 and usage['limit_usd'] is None and usage['remaining_usd'] is None


def test_signed_sessions_are_never_charged_to_a_key(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', service.users['registered'])
    mint(service, label='owner', user_id=service.users['registered'], budget={'limit_usd': 1, 'period': 'total'})
    claims = {'sub': service.users['registered'], 'principal_kind': 'registered', 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
              'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid()}
    headers = {'Authorization': 'Bearer ' + jwt.encode(claims, 's' * 40, algorithm='HS256'), 'Idempotency-Key': uid()}
    created = service.client.post('/v1/jobs', json={'kind': 'agent', 'prompt': 'x', 'limits': {'max_budget_usd': 2}}, headers=headers)
    assert created.status_code == 202
    with service.repo.read_transaction() as tx: assert tx.list(api_key_budget.USAGE_KIND) == []


def test_mint_refuses_a_malformed_budget(service):
    for budget in ({'limit_usd': -1, 'period': 'total'}, {'limit_usd': 5}, 'five dollars', {'limit_usd': 1e9, 'period': 'month'}):
        assert mint(service, label='x', budget=budget).status_code == 422


def test_a_job_that_ends_without_a_recorded_run_is_charged_its_hold_only_if_it_dispatched(service, monkeypatch):
    key, key_id = budgeted(service, monkeypatch, 10)
    ran, never = (spend(service, key, 2).json()['id'] for _ in range(2))
    with service.repo.transaction() as tx:
        for job_id, box in ((ran, {'box_id': 'b1', 'phase': 'running'}), (never, None)):
            execution = tx.get('execution', job_id)
            tx.put('execution', job_id, execution['owner'], {**execution['data'], 'box': box})
            row = tx.get('job', job_id); tx.put('job', job_id, row['owner'], {**row['data'], 'status': 'cancelled'})
    usage = listed(service, key_id)['usage']
    assert usage['spent_usd'] == 2.0 and usage['in_progress_usd'] == 0


def test_an_analysis_holds_its_cap_and_is_charged_its_statement_jobs(service):
    owner = service.users['registered']
    analysis, statement, other = uid(), uid(), uid()
    key = {'key_id': uid(), 'user_id': owner, 'budget': {'limit_usd': 50, 'period': 'month'}}
    with service.repo.transaction() as tx:
        tx.put(api_key_budget.USAGE_KIND, key['key_id'], 'admin-console', {'format': api_key_budget.FORMAT, 'key_id': key['key_id'],
            'jobs': {analysis: {'kind': 'analysis', 'created_at': job_metrics.now(), 'reserve_usd': 5.0}}})
        tx.put('job', analysis, owner, {'id': analysis, 'kind': 'analysis', 'status': 'running'})
        for job_id, parent, cost in ((statement, analysis, 0.4), (other, uid(), 9.0)):   # only its own statement counts
            tx.put('job_metrics', job_id, owner, {'format': job_metrics.FORMAT, 'job_id': job_id, 'parent_job_id': parent, 'charged_usd': cost})
    with service.repo.read_transaction() as tx:
        usage = api_key_budget.usage(tx, key, owner)
    assert usage == {'spent_usd': 5.4, 'in_progress_usd': 5.0, 'jobs': 1, 'period_start': usage['period_start']}
    assert usage['period_start'].endswith('-01T00:00:00')


def test_a_minted_key_shows_its_spend_at_once(service):
    created = mint(service, label='new', budget={'limit_usd': 25, 'period': 'month'}).json()
    assert created['key']['usage']['spent_usd'] == 0 and created['key']['usage']['remaining_usd'] == 25.0
