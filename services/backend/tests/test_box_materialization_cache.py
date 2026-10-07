"""Exact hosted closure reuse and checker deadlines, with isolated fake sources."""
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from reveal_backend import box_remote, box_research, scientific_account_lint
from reveal_backend.box_mcp import Ledger
from reveal_backend.box_research import AuthoringTiming, HostedResearchClient, ResearchAccessError, authoring_timing_snapshot
from test_box_research import CONTEXT, TOKEN, hosted


def test_identical_complete_closure_reuses_verified_bytes_without_http_after_restart(hosted, monkeypatch):
    client, state, _ = hosted
    path = client.materialize()
    before = list(state['calls'])
    recovered = HostedResearchClient(CONTEXT, TOKEN, client.root, seed_path=client.seed_path,
        execution_id='job:1', opener=client._opener)
    monkeypatch.setattr(recovered, '_http', lambda *a, **k: pytest.fail('Verified closure must need zero HTTP'))
    phases = []
    assert recovered.materialize(progress=phases.append) == path
    assert phases == ['cache_verification'] and state['calls'] == before
    assert (path.parent / 'sources/observations.json').read_bytes() == state['artifact']
    assert TOKEN not in (path.parent / 'materialized.json').read_text()
    recovered.freeze()
    with pytest.raises(ResearchAccessError, match='ended'): recovered.materialize()


@pytest.mark.parametrize('relative', ['evidence-package.json', 'manifest.json', 'sources/observations.json'])
def test_missing_complete_cache_member_resumes_verified_download(hosted, relative):
    client, state, _ = hosted
    path = client.materialize(); member = path.parent / relative
    original = member.read_bytes(); member.unlink(); previous = len(state['calls'])
    assert client.materialize() == path
    assert member.read_bytes() == original
    assert len(state['calls']) > previous


@pytest.mark.parametrize('relative', ['evidence-package.json', 'manifest.json', 'sources/observations.json'])
def test_altered_cache_bytes_fail_closed_without_http(hosted, monkeypatch, relative):
    client, _, _ = hosted
    path = client.materialize(); member = path.parent / relative
    member.chmod(0o600); member.write_bytes(member.read_bytes()+b' ')
    monkeypatch.setattr(client, '_http', lambda *a, **k: pytest.fail('Do not replace altered trusted bytes'))
    with pytest.raises(ResearchAccessError, match='Cached evidence checksum differs'): client.materialize()


@pytest.mark.parametrize('change', ['receipt', 'reuse', 'seed', 'execution', 'scope'])
def test_cache_is_bound_to_exact_selection_seed_and_execution(hosted, change):
    client, _, _ = hosted
    client.materialize()
    if change == 'receipt': client.receipt_ids.add('new-receipt')
    elif change == 'reuse': client.reuse_receipt_ids.add('new-reuse')
    elif change == 'seed': client.seed_path.write_bytes(client.seed_path.read_bytes()+b' ')
    elif change == 'execution': client.execution_id = 'different-attempt'
    else: client.context = {**client.context, 'local_work_id': 'different-work'}
    class RefreshRequired(Exception): pass
    def remote(*args, **kwargs): raise RefreshRequired()
    client._invoke = remote
    with pytest.raises(RefreshRequired): client.materialize()


def test_cached_closure_obeys_expired_caller_deadline_before_any_read(hosted, monkeypatch):
    client, _, _ = hosted
    client.materialize()
    monkeypatch.setattr(client, '_cached_materialization', lambda *a: pytest.fail('No source read after deadline'))
    monkeypatch.setattr(client, '_http', lambda *a, **k: pytest.fail('No HTTP after deadline'))
    with pytest.raises(ResearchAccessError, match='deadline'): client.materialize(deadline=time.monotonic()-1)


def test_interrupted_http_returns_on_absolute_deadline_and_quarantines_late_export(hosted):
    client, state, root = hosted
    client.definitions()
    original = client._opener.open; release = threading.Event(); finished = threading.Event()
    def slow(request, timeout):
        response = original(request, timeout)
        try: assert release.wait(2)
        finally: finished.set()
        return response
    client._opener = SimpleNamespace(open=slow)
    receipts = (root / 'ledger/research-receipts.json').read_bytes()
    started = time.monotonic()
    with pytest.raises(ResearchAccessError, match='deadline'):
        client.materialize(deadline=started+.05)
    assert time.monotonic()-started < .4
    assert not list(client.root.iterdir())
    release.set(); assert finished.wait(1)
    # Drain the isolated worker before checking that no late acceptance occurs.
    until = time.monotonic()+1
    while not client._transport_slots.acquire(blocking=False):
        assert time.monotonic() < until
        time.sleep(.001)
    client._transport_slots.release()
    assert not list(client.root.iterdir())
    assert (root / 'ledger/research-receipts.json').read_bytes() == receipts
    assert not (root / 'ledger/research-operations.json').exists()
    sent = [args for name, args in state['calls'] if name == 'export_evidence_context']
    assert len(sent) == 1
    client._opener = SimpleNamespace(open=original)
    client.materialize()
    sent = [args for name, args in state['calls'] if name == 'export_evidence_context']
    assert len(sent) == 2 and sent[0]['idempotency_key'] == sent[1]['idempotency_key']


def _checker_fixture(tmp_path, monkeypatch, *, execution_deadline=None):
    state = tmp_path/'state'; state.mkdir(); output = tmp_path/'output'; output.mkdir()
    (output/'account-1.json').write_text('{}')
    runtime = {'evidence_package':str(tmp_path/'seed.json'), 'dapper_root':'fixture'}
    if execution_deadline is not None: runtime['execution_deadline_monotonic'] = execution_deadline
    (state/'runtime.json').write_text(json.dumps(runtime))
    (tmp_path/'seed.json').write_text('{"dapper_context":{}}')
    tick = [100.0]
    monkeypatch.setattr(box_remote, 'time', SimpleNamespace(monotonic=lambda: tick[0]))
    monkeypatch.setattr(box_remote, 'STATE', state); monkeypatch.setattr(box_remote, 'OUTPUT', output)
    return tick, state, output


def test_tiny_remaining_execution_budget_stops_before_preflight_or_export(tmp_path, monkeypatch):
    tick, state, output = _checker_fixture(tmp_path, monkeypatch, execution_deadline=100.2)
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', lambda *a, **k: pytest.fail('No subprocess'))
    monkeypatch.setattr(box_remote, 'RESEARCH', SimpleNamespace(materialize=lambda **k: pytest.fail('No export')))
    with pytest.raises(ResearchAccessError, match='Only 0.200 seconds remain'):
        box_remote.lint_tool('account-1.json', Ledger(state/'ledger', 'fixture', 1))
    snapshot = authoring_timing_snapshot(state/'ledger/authoring-timing.json', clock=lambda:tick[0])
    assert snapshot['calls'][-1]['status'] == 'failed'
    assert snapshot['calls'][-1]['phase'] == 'input_read'
    assert (output/'account-1.json').read_text() == '{}'


@pytest.mark.parametrize('remaining_execution', [None, 50])
def test_lint_records_actual_stages_and_reports_only_known_execution_budget(tmp_path, monkeypatch, remaining_execution):
    tick, state, output = _checker_fixture(tmp_path, monkeypatch,
        execution_deadline=None if remaining_execution is None else 100+remaining_execution)
    def preflight(*a, **k): tick[0]+=2; return {'valid':True}
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', preflight)
    def materialize(*, deadline, progress):
        assert deadline == (155 if remaining_execution is None else 150)
        progress('cache_verification'); tick[0]+=1
        progress('export_wait'); tick[0]+=3
        progress('artifact_download'); tick[0]+=4
        return tmp_path/'seed.json'
    monkeypatch.setattr(box_remote, 'RESEARCH', SimpleNamespace(materialize=materialize))
    def lint(*a, **k):
        assert k['timeout'] == (45 if remaining_execution is None else 40)
        tick[0]+=5; return {'valid':True, 'findings':[]}
    monkeypatch.setattr(scientific_account_lint, 'lint_scientific_account', lint)
    result = box_remote.lint_tool('account-1.json', Ledger(state/'ledger', 'fixture', 1))
    assert not result['isError']
    retained = json.loads(Path(result['structuredContent']['report']['path']).read_bytes())
    assert 'execution_budget' not in retained
    if remaining_execution is None:
        assert 'execution_budget' not in result['structuredContent']
    else:
        assert result['structuredContent']['execution_budget'] == {'remaining_seconds':35, 'authoring_call_elapsed_seconds':15}
        assert 'Execution time remaining: 35.000 seconds' in result['content'][-1]['text']
    snapshot = authoring_timing_snapshot(state/'ledger/authoring-timing.json', clock=lambda:tick[0])
    call = snapshot['calls'][-1]
    assert call['status'] == 'completed' and call['elapsed_ms'] == 15000
    assert call['durations_ms'] == {'input_read':0, 'structure_preflight':2000, 'cache_verification':1000,
        'export_wait':3000, 'artifact_download':4000, 'full_lint':5000, 'diagnostic_report':0}


def test_checker_cannot_return_success_after_remaining_budget(tmp_path, monkeypatch):
    tick, state, output = _checker_fixture(tmp_path, monkeypatch, execution_deadline=105)
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', lambda *a, **k: {'valid':True})
    monkeypatch.setattr(box_remote, 'RESEARCH', None)
    def slow(*a, **k): tick[0]+=6; return {'valid':True, 'findings':[]}
    monkeypatch.setattr(scientific_account_lint, 'lint_scientific_account', slow)
    with pytest.raises(ResearchAccessError, match='deadline'):
        box_remote.lint_tool('account-1.json', Ledger(state/'ledger', 'fixture', 1))
    assert not (output/'reports').exists()


def test_authoring_timing_preserves_overlapping_calls_and_interrupted_snapshot(tmp_path):
    path = tmp_path/'timing.json'; tick = [100.0]; clock = lambda:tick[0]
    first = AuthoringTiming(path, 'write_account_draft', 155, clock=clock)
    tick[0]+=1
    second = AuthoringTiming(path, 'lint_account', 156, clock=clock)
    tick[0]+=2; first.phase('structure_preflight')
    tick[0]+=3; second.phase('export_wait')
    first.__exit__(None, None, None)
    tick[0]+=4
    snapshot = authoring_timing_snapshot(path, terminal=True, clock=clock)
    assert len(snapshot['calls']) == 2 and snapshot['complete']
    assert snapshot['calls'][0]['status'] == 'completed'
    assert snapshot['calls'][0]['durations_ms']['structure_preflight'] == 3000
    assert snapshot['calls'][1]['status'] == 'interrupted'
    assert snapshot['calls'][1]['active_phase_elapsed_ms'] == 4000
    assert 'arguments' not in json.dumps(snapshot)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1])
def test_authoring_timing_rejects_nonfinite_or_negative_numbers(tmp_path, bad):
    path = tmp_path/'timing.json'
    timing = AuthoringTiming(path, 'lint_account', 155, clock=lambda:100)
    value = json.loads(path.read_bytes()); value['calls'][0]['elapsed_ms'] = bad
    path.write_text(json.dumps(value))
    snapshot = authoring_timing_snapshot(path, clock=lambda:100)
    assert snapshot['calls'] == [] and snapshot['rejected_records'] == 1 and not snapshot['complete']


def test_authoring_timing_has_explicit_bounded_history(tmp_path):
    path = tmp_path/'timing.json'; tick = [100.0]
    for _ in range(35):
        with AuthoringTiming(path, 'lint_account', 1000, clock=lambda:tick[0]): tick[0]+=1
    snapshot = authoring_timing_snapshot(path, clock=lambda:tick[0])
    assert len(snapshot['calls']) == 32 and snapshot['dropped_calls'] == 3 and not snapshot['complete']
    assert path.stat().st_size < 100_000


def test_transport_workers_have_strict_inflight_bound(hosted):
    client, _, _ = hosted
    from test_box_research import Response
    release = threading.Event(); started = []
    def blocked(request, timeout):
        started.append(request.full_url)
        assert release.wait(2)
        return Response(b'{}', request.full_url)
    client._opener = SimpleNamespace(open=blocked); client.timeout=.02
    try:
        for _ in range(4):
            with pytest.raises(ResearchAccessError, match='deadline'): client._http(CONTEXT['mcp_url'])
        with pytest.raises(ResearchAccessError, match='outstanding reads'): client._http(CONTEXT['mcp_url'])
        assert len(started) == 4
    finally: release.set()


def test_local_cache_rejects_symlink_without_following_or_remote_refresh(hosted, monkeypatch, tmp_path):
    client, _, _ = hosted
    client.materialize(); path = client.root/'sources/observations.json'
    outside = tmp_path/'unrelated'; outside.write_text('unrelated private bytes')
    path.unlink(); path.symlink_to(outside)
    monkeypatch.setattr(client, '_http', lambda *a, **k: pytest.fail('No remote refresh for unsafe local cache'))
    with pytest.raises(ResearchAccessError, match='unsafe'): client.materialize()
    assert outside.read_text() == 'unrelated private bytes'


@pytest.mark.parametrize('remaining_execution', [None, 5])
def test_successful_write_exposes_only_actual_remaining_execution_budget(tmp_path, monkeypatch, remaining_execution):
    tick, state, output = _checker_fixture(tmp_path, monkeypatch,
        execution_deadline=None if remaining_execution is None else 100+remaining_execution)
    def preflight(*a, **k): tick[0]+=2; return {'valid':True}
    monkeypatch.setattr('reveal_backend.authoring_structure.preflight_document', preflight)
    monkeypatch.setattr(box_remote, 'RESEARCH', None)
    monkeypatch.setattr(box_remote.pwd, 'getpwnam', lambda *a: SimpleNamespace(pw_uid=123, pw_gid=456))
    monkeypatch.setattr(box_remote.os, 'chown', lambda *a: None)
    result = box_remote.write_draft_tool('account-1.json', {'scientific_accounts':[{'id':'urn:test:account'}]})
    if remaining_execution is None:
        assert 'structuredContent' not in result
    else:
        assert result['structuredContent']['execution_budget'] == {'remaining_seconds':3, 'authoring_call_elapsed_seconds':2}
        assert 'Execution time remaining: 3.000 seconds' in result['content'][-1]['text']
    assert 'urn:test:account' in (output/'account-1.json').read_text()


def test_freezing_during_http_prevents_materialization_and_receipt_changes(hosted):
    client, _, root = hosted
    client.definitions(); original = client._opener.open
    release = threading.Event(); entered = threading.Event(); errors = []
    def blocked(request, timeout):
        response = original(request, timeout); entered.set()
        assert release.wait(2)
        return response
    client._opener = SimpleNamespace(open=blocked)
    def materialize():
        try: client.materialize()
        except ResearchAccessError as error: errors.append(str(error))
    worker = threading.Thread(target=materialize); worker.start()
    assert entered.wait(1)
    client.freeze(); before = (root/'ledger/research-receipts.json').read_bytes()
    release.set(); worker.join(1)
    assert not worker.is_alive() and errors and 'ended' in errors[0]
    assert not list(client.root.iterdir())
    assert before == (root/'ledger/research-receipts.json').read_bytes()


def test_terminal_runtime_and_capture_retain_active_authoring_phase(tmp_path, monkeypatch):
    tick, state, output = _checker_fixture(tmp_path, monkeypatch, execution_deadline=105)
    timer = AuthoringTiming(state/'ledger/authoring-timing.json', 'lint_account', 105, clock=lambda:tick[0])
    tick[0]+=1; timer.phase('export_wait'); tick[0]+=4
    summary = box_remote.runtime_completion({'timeout_seconds':5, 'max_turns':50},
        100, 'failed', 'execution deadline')
    call = summary['authoring_timing']['calls'][-1]
    assert call['status'] == 'interrupted' and call['phase'] == 'export_wait'
    assert call['elapsed_ms'] == 5000 and call['active_phase_elapsed_ms'] == 4000
    assert 'ledger/authoring-timing.json' in box_remote.collect()['files']
