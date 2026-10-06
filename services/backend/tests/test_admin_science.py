"""Administrative science reads have their own authority and never impersonate owners."""
from copy import deepcopy
import hashlib
import json
import logging
import re
import socket
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import jwt
import pytest

from reveal_backend import app as api, analysis_outcomes
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.runtime_config import ROOT


KEY = 'rvl_admin_' + 'A' * 43
OTHER_KEY = 'rvl_admin_' + 'B' * 43
WORKSPACE_KEY = 'rvl_' + 'C' * 43
STAMP = '2026-10-01T12:00:00Z'
EXPIRES = '2099-01-01T00:00:00Z'


def no_network(*args, **kwargs):
    raise AssertionError('Administrative saved-science reads must not make network calls')


def auth_headers(token=KEY):
    return {'Authorization': 'Bearer ' + token}


def owner_headers(owner):
    return auth_headers(jwt.encode({'sub': owner, 'principal_kind': 'registered',
        'iss': 'reveal-nextjs', 'aud': 'reveal-api', 'iat': int(time.time()),
        'exp': int(time.time()) + 120, 'jti': uid()}, 's' * 40, algorithm='HS256'))


def archive(gap_id, *, account_id=None, outcome_id=None):
    transition = {'from_reference_generation': 'a' * 64, 'to_reference_generation': 'b' * 64,
        'archived_at': STAMP}
    return {'status': 'archived', 'reason': 'reference_generation_superseded', **transition,
        'history': [transition], 'reference': {'model': 'eaggl-capped-v1', 'anchors': []},
        'gap': {'id': gap_id, 'source_id': 'dismech:retained', 'source_revision': 'c' * 64},
        'analysis': {'job_id': uid(), 'request_id': uid(), 'evidence_package_sha256': 'd' * 64,
            'account_id': account_id, 'outcome_id': outcome_id}}


def artifact_links(envelope):
    for position, artifact in enumerate(envelope['artifacts']):
        artifact.update(download_url=f'https://private-artifacts.invalid/owner/{position}', expires_at=EXPIRES)
    return envelope


def without_artifact_links(envelope):
    expected = deepcopy(envelope)
    for artifact in expected['artifacts']:
        artifact.update(download_url=None, expires_at=None)
    expected['coverage']['next_cursor'] = None
    return expected


def seed_account(repo, fixture, owner, *, public=False, archived=False, paragraph=True):
    result = artifact_links(deepcopy(fixture['account']))
    result.pop('publication', None)
    account_id = result['root_id']; record_id = digest([owner, account_id])
    paragraph_result = artifact_links(deepcopy(fixture['paragraph'])) if paragraph else None
    result['research_statement'] = {'status': 'succeeded' if paragraph else 'not_requested',
        'paragraph_id': paragraph_result['root_id'] if paragraph else None, 'job_id': uid() if paragraph else None}
    summary = {'account': deepcopy(result['document']['scientific_accounts'][0]),
        'knowledge_gap': deepcopy(fixture['gaps']['items'][0]['object']),
        'claim_count': len(result['document']['scientific_accounts'][0]['component_claims']),
        'created_at': STAMP, 'job_id': uid(), 'research_statement': deepcopy(result['research_statement'])}
    if archived:
        result['archive'] = archive(summary['knowledge_gap']['id'], account_id=account_id)
        summary['archive'] = deepcopy(result['archive'])
    with repo.transaction() as tx:
        tx.put('account', record_id, owner, {'result': result, 'summary': summary,
            'internal_capture_path': '/private/admin-test/account'})
        tx.put('account_membership', record_id, owner, {'account_id': account_id, 'summary': summary})
        if paragraph_result:
            tx.put('paragraph', digest([owner, paragraph_result['root_id']]), owner,
                {'account_id': account_id, 'result': paragraph_result, 'storage': {'private': 'not-a-response'}})
        if public:
            tx.put('publication', record_id, owner, {'account_id': account_id, 'visibility': 'public',
                'version': 3, 'published_at': STAMP, 'updated_at': STAMP, 'snapshot_id': uid(),
                'paragraph_id': result['research_statement']['paragraph_id'], 'summary': summary})
    return SimpleNamespace(id=record_id, owner=owner, result=result, summary=summary, paragraph=paragraph_result)


def seed_outcome(repo, fixture, owner, *, public=False, archived=False):
    identity = uid(); gap = deepcopy(fixture['gaps']['items'][0]['object'])
    source_artifacts = artifact_links(deepcopy(fixture['account']))['artifacts'][:1]
    record = {'id': identity, 'outcome': 'insufficient_evidence', 'summary': 'A bounded saved exploration.',
        'reason': 'The captured query did not establish the proposed connection.', 'explored_topics': ['test topic'],
        'missing_evidence': ['A direct supported connection'], 'limitations': ['Only the saved query scope was inspected'],
        'next_steps': ['Inspect independent evidence'], 'knowledge_gap': gap,
        'source_gap': {'id': gap['id'], 'source_id': 'dismech:fixture', 'source_revision': 'e' * 64},
        'anchors': [], 'selected_kgs': [], 'created_at': STAMP, 'attribution': None,
        'scope_note': analysis_outcomes.SCOPE_NOTE, 'record_format': 'structured', 'job_id': uid(),
        'provenance': {'evidence_package_sha256': 'a' * 64, 'outcome_sha256': 'b' * 64,
            'runtime_sha256': None, 'ledger_sha256': None, 'execution_mode': 'deterministic',
            'source_bindings': [], 'coverage': {'test_fixture': True}, 'source_artifacts': source_artifacts,
            'evidence_refs': [{'source': 'tool_response', 'ledger_sequence': 1, 'pointer': '/rows/0',
                'artifact_sha256': 'c' * 64,
                'download_url': 'https://private-artifacts.invalid/capture'}], 'graph_queries': []}}
    if archived: record['archive'] = archive(gap['id'], outcome_id=identity)
    summary = analysis_outcomes.summary(record)
    with repo.transaction() as tx:
        tx.put('analysis_outcome', identity, owner, {'record': record,
            'artifacts': {'c' * 64: {'storage': {'key': 'internal-storage-marker'}, 'path': '/private/capture'}}})
        tx.put('outcome_summary', identity, owner, summary)
        if public:
            tx.put('outcome_publication', identity, owner, {'visibility': 'public', 'version': 2,
                'published_at': STAMP, 'updated_at': STAMP, 'snapshot_id': uid(), 'summary': summary})
    return SimpleNamespace(id=identity, owner=owner, result=record, summary=summary)


@pytest.fixture
def service(monkeypatch, tmp_path):
    owners = [uid() for _ in range(3)]
    env = {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_SERVICE_TOKEN': 't' * 40,
        'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api',
        'REVEAL_ADMIN_READ_API_KEY_SHA256': hashlib.sha256(KEY.encode()).hexdigest(),
        'REVEAL_ADMIN_READ_API_KEY_ID': uid(), 'REVEAL_API_KEY_SHA256': hashlib.sha256(WORKSPACE_KEY.encode()).hexdigest(),
        'REVEAL_API_KEY_USER_ID': owners[0], 'REVEAL_ARTIFACT_STORE': 'filesystem',
        'REVEAL_ARTIFACTS_DIR': str(tmp_path / 'artifacts'), 'REVEAL_JOB_TRANSPORT': 'database',
        'REVEAL_PUBLIC_API_URL': 'https://testserver', 'REVEAL_NOTIFICATION_NAMESPACE': 'admin-science-test',
        'REVEAL_NOTIFICATION_REDIS_URL': '', 'REVEAL_NOTIFICATION_REDIS_REST_URL': '',
        'REVEAL_NOTIFICATION_REDIS_REST_TOKEN': '', 'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': ''}
    for key, value in env.items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    repo = Repository(str(tmp_path / 'admin.sqlite')); repo.migrate()
    with repo.transaction() as tx:
        for owner in owners:
            me = {**api.fresh_principal('registered'), 'user_id': owner, 'workspace_expires_at': None}
            tx.put('principal', owner, owner, {'me': me, 'retired': False})
    fixture = json.loads((ROOT / 'services/frontend/src/lib/fixtures/contract.json').read_text())
    accounts = [seed_account(repo, fixture, owner, public=index == 1, archived=index == 2)
        for index, owner in enumerate(owners)]
    outcomes = [seed_outcome(repo, fixture, owner, public=index == 1, archived=index == 2)
        for index, owner in enumerate(owners)]
    bookmark = uid()
    with repo.transaction() as tx:
        tx.put('exploration', bookmark, owners[0], {'id': bookmark, 'label': 'Bookmark, not a saved outcome'})
    monkeypatch.setattr(api, 'repo', repo)
    monkeypatch.setattr(api, 'catalog', SimpleNamespace(load=no_network))
    with TestClient(api.app, base_url='https://testserver') as client:
        yield SimpleNamespace(client=client, repo=repo, owners=owners, accounts=accounts, outcomes=outcomes,
            bookmark=bookmark, fixture=fixture, headers=auth_headers())


def get(service, path, **params):
    return service.client.get(path, params=params, headers=service.headers)


def assert_private(response):
    assert 'no-store' in response.headers.get('cache-control', ''), response.text
    assert 'authorization' in response.headers.get('vary', '').lower()


def saved_rows(repo):
    with repo.read_transaction() as tx:
        return tx.execute('SELECT kind,id,owner_id,version,payload,updated_at FROM reveal_records ORDER BY kind,id').fetchall()


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
def test_admin_lists_private_by_default_and_applies_explicit_visibility(service, kind):
    records = service.accounts if kind == 'accounts' else service.outcomes
    path = '/v1/admin/' + kind
    for visibility, expected in [(None, {records[0].id, records[2].id}),
            ('private', {records[0].id, records[2].id}), ('public', {records[1].id}),
            ('all', {row.id for row in records})]:
        response = get(service, path, **({'visibility': visibility} if visibility else {}))
        assert response.status_code == 200, response.text
        assert_private(response)
        items = response.json()['items']
        api.validate(response.json(), 'AdminAccountList' if kind == 'accounts' else 'AdminExplorationList')
        assert {item['record_id'] for item in items} == expected
        for item in items:
            assert set(item) == {'record_id', 'owner_user_id', 'summary'}
            original = next(row for row in records if row.id == item['record_id'])
            assert item['owner_user_id'] == original.owner
            assert item['summary']['publication']['can_manage'] is False
            if kind == 'accounts': assert item['summary']['votes'] is None
        assert service.bookmark not in {item['record_id'] for item in items}


def test_same_dapper_id_across_owners_has_distinct_record_routes(service):
    items = get(service, '/v1/admin/accounts', visibility='all').json()['items']
    assert len({item['summary']['account']['id'] for item in items}) == 1
    assert len({item['record_id'] for item in items}) == len(service.owners)
    for row in service.accounts:
        response = get(service, '/v1/admin/accounts/' + row.id)
        assert response.status_code == 200, response.text
        assert response.json()['owner_user_id'] == row.owner
        assert response.json()['result']['research_statement']['job_id'] == row.result['research_statement']['job_id']
    assert get(service, '/v1/admin/accounts/' + service.accounts[0].result['root_id']).status_code == 404


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
@pytest.mark.parametrize('owner_state', ['retired', 'expired', 'missing'])
def test_retained_archived_science_survives_owner_lifecycle(service, kind, owner_state):
    record = (service.accounts if kind == 'accounts' else service.outcomes)[2]
    with service.repo.transaction() as tx:
        principal = tx.get('principal', record.owner)['data']
        if owner_state == 'missing': tx.execute('DELETE FROM reveal_records WHERE kind=%s AND id=%s', ('principal', record.owner))
        else:
            if owner_state == 'retired': principal['retired'] = True
            else: principal['me']['workspace_expires_at'] = '2000-01-01T00:00:00Z'
            tx.put('principal', record.owner, record.owner, principal)
    response = get(service, f'/v1/admin/{kind}/{record.id}')
    assert response.status_code == 200, response.text
    assert response.json()['result']['archive'] == record.result['archive']
    assert response.json()['summary']['publication']['can_manage'] is False
    assert record.id in {item['record_id'] for item in get(service, '/v1/admin/' + kind).json()['items']}


def test_account_detail_preserves_saved_science_and_linked_paragraph_without_artifact_authority(service):
    row = service.accounts[1]
    response = get(service, '/v1/admin/accounts/' + row.id)
    assert response.status_code == 200, response.text
    value = response.json(); assert_private(response)
    expected = without_artifact_links(row.result)
    expected['publication'] = value['summary']['publication']
    assert value['result'] == expected
    assert value['paragraph'] == without_artifact_links(row.paragraph)
    assert 'internal_capture_path' not in response.text and 'not-a-response' not in response.text
    assert 'private-artifacts.invalid' not in response.text
    api.validate(value['result'], 'AccountResult'); api.validate(value['paragraph'], 'ParagraphObjectResult')
    api.validate(value, 'AdminAccountDetail')


def test_admin_detail_does_not_reuse_owner_provenance_cursors(service):
    row = service.accounts[0]
    with service.repo.transaction() as tx:
        for kind, identity in [('account', row.id), ('paragraph', digest([row.owner, row.paragraph['root_id']]))]:
            data = tx.get(kind, identity)['data']
            data['result']['coverage'].update(complete=False, next_cursor='private-owner-scoped-cursor')
            tx.put(kind, identity, row.owner, data)
    response = get(service, '/v1/admin/accounts/' + row.id)
    assert response.status_code == 200, response.text
    for field in ('result', 'paragraph'):
        assert response.json()[field]['coverage']['next_cursor'] is None
        assert response.json()[field]['coverage']['complete'] is False
    assert 'private-owner-scoped-cursor' not in response.text


def test_local_account_summary_uses_frozen_attribution_without_routing_hints(service):
    row = service.accounts[0]; local_id, request_id, original_author = uid(), uid(), uid()
    attribution = {'user_id': original_author, 'person_id': None, 'principal_kind': 'registered',
        'display_name': 'Frozen original author', 'orcid': None, 'orcid_authenticated': False,
        'observed_at': STAMP}
    with service.repo.transaction() as tx:
        saved = tx.get('account', row.id)['data']
        saved['summary'].update(job_id=local_id, local_work_id=local_id, execution_mode='local_agent')
        tx.put('account', row.id, row.owner, saved)
        tx.put('local_work', local_id, row.owner, {'research_request_id': request_id})
        tx.put('request', request_id, row.owner, {'attribution': {**attribution, 'email': 'never-render@example.invalid'}})
    detail = get(service, '/v1/admin/accounts/' + row.id)
    listing = get(service, '/v1/admin/accounts')
    for item in (detail.json(), next(item for item in listing.json()['items'] if item['record_id'] == row.id)):
        assert item['summary']['attribution'] == attribution
        assert item['summary']['job_id'] == local_id
        assert 'local_work_id' not in item['summary'] and 'execution_mode' not in item['summary']
        api.validate(item['summary'], 'AccountSummary')
    assert 'never-render@example.invalid' not in detail.text + listing.text


@pytest.mark.parametrize('foreign_record', ['job', 'request'])
def test_summary_attribution_cannot_follow_a_foreign_owned_record(service, foreign_record):
    row = service.accounts[0]; job_id = row.summary['job_id']; request_id = uid()
    with service.repo.transaction() as tx:
        tx.put('job', job_id, service.owners[1] if foreign_record == 'job' else row.owner,
            {'research_request_id': request_id})
        tx.put('request', request_id, service.owners[1] if foreign_record == 'request' else row.owner,
            {'attribution': {'user_id': uid(), 'person_id': None, 'principal_kind': 'registered',
                'display_name': 'Unrelated author', 'orcid': None, 'orcid_authenticated': False, 'observed_at': STAMP}})
    value = get(service, '/v1/admin/accounts/' + row.id).json()
    assert value['summary']['attribution'] is None


@pytest.mark.parametrize('relationship', ['not_requested', 'missing', 'other_account', 'other_owner'])
def test_paragraph_is_nullable_and_must_match_both_owner_and_account(service, relationship):
    row = service.accounts[0]; paragraph_id = row.paragraph['root_id']
    with service.repo.transaction() as tx:
        if relationship == 'not_requested':
            saved = tx.get('account', row.id)['data']
            saved['result']['research_statement'] = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
            tx.put('account', row.id, row.owner, saved)
        elif relationship == 'missing':
            tx.execute('DELETE FROM reveal_records WHERE kind=%s AND id=%s', ('paragraph', digest([row.owner, paragraph_id])))
        else:
            key = digest([row.owner, paragraph_id]); saved = tx.get('paragraph', key)['data']
            if relationship == 'other_account': saved['account_id'] = 'dapper:ScientificAccount.' + 'x' * 32
            tx.put('paragraph', key, service.owners[1] if relationship == 'other_owner' else row.owner, saved)
    response = get(service, '/v1/admin/accounts/' + row.id)
    assert response.status_code == 200, response.text
    assert response.json()['paragraph'] is None


def test_exploration_detail_is_saved_outcome_not_bookmark_and_omits_internal_storage(service):
    row = service.outcomes[0]
    response = get(service, '/v1/admin/explorations/' + row.id)
    assert response.status_code == 200, response.text
    value = response.json(); assert_private(response)
    expected = deepcopy(row.result)
    expected['publication'] = value['summary']['publication']
    for item in expected['provenance']['source_artifacts']: item.update(download_url=None, expires_at=None)
    for item in expected['provenance']['evidence_refs']: item['download_url'] = None
    assert value['result'] == expected
    assert 'internal-storage-marker' not in response.text and '/private/capture' not in response.text
    assert 'private-artifacts.invalid' not in response.text
    assert get(service, '/v1/admin/explorations/' + service.bookmark).status_code == 404
    api.validate(value['result'], 'AnalysisOutcome')
    api.validate(value, 'AdminExplorationDetail')


def test_exploration_listing_recovers_retained_outcome_without_summary_index(service):
    row = service.outcomes[0]
    with service.repo.transaction() as tx:
        tx.execute('DELETE FROM reveal_records WHERE kind=%s AND id=%s', ('outcome_summary', row.id))
    response = get(service, '/v1/admin/explorations')
    assert response.status_code == 200, response.text
    item = next(item for item in response.json()['items'] if item['record_id'] == row.id)
    assert item['summary']['summary'] == row.result['summary']
    assert item['summary']['knowledge_gap'] == row.result['knowledge_gap']


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
@pytest.mark.parametrize('credential', ['none', 'jwt', 'workspace', 'service', 'wrong_admin'])
def test_ordinary_credentials_cannot_read_admin_science(service, kind, credential):
    headers = {'none': {}, 'jwt': owner_headers(service.owners[0]), 'workspace': auth_headers(WORKSPACE_KEY),
        'service': auth_headers('t' * 40), 'wrong_admin': auth_headers(OTHER_KEY)}[credential]
    records = service.accounts if kind == 'accounts' else service.outcomes
    for suffix in ('', '/' + records[0].id):
        with patch.object(service.repo, 'read_transaction', side_effect=AssertionError('Unauthorized admin read reached storage')):
            response = service.client.get('/v1/admin/' + kind + suffix, headers=headers)
        assert response.status_code == 401, response.text
        assert_private(response)
        assert records[0].result.get('reason', 'private-artifacts.invalid') not in response.text


def test_admin_key_cannot_become_workspace_gateway_or_research_authority(service):
    row = service.accounts[0]
    for path in ('/v1/me', '/v1/drafts', '/v1/research-requests', '/v1/jobs', '/v1/local-work',
            '/v1/accounts/' + row.result['root_id'], '/v1/analysis-outcomes/' + service.outcomes[0].id):
        assert get(service, path).status_code == 401, path
    for path in ('/internal/v1/principals/anonymous', '/internal/v1/principals/resolve', '/internal/v1/principals/claim'):
        assert service.client.post(path, headers=service.headers, json={}).status_code == 403
    for path in ('/internal/v1/admin/telemetry', '/internal/v1/admin/jobs/test', '/internal/v1/admin/tables/records'):
        assert get(service, path).status_code == 403
    response = service.client.post('/mcp', headers={**service.headers, 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
def test_admin_read_key_cannot_mutate_records_or_publication(service, kind):
    row = (service.accounts if kind == 'accounts' else service.outcomes)[0]
    before = saved_rows(service.repo)
    for method in ('post', 'put', 'patch', 'delete'):
        response = service.client.request(method, '/v1/admin/' + kind + '/' + row.id,
            headers=service.headers, json={'visibility': 'public'})
        assert response.status_code == 405, response.text
        assert_private(response)
    normal_path = ('/v1/accounts/' + row.result['root_id'] if kind == 'accounts' else '/v1/analysis-outcomes/' + row.id)
    response = service.client.post(normal_path + '/publication', headers={**service.headers, 'Idempotency-Key': uid()},
        json={'visibility': 'public', 'expected_version': 0})
    assert response.status_code == 401, response.text
    assert saved_rows(service.repo) == before


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
def test_pagination_is_stable_bounded_and_has_no_duplicate_records(service, kind):
    path = '/v1/admin/' + kind; identities = []; cursor = None
    with patch.object(service.repo, 'transaction', side_effect=AssertionError('Admin reads acquired a write transaction')):
        while True:
            response = get(service, path, visibility='all', limit=1, **({'cursor': cursor} if cursor else {}))
            assert response.status_code == 200, response.text
            assert_private(response)
            value = response.json(); identities.extend(item['record_id'] for item in value['items'])
            assert len(value['items']) == 1
            cursor = value['page']['next_cursor']
            assert value['page']['has_more'] == bool(cursor)
            if not cursor: break
    expected = service.accounts if kind == 'accounts' else service.outcomes
    assert len(identities) == len(set(identities)) == len(expected)
    assert set(identities) == {row.id for row in expected}


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
@pytest.mark.parametrize('change', ['kind', 'filter', 'key_id', 'key_rotation', 'publication', 'content', 'signature'])
def test_cursor_is_bound_to_key_query_publication_and_saved_content(service, monkeypatch, kind, change):
    path = '/v1/admin/' + kind
    first = get(service, path, visibility='all', limit=1)
    assert first.status_code == 200, first.text
    cursor = first.json()['page']['next_cursor']; assert cursor
    visibility = 'all'
    if change == 'kind': path = '/v1/admin/' + ('explorations' if kind == 'accounts' else 'accounts')
    elif change == 'filter': visibility = 'private'
    elif change == 'key_id': monkeypatch.setenv('REVEAL_ADMIN_READ_API_KEY_ID', uid())
    elif change == 'key_rotation':
        monkeypatch.setenv('REVEAL_ADMIN_READ_API_KEY_SHA256', hashlib.sha256(OTHER_KEY.encode()).hexdigest())
        service.headers = auth_headers(OTHER_KEY)
    elif change == 'signature': cursor = cursor[:-1] + ('0' if cursor[-1] != '0' else '1')
    else:
        record = (service.accounts if kind == 'accounts' else service.outcomes)[1]
        with service.repo.transaction() as tx:
            table = ('publication' if kind == 'accounts' else 'outcome_publication') if change == 'publication' else ('account' if kind == 'accounts' else 'analysis_outcome')
            saved = tx.get(table, record.id)['data']
            if change == 'publication': saved['version'] += 1
            elif kind == 'accounts': saved['result']['coverage']['complete'] = False
            else: saved['record']['reason'] += ' Revised stored evidence scope.'
            tx.put(table, record.id, record.owner, saved)
    response = get(service, path, visibility=visibility, limit=1, cursor=cursor)
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'CURSOR_EXPIRED'
    assert_private(response)


@pytest.mark.parametrize('kind', ['accounts', 'explorations'])
@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 101}, {'limit': 'oops'}, {'visibility': 'workspace'}])
def test_invalid_queries_fail_without_cacheable_response(service, kind, params):
    response = get(service, '/v1/admin/' + kind, **params)
    assert response.status_code == 422, response.text
    assert_private(response)


def test_admin_reads_leave_every_persisted_record_unchanged(service):
    before = saved_rows(service.repo)
    with patch.object(service.repo, 'transaction', side_effect=AssertionError('Admin reads acquired a write transaction')):
        for kind, records in [('accounts', service.accounts), ('explorations', service.outcomes)]:
            assert get(service, '/v1/admin/' + kind, visibility='all').status_code == 200
            for row in records:
                response = get(service, '/v1/admin/' + kind + '/' + row.id)
                assert response.status_code == 200, response.text
            missing = get(service, '/v1/admin/' + kind + '/' + ('f' * 64 if kind == 'accounts' else uid()))
            assert missing.status_code == 404, missing.text
            assert_private(missing)
    assert saved_rows(service.repo) == before


def test_operational_audit_contains_only_key_id_action_and_count(service, caplog, monkeypatch):
    key_id = uid(); monkeypatch.setenv('REVEAL_ADMIN_READ_API_KEY_ID', key_id)
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        assert get(service, '/v1/admin/accounts', limit=1).status_code == 200
        assert get(service, '/v1/admin/explorations/' + service.outcomes[0].id).status_code == 200
    messages = [record.getMessage() for record in caplog.records if record.getMessage().startswith('admin_science_read ')]
    assert len(messages) == 2
    for message in messages:
        assert re.fullmatch(r'admin_science_read key_id=' + re.escape(key_id) + r' action=(?:list|get)_(?:account|analysis_outcome) count=1', message)
        assert KEY not in message
        assert not any(owner in message for owner in service.owners)
        assert 'evidence' not in message and 'private' not in message
