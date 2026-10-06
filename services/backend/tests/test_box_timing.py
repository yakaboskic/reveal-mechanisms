"""Synthetic streams only: receipt intervals are not provider compute estimates."""
import base64
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from reveal_backend import box_remote
from reveal_backend.box_stream import ClaudeStream, StreamProtocolError
from reveal_backend.box_timing import MAX_ACTIVE_TOOLS, RuntimeTiming, provider_metrics
from reveal_backend.public_tool_activity import tool_failed, tool_result_payload


def line(value):
    return json.dumps(value).encode() + b'\n'


def tool(identity, *, result=False, failed=False):
    block = {'type': 'tool_result', 'tool_use_id': identity, 'is_error': failed} if result else {
        'type': 'tool_use', 'id': identity, 'name': 'Read', 'input': {'private': 'PRIVATE_INPUT'}}
    return {'type': 'user' if result else 'assistant', 'message': {'content': [block]}}


def test_stream_lifecycle_and_notifications_count_without_retaining_content():
    tick = [0.0]
    timing = RuntimeTiming(clock=lambda: tick[0])
    parser = ClaudeStream(timing=timing, secrets=('PRIVATE_CREDENTIAL',))
    events = []
    for index, subtype in enumerate(('message_start', 'content_block_start', 'content_block_delta',
                                      'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop'), 1):
        tick[0] = index
        events += parser.feed(line({'type': 'stream_event', 'event': {'type': subtype,
            'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE_THOUGHT', 'signature': 'PRIVATE_CREDENTIAL'},
            'message': {'content': 'PRIVATE_PROMPT'}}}))
    for kind in ('rate_limit_event', 'tool_progress', 'tool_use_summary', 'auth_status', 'api_retry'):
        events += parser.feed(line({'type': kind, 'output': 'PRIVATE_STDERR', 'retry_after': 'PRIVATE_CREDENTIAL'}))
    events += parser.feed(line({'type': 'system', 'subtype': 'api_retry', 'message': 'PRIVATE_ERROR'}))
    timing.bytes_received('stderr', 42)
    tick[0] = 12
    snapshot = timing.snapshot()
    assert events == []
    assert snapshot['stream_events']['lifecycle']['content_block_delta'] == 2
    assert snapshot['stream_events']['lifecycle']['message_start'] == 1
    assert snapshot['stream_events']['lifecycle']['message_stop'] == 1
    assert snapshot['stream_events']['lifecycle_receipts']['content_block_delta'] == {'first_elapsed_seconds': 3, 'last_elapsed_seconds': 4}
    assert snapshot['stream_events']['lifecycle_receipts']['message_stop'] == {'first_elapsed_seconds': 7, 'last_elapsed_seconds': 7}
    assert snapshot['stream_events']['message_open_observed'] is False
    assert snapshot['stream_events']['notifications']['api_retry'] == 2
    assert all(value == 1 for name, value in snapshot['stream_events']['notifications'].items() if name != 'api_retry')
    assert snapshot['stream_events']['silence_seconds'] == 5
    assert snapshot['streams']['stderr']['bytes'] == 42
    assert snapshot['streams']['stdout']['chunks'] == 13
    assert 'PRIVATE' not in json.dumps(snapshot)
    assert 'provider, network or reasoning time' in snapshot['measurement']


def test_parallel_tool_union_is_distinct_from_completed_duration_sum():
    tick = [0.0]
    timing = RuntimeTiming(clock=lambda: tick[0])
    for moment, event in ((1, tool('a')), (3, tool('b')), (6, tool('a', result=True)),
                           (8, tool('b', result=True, failed=True))):
        tick[0] = moment
        timing.observe(event)
    tick[0] = 10
    tools = timing.snapshot()['tools']
    assert tools == {'started': 2, 'completed': 2, 'failed': 1, 'active': 0, 'peak_active': 2,
        'tracking_complete': True, 'active_union_seconds': 7, 'completed_duration_sum_seconds': 10,
        'longest_completed_seconds': 5, 'oldest_active_seconds': None}


def test_active_intervals_and_silence_remain_observable_without_terminal_result():
    tick = [0.0]
    timing = RuntimeTiming(clock=lambda: tick[0])
    parser = ClaudeStream(timing=timing)
    tick[0] = 2
    parser.feed(line(tool('unfinished')))
    tick[0] = 9
    with pytest.raises(StreamProtocolError, match='without a terminal'):
        parser.finish()
    summary = timing.snapshot()
    assert summary['tools']['active'] == 1
    assert summary['tools']['active_union_seconds'] == summary['tools']['oldest_active_seconds'] == 7
    assert summary['streams']['stdout']['silence_seconds'] == 7
    assert summary['stream_events']['silence_seconds'] == 7
    assert provider_metrics(parser.result)['usage'] is None
    assert provider_metrics(parser.result)['cost_usd'] is None


def test_timing_memory_and_snapshot_are_bounded_when_stream_has_many_distinct_tools():
    timing = RuntimeTiming(clock=lambda: 1)
    for index in range(MAX_ACTIVE_TOOLS + 50):
        timing.observe(tool('PRIVATE_ID_' + str(index)))
    for _ in range(1000):
        timing.observe({'type': 'rate_limit_event', 'payload': 'PRIVATE_RATE_LIMIT'})
        timing.observe({'type': 'PRIVATE_UNKNOWN', 'payload': {'arbitrary': 'PRIVATE'}})
    summary = timing.snapshot()
    assert len(timing.active) == MAX_ACTIVE_TOOLS
    assert summary['tools']['tracking_complete'] is False
    assert summary['tools']['started'] == MAX_ACTIVE_TOOLS + 50
    assert summary['stream_events']['notifications']['rate_limit_event'] == 1000
    assert len(json.dumps(summary)) < 3000
    assert 'PRIVATE' not in json.dumps(summary)
    assert all(isinstance(identity, bytes) and len(identity) == 32 for identity in timing.active)


def test_provider_projection_preserves_only_reported_finite_nonnegative_numbers():
    source = {'duration_ms': 123.456, 'duration_api_ms': 100, 'total_cost_usd': 0,
        'num_turns': 4, 'subtype': 'success', 'usage': {'input_tokens': 19, 'output_tokens': 7,
        'cache_read_input_tokens': 0, 'PRIVATE': 'PRIVATE', 'reasoning': {'tokens': 1000}},
        'result': 'PRIVATE', 'modelUsage': {'PRIVATE_MODEL': 'PRIVATE'}}
    report = provider_metrics(source)
    assert report == {'duration_ms': 123.456, 'duration_api_ms': 100, 'cost_usd': 0,
        'num_turns': 4, 'subtype': 'success', 'usage': {'input_tokens': 19, 'output_tokens': 7, 'cache_read_input_tokens': 0}}
    for invalid in (True, False, -1, float('nan'), float('inf'), '123', {}, [], 10 ** 1000):
        value = provider_metrics({'duration_ms': invalid, 'duration_api_ms': invalid,
            'total_cost_usd': invalid, 'num_turns': invalid, 'usage': {'input_tokens': invalid},
            'subtype': 'PRIVATE_CREDENTIAL'})
        assert all(item is None for item in value.values())
    assert provider_metrics({'usage': {'input_tokens': 1.0}})['usage'] is None


@pytest.mark.parametrize('failed_tool', ['lint_account', 'write_account_draft'])
def test_repair_phase_survives_reads_until_successful_lint(failed_tool):
    failed = box_remote.observable_activity('tool_result', {'tool_name': 'mcp__reveal__' + failed_tool,
        'status': 'error'}, {'result': {'is_error': True, 'content': 'PRIVATE_FINDINGS'}})
    read = box_remote.observable_activity('tool_call', {'tool_name': 'Read'}, {}, failed)
    finished_read = box_remote.observable_activity('tool_result', {'tool_name': 'Read'}, {'result': {}}, read)
    request = {'kind': 'research', 'timeout_seconds': 900}
    assert 'repairing the draft' in box_remote.deadline_reason(request, finished_read)
    assert 'PRIVATE' not in json.dumps(finished_read)
    fixed = box_remote.observable_activity('tool_result', {'tool_name': 'mcp__reveal__lint_account',
        'status': 'completed'}, {'result': {}}, finished_read)
    assert 'repairing' not in box_remote.deadline_reason(request, fixed)


@pytest.mark.parametrize('name', ['lint_account', 'write_account_draft'])
@pytest.mark.parametrize('inner', [
    {'isError': True, 'content': [{'type': 'text', 'text': 'Repair needed.'}]},
    {'isError': False, 'structuredContent': {'valid': False}, 'content': []},
    {'valid': False, 'findings': [{'message': 'Repair needed.'}]},
])
def test_wrapped_mcp_authoring_failures_share_public_status_timing_and_repair_phase(name, inner):
    parser = ClaudeStream()
    call = tool('authoring'); call['message']['content'][0]['name'] = 'mcp__reveal__' + name
    parser.feed(line(call))
    returned = tool('authoring', result=True)
    returned['message']['content'][0].pop('is_error')
    returned['message']['content'][0]['content'] = [{'type': 'text', 'text': json.dumps(inner)}]
    events = parser.feed(line(returned))
    payload = next(payload for kind, payload in events if kind == 'tool_result')
    assert payload['status'] == 'error'
    assert parser.timing.snapshot()['tools']['failed'] == 1
    activity = box_remote.observable_activity('tool_result', payload, parser.tools['authoring'])
    activity = box_remote.observable_activity('tool_call', {'tool_name': 'Read'}, {}, activity)
    assert activity['authoring_phase'] == 'draft_repair'


def test_mcp_unwrapping_is_bounded_and_does_not_confuse_scientific_payloads_with_errors():
    for scientific in ({'valid': False}, {'isError': True}, {'isError': True, 'content': [], 'gene': 'BRCA1'}):
        result = {'content': [{'type': 'text', 'text': json.dumps(scientific)}]}
        assert not tool_failed('get_factor', result)
        assert tool_result_payload('x', 'get_factor', {}, result)['status'] == 'completed'
    envelope = {'isError': True, 'content': []}
    assert tool_failed('get_factor', {'content': json.dumps(envelope)})
    assert not tool_failed('lint_account', {'content': [{'type': 'text', 'text': ' ' * 100_000 + json.dumps(envelope)}]})
    assert not tool_failed('lint_account', {'content': [{'type': 'text', 'text': '[' * 2000}]})
    nested = envelope
    for _ in range(5):
        nested = {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(nested)}]}
    assert not tool_failed('lint_account', nested)


def test_timeout_persists_safe_checkpoint_and_completion_without_terminal_usage(tmp_path):
    root = tmp_path; state = root / 'state'; output = root / 'output'
    request = {'job_id': 'offline', 'attempt': 1, 'kind': 'research', 'selected_graphs': [],
               'claude_version': 'test', 'model': 'test', 'max_turns': 3, 'max_budget_usd': .1, 'timeout_seconds': 5}
    (root / 'request.json').write_text(json.dumps(request))
    (root / 'credentials.json').write_text('{"ANTHROPIC_API_KEY":"PRIVATE_CREDENTIAL"}')
    runtime = {'dapper_root': str(root / 'dapper')}
    tick = [0.0]
    stdout = SimpleNamespace(fileno=lambda: 100)
    stderr = SimpleNamespace(fileno=lambda: 101)
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=stdout, stderr=stderr, pid=999999,
                              returncode=124, poll=lambda: 124)
    selector = Mock(); selector.get_map.return_value = {100: True}
    def select(**kwargs):
        tick[0] += 3
        if tick[0] == 3:
            return [(SimpleNamespace(fileobj=stdout, data='stdout'), None),
                    (SimpleNamespace(fileobj=stderr, data='stderr'), None)]
        return []
    selector.select.side_effect = select
    lint = tool('lint'); lint['message']['content'][0]['name'] = 'mcp__reveal__lint_account'
    lint_result = tool('lint', result=True)
    lint_result['message']['content'][0].pop('is_error')
    lint_result['message']['content'][0]['content'] = [{'type': 'text', 'text': json.dumps({'isError': True,
        'content': [{'type': 'text', 'text': 'Repair needed.'}]})}]
    stream = b''.join(line(event) for event in [lint, lint_result, tool('read'),
        {'type': 'stream_event', 'event': {'type': 'message_start'}},
        {'type': 'stream_event', 'event': {'type': 'content_block_delta',
          'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE_THOUGHT PRIVATE_CREDENTIAL'}}}])
    user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root))
    writes = []; original_write = box_remote.write_json
    def write_json(path, data):
        if path.name == 'runtime.json':
            writes.append(json.loads(json.dumps(data)))
        original_write(path, data)
    with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=output, SECRETS=(), RESEARCH=None), \
         patch.object(box_remote.os, 'getuid', return_value=0), \
         patch.object(box_remote.os, 'read', side_effect=lambda fd, size: stream if fd == 100 else b'PRIVATE_STDERR'), \
         patch.object(box_remote.pwd, 'getpwnam', return_value=user), \
         patch.object(box_remote, 'time', SimpleNamespace(monotonic=lambda: tick[0])), \
         patch.object(box_remote, 'setup', return_value=(root, runtime, 'PRIVATE_PROMPT')), \
         patch.object(box_remote, 'write_json', side_effect=write_json), \
         patch.object(box_remote, 'serve', return_value=Mock()), \
         patch.object(box_remote, 'terminate'), \
         patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(stdout='test version')), \
         patch.object(box_remote.subprocess, 'Popen', return_value=process), \
         patch.object(box_remote.selectors, 'DefaultSelector', return_value=selector):
        box_remote.main()
        captured = box_remote.collect()
    stored = json.loads((state / 'runtime.json').read_bytes())
    completion = stored['completion']; measured = completion['timing']
    assert completion['status'] == 'failed'
    assert '5-second execution limit' in completion['reason']
    assert 'repairing the draft' in completion['reason']
    assert completion['provider_terminal_received'] is False
    assert completion['cost_usd'] is None and completion['provider_reported']['usage'] is None
    assert completion['usage_status'] == 'unavailable'
    assert measured['tools']['active'] == 1 and measured['tools']['failed'] == 1
    assert measured['streams']['stderr']['bytes'] == len(b'PRIVATE_STDERR')
    assert measured['stream_events']['lifecycle']['content_block_delta'] == 1
    assert measured['stream_events']['message_open_observed'] is True
    assert measured['stream_events']['lifecycle_receipts']['message_start']['last_elapsed_seconds'] == 3
    assert any('completion' not in item and item['timing_checkpoint']['timing']['elapsed_seconds'] == 6 for item in writes)
    assert stored['timing_checkpoint']['timing'] == measured
    assert 'PRIVATE' not in json.dumps(stored)
    assert (state / 'runtime.json').stat().st_mode & 0o777 == 0o600
    manifest = json.loads((state / 'ledger/manifest.json').read_bytes())
    assert manifest['calls'][0]['status'] == 'failed'
    response = state / 'ledger' / manifest['calls'][0]['response']['path']
    assert json.loads(response.read_bytes()) == lint_result['message']['content'][0]
    assert not any('stderr' in name or 'stream' in name for name in captured['files'])
    for encoded in captured['files'].values():
        content = base64.b64decode(encoded)
        assert b'PRIVATE_THOUGHT' not in content and b'PRIVATE_STDERR' not in content and b'PRIVATE_PROMPT' not in content
