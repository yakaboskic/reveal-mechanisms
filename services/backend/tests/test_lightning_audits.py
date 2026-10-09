"""Private one-attempt audits using isolated SQLite and fake source/model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import Mock

import httpx
import pytest

import test_application as application
from test_lightning_payload import audit_result, source_state
from reveal_backend import lightning_audits as audits, user_inputs
from reveal_backend.auth import Problem, owned
from reveal_backend.repository import digest, now, uid


class Queue:
    def __init__(self): self.calls = []
    def submit(self, fn, *args): self.calls.append((fn, args))
    def run(self):
        fn, args = self.calls.pop(0); fn(*args)


def response():
    return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(audit_result())}],
            'usage': {'input_tokens': 800, 'output_tokens': 100}}


@pytest.fixture
def case(monkeypatch):
    instance = application.ApplicationTests(); instance.setUp()
    monkeypatch.setenv('REVEAL_LIGHTNING_ENABLED', 'true')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'isolated-provider-secret')
    monkeypatch.setenv('REVEAL_CLAUDE_MODEL', 'configured-fixture-model')
    instance.queue = Queue(); monkeypatch.setattr(audits, '_pool', instance.queue)
    monkeypatch.setattr(audits, '_slots', threading.BoundedSemaphore(4))
    instance.owner = instance.provision(); instance.saved = instance.draft(instance.owner)
    instance.authorization = instance.headers(instance.owner)['Authorization']
    instance.body = {'draft_id': instance.saved['id'], 'draft_version': 1}; instance.key = uid()
    with instance.repo.transaction() as tx:
        row = tx.get('draft', instance.saved['id'])
        row['data']['composer'].update(source_gap={'id': 'gap:1'}, eaggl_anchors=[{'reference': {'source_id': 'factor:1'}}],
                                     context='My original research notes', selected_kgs=['graph:future'])
        tx.put('draft', instance.saved['id'], instance.owner, row['data'])
    instance.prepared = {'state': source_state(), 'coverage': source_state()['coverage'], 'reference_generation_id': 'a' * 64}
    instance.builder = Mock(return_value=instance.prepared)
    monkeypatch.setattr(audits.cfde_assessment_state, 'build_state', instance.builder)
    instance.provider = Mock(return_value=response()); monkeypatch.setattr(audits, 'call_provider', instance.provider)

    def freeze(tx, actor, body, *, retrieval_mode, write):
        assert write is False and retrieval_mode == 'progressive'
        draft = owned(tx, 'draft', body['draft_id'], actor['user_id'])['data']
        frozen = {'id': uid(), 'owner_user_id': actor['user_id'], 'source_draft_id': draft['id'],
            'source_draft_version': draft['version'], 'composer': deepcopy(draft['composer']),
            'document': {'knowledge_gaps': [{'id': 'dapper:KnowledgeGap.' + 'a' * 32, 'text': 'Could secretion explain this gap?'}]},
            'user_inputs': user_inputs.resolve(tx, actor['user_id'], draft['composer']), 'retrieval_mode': retrieval_mode}
        binding = {'anchors': [{'reference_generation_id': 'a' * 64}]}
        return frozen, binding, [('request', frozen['id'], actor['user_id'], frozen), ('request_binding', frozen['id'], actor['user_id'], binding)]
    instance.freeze = freeze; instance.gate = Mock()
    try: yield instance
    finally: instance.doCleanups()


def start(case, *, body=None, key=None, authorization=None):
    return audits.start(case.repo, object(), authorization or case.authorization, body or case.body, key or case.key,
                        freeze=case.freeze, reload_gate=case.gate)


def read(case, identity):
    return audits.get(case.repo, case.authorization, identity)


def stored(case, identity):
    with case.repo.read_transaction() as tx: return tx.get(audits.KIND, identity)['data']


def edit(case, identity, fn):
    with case.repo.transaction() as tx:
        row = tx.get(audits.KIND, identity); fn(row['data']); tx.put(audits.KIND, identity, row['owner'], row['data'])


def test_frozen_input_single_call_and_exact_private_retention(case):
    active = []
    transaction = case.repo.transaction
    @contextmanager
    def tracked(*args, **kwargs):
        with transaction(*args, **kwargs) as tx:
            active.append(True)
            try: yield tx
            finally: active.pop()
    case.repo.transaction = tracked
    def build(*args, **kwargs):
        assert not active; assert args[1]['context'] == 'My original research notes'
        return case.prepared
    def send(payload, **kwargs):
        assert not active
        assert payload['model'] == 'configured-fixture-model' and payload['max_tokens'] == 3000
        assert 'tools' not in payload and 'isolated-provider-secret' not in json.dumps(payload)
        assert payload['output_config']['format']['type'] == 'json_schema'
        return response()
    case.builder.side_effect = build; case.provider.side_effect = send
    first = start(case); assert first['status'] == 'preparing'; assert case.provider.call_count == 0
    # Later editor deletion cannot erase a frozen audit's input or authority.
    with case.repo.transaction() as tx: tx.remove('draft', case.saved['id'])
    case.queue.run()
    complete = read(case, first['id']); data = stored(case, first['id'])
    application.api.validate(first, 'LightningAudit'); application.api.validate(complete, 'LightningAudit')
    assert complete['status'] == 'succeeded'; assert complete['result'] == audit_result()
    assert complete['usage'] == {'input_tokens': 800, 'output_tokens': 100}
    assert data['source_state'] == source_state() and data['response'] == audit_result()
    assert set(data['timings']) == {'preparation_ms', 'provider_ms', 'total_ms'}
    assert complete['provenance']['source_state_sha256'] == digest(data['source_state'])
    assert complete['provenance']['request_sha256'] == digest(data['model_payload'])
    assert case.provider.call_count == 1
    with case.repo.read_transaction() as tx:
        assert tx.list('job') == [] and tx.list('account') == []
        assert tx.get('research_pin', complete['research_request_id'])['data']['state'] == 'active'
    assert start(case) == complete


def test_replay_survives_configuration_and_draft_changes(case, monkeypatch):
    first = start(case); case.queue.run()
    monkeypatch.setenv('REVEAL_LIGHTNING_ENABLED', 'false'); monkeypatch.delenv('ANTHROPIC_API_KEY')
    case.freeze = Mock(side_effect=AssertionError('replay froze again'))
    assert start(case)['id'] == first['id']
    with pytest.raises(Problem) as conflict: start(case, body={**case.body, 'draft_version': 2})
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert len(case.queue.calls) == 0 and case.provider.call_count == 1


def test_flag_default_and_unconfigured_rejections_create_nothing(case, monkeypatch):
    monkeypatch.delenv('REVEAL_LIGHTNING_ENABLED')
    with pytest.raises(Problem) as error: start(case)
    assert error.value.code == 'LIGHTNING_DISABLED'
    monkeypatch.setenv('REVEAL_LIGHTNING_ENABLED', 'true'); monkeypatch.delenv('ANTHROPIC_API_KEY')
    with pytest.raises(Problem) as error: start(case)
    assert error.value.code == 'LIGHTNING_UNAVAILABLE'
    with case.repo.read_transaction() as tx: assert tx.list(audits.KIND) == []
    case.provider.assert_not_called()


def test_cold_catalog_warms_outside_read_or_write_transactions(case, monkeypatch):
    active = []
    for name in ('transaction', 'read_transaction'):
        original = getattr(case.repo, name)
        @contextmanager
        def tracked(*args, _original=original, **kwargs):
            with _original(*args, **kwargs) as tx:
                active.append(True)
                try: yield tx
                finally: active.pop()
        monkeypatch.setattr(case.repo, name, tracked)
    class ColdCatalog:
        loaded = False
        def load(self):
            assert not active
            self.loaded = True
    catalog = ColdCatalog(); freeze = case.freeze
    def frozen(*args, **kwargs):
        assert catalog.loaded
        return freeze(*args, **kwargs)
    first = audits.start(case.repo, catalog, case.authorization, case.body, case.key, freeze=frozen, reload_gate=case.gate)
    case.queue.run()
    assert read(case, first['id'])['status'] == 'succeeded'


def test_listing_and_reconciliation_never_hydrate_all_audit_artifacts(case, monkeypatch):
    first = start(case); case.queue.run()
    from reveal_backend.repository import Transaction
    original = Transaction.list
    def no_audit_list(tx, kind, *args, **kwargs):
        assert kind != audits.KIND
        return original(tx, kind, *args, **kwargs)
    monkeypatch.setattr(Transaction, 'list', no_audit_list)
    history = audits.listing(case.repo, case.authorization, paginate=application.api.page)
    application.api.validate(history, 'LightningAuditList')
    assert history['items'][0]['id'] == first['id']
    assert audits.reconcile(case.repo) == 0


def test_concurrent_duplicate_requests_dispatch_one_worker(case):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(case), range(2)))
    assert results[0]['id'] == results[1]['id'] and len(case.queue.calls) == 1
    case.queue.run(); assert case.provider.call_count == 1


def test_bounded_dispatch_rejects_without_freezing_or_writing(case, monkeypatch):
    semaphore = threading.BoundedSemaphore(1); semaphore.acquire()
    monkeypatch.setattr(audits, '_slots', semaphore)
    case.freeze = Mock(side_effect=AssertionError('busy request froze input'))
    with pytest.raises(Problem) as error: start(case)
    assert error.value.code == 'LIGHTNING_BUSY'
    with case.repo.read_transaction() as tx: assert tx.list(audits.KIND) == []
    case.provider.assert_not_called()


def test_daily_quota_counts_creation_time_and_allows_idempotent_replay(case):
    first = start(case); case.queue.run()
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace('+00:00', 'Z')
    with case.repo.transaction() as tx:
        tx.insert_many([(audits.KIND, uid(), case.owner, {'public': {'created_at': old}}) for _ in range(20)])
    second = start(case, key=uid()); case.queue.run()  # recently touched old rows consume no new-call allowance
    with case.repo.transaction() as tx:
        tx.insert_many([(audits.KIND, uid(), case.owner, {'public': {'created_at': now()}}) for _ in range(18)])
    with pytest.raises(Problem) as error: start(case, key=uid())
    assert error.value.code == 'LIGHTNING_QUOTA'
    assert start(case)['id'] == first['id']
    assert case.provider.call_count == 2


def test_owner_auth_and_private_history(case):
    first = start(case); other = case.provision(); other_auth = case.headers(other)['Authorization']
    with pytest.raises(Problem) as error: audits.get(case.repo, other_auth, first['id'])
    assert error.value.status == 404
    assert audits.listing(case.repo, other_auth)['items'] == []
    assert audits.listing(case.repo, case.authorization)['items'][0]['id'] == first['id']
    with pytest.raises(Problem) as error: start(case, authorization=other_auth, key=uid())
    assert error.value.status == 404
    case.queue.run()


@pytest.mark.parametrize('failure', ['timeout', 'refusal', 'incomplete', 'reference', 'bad_json'])
def test_provider_failures_are_explicit_and_never_automatically_retried(case, failure):
    value = response()
    if failure == 'timeout': case.provider.side_effect = Problem(504, 'LIGHTNING_TIMEOUT', 'The audit deadline expired.')
    elif failure == 'refusal': value['stop_reason'] = 'refusal'
    elif failure == 'incomplete': value['stop_reason'] = 'max_tokens'
    elif failure == 'reference': value['content'][0]['text'] = json.dumps(audit_result('invented'))
    elif failure == 'bad_json': value['content'][0]['text'] = 'not json'
    case.provider.return_value = value
    first = start(case); case.queue.run()
    result = read(case, first['id']); assert result['status'] == 'failed' and result['result'] is None and result['error']
    for _ in range(3): assert read(case, first['id']) == result
    assert start(case) == result
    assert case.provider.call_count == 1 and len(case.queue.calls) == 0
    audits.reconcile(case.repo)
    with case.repo.read_transaction() as tx: assert tx.get('research_pin', first['research_request_id'])['data']['state'] == 'released'


def test_oversized_input_and_changed_generation_never_call_provider(case):
    case.prepared['state']['user_inputs']['context']['text'] = 'x' * 100001
    first = start(case); case.queue.run()
    assert read(case, first['id'])['error']['code'] == 'LIGHTNING_INPUT_TOO_LARGE'
    case.prepared['state'] = source_state(); case.prepared['reference_generation_id'] = 'b' * 64
    second = start(case, key=uid()); case.queue.run()
    assert read(case, second['id'])['error']['code'] == 'SOURCE_REVISION_CHANGED'
    case.provider.assert_not_called()


def test_expired_pending_receipt_has_no_model_retry_and_pin_releases(case):
    first = start(case)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
    edit(case, first['id'], lambda data: data.update(deadline_at=past))
    assert read(case, first['id'])['status'] == 'interrupted'
    case.queue.run(); case.provider.assert_not_called()
    audits.reconcile(case.repo)
    assert stored(case, first['id'])['public']['status'] == 'interrupted'
    with case.repo.read_transaction() as tx: assert tx.get('research_pin', first['research_request_id'])['data']['state'] == 'released'
    assert audits.reconcile(case.repo) == 0


@pytest.mark.parametrize('change', ['owner', 'attempt', 'status', 'deadline'])
def test_inflight_completion_cannot_cross_control_fence(case, change):
    first = start(case)
    def send(*args, **kwargs):
        def mutate(data):
            if change == 'owner': data['control_owner'] = 'new-owner'
            if change == 'attempt': data['attempt_id'] = uid()
            if change == 'status': data['public']['status'] = 'interrupted'
            if change == 'deadline': data['deadline_at'] = '2000-01-01T00:00:00Z'
        edit(case, first['id'], mutate)
        return response()
    case.provider.side_effect = send; case.queue.run()
    data = stored(case, first['id'])
    assert data['public']['result'] is None and 'response' not in data


def test_successful_audit_pin_expires_independently_of_child_pin(case):
    first = start(case); case.queue.run()
    edit(case, first['id'], lambda data: data['public'].update(continuation_expires_at='2000-01-01T00:00:00Z'))
    with case.repo.transaction() as tx: tx.put('research_pin', 'child-request', case.owner, {'state': 'active'})
    audits.reconcile(case.repo)
    with case.repo.read_transaction() as tx:
        assert tx.get('research_pin', first['research_request_id'])['data']['state'] == 'released'
        assert tx.get('research_pin', 'child-request')['data']['state'] == 'active'
    assert read(case, first['id'])['status'] == 'succeeded'


def test_long_poll_holds_no_database_lease_and_wakes_on_completion(case, monkeypatch):
    first = start(case); original_wait = audits.wakeups.wait
    async def finish(key, seen, timeout):
        # The worker can take its write transactions while this request waits.
        await asyncio.to_thread(case.queue.run)
        await original_wait(key, seen, timeout)
    monkeypatch.setattr(audits.wakeups, 'wait', finish)
    result = asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=10))
    assert result['status'] == 'succeeded' and case.provider.call_count == 1
    assert asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=10)) == result


@pytest.mark.parametrize('variant', ['ok', 'http', 'timeout', 'oversized', 'invalid'])
def test_transport_one_request_and_bounded_response(monkeypatch, variant):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    attempts = []
    def handler(request):
        attempts.append(request)
        assert request.headers['x-api-key'] == 'transport-test-private-key'
        assert request.url == 'https://api.anthropic.com/v1/messages'
        if variant == 'timeout': raise httpx.ReadTimeout('private transport internals')
        if variant == 'http': return httpx.Response(429, text='private provider diagnostics')
        if variant == 'oversized': return httpx.Response(200, content=b'x' * (audits.MAX_RESPONSE_BYTES + 1))
        if variant == 'invalid': return httpx.Response(200, text='not JSON')
        return httpx.Response(200, json=response())
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    from time import monotonic
    if variant == 'ok': assert audits.call_provider({'model': 'fixture'}, deadline=monotonic() + 10) == response()
    else:
        with pytest.raises(Problem) as error: audits.call_provider({'model': 'fixture'}, deadline=monotonic() + 10)
        assert 'private' not in error.value.detail
    assert len(attempts) == 1


def test_absolute_provider_deadline_cancels_an_idle_read_after_chunks(monkeypatch):
    from time import monotonic
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    class StalledStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for _ in range(3):
                yield b' ' * 16384
                await asyncio.sleep(.01)
            # A per-read deadline alone can keep resetting on the previous chunks.
            await asyncio.sleep(10)
        async def aclose(self): self.closed = True
    stream = StalledStream(); attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(200, stream=stream)
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    started = monotonic()
    with pytest.raises(Problem) as error:
        audits.call_provider({'model': 'fixture'}, deadline=started + .06)
    assert error.value.code == 'LIGHTNING_TIMEOUT'
    assert monotonic() - started < .4
    assert stream.closed and len(attempts) == 1
