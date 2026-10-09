"""Private one-attempt audits using isolated SQLite and fake source/model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from unittest.mock import Mock

import httpx
import pytest

import test_application as application
from test_lightning_payload import audit_result, source_state
from test_lightning_stream import provider_bytes, provider_events, event
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
    sent = []
    def send(payload, **kwargs):
        sent.append(audits.serialize_request(payload))
        assert not active
        assert payload['model'] == 'configured-fixture-model' and payload['max_tokens'] == 3000
        assert payload['stream'] is True
        assert 'tools' not in payload and 'isolated-provider-secret' not in json.dumps(payload)
        assert payload['output_config']['format']['type'] == 'json_schema'
        blocks = payload['messages'][0]['content']
        assert len(blocks) == 2 and all(block['type'] == 'text' for block in blocks)
        projected = json.loads(blocks[0]['text'].split('\n', 1)[1])
        assert projected['knowledge_gap']['question'] == 'Could secretion explain this gap?'
        assert projected['selected_graphs_for_future_research'] == ['graph:future']
        assert blocks[1]['text'] == audits.lightning_payload.TASK
        assert 'paragraphs totaling 200-260 words' in blocks[1]['text']
        assert 'CFDE' in payload['system']
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
    assert sent == [data['model_request_json']]
    assert json.loads(data['model_request_json']) == data['model_payload']
    assert len(data['model_request_json'].encode()) <= audits.MAX_REQUEST_BYTES
    assert complete['provenance']['request_sha256'] == hashlib.sha256(data['model_request_json'].encode()).hexdigest()
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


def test_empty_live_response_is_retained_diagnosed_and_never_retried(case):
    value = {'assessment': 'partial', 'limitations': [], 'missing_evidence': [], 'next_steps': [],
        'observations': [], 'recommended_direction': '', 'summary': ''}
    raw = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(value)}],
        'usage': {'input_tokens': 20933, 'output_tokens': 42}}
    case.provider.return_value = raw
    first = start(case); case.queue.run()
    complete = read(case, first['id']); retained = stored(case, first['id'])
    assert complete['status'] == 'failed' and complete['result'] is None
    assert complete['error']['code'] == 'LIGHTNING_RESPONSE_EMPTY'
    assert 'without a rationale or research direction' in complete['error']['detail']
    assert complete['usage'] == raw['usage'] and retained['provider_response'] == raw
    assert retained['source_state'] == source_state()
    assert complete['provenance']['response_sha256'] == digest(raw)
    assert complete['provenance']['prompt_version'] == audits.lightning_payload.PROMPT_VERSION
    assert case.provider.call_count == 1
    assert start(case) == complete and read(case, first['id']) == complete
    assert not case.queue.calls


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
        return httpx.Response(200, content=provider_bytes(response()), headers={'Content-Type': 'text/event-stream'})
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
                yield event('ping')
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


@pytest.mark.parametrize('variant', ['http', 'unknown', 'oversized', 'transport'])
def test_provider_failure_diagnostics_are_bounded_and_private(case, monkeypatch, variant):
    attempts = []
    def handler(request):
        attempts.append(request)
        if variant == 'transport': raise httpx.ConnectError('private transport configuration')
        error_type = 'private error type' if variant == 'unknown' else 'invalid_request_error'
        raw = json.dumps({'error': {'type': error_type, 'message': 'private model request and secret'}})
        if variant == 'oversized': raw += ' ' * audits.MAX_ERROR_BYTES
        return httpx.Response(400, text=raw, headers={'request-id': 'req_fixture_123'})
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    case.provider.side_effect = lambda payload, **kwargs: asyncio.run(audits._provider_request(payload, **kwargs))
    first = start(case); case.queue.run()
    complete = read(case, first['id']); retained = stored(case, first['id'])
    expected = {'category': 'transport'} if variant == 'transport' else {'http_status': 400, 'request_id': 'req_fixture_123'}
    if variant == 'http': expected['error_type'] = 'invalid_request_error'
    assert retained['provider_failure'] == expected
    assert complete['status'] == 'failed' and complete['error']['code'] == 'LIGHTNING_PROVIDER_UNAVAILABLE'
    assert 'provider_failure' not in complete
    assert 'private' not in json.dumps(retained['provider_failure'])
    assert 'private' not in json.dumps(complete['error'])
    assert len(attempts) == 1 and case.provider.call_count == 1
    assert start(case) == complete and not case.queue.calls


def test_transport_preserves_rationale_first_schema_order_and_exact_wire_bytes(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    payload = {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'A species-qualified résumé'}],
        'output_config': {'format': {'type': 'json_schema', 'schema': audits.lightning_payload.RESULT_SCHEMA}}}
    expected = audits.serialize_request(payload).encode(); captured = []
    def handler(request):
        captured.append(request.content)
        decoded = json.loads(request.content)
        assert list(decoded['output_config']['format']['schema']['properties']) == [
            'assessment', 'summary', 'observations', 'recommended_direction', 'missing_evidence', 'next_steps', 'limitations']
        assert request.content == expected
        return httpx.Response(200, content=provider_bytes(response()), headers={'Content-Type': 'text/event-stream'})
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    from time import monotonic
    assert audits.call_provider(payload, deadline=monotonic() + 10) == response()
    assert len(captured) == 1


def test_live_nonblank_rationale_fragment_fails_and_retains_raw_response_without_retry(case):
    value = audit_result(); value['summary'] = 'The CFDE package contains a factor explicitly named '
    raw = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(value)}],
        'usage': {'input_tokens': 20933, 'output_tokens': 763}}
    case.provider.return_value = raw
    first = start(case); case.queue.run()
    complete = read(case, first['id']); retained = stored(case, first['id'])
    assert complete['status'] == 'failed' and complete['result'] is None
    assert complete['error']['code'] == 'LIGHTNING_INCOMPLETE'
    assert 'incomplete rationale' in complete['error']['detail']
    assert complete['usage'] == raw['usage'] and retained['provider_response'] == raw
    assert retained['source_state'] == source_state()
    assert complete['provenance']['response_sha256'] == digest(raw)
    assert case.provider.call_count == 1
    assert start(case) == complete and read(case, first['id']) == complete
    assert not case.queue.calls


def test_streamed_prose_is_private_pending_only_throttled_and_not_a_workspace_invalidation(case):
    first = start(case); observations = []
    def send(payload, *, deadline, on_progress):
        with case.repo.read_transaction() as tx:
            before = tx.get(audits.KIND, first['id'])
            events_before = tx.list('workspace_event', case.owner)
        on_progress('{"summary":"The supplied CFDE rows provide a possible lead')
        pending = read(case, first['id']); progress = pending['progress']
        assert pending['status'] == 'assessing' and pending['result'] is None
        assert progress['summary'] == 'The supplied CFDE rows provide a possible lead'
        assert progress['phase'] == 'writing' and progress['revision'] == 1
        application.api.validate(pending, 'LightningAudit')
        on_progress('{"summary":"The supplied CFDE rows provide a possible lead with more context')
        assert read(case, first['id'])['progress'] == progress  # at most one write per second
        on_progress.last_write -= 1; on_progress.last_attempt -= 1
        on_progress('{"summary":"The supplied CFDE rows provide a possible lead with more context')
        assert read(case, first['id'])['progress']['revision'] == 2
        with case.repo.read_transaction() as tx:
            assert tx.get(audits.KIND, first['id']) == before  # evidence is neither rerewritten nor re-versioned
            assert tx.list('workspace_event', case.owner) == events_before
        other = case.provision()
        with pytest.raises(Problem) as denied: audits.get(case.repo, case.headers(other)['Authorization'], first['id'])
        assert denied.value.status == 404
        observations.append(progress)
        return response()
    case.provider.side_effect = send; case.queue.run()
    complete = read(case, first['id'])
    assert complete['status'] == 'succeeded' and 'progress' not in complete
    assert len(observations) == 1 and case.provider.call_count == 1
    assert stored(case, first['id'])['timings']['first_preview_ms'] >= 0
    with case.repo.read_transaction() as tx: assert tx.get(audits.PROGRESS_KIND, first['id']) is None


@pytest.mark.parametrize('change', ['owner', 'attempt', 'status', 'deadline', 'retirement'])
def test_progress_and_reads_respect_live_control_fences(case, change):
    first = start(case)
    def send(payload, *, deadline, on_progress):
        on_progress('{"summary":"An unfinished first sentence')
        initial = read(case, first['id'])['progress']
        if change == 'retirement':
            with case.repo.transaction() as tx:
                row = tx.get('principal', case.owner); row['data']['retired'] = True
                tx.put('principal', case.owner, case.owner, row['data'])
        else:
            def mutate(data):
                if change == 'owner': data['control_owner'] = 'new-owner'
                elif change == 'attempt': data['attempt_id'] = uid()
                elif change == 'status': data['public']['status'] = 'interrupted'
                elif change == 'deadline': data['deadline_at'] = '2000-01-01T00:00:00Z'
            edit(case, first['id'], mutate)
        on_progress.last_write -= 1; on_progress.last_attempt -= 1
        on_progress('{"summary":"This later text must never be delivered')
        assert on_progress.fenced is True
        if change == 'retirement':
            with pytest.raises(Problem) as denied: read(case, first['id'])
            assert denied.value.status == 401
        else: assert 'progress' not in read(case, first['id'])
        with case.repo.read_transaction() as tx:
            assert tx.get(audits.PROGRESS_KIND, first['id'])['data']['progress'] == initial
        return response()
    case.provider.side_effect = send; case.queue.run()
    assert stored(case, first['id'])['public']['result'] is None
    assert case.provider.call_count == 1


def test_actual_workspace_transfer_erases_progress_and_fences_late_stream(case):
    first = start(case); other = case.provision(); other_auth = case.headers(other)['Authorization']
    def send(payload, *, deadline, on_progress):
        on_progress('{"summary":"Private unfinished rationale')
        assert read(case, first['id'])['progress']['revision'] == 1
        with case.repo.transaction() as tx: tx.transfer(case.owner, other)
        with pytest.raises(Problem) as denied: read(case, first['id'])
        assert denied.value.status == 404
        moved = audits.get(case.repo, other_auth, first['id'])
        assert moved['status'] == 'interrupted' and 'progress' not in moved and moved['result'] is None
        assert moved['error']['code'] == 'WORKSPACE_TRANSFERRED'
        on_progress.last_write -= 1; on_progress.last_attempt -= 1
        on_progress('{"summary":"Late text after workspace transfer')
        assert on_progress.fenced
        with case.repo.read_transaction() as tx: assert tx.get(audits.PROGRESS_KIND, first['id']) is None
        return response()
    case.provider.side_effect = send; case.queue.run()
    assert audits.get(case.repo, other_auth, first['id'])['result'] is None
    assert case.provider.call_count == 1


@pytest.mark.parametrize('failure', ['disconnect', 'refusal', 'max_tokens', 'invalid_reference', 'http_200_error'])
def test_real_stream_failures_clear_preview_and_never_retry(case, monkeypatch, failure):
    complete = response()
    if failure == 'refusal': complete['stop_reason'] = 'refusal'
    if failure == 'max_tokens': complete['stop_reason'] = 'max_tokens'
    if failure == 'invalid_reference': complete['content'][0]['text'] = json.dumps(audit_result('invented'))
    chunks = list(provider_events(complete))
    if failure == 'disconnect': chunks = chunks[:6]
    elif failure == 'http_200_error': chunks = chunks[:6] + [event('error', error={'type': 'overloaded_error', 'message': 'private'})]
    snapshots = []; calls = []
    first = start(case)
    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk
                pending = await asyncio.to_thread(read, case, first['id'])
                if pending.get('progress'): snapshots.append(pending['progress'])
        async def aclose(self): pass
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)['stream'] is True
        return httpx.Response(200, stream=ProviderStream(), headers={'Content-Type': 'text/event-stream'})
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    case.provider.side_effect = lambda payload, **kwargs: asyncio.run(audits._provider_request(payload, **kwargs))
    case.queue.run()
    final = read(case, first['id']); data = stored(case, first['id'])
    assert snapshots and final['status'] == 'failed' and final['result'] is None and 'progress' not in final
    expected = {'disconnect': 'LIGHTNING_INCOMPLETE', 'refusal': 'LIGHTNING_REFUSED', 'max_tokens': 'LIGHTNING_INCOMPLETE',
        'invalid_reference': 'LIGHTNING_REFERENCES_INVALID', 'http_200_error': 'LIGHTNING_PROVIDER_UNAVAILABLE'}
    assert final['error']['code'] == expected[failure]
    with case.repo.read_transaction() as tx: assert tx.get(audits.PROGRESS_KIND, first['id']) is None
    if failure in ('disconnect', 'http_200_error'):
        assert data['provider_partial_response']['content'][0]['text']
        assert 'provider_partial_response' not in final
    else: assert data['provider_response'] == complete
    assert read(case, first['id']) == final and start(case) == final
    assert len(calls) == 1 and case.provider.call_count == 1 and not case.queue.calls


def test_revision_poll_returns_newer_preview_without_wait_and_never_starts_inference(case, monkeypatch):
    first = start(case); data = stored(case, first['id'])
    assert audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    writer('{"summary":"A first visible sentence')
    async def unexpected_wait(*args): raise AssertionError('new progress was already available')
    monkeypatch.setattr(audits.wakeups, 'wait', unexpected_wait)
    pending = asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=20, after_revision=0))
    assert pending['progress']['revision'] == 1 and pending['result'] is None
    case.provider.assert_not_called()


def test_revision_poll_wakes_on_next_chunk_with_no_database_lease(case, monkeypatch):
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    writer('{"summary":"A first visible sentence')
    original_wait = audits.wakeups.wait; calls = []
    async def advance(key, seen, timeout):
        calls.append(timeout)
        # A new writer obtains the transaction fence while this request waits.
        writer.last_write -= 1; writer.last_attempt -= 1
        await asyncio.to_thread(writer, '{"summary":"A first visible sentence and another one')
        await original_wait(key, seen, timeout)
    monkeypatch.setattr(audits.wakeups, 'wait', advance)
    result = asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=20, after_revision=1))
    assert result['progress']['revision'] == 2 and len(calls) == 1 and calls[0] <= 1
    case.provider.assert_not_called()


def test_revision_poll_sees_cross_process_progress_without_local_notification(case, monkeypatch):
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    calls = []
    async def remote_write(key, seen, timeout):
        calls.append(timeout)
        monkeypatch.setattr(audits.wakeups, 'notify', lambda identity: None)
        await asyncio.to_thread(writer, '{"summary":"Cross process text')
    monkeypatch.setattr(audits.wakeups, 'wait', remote_write)
    result = asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=20, after_revision=0))
    assert result['progress']['summary'] == 'Cross process text' and calls == [1]
    case.provider.assert_not_called()


def test_revision_poll_does_not_miss_update_during_initial_read(case, monkeypatch):
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    original_get = audits.get; reads = []
    def racing_get(*args, **kwargs):
        value = original_get(*args, **kwargs); reads.append(True)
        if len(reads) == 1: writer('{"summary":"Progress committed after the read')
        return value
    monkeypatch.setattr(audits, 'get', racing_get)
    result = asyncio.run(audits.wait_get(case.repo, case.authorization, first['id'], wait=20, after_revision=0))
    assert result['progress']['revision'] == 1 and len(reads) == 2
    case.provider.assert_not_called()


@pytest.mark.parametrize('query', ['after_revision=-1', 'after_revision=1.5', 'after_revision=true', 'after_revision=9999999999999'])
def test_progress_query_validation(case, query):
    first = start(case)
    response = case.client.get('/v1/lightning-audits/' + first['id'] + '?' + query, headers=case.headers(case.owner))
    assert response.status_code == 422 and response.json()['code'] == 'INVALID_QUERY'
    case.provider.assert_not_called()


def test_timeout_after_real_text_cancels_one_stream_and_retains_fragment_privately(monkeypatch):
    from time import monotonic
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    calls = []; previews = []
    class StalledAfterText(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for chunk in list(provider_events(response()))[:6]: yield chunk
            await asyncio.sleep(10)
        async def aclose(self): self.closed = True
    stream = StalledAfterText()
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    started = monotonic()
    with pytest.raises(Problem) as error:
        audits.call_provider({'model': 'fixture', 'stream': True}, deadline=started + .06, on_progress=previews.append)
    assert error.value.code == 'LIGHTNING_TIMEOUT' and len(calls) == 1 and stream.closed
    assert previews and error.value.partial_response['content'][0]['text'] == previews[-1]
    assert monotonic() - started < .4 and 'private' not in error.value.detail


def test_optional_preview_write_does_not_extend_provider_deadline(monkeypatch):
    from time import monotonic
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    release = threading.Event(); entered = threading.Event(); finished = threading.Event()
    def blocked_preview(text):
        entered.set()
        try: release.wait(2)
        finally: finished.set()
    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b''.join(list(provider_events(response()))[:6])
            await asyncio.sleep(10)
        async def aclose(self): pass
    def handler(request): return httpx.Response(200, stream=StalledStream())
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    started = monotonic()
    try:
        with pytest.raises(Problem) as error:
            audits.call_provider({'model': 'fixture', 'stream': True}, deadline=started + .05, on_progress=blocked_preview)
        assert error.value.code == 'LIGHTNING_TIMEOUT' and entered.is_set()
        assert monotonic() - started < .2
        assert not finished.is_set()  # The private loop did not join its pending database writer.
    finally:
        release.set()
        assert finished.wait(1)


def test_busy_optional_preview_storage_does_not_fail_paid_completion(case, monkeypatch):
    from reveal_backend.repository import FenceBusy
    writer = Mock(side_effect=FenceBusy('busy preview fence'))
    monkeypatch.setattr(audits, '_write_progress', writer)
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(200, content=provider_bytes(response()))
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    case.provider.side_effect = lambda payload, **kwargs: asyncio.run(audits._provider_request(payload, **kwargs))
    first = start(case); case.queue.run()
    value = read(case, first['id'])
    assert value['status'] == 'succeeded' and value['result'] == audit_result() and 'progress' not in value
    assert writer.call_count == 2  # first write, then forced final flush; not each token
    assert len(attempts) == 1 and case.provider.call_count == 1


def test_detail_reads_one_compact_authorized_projection_and_defers_reference_rows(case, monkeypatch):
    from reveal_backend.repository import Transaction
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing',
        public_fields={'evidence_references': [{'id': 'E1', 'value': 'retained evidence'}]},
        source_state={'private': 'source snapshot'}, model_payload={'private': 'model prompt'})
    original = Transaction.execute; reads = []
    def execute(tx, sql, params=()):
        reads.append(sql)
        return original(tx, sql, params)
    monkeypatch.setattr(Transaction, 'execute', execute)
    result = read(case, first['id'])
    assert len(reads) == 1 and reads[0].startswith('SELECT kind,id,owner_id,version,CASE')
    assert 'JSON_SET' in reads[0] and "JSON_EXTRACT((CASE" in reads[0]
    assert result['evidence_references'] == [] and 'source_state' not in result and 'model_payload' not in result
    monkeypatch.setattr(Transaction, 'execute', original)
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='succeeded')
    assert read(case, first['id'])['evidence_references'] == [{'id': 'E1', 'value': 'retained evidence'}]


def test_progress_write_round_trips_only_compact_control_principal_and_preview(case, monkeypatch):
    from reveal_backend.repository import Transaction
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    original = Transaction.execute; statements = []
    def execute(tx, sql, params=()):
        statements.append(sql)
        return original(tx, sql, params)
    monkeypatch.setattr(Transaction, 'execute', execute)
    writer('{"summary":"Small streamed content')
    assert len(statements) == 3
    assert statements[0].startswith('SELECT kind,id,owner_id,version,payload')
    assert statements[1].startswith('SELECT owner_id,JSON_OBJECT')
    assert statements[2].startswith('INSERT INTO reveal_records')
    assert writer.revision == 1


def test_slow_optional_preview_does_not_delay_or_fail_fast_complete_stream(monkeypatch):
    from time import monotonic
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'transport-test-private-key')
    release = threading.Event(); entered = threading.Event(); finished = threading.Event()
    def blocked_preview(text):
        entered.set()
        try: release.wait(2)
        finally: finished.set()
    def handler(request): return httpx.Response(200, content=provider_bytes(response()))
    factory = httpx.AsyncClient
    monkeypatch.setattr(audits.httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs))
    started = monotonic()
    try:
        value = audits.call_provider({'model': 'fixture', 'stream': True}, deadline=started + .5, on_progress=blocked_preview)
        assert value == response() and entered.is_set() and not finished.is_set()
        assert monotonic() - started < .15
    finally:
        release.set()
        assert finished.wait(1)


def test_late_preview_callback_cannot_overlap_revision_writes(case, monkeypatch):
    first = start(case); data = stored(case, first['id'])
    audits._update(case.repo, case.owner, first['id'], data['attempt_id'], status='assessing')
    writer = audits._ProgressWriter(case.repo, case.owner, first['id'], data['attempt_id'])
    entered = threading.Event(); release = threading.Event(); calls = []
    original = audits._write_progress
    def blocked_write(*args):
        calls.append(args); entered.set()
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(audits, '_write_progress', blocked_write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer, '{"summary":"First provisional sentence')
        try:
            assert entered.wait(1)
            writer.finish('{"summary":"Later provisional sentence')
            assert len(calls) == 1 and writer.revision == 0
        finally: release.set()
        future.result(1)
    assert writer.revision == 1
    assert read(case, first['id'])['progress']['summary'] == 'First provisional sentence'
