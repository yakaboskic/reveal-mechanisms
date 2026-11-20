"""Isolated draft assessments: no live provider, scientific writes or user accounts."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import httpx
import pytest

import test_application as application
from reveal_backend import app as api, cfde_assessment as assessment, cfde_assessment_state as state, jev_batch
from reveal_backend.auth import Problem
from reveal_backend.repository import digest, uid


class Queue:
    def __init__(self): self.calls = []
    def submit(self, fn, *args): self.calls.append((fn, args))
    def run(self):
        fn, args = self.calls.pop(0)
        fn(*args)


class Catalog(application.NoSources):
    reference_generation_id = 'a' * 64
    def load(self): pass


def provider_response():
    return {'model': assessment.MODEL, 'answers': {
        'cfde_support': {'type': 'choice', 'choice': 'yes', 'probabilities': {'yes': .56, 'no': .43}, 'confidence': .48},
        'main_blocker': {'type': 'choice', 'choice': 'weak_relevance', 'confidence': .7,
            'probabilities': {key: float(key == 'weak_relevance') for key in assessment.BLOCKERS}},
        **{key: {'type': 'noul', 'noul': .6} for key in ('gene_gene_set', 'gene_mechanism', 'gene_set_mechanism')}},
        'usage': {'input_tokens': 200, 'output_tokens': 20}}


@pytest.fixture
def case(monkeypatch):
    instance = application.ApplicationTests(); instance.setUp()
    monkeypatch.setenv('TYPESAFE_API_KEY', 'isolated-test-key-never-expose')
    instance.catalog = Catalog(); monkeypatch.setattr(api, 'catalog', instance.catalog)
    instance.queue = Queue(); monkeypatch.setattr(assessment, '_pool', instance.queue)
    monkeypatch.setattr(assessment, '_slots', threading.BoundedSemaphore(4))
    instance.owner = instance.provision(); instance.saved = instance.draft(instance.owner)
    example = json.loads((Path(__file__).resolve().parents[3] / 'api/examples/createCfdeAssessment.current_composer.json').read_text())
    instance.body = deepcopy(example['request']['body']); instance.body['draft_version'] = 1
    instance.body['composer']['context'] = 'My current, unsaved question.'
    instance.route = '/v1/drafts/' + instance.saved['id'] + '/cfde-assessments'
    instance.key = instance.headers(instance.owner)
    instance.prepared = {'state': {'public_scientific_fixture': True}, 'reference_generation_id': 'a' * 64,
        'coverage': {'factor_count': 1, 'gene_loading_count': 50, 'gene_set_loading_count': 50,
            'unique_gene_set_count': 50, 'missing': [], 'truncations': [], 'complete': True}}
    instance.builder = Mock(return_value=instance.prepared); monkeypatch.setattr(state, 'build_state', instance.builder)
    instance.provider = Mock(return_value=provider_response()); monkeypatch.setattr(assessment, 'call_provider', instance.provider)
    try: yield instance
    finally: instance.doCleanups()


def start(case, *, body=None, headers=None):
    return case.client.post(case.route, json=case.body if body is None else body,
                           headers=case.key if headers is None else headers)


def read(case, result, *, owner=None):
    return case.client.get(case.route + '/' + result['id'], headers=case.headers(owner or case.owner))


def test_current_unsaved_snapshot_async_and_no_research_side_effects(case):
    # Both state retrieval and the vendor call must allow unrelated writers.
    transaction = case.repo.transaction
    active = []
    @contextmanager
    def tracked():
        with transaction() as tx:
            active.append(True)
            try: yield tx
            finally: active.pop()
    case.repo.transaction = tracked
    def prepare(*args, **kwargs):
        assert not active
        assert args[1] == case.body['composer']
        assert args[2]['context'] == case.body['composer']['context']
        return case.prepared
    def send(body, **kwargs):
        assert not active
        assert body['model'] == 'jev-1.13.0'
        assert body['state'] == assessment.model_state(case.prepared['state'])
        assert 'isolated-test-key' not in json.dumps(body)
        return provider_response()
    case.builder.side_effect = prepare; case.provider.side_effect = send
    response = start(case); assert response.status_code == 202, response.text
    first = response.json(); api.validate(first, 'CfdeAssessment')
    assert first['status'] == 'preparing' and first['result'] is None
    assert response.headers['location'] == case.route + '/' + first['id']
    assert response.headers['retry-after'] == '2'
    assert response.headers['cache-control'] == 'private, no-store'
    case.provider.assert_not_called()
    case.queue.run()
    response = read(case, first); result = response.json(); api.validate(result, 'CfdeAssessment')
    assert result['status'] == 'succeeded'
    assert result['result']['probability_yes'] == .56
    assert result['result']['probability_no'] == .43
    assert result['result']['confidence'] == .48
    assert result['result']['calibration'] == 'not_calibrated'
    assert 'authorization' in response.headers['vary'].lower()
    assert 'state' not in result and 'request' not in result and 'response' not in result
    with case.repo.read_transaction() as tx:
        assert tx.get('draft', case.saved['id'])['data']['composer'] == case.saved['composer']
        row = tx.get('cfde_assessment', first['id'])['data']
        assert row['request_sha256'] == digest(row['request'])
        assert row['response_sha256'] == digest(row['response'])
        for kind in ('request', 'job', 'claim', 'account', 'publication', 'artifact'):
            assert not tx.list(kind)


def test_replay_content_dedup_and_conflict(case, monkeypatch):
    first = start(case).json()
    assert start(case).json()['id'] == first['id']
    assert start(case, headers=case.headers(case.owner)).json()['id'] == first['id']
    changed = deepcopy(case.body); changed['composer']['context'] = 'Different inputs'
    conflict = start(case, body=changed)
    assert conflict.status_code == 409 and conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'
    assert len(case.queue.calls) == 1
    case.queue.run()
    assert start(case, headers=case.headers(case.owner)).json()['id'] == first['id']
    assert case.provider.call_count == 1
    # Recovery reads the receipt even if the provider is temporarily disabled.
    monkeypatch.delenv('TYPESAFE_API_KEY')
    case.catalog.load = Mock(side_effect=AssertionError('Replay must not warm the catalog'))
    assert start(case).json()['status'] == 'succeeded'
    assert case.provider.call_count == 1


def test_owner_authentication_and_expiry_precede_source_and_vendor(case, monkeypatch):
    other = case.provision()
    case.catalog.load = Mock(side_effect=AssertionError('Unauthorized request reached sources'))
    assert start(case, headers=case.headers(other)).status_code == 404
    assert case.client.post(case.route, json=case.body).status_code == 400  # missing request key
    assert start(case, headers={'Idempotency-Key': uid()}).status_code == 401
    assert start(case, headers={'Idempotency-Key': uid(), 'Authorization': 'Bearer rvl_admin_invalid'}).status_code == 401
    with case.repo.transaction() as tx:
        draft = tx.get('draft', case.saved['id'])['data']; draft.update(lifecycle='temporary', expires_at='2000-01-01T00:00:00Z')
        tx.put('draft', draft['id'], case.owner, draft)
    assert start(case).status_code == 410
    case.builder.assert_not_called(); case.provider.assert_not_called()


def test_cross_owner_assessment_and_wrong_draft_reads(case):
    first = start(case).json(); other = case.provision()
    assert read(case, first, owner=other).status_code == 404
    other_draft = case.draft(case.owner)
    route = '/v1/drafts/' + other_draft['id'] + '/cfde-assessments/' + first['id']
    assert case.client.get(route, headers=case.headers(case.owner)).status_code == 404
    assert read(case, first).json()['status'] == 'preparing'
    case.provider.assert_not_called()


def test_restart_expiry_no_automatic_retry_and_explicit_retry(case):
    first = start(case).json()
    with case.repo.transaction() as tx:
        data = tx.get('cfde_assessment', first['id'])['data']
        data['public']['expires_at'] = '2000-01-01T00:00:00Z'
        tx.put('cfde_assessment', first['id'], case.owner, data)
    before = len(case.queue.calls)
    for _ in range(3):
        value = read(case, first).json(); api.validate(value, 'CfdeAssessment')
        assert value['status'] == 'interrupted' and value['result'] is None
    assert len(case.queue.calls) == before
    assert start(case).json()['status'] == 'interrupted'
    retry = start(case, headers=case.headers(case.owner)).json()
    assert retry['id'] != first['id'] and retry['status'] == 'preparing'
    case.provider.assert_not_called()


@pytest.mark.parametrize('error', [Problem(503, 'CFDE_MODEL_TIMEOUT', 'Timed out.'), ValueError('secret diagnostic')])
def test_provider_failure_never_returns_no_and_requires_explicit_retry(case, error):
    case.provider.side_effect = error
    first = start(case).json(); case.queue.run()
    failed = read(case, first).json(); api.validate(failed, 'CfdeAssessment')
    assert failed['status'] == 'failed' and failed['result'] is None
    assert 'secret diagnostic' not in json.dumps(failed)
    assert start(case).json()['id'] == first['id']
    assert len(case.queue.calls) == 0
    assert start(case, headers=case.headers(case.owner)).json()['id'] != first['id']


def test_saved_version_and_reference_cutover_invalidate_scores(case):
    conflict = start(case, body={**case.body, 'draft_version': 2})
    assert conflict.status_code == 409
    first = start(case).json(); case.queue.run()
    case.catalog.reference_generation_id = 'b' * 64
    assert read(case, first).json()['stale'] is True
    case.catalog.reference_generation_id = 'a' * 64
    with case.repo.transaction() as tx:
        draft = tx.get('draft', case.saved['id'])['data']; draft['version'] += 1
        tx.put('draft', draft['id'], case.owner, draft)
    assert read(case, first).json()['stale'] is True
    assert start(case, headers=case.headers(case.owner)).status_code == 409


def save_assessment_composer(case):
    """Save the exact working composer without invoking source/catalog fixtures."""
    with case.repo.transaction() as tx:
        draft = tx.get('draft', case.saved['id'])['data']
        draft.update(version=draft['version'] + 1, composer=deepcopy(case.body['composer']))
        tx.put('draft', draft['id'], case.owner, draft)
    case.body['draft_version'] = draft['version']


def attach_assessment_upload(case):
    identity = uid()
    value = {'id': identity, 'draft_id': case.saved['id'], 'filename': 'fixture.txt',
        'media_type': 'text/plain', 'size_bytes': 6, 'sha256': '1' * 64, 'status': 'ready',
        'created_at': '2026-10-07T00:00:00Z', 'expires_at': '2999-01-01T00:00:00Z',
        'storage': {'store': 'filesystem', 'key': 'isolated-unread-fixture', 'size_bytes': 6, 'sha256': '1' * 64},
        'extraction': {'storage': {'store': 'filesystem', 'key': 'isolated-unread-extraction',
            'size_bytes': 60, 'sha256': '2' * 64}}, 'error': None}
    with case.repo.transaction() as tx: tx.put('upload', identity, case.owner, value)
    case.body['composer']['upload_ids'] = [identity]
    return identity


@pytest.mark.parametrize('with_upload', [False, True])
def test_unchanged_private_save_reuses_completion_with_fresh_version_receipt(case, monkeypatch, with_upload):
    if with_upload: attach_assessment_upload(case)
    first = start(case).json(); case.queue.run()
    historical = read(case, first).json()
    with case.repo.read_transaction() as tx:
        original = deepcopy(tx.get('cfde_assessment', first['id']))
    save_assessment_composer(case)
    # A valid completion hit needs neither quota, a dispatch slot nor a key.
    with case.repo.transaction() as tx:
        for _ in range(20): tx.put('cfde_assessment', uid(), case.owner, {'owns_attempt': True})
    monkeypatch.delenv('TYPESAFE_API_KEY')
    monkeypatch.setattr(assessment, '_slots', Mock(acquire=Mock(side_effect=AssertionError('Cache hit reserved a worker'))))
    response = start(case, headers=case.headers(case.owner))
    assert response.status_code == 202, response.text
    current = response.json(); api.validate(current, 'CfdeAssessment')
    assert current['id'] != first['id'] and current['draft_version'] == 2
    assert current['status'] == 'succeeded' and current['stale'] is False
    assert current['result'] == historical['result'] and current['coverage'] == historical['coverage']
    assert current['updated_at'] == historical['updated_at']
    assert read(case, first).json()['draft_version'] == 1
    assert read(case, first).json()['stale'] is True
    assert not case.queue.calls
    case.provider.assert_called_once(); case.builder.assert_called_once()
    with case.repo.read_transaction() as tx:
        assert tx.get('cfde_assessment', first['id']) == original
        receipt = tx.get('cfde_assessment', current['id'])['data']
        assert receipt['owns_attempt'] is False and receipt['reused_assessment_id'] == first['id']
        assert 'response' not in receipt and 'request' not in receipt
        index = assessment._private_completion_key(case.owner, case.saved['id'], case.body['composer'], 'a' * 64)
        assert tx.get('cfde_assessment_cache', index)['data']['id'] == first['id']
    # A second unchanged save still points back to the one original capture.
    save_assessment_composer(case)
    third = start(case, headers=case.headers(case.owner)).json()
    assert third['status'] == 'succeeded' and third['draft_version'] == 3
    with case.repo.read_transaction() as tx:
        assert tx.get('cfde_assessment', third['id'])['data']['reused_assessment_id'] == first['id']
    case.provider.assert_called_once()


@pytest.mark.parametrize('change', ['generation', 'content', 'model', 'rubric'])
def test_changed_private_completion_inputs_need_a_new_attempt(case, monkeypatch, change):
    first = start(case).json(); case.queue.run()
    save_assessment_composer(case)
    if change == 'generation': case.catalog.reference_generation_id = 'b' * 64
    elif change == 'content': case.body['composer']['context'] += ' A changed hypothesis.'
    elif change == 'model': monkeypatch.setattr(assessment, 'MODEL', 'jev-next')
    else: monkeypatch.setattr(assessment, 'RUBRIC_VERSION', 'next-rubric')
    response = start(case, headers=case.headers(case.owner))
    assert response.status_code == 202, response.text
    current = response.json()
    assert current['id'] != first['id'] and current['status'] == 'preparing'
    assert len(case.queue.calls) == 1
    with case.repo.read_transaction() as tx: assert tx.get('cfde_assessment', current['id'])['data']['owns_attempt'] is True


def test_pending_private_work_is_not_reused_across_saved_versions(case):
    first = start(case).json()
    save_assessment_composer(case)
    second = start(case, headers=case.headers(case.owner)).json()
    assert second['id'] != first['id'] and second['draft_version'] == 2
    assert second['status'] == 'preparing' and len(case.queue.calls) == 2
    case.queue.run(); case.provider.assert_not_called()
    assert read(case, first).json()['error']['code'] == 'VERSION_CONFLICT'
    case.queue.run(); case.provider.assert_called_once()
    assert read(case, second).json()['status'] == 'succeeded'


def test_private_completion_reuse_remains_owner_and_draft_scoped(case):
    start(case); case.queue.run(); save_assessment_composer(case)
    other_owner = case.provision(); other_draft = case.draft(other_owner)
    other_body = {**deepcopy(case.body), 'draft_version': other_draft['version']}
    route = '/v1/drafts/' + other_draft['id'] + '/cfde-assessments'
    result = case.client.post(route, json=other_body, headers=case.headers(other_owner))
    assert result.status_code == 202 and result.json()['status'] == 'preparing'
    own_other_draft = case.draft(case.owner)
    route = '/v1/drafts/' + own_other_draft['id'] + '/cfde-assessments'
    result = case.client.post(route, json={**case.body, 'draft_version': own_other_draft['version']}, headers=case.headers(case.owner))
    assert result.status_code == 202 and result.json()['status'] == 'preparing'
    assert len(case.queue.calls) == 2


@pytest.mark.parametrize('change', ['owner', 'not_ready', 'bytes'])
def test_new_version_completion_hit_reauthorizes_and_resolves_uploads(case, change):
    identity = attach_assessment_upload(case)
    start(case); case.queue.run(); save_assessment_composer(case)
    with case.repo.transaction() as tx:
        row = tx.get('upload', identity); value = row['data']
        if change == 'not_ready': value['status'] = 'pending'
        if change == 'bytes': value['extraction']['storage']['sha256'] = '3' * 64
        tx.put('upload', identity, 'another-owner' if change == 'owner' else case.owner, value)
    response = start(case, headers=case.headers(case.owner))
    assert response.status_code == {'owner': 404, 'not_ready': 409, 'bytes': 202}[change]
    if change == 'bytes': assert response.json()['status'] == 'preparing'
    else: assert not case.queue.calls
    case.provider.assert_called_once()


def test_edit_while_queued_fails_without_model_call(case):
    first = start(case).json()
    with case.repo.transaction() as tx:
        draft = tx.get('draft', case.saved['id'])['data']; draft['version'] += 1
        tx.put('draft', draft['id'], case.owner, draft)
    case.queue.run()
    value = read(case, first).json()
    assert value['status'] == 'failed' and value['stale'] is True
    assert value['error']['code'] == 'VERSION_CONFLICT'
    case.provider.assert_not_called()


def test_size_budget_and_generation_mismatch_never_call_provider(case):
    case.prepared['state'] = {'user_inputs': {'context': {'text': 'x' * assessment.MAX_REQUEST_BYTES}}}
    first = start(case).json(); case.queue.run()
    failed = read(case, first).json()
    assert failed['error']['code'] == 'CFDE_ASSESSMENT_TOO_LARGE'
    assert failed['result'] is None and failed['coverage']['factor_count'] == 1
    case.prepared['reference_generation_id'] = 'b' * 64
    second = start(case, headers=case.headers(case.owner)).json(); case.queue.run()
    assert read(case, second).json()['error']['code'] == 'SOURCE_REVISION_CHANGED'
    case.provider.assert_not_called()


def test_configuration_header_validation_and_bounded_dispatch(case, monkeypatch):
    for key in ('short', 'x' * 129):
        assert start(case, headers={**case.key, 'Idempotency-Key': key}).status_code == 400
    monkeypatch.delenv('TYPESAFE_API_KEY')
    assert start(case).status_code == 503
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test')
    for i in range(4):
        body = deepcopy(case.body); body['composer']['context'] = str(i)
        assert start(case, body=body, headers=case.headers(case.owner)).status_code == 202
    busy = start(case)
    assert busy.status_code == 429 and busy.json()['code'] == 'CFDE_ASSESSMENT_BUSY'
    assert len(case.queue.calls) == 4


def test_daily_allowance_checked_before_dispatch(case):
    with case.repo.transaction() as tx:
        for _ in range(20): tx.put('cfde_assessment', uid(), case.owner, {'test_fixture': True, 'owns_attempt': True})
    limited = start(case)
    assert limited.status_code == 429 and limited.json()['code'] == 'CFDE_ASSESSMENT_QUOTA'
    assert not case.queue.calls


def another_draft_check(case, body):
    owner = case.provision(); draft = case.draft(owner)
    route = '/v1/drafts/' + draft['id'] + '/cfde-assessments'
    result = case.client.post(route, json=body, headers=case.headers(owner))
    assert result.status_code == 202, result.text
    return owner, draft, route, result.json()


def test_shared_defaults_get_private_receipts_and_one_model_attempt(case):
    case.body['composer'].pop('context')
    first = start(case).json()
    owner, draft, route, other = another_draft_check(case, case.body)
    assert first['id'] != other['id'] and other['draft_id'] == draft['id']
    assert len(case.queue.calls) == 1
    assert read(case, first, owner=owner).status_code == 404
    case.queue.run()
    result = case.client.get(route + '/' + other['id'], headers=case.headers(owner)).json()
    api.validate(result, 'CfdeAssessment')
    assert result['status'] == 'succeeded' and result['result']['probability_yes'] == .56
    assert first['id'] not in json.dumps(result) and case.owner not in json.dumps(result)
    case.provider.assert_called_once()
    third_owner, _, _, third = another_draft_check(case, case.body)
    assert third['status'] == 'succeeded' and third['id'] not in (first['id'], other['id'])
    assert len(case.queue.calls) == 0
    with case.repo.read_transaction() as tx:
        for row in tx.list('cfde_assessment_shared'):
            text = json.dumps(row['data'])
            assert 'composer' not in text and 'inputs' not in text and 'request' not in text
            assert case.owner not in text and owner not in text and third_owner not in text


def test_private_notes_never_share_across_users(case):
    first = start(case).json()
    _, _, _, other = another_draft_check(case, case.body)
    assert first['id'] != other['id'] and len(case.queue.calls) == 2
    case.queue.run(); case.queue.run()
    assert case.provider.call_count == 2
    with case.repo.read_transaction() as tx: assert not tx.list('cfde_assessment_shared')


def test_cold_catalog_returns_receipt_without_loading_and_then_deduplicates(case):
    case.body['composer'].pop('context')
    case.catalog.reference_generation_id = None
    case.catalog.load = Mock(side_effect=AssertionError('POST must not warm the catalog'))
    first = start(case).json()
    owner, _, route, other = another_draft_check(case, case.body)
    assert first['reference_generation_id'] is None and len(case.queue.calls) == 2
    case.catalog.reference_generation_id = 'a' * 64
    case.queue.run(); case.queue.run()
    assert case.provider.call_count == 1
    result = case.client.get(route + '/' + other['id'], headers=case.headers(owner)).json()
    assert result['status'] == 'succeeded' and result['reference_generation_id'] == 'a' * 64


def test_shared_failure_retry_preserves_historical_follower(case):
    case.body['composer'].pop('context')
    first = start(case).json()
    owner, _, route, follower = another_draft_check(case, case.body)
    case.provider.side_effect = Problem(503, 'CFDE_MODEL_TIMEOUT', 'Timed out.')
    case.queue.run()
    failed = case.client.get(route + '/' + follower['id'], headers=case.headers(owner)).json()
    assert failed['status'] == 'failed'
    case.provider.side_effect = None
    retried = start(case, headers=case.headers(case.owner)).json()
    assert retried['id'] != first['id']
    case.queue.run()
    assert read(case, retried).json()['status'] == 'succeeded'
    old = case.client.get(route + '/' + follower['id'], headers=case.headers(owner)).json()
    assert old['status'] == 'failed' and old['result'] is None


def test_completed_shared_cache_works_without_provider_key(case, monkeypatch):
    case.body['composer'].pop('context')
    start(case); case.queue.run()
    monkeypatch.delenv('TYPESAFE_API_KEY')
    _, _, _, result = another_draft_check(case, case.body)
    assert result['status'] == 'succeeded'
    case.provider.assert_called_once()


@pytest.mark.parametrize('change', ['edit', 'delete', 'expire', 'transfer'])
def test_shared_followers_survive_changes_to_initiating_draft(case, change):
    case.body['composer'].pop('context')
    start(case)
    owner, _, route, follower = another_draft_check(case, case.body)
    with case.repo.transaction() as tx:
        draft = tx.get('draft', case.saved['id'])['data']
        if change == 'delete': tx.remove('draft', draft['id'])
        elif change == 'transfer':
            # Full transfer rules have their own tests; exercise the in-flight
            # worker's captured owner independently of a moved wrapper here.
            row = tx.list('cfde_assessment', case.owner)[0]
            tx.put('cfde_assessment', row['id'], owner, row['data'])
        else:
            if change == 'edit': draft['version'] += 1
            else: draft.update(lifecycle='temporary', expires_at='2000-01-01T00:00:00Z')
            tx.put('draft', draft['id'], case.owner, draft)
    case.queue.run()
    result = case.client.get(route + '/' + follower['id'], headers=case.headers(owner)).json()
    assert result['status'] == 'succeeded' and not result['stale']
    case.provider.assert_called_once()


def test_cached_receipts_do_not_consume_uncached_allowance(case):
    with case.repo.transaction() as tx:
        for _ in range(20): tx.put('cfde_assessment', uid(), case.owner, {'owns_attempt': False})
    assert start(case).status_code == 202


def test_public_cache_concurrent_requests_single_dispatch(case):
    from concurrent.futures import ThreadPoolExecutor
    case.body['composer'].pop('context')
    owner = case.provision(); draft = case.draft(owner)
    other_route = '/v1/drafts/' + draft['id'] + '/cfde-assessments'
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(start, case)
        b = executor.submit(case.client.post, other_route, json=case.body, headers=case.headers(owner))
        one, two = a.result(), b.result()
    assert one.status_code == two.status_code == 202
    assert one.json()['id'] != two.json()['id'] and len(case.queue.calls) == 1


def test_initial_reference_connection_bounds_tls_verification(monkeypatch):
    from unittest.mock import MagicMock
    from reveal_backend import mysql_database
    import pymysql
    monkeypatch.setenv('REVEAL_MYSQL_PASSWORD', 'isolated-test-password')
    import ssl
    connection = MagicMock(); connection._secure = True
    connection._sock = MagicMock(spec=ssl.SSLSocket)  # the verified handshake itself proves TLS; no SHOW query
    connect = Mock(return_value=connection); monkeypatch.setattr(pymysql, 'connect', connect)
    mysql_database.connect(timeout_seconds=3)
    assert connect.call_args.kwargs['connect_timeout'] == 3
    assert connect.call_args.kwargs['read_timeout'] == 3
    assert connect.call_args.kwargs['write_timeout'] == 3
    assert connect.call_args.kwargs['init_command'] is None and connect.call_args.kwargs['autocommit'] is False
    assert connection.reveal_verified_tls is True
    connection.cursor.assert_not_called()
    connection._sock = MagicMock()
    with pytest.raises(ValueError, match='Verified TLS is required'): mysql_database.connect(timeout_seconds=3)
    connection.close.assert_called_once()


@pytest.mark.parametrize('variant', ['valid', 'nan', 'bad_choice', 'wrong_model', 'huge', 'redirect', 'timeout', 'provider_error', 'token_limit'])
def test_provider_protocol_one_attempt_and_no_diagnostics(monkeypatch, variant):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'isolated-provider-key')
    body = {'model': assessment.MODEL, 'questions': assessment.questions(), 'state': {'fixture': True}}
    value = provider_response(); value['diagnostics'] = {'secret': 'must-not-be-retained'}
    if variant == 'nan': value['answers']['cfde_support']['confidence'] = float('nan')
    if variant == 'bad_choice': value['answers']['cfde_support']['choice'] = 'no'
    if variant == 'wrong_model': value['model'] = 'unrequested-model'
    requests = []
    def handle(request):
        requests.append(request)
        assert str(request.url) == 'https://api.typesafe.ai/v1/systemone'
        assert request.headers['authorization'] == 'Bearer isolated-provider-key'
        if variant == 'timeout': raise httpx.ReadTimeout('sensitive message')
        if variant == 'redirect': return httpx.Response(302, headers={'Location': 'https://untrusted.invalid'})
        if variant == 'provider_error': return httpx.Response(500, text='sensitive message')
        if variant == 'token_limit': return httpx.Response(400, json={'detail': {'error_type': 'max_tokens_exceeded'}})
        if variant == 'huge': return httpx.Response(200, content=b' ' * 64_001)
        return httpx.Response(200, content=json.dumps(value).encode())
    factory = httpx.Client
    monkeypatch.setattr(jev_batch.httpx, 'Client', lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    if variant == 'valid':
        result = assessment.call_provider(body)
        assert result['answers']['cfde_support']['probabilities']['yes'] == .56
        assert result['validation_warnings'][0]['kind'] == 'probability_sum_drift'
        assert 'diagnostics' not in result
    else:
        with pytest.raises(Problem) as caught: assessment.call_provider(body)
        assert 'sensitive' not in caught.value.detail
        if variant == 'token_limit': assert caught.value.code == 'CFDE_ASSESSMENT_TOO_LARGE' and caught.value.status == 422
    assert len(requests) == 1


def test_model_projection_is_simple_and_retains_exact_scientific_rows():
    state = {'gene_sets': {'columns': ['id', 'name', 'library', 'collection_id', 'n_genes', 'object_sha256', 'context'],
        'rows': [['dapper:GeneSet.example', 'Source signature', 'LINCS', 'collection', 250, 'a' * 64, {}]]},
        'collections': {'collection': {'context': {'name': 'LINCS source', 'organism': 'human'}, 'provenance': {'secret': 'not model input'}}},
        'factors': [{'label': 'Selected mechanism', 'genes': {'columns': ['symbol', 'loading'], 'rows': [['GENE_A', .123456789012345]]},
            'gene_sets': {'columns': ['gene_set_index', 'joint_loading', 'joint_loading_text'],
            'rows': [[0, .2, '0.200000000000000000001']]}}],
        'user_inputs': {'context': {'text': 'Researcher context', 'sha256': 'b' * 64}}}
    original = deepcopy(state); projected = assessment.model_state(state)
    assert state == original
    assert projected['eaggl_mechanisms'][0]['top_genes']['rows'] == [['GENE_A', .123456789012345]]
    assert projected['eaggl_mechanisms'][0]['top_gene_sets']['rows'] == [['Source signature', 'source_1', '0.200000000000000000001', None, 250]]
    assert projected['gene_set_sources'][0]['organism'] == 'human'
    assert projected['additional_context']['context'] == 'Researcher context'
    encoded = json.dumps(projected)
    for omitted in ('dapper:', 'sha256', 'provenance', 'compaction', 'source_revision', 'not model input'):
        assert omitted not in encoded


def test_model_projection_preserves_mixed_species_and_structured_qualifiers():
    state = {'gene_sets': {'columns': ['id', 'name', 'library', 'collection_id', 'context'],
        'rows': [['one', 'Human signature', 'LINCS', 'collection', {'organism': {'label': 'human'},
            'dose': {'value': 10, 'unit': 'nM', 'sha256': 'never-send'}, 'tissue': {'excerpt': 'liver', 'sha256': 'never-send'}}],
            ['two', 'Mouse signature', 'LINCS', 'collection', {'organism': {'label': 'mouse'}}]]},
        'factors': [{'gene_sets': {'columns': ['gene_set_index'], 'rows': [[0], [1]]}}]}
    result = assessment.model_state(state)
    assert [source['organism'] for source in result['gene_set_sources']] == ['human', 'mouse']
    assert result['eaggl_mechanisms'][0]['top_gene_sets']['rows'][0][-1] == {'dose': {'value': 10, 'unit': 'nM'}, 'tissue': 'liver'}
    assert 'never-send' not in json.dumps(result)


@pytest.mark.parametrize('edit', ['subquery', 'dismissed', 'origin'])
def test_editor_only_edits_reuse_a_completed_private_forecast_with_their_own_receipt(case, edit):
    first = start(case).json(); case.queue.run()
    composer = case.body['composer']
    if edit == 'subquery': composer['mechanism_subquery'] = 'typed while browsing mechanisms'
    elif edit == 'dismissed': composer['dismissed_source_ids'] = ['factor:dismissed-suggestion']
    else: composer['eaggl_anchors'][0].update(origin='automatic', suggestion_id=uid())
    current = start(case, headers=case.headers(case.owner)).json(); api.validate(current, 'CfdeAssessment')
    assert current['id'] != first['id'] and (current['status'], current['stale']) == ('succeeded', False)
    assert current['composer_sha256'] == digest(composer) != first['composer_sha256']
    assert current['result'] == read(case, first).json()['result']
    assert not case.queue.calls and case.provider.call_count == 1   # no second billed attempt or quota use
    with case.repo.read_transaction() as tx:
        receipt = tx.get('cfde_assessment', current['id'])['data']
    assert receipt['composer'] == composer and receipt['owns_attempt'] is False and receipt['reused_assessment_id'] == first['id']


@pytest.mark.parametrize('edit', ['anchor_order', 'graphs'])
def test_forecast_inputs_still_need_a_new_attempt(case, edit):
    start(case); case.queue.run()
    composer = case.body['composer']
    if edit == 'anchor_order':
        extra = deepcopy(composer['eaggl_anchors'][0]); extra['reference'] = {**extra['reference'], 'source_id': extra['reference']['source_id'] + 'b'}
        composer['eaggl_anchors'] = [extra, composer['eaggl_anchors'][0]]
    else: composer['selected_kgs'] = [] if composer.get('selected_kgs') else ['prokn']
    assert start(case, headers=case.headers(case.owner)).json()['status'] == 'preparing'
    assert len(case.queue.calls) == 1


def test_rejections_decided_on_the_snapshot_never_take_the_write_fence(case, monkeypatch):
    monkeypatch.setattr(case.repo, 'transaction', Mock(side_effect=AssertionError('rejection took the write fence')))
    no_anchor = deepcopy(case.body); no_anchor['composer']['eaggl_anchors'] = []
    assert start(case, body=no_anchor).json()['code'] == 'ANCHOR_REQUIRED'
    assert start(case, body={**case.body, 'draft_version': 9}, headers=case.headers(case.owner)).json()['code'] == 'VERSION_CONFLICT'
    foreign = deepcopy(case.body); foreign['composer']['upload_ids'] = [uid()]
    assert start(case, body=foreign, headers=case.headers(case.owner)).status_code == 404
    monkeypatch.setattr(assessment, '_slots', Mock(acquire=Mock(return_value=False)))
    assert start(case, headers=case.headers(case.owner)).json()['code'] == 'CFDE_ASSESSMENT_BUSY'
    monkeypatch.delenv('TYPESAFE_API_KEY')
    assert start(case, headers=case.headers(case.owner)).json()['code'] == 'CFDE_ASSESSMENT_UNAVAILABLE'
    assert not case.queue.calls


def test_the_fenced_decision_rereads_rows_and_reuses_work_created_after_the_snapshot(case):
    # Snapshot rows are never carried into the write fence: a receipt committed between the two is found there.
    transaction, raced = case.repo.transaction, []
    @contextmanager
    def racing(*args, **kwargs):
        if not raced:
            raced.append(None); raced.append(start(case, headers=case.headers(case.owner)).json())
        with transaction(*args, **kwargs) as tx: yield tx
    case.repo.transaction = racing
    response = start(case)
    assert response.status_code == 202, response.text
    assert response.json()['id'] == raced[1]['id'] and len(case.queue.calls) == 1
    assert assessment._slots._value == 3   # the losing request's reserved slot was returned; the worker keeps one


def test_a_principal_row_keyed_apart_from_its_user_id_falls_back_to_exact_reads(case):
    first = start(case).json()
    with case.repo.transaction() as tx:
        tx.put('principal', 'alias-subject', 'alias-subject', tx.get('principal', case.owner)['data'])
    alias = case.headers('alias-subject')
    assert case.client.get(case.route + '/' + first['id'], headers=alias).json() == read(case, first).json()
    assert start(case, headers=alias).json()['id'] == first['id']   # the owner's pending work, as before


def wait_read(case, result, wait, *, owner=None):
    began = time.monotonic()
    response = case.client.get(case.route + '/' + result['id'] + '?wait=%s' % wait, headers=case.headers(owner or case.owner))
    return response, time.monotonic() - began


def test_long_poll_answers_as_soon_as_the_worker_changes_status(case):
    first = start(case).json()
    worker = threading.Timer(.3, case.queue.run); worker.start()
    try: response, elapsed = wait_read(case, first, 10)
    finally: worker.join(5)
    assert response.status_code == 200, response.text
    assert response.json()['status'] in ('assessing', 'succeeded') and .25 < elapsed < 5
    response, elapsed = wait_read(case, first, 10)   # finished: answered at once from one read
    assert response.json()['status'] == 'succeeded' and elapsed < 1


def test_long_poll_followers_wake_on_their_shared_leader(case):
    case.body['composer'].pop('context')
    start(case)
    owner, _, route, follower = another_draft_check(case, case.body)
    worker = threading.Timer(.3, case.queue.run); worker.start()
    try:
        began = time.monotonic()
        response = case.client.get(route + '/' + follower['id'] + '?wait=10', headers=case.headers(owner))
        elapsed = time.monotonic() - began
    finally: worker.join(5)
    assert response.json()['status'] in ('assessing', 'succeeded') and elapsed < 5


def test_long_poll_rereads_when_its_wait_or_the_deadline_ends(case):
    from round_trips import count_round_trips
    first = start(case).json()
    with count_round_trips() as budget: response, elapsed = wait_read(case, first, 1)
    assert response.json()['status'] == 'preparing' and .9 < elapsed < 3
    assert budget.kinds() == ['read', 'read'] and budget.locked_trips() == 0   # no lease is held while waiting
    with case.repo.transaction() as tx:
        data = tx.get('cfde_assessment', first['id'])['data']
        data['public']['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=.5)).isoformat().replace('+00:00', 'Z')
        tx.put('cfde_assessment', first['id'], case.owner, data)
    response, elapsed = wait_read(case, first, 20)
    assert response.json()['status'] == 'interrupted' and elapsed < 3
    for wait in ('21', '-1', 'soon'):
        response = case.client.get(case.route + '/' + first['id'] + '?wait=' + wait, headers=case.headers(case.owner))
        assert response.status_code == 422 and response.json()['code'] == 'INVALID_QUERY'


def test_wakeups_never_miss_a_change_and_end_at_shutdown(monkeypatch):
    from reveal_backend import redis_notifications
    wakeups = assessment.Wakeups()
    async def scenario():
        seen = wakeups.mark(); wakeups.notify('another')   # any change since the read: answer at once
        began = time.monotonic(); await wakeups.wait('key', seen, 5); assert time.monotonic() - began < .5
        waiting = asyncio.ensure_future(wakeups.wait('key', wakeups.mark(), 5)); await asyncio.sleep(.05)
        threading.Thread(target=wakeups.notify, args=('key',)).start()   # the worker commits on its own thread
        await asyncio.wait_for(waiting, 1); assert wakeups.waiting == {}
        waiting = asyncio.ensure_future(wakeups.wait('key', wakeups.mark(), 5)); await asyncio.sleep(.05)
        monkeypatch.setattr(redis_notifications, '_closing', True)
        wakeups.wake_all(); await asyncio.wait_for(waiting, 1)
        began = time.monotonic(); await wakeups.wait('key', wakeups.mark(), 5); assert time.monotonic() - began < .5
        assert wakeups.waiting == {}
    asyncio.run(scenario())
