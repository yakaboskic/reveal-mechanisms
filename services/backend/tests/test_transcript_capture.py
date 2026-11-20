"""The run's transcript and configuration are captured: Claude Code's stream and stderr gzipped under the ledger
(bounded, head and tail kept when cut), scanned for credentials as inflated, and runtime.json saying how Claude Code
was started."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from reveal_backend import box_agent, box_remote, box_upload as upload, direct_capture as capture
from reveal_backend.box_adapter import BoxExecutionAdapter, BoxTransportError, egress_policy
from reveal_backend.box_mcp import ScopedTools
from reveal_backend.box_stream import TRANSCRIPT_CLOCK, TRANSCRIPT_CUT, Inflater, TranscriptWriter, inflate
from reveal_backend.evidence_package import canonical_json
from test_account_kit import PINS
from test_agent_jobs import ROOT, account_job, frozen
from test_box_upload import fixture
from test_direct_capture import BINDING, fixture_bytes, staged

STREAM, STDERR = upload.TRANSCRIPT_FILES


def lines(path):
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]


# The writer --------------------------------------------------------------------------------------------------

def test_a_stream_that_fits_is_stored_whole_and_gzipped(tmp_path):
    path = tmp_path / 'stream.jsonl.gz'
    events = [{'type': 'assistant', 'n': n} for n in range(50)]
    with TranscriptWriter(path, 100_000, expanded=1_000_000) as writer:
        raw = b''.join(json.dumps(event).encode() + b'\n' for event in events)
        for offset in range(0, len(raw), 7):   # chunks split lines anywhere
            writer.write(raw[offset:offset + 7])
    assert gzip.decompress(path.read_bytes()) == raw
    assert path.stat().st_mode & 0o777 == 0o600
    assert writer.summary() == {'stored_bytes': path.stat().st_size, 'original_bytes': len(raw), 'cut': False,
                                'omitted_bytes': 0, 'omitted_lines': 0}


@pytest.mark.parametrize('limit', [8_000, 60_000, 400_000])
def test_an_incompressible_stream_keeps_its_head_and_tail_under_the_cap(tmp_path, limit):
    path = tmp_path / 'stream.jsonl.gz'
    events = [{'type': 'user', 'n': n, 'noise': os.urandom(200).hex()} for n in range(2000)]
    with TranscriptWriter(path, limit, expanded=10_000_000) as writer:
        for event in events:
            writer.write(json.dumps(event).encode() + b'\n')
    assert path.stat().st_size <= limit
    kept = lines(path)
    cut = [index for index, event in enumerate(kept) if event['type'] == TRANSCRIPT_CUT]
    assert len(cut) == 1 and 0 < cut[0] < len(kept) - 1
    head, marker, tail = kept[:cut[0]], kept[cut[0]], kept[cut[0] + 1:]
    # Whole lines only: the head is the first events, the tail the last ones, and the marker counts the rest.
    assert [event['n'] for event in head] == list(range(len(head)))
    assert [event['n'] for event in tail] == list(range(2000 - len(tail), 2000))
    assert marker['omitted_lines'] == 2000 - len(head) - len(tail) == writer.summary()['omitted_lines']
    assert marker['original_bytes'] == sum(len(json.dumps(event)) + 1 for event in events)
    assert writer.summary()['cut'] is True and writer.summary()['stored_bytes'] == path.stat().st_size


def test_a_compressible_stream_is_cut_by_its_compressed_size_and_never_inflates_past_its_bound(tmp_path):
    path = tmp_path / 'stream.jsonl.gz'
    line = json.dumps({'type': 'assistant', 'text': 'a' * 900}).encode() + b'\n'
    with TranscriptWriter(path, 50_000, expanded=400_000) as writer:
        for _ in range(2000):   # 1.8 MB that compresses to a few kilobytes
            writer.write(line)
    data, complete = inflate(path.read_bytes(), 400_000)
    assert complete and len(data) <= 400_000 and path.stat().st_size <= 50_000
    assert writer.summary()['cut'] and TRANSCRIPT_CUT.encode() in data


def test_clock_lines_say_when_lines_arrived_at_most_once_a_second(tmp_path):
    path = tmp_path / 'stream.jsonl.gz'
    times = iter([1000.1, 1000.5, 1001.2, 1001.9, 1003.0])
    with TranscriptWriter(path, 100_000, expanded=1_000_000, clock=lambda: next(times)) as writer:
        for n in range(5):
            writer.write(json.dumps({'type': 'assistant', 'n': n}).encode() + b'\n')
    kinds = [(event['type'], event.get('at') or event.get('n')) for event in lines(path)]
    assert kinds == [(TRANSCRIPT_CLOCK, '1970-01-01T00:16:40.100Z'), ('assistant', 0), ('assistant', 1),
                     (TRANSCRIPT_CLOCK, '1970-01-01T00:16:41.200Z'), ('assistant', 2), ('assistant', 3),
                     (TRANSCRIPT_CLOCK, '1970-01-01T00:16:43.000Z'), ('assistant', 4)]


def test_a_stream_line_up_to_claude_streams_limit_is_never_split(tmp_path):
    path = tmp_path / 'stream.jsonl.gz'
    long = json.dumps({'type': 'user', 'text': 'z' * 2_000_000}).encode() + b'\n'
    times = iter(float(second) for second in range(1000, 2000))
    with TranscriptWriter(path, 20_000_000, expanded=48_000_000, clock=lambda: next(times)) as writer:
        for offset in range(0, len(long), 65536):   # arrives over many reads, a second apart
            writer.write(long[offset:offset + 65536])
    assert [event['type'] for event in lines(path)] == [TRANSCRIPT_CLOCK, 'user']


def test_the_gzip_trailer_is_written_when_the_run_fails_and_an_unfinished_line_is_kept(tmp_path):
    path = tmp_path / 'stderr.txt.gz'
    with pytest.raises(RuntimeError):
        with TranscriptWriter(path, 100_000, expanded=1_000_000) as writer:
            writer.write(b'first line\nunfinished')
            raise RuntimeError('the run stopped')
    data, complete = inflate(path.read_bytes(), 1_000_000)
    assert complete and data == b'first line\nunfinished'


def test_inflation_tolerates_a_file_cut_short_and_refuses_corrupt_extra_or_oversized_bytes(tmp_path):
    stored = gzip.compress(b'x' * 10_000 + b'\n')
    data, complete = inflate(stored[:len(stored) // 2], 20_000)   # a runner stopped mid-write
    assert not complete and set(data) <= {ord('x')}
    for bad in (b'not gzip at all', stored + stored, stored + b'junk'):
        with pytest.raises(ValueError):
            inflate(bad, 100_000)
    with pytest.raises(ValueError, match='limit'):
        inflate(stored, 5_000)
    inflater = Inflater(20_000)
    assert b''.join(inflater.feed(stored[offset:offset + 3]) for offset in range(0, len(stored), 3)) == b'x' * 10_000 + b'\n'


# Capture -----------------------------------------------------------------------------------------------------

def test_the_transcript_files_have_their_own_capture_limit():
    assert upload.capture_file_limit(STREAM) == upload.capture_file_limit(STDERR) == upload.MAX_TRANSCRIPT_FILE == 20_000_000
    assert upload.capture_file_limit('ledger/claude-stream.jsonl') == upload.MAX_FILE
    assert upload.capture_file_limit('output/claude-stream.jsonl.gz') == upload.MAX_FILE


def test_the_capture_takes_the_transcript_beyond_the_ordinary_file_limit(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    with TranscriptWriter(root / 'state' / STREAM, upload.capture_file_limit(STREAM), expanded=upload.MAX_TRANSCRIPT_EXPANDED) as writer:
        writer.write(os.urandom(3000).hex().encode() + b'\n')
    (root / 'state' / STDERR).write_bytes(gzip.compress(b'warning\n'))
    monkeypatch.setattr(upload, 'MAX_FILE', 1000)   # smaller than the stream: only its own limit applies
    names = {item['path']: item for item in upload.inventory(root, BINDING)['files']}
    assert {STREAM, STDERR} <= set(names) and names[STREAM]['size_bytes'] > 1000
    monkeypatch.setattr(upload, 'MAX_TRANSCRIPT_FILE', 1000)
    with pytest.raises(ValueError, match='bounded'):
        upload.source_file(root / 'state', STREAM, limit=upload.capture_file_limit(STREAM))


def test_the_direct_capture_scans_a_transcript_as_inflated():
    secret = b'synthetic-known-credential'
    stream = gzip.compress(b'{"type":"assistant","text":"' + secret + b'"}\n')
    assert secret not in stream   # compression hides it from a byte match
    storage, files, receipts, _ = staged(fixture_bytes(**{STREAM: stream}))
    item = next(index for index, entry in enumerate(files) if entry['path'] == STREAM)
    with pytest.raises(BoxTransportError, match='credential'):
        capture.verify_uploaded(storage, files[item], receipts[item], (secret,))
    clean = staged(fixture_bytes(**{STREAM: gzip.compress(b'{"type":"assistant"}\n')}))
    index = next(index for index, entry in enumerate(clean[1]) if entry['path'] == STREAM)
    assert capture.verify_uploaded(clean[0], clean[1][index], clean[2][index], (secret,))[0] == STREAM
    corrupt = staged(fixture_bytes(**{STREAM: b'not gzip'}))
    index = next(index for index, entry in enumerate(corrupt[1]) if entry['path'] == STREAM)
    with pytest.raises(BoxTransportError, match='gzip'):
        capture.verify_uploaded(corrupt[0], corrupt[1][index], corrupt[2][index], (secret,))
    # The inventory accepts the transcript up to its own limit.
    big = staged(fixture_bytes(**{STREAM: b'x' * (upload.MAX_FILE + 1)}))[1]
    capture.validate_inventory({'format': 'reveal.direct-capture/1', 'binding': BINDING, 'state': {'status': 'succeeded'},
                                'files': big}, BINDING, {'status': 'succeeded'})


# The runner --------------------------------------------------------------------------------------------------

def test_the_runner_writes_the_transcript_under_the_ledger_and_records_the_configuration(tmp_path):
    """box_remote.main end to end, with Claude Code replaced by pipes carrying a short stream."""
    root = tmp_path / 'reveal'; state = root / 'state'; output = root / 'output'
    root.mkdir(parents=True)
    value = frozen(tools=['Read', 'Bash'], system_prompt='CALLER-SYSTEM-PROMPT', network='restricted',
                   allowed_domains=['eutils.ncbi.nlm.nih.gov'])
    (root / 'agent-input.json').write_bytes(canonical_json(value))
    (root / 'toolchain.sha256').write_text(BoxExecutionAdapter.toolchain_stamp('2.1.282'))
    key = 'sk-ant-offline-credential-value'
    request = {'job_id': 'agent-job', 'attempt': 1, 'kind': 'agent', 'selected_graphs': [], 'claude_version': '2.1.282',
               'model': value['model'], 'max_turns': 50, 'max_budget_usd': 2.0, 'timeout_seconds': 900,
               'agent': box_agent.bootstrap_settings(value)}
    (root / 'request.json').write_text(json.dumps(request))
    (root / 'credentials.json').write_text(json.dumps({'ANTHROPIC_API_KEY': key}))
    runtime = {'job_id': 'agent-job', 'attempt': 1,
               'agent': {'tools': value['tools'], 'graphs': [], 'network': 'restricted', 'effort': 'medium', 'limits': value['limits']},
               'execution_limits': {'timeout_seconds': 900, 'max_budget_usd': 2.0, 'max_turns': 50, 'max_output_tokens': 32000}}
    stream = [{'type': 'system', 'subtype': 'init', 'model': value['model'], 'claude_code_version': '2.1.282',
               'tools': ['Read', 'Bash', 'mcp__reveal__query_claims'], 'mcp_servers': [{'name': 'reveal', 'status': 'connected'}]},
              {'type': 'assistant', 'message': {'id': 'm1', 'content': [{'type': 'text', 'text': 'Reading with ' + key}]}},
              {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'DONE', 'num_turns': 1, 'total_cost_usd': 0.01}]
    out_read, out_write = os.pipe(); err_read, err_write = os.pipe()
    os.write(out_write, b''.join(json.dumps(event).encode() + b'\n' for event in stream)); os.close(out_write)
    os.write(err_write, b'a warning naming ' + key.encode() + b'\n'); os.close(err_write)
    process = SimpleNamespace(stdin=Mock(), stdout=os.fdopen(out_read, 'rb'), stderr=os.fdopen(err_read, 'rb'), pid=999999,
                              returncode=0, poll=lambda: 0)
    proxy = SimpleNamespace(base_url='http://127.0.0.1:8766', shutdown=lambda: None)
    user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root))
    popen = Mock(return_value=process)
    with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=output, SECRETS=(), RESEARCH=None), \
            patch.object(box_remote.os, 'getuid', return_value=0), patch.object(box_remote.pwd, 'getpwnam', return_value=user), \
            patch.object(box_remote, 'setup', return_value=(root, runtime, 'prompt')), patch.object(box_remote, 'serve', return_value=Mock()), \
            patch.object(box_remote, 'KeyProxy', return_value=SimpleNamespace(start=lambda: proxy)), \
            patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(stdout='2.1.282 (Claude Code)')), \
            patch.object(box_remote.subprocess, 'Popen', popen):
        box_remote.main()
    status = json.loads((state / 'status.json').read_text())
    assert status['status'] == 'succeeded', status
    assert not (state / 'claude-stream.jsonl').exists() and not (state / 'claude-stderr.txt').exists()
    kept = lines(state / STREAM)
    assert kept[0]['type'] == TRANSCRIPT_CLOCK and [event['type'] for event in kept if event['type'] != TRANSCRIPT_CLOCK] == \
        ['system', 'assistant', 'result']
    assert key.encode() not in gzip.decompress((state / STREAM).read_bytes())
    assert gzip.decompress((state / STDERR).read_bytes()) == b'a warning naming [REDACTED_CREDENTIAL]\n'
    recorded = json.loads((state / 'runtime.json').read_text())
    assert recorded['transcript'][STREAM]['stored_bytes'] == (state / STREAM).stat().st_size
    assert recorded['transcript'][STREAM]['cut'] is False and recorded['transcript'][STDERR]['cut'] is False
    configuration = recorded['configuration']
    assert configuration['format'] == 'reveal.run-configuration/1'
    assert configuration['claude_code'] == {'pinned_version': '2.1.282', 'observed_version': '2.1.282 (Claude Code)'}
    argv = configuration['cli']['argv']
    assert argv[0] == box_remote.CLAUDE_BINARY and argv[argv.index('--model') + 1] == value['model']
    assert argv[argv.index('--tools') + 1] == 'Read,Bash' and argv[argv.index('--max-turns') + 1] == '50'
    # The caller's system prompt is request content: its size and sha256 only.
    sent = popen.call_args.args[0]
    prompt = sent[sent.index('--append-system-prompt') + 1]
    assert 'CALLER-SYSTEM-PROMPT' in prompt and 'CALLER-SYSTEM-PROMPT' not in json.dumps(configuration)
    assert argv[argv.index('--append-system-prompt') + 1] == f'<{len(prompt)} characters, sha256 {hashlib.sha256(prompt.encode()).hexdigest()}>'
    assert configuration['cli']['wrapper'][0] == 'setpriv' and configuration['cli']['prompt'] == 'stdin'
    environment = configuration['cli']['environment']
    assert environment['ANTHROPIC_API_KEY'] == box_agent.KEY_PROXY_PLACEHOLDER and environment['ANTHROPIC_BASE_URL'] == proxy.base_url
    assert environment['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] == str(value['limits']['max_output_tokens'])
    assert key not in (state / 'runtime.json').read_text()
    assert configuration['tools'] == {'builtin': ['Read', 'Bash'], 'mcp': [],
                                      'mcp_servers': {'reveal': {'type': 'http', 'url': 'http://127.0.0.1:8765/mcp'}}}
    assert configuration['network'] == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'eutils.ncbi.nlm.nih.gov']}
    assert configuration['toolchain'] == {'stamp': BoxExecutionAdapter.toolchain_stamp('2.1.282')}
    assert configuration['account_kit'] is None and configuration['source_tools'] is None
    assert configuration['limits'] == {'execution': runtime['execution_limits'], 'agent': value['limits']}
    # What Claude Code itself reported having.
    assert recorded['observed_claude_runtime']['tools'] == ['Read', 'Bash', 'mcp__reveal__query_claims']
    assert recorded['observed_claude_runtime']['mcp_servers'] == [{'name': 'reveal', 'status': 'connected'}]


def test_the_configuration_never_records_a_research_runs_key_and_names_the_mcp_tools():
    command = ['setpriv', '/usr/bin/timeout', '600', box_remote.CLAUDE_BINARY, '-p', '--tools', 'Read,Write',
               '--json-schema', '{"type":"object"}']
    env = {'PATH': '/bin', 'HOME': '/home/agent', 'ANTHROPIC_API_KEY': 'sk-real-key', 'UNLISTED': 'x'}
    tools = ScopedTools(('prokn',), Mock(), lint=lambda name: None)
    request = {'kind': 'research', 'claude_version': '2.1.282', 'selected_graphs': ['prokn']}
    value = box_remote.run_configuration(request, {'execution_limits': {'max_turns': 5}}, command, env, tools,
                                         {'mcpServers': {'reveal': {'type': 'http', 'url': 'http://127.0.0.1:8765/mcp'}}}, None)
    assert value['cli']['environment'] == {'PATH': '/bin', 'HOME': '/home/agent', 'ANTHROPIC_API_KEY': '[REDACTED_CREDENTIAL]'}
    assert 'sk-real-key' not in json.dumps(value)
    schema = '{"type":"object"}'
    assert value['cli']['argv'][-1] == f'<{len(schema)} characters, sha256 {hashlib.sha256(schema.encode()).hexdigest()}>'
    assert value['tools']['mcp'] == ['mcp__reveal__get_schema', 'mcp__reveal__describe_kg', 'mcp__reveal__query_graph',
                                     'mcp__reveal__lint_account']
    assert value['network'] == egress_policy(request) and value['limits'] == {'execution': {'max_turns': 5}, 'agent': None}


@pytest.mark.parametrize('settings', [
    {'network': 'open', 'allowed_domains': [], 'deliverable': 'answer'},
    {'network': 'restricted', 'allowed_domains': ['eutils.ncbi.nlm.nih.gov'], 'deliverable': 'scientific_account'},
    {'network': 'restricted', 'allowed_domains': [], 'deliverable': 'answer', 'claims_url': 'https://claims.example.org'}])
def test_the_recorded_network_is_the_policy_the_worker_applies(settings):
    request = {'kind': 'agent', 'selected_graphs': ['prokn'], 'agent': settings, 'claude_version': '2.1.282'}
    tools = ScopedTools((), Mock())
    command = [box_remote.CLAUDE_BINARY, '--tools', 'Read']
    recorded = box_remote.run_configuration(request, {}, command, {}, tools, {'mcpServers': {}}, None)['network']
    assert recorded == egress_policy(request)
    assert recorded == box_agent.network_policy(['prokn'], settings['network'], settings['allowed_domains'], settings['deliverable'],
                                                'claims.example.org' if settings.get('claims_url') else None)


def test_an_account_job_records_the_pinned_kit_it_was_given(tmp_path):
    value = account_job()
    work = tmp_path / 'trusted'; work.mkdir()
    runtime = {'agent': {'limits': value['limits']}}
    with patch.object(box_remote, 'BASE', tmp_path), patch('reveal_backend.dapper_release.clone_release', return_value={}), \
            patch.object(box_remote, 'source_tool_versions', return_value={'curl': '8.5.0'}):
        box_remote.account_workspace(work, ROOT, None, value, runtime)
    command = [box_remote.CLAUDE_BINARY, '--tools', 'Read,Bash']
    request = {'kind': 'agent', 'claude_version': '2.1.282', 'selected_graphs': []}
    kit = box_remote.run_configuration(request, runtime, command, {}, ScopedTools((), Mock()), {'mcpServers': {}}, None)
    assert kit['account_kit'] == {'skill': box_remote.ACCOUNT_SKILL, 'skill_sha256': PINS[box_remote.ACCOUNT_SKILL],
                                  'schema_excerpt_sha256': runtime['derived_input_views']['account/authoring-schema-excerpt.yaml']}
    assert kit['source_tools'] == {'curl': '8.5.0'}


def test_the_stream_parser_keeps_what_claude_code_says_it_has():
    from reveal_backend.box_stream import ClaudeStream
    parser = ClaudeStream()
    parser.feed(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'claude_code_version': '2.1.282', 'tools': ['Read'] * 3,
                            'mcp_servers': [{'name': 'reveal', 'status': 'connected', 'extra': 'dropped'}]}).encode() + b'\n')
    assert parser.runtime == {'model': 'm', 'claude_code_version': '2.1.282', 'tools': ['Read'] * 3,
                              'mcp_servers': [{'name': 'reveal', 'status': 'connected'}]}
    parser = ClaudeStream()
    parser.feed(json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['x' * 500]}).encode() + b'\n')
    assert 'tools' not in parser.runtime


# The transcript's own allowance --------------------------------------------------------------------------------

def fill_to_the_total(root):
    """Output sources that bring the run's other captured files to exactly MAX_TOTAL."""
    other = sum(path.stat().st_size for path in [*(root / 'output').rglob('*'), *(root / 'state/ledger').rglob('*'), root / 'state/runtime.json']
                if path.is_file() and path.name not in {Path(name).name for name in upload.TRANSCRIPT_FILES})
    remaining = upload.MAX_TOTAL - other
    (root / 'output/sources').mkdir()
    index = 0
    while remaining:
        size = min(remaining, upload.MAX_SOURCE_FILE)
        (root / f'output/sources/paper-{index}.pdf').write_bytes(b'\0' * size)
        remaining -= size; index += 1


def test_a_run_at_its_source_limit_still_captures_two_full_transcripts(tmp_path):
    root = fixture(tmp_path)
    fill_to_the_total(root)
    for name in upload.TRANSCRIPT_FILES:
        (root / 'state' / name).write_bytes(b'\1' * upload.MAX_TRANSCRIPT_FILE)
    files = upload.inventory(root, BINDING)['files']
    sizes = {item['path']: item['size_bytes'] for item in files}
    assert sum(size for name, size in sizes.items() if name not in upload.TRANSCRIPT_FILES) == upload.MAX_TOTAL
    assert [sizes[name] for name in upload.TRANSCRIPT_FILES] == [upload.MAX_TRANSCRIPT_FILE] * 2
    assert sum(sizes.values()) == upload.MAX_TOTAL + upload.MAX_TRANSCRIPT_TOTAL == 140_000_000
    state = {'status': 'succeeded'}
    assert capture.validate_inventory({'format': 'reveal.direct-capture/1', 'binding': BINDING, 'state': state, 'files': files},
                                      BINDING, state) == files
    # One byte more of anything else is still over the run's total.
    with pytest.raises(BoxTransportError, match='byte budget'):
        capture.validate_inventory({'format': 'reveal.direct-capture/1', 'binding': BINDING, 'state': state,
                                    'files': [*files, {'path': 'output/extra', 'size_bytes': 1, 'sha256': 'a' * 64}]}, BINDING, state)


def test_the_transcript_allowance_is_kept_apart_on_every_capture_path(tmp_path, monkeypatch):
    """The same rule, at a small scale: the runner's collect and the freeze count everything else to MAX_TOTAL."""
    root = fixture(tmp_path)
    for name in upload.TRANSCRIPT_FILES:
        (root / 'state' / name).write_bytes(b'\1' * 5000)
    other = sum(path.stat().st_size for path in [root / 'output/account.json', root / 'state/ledger/manifest.json', root / 'state/runtime.json'])
    monkeypatch.setattr(upload, 'MAX_TOTAL', other)
    monkeypatch.setattr(box_remote, 'MAX_TOTAL', other)
    with patch.multiple(box_remote, OUTPUT=root / 'output', STATE=root / 'state'), \
            patch.object(upload.os, 'geteuid', return_value=(root / 'state/runtime.json').stat().st_uid):
        collected = box_remote.collect()['files']
        assert set(upload.TRANSCRIPT_FILES) <= set(collected)
        names = {item['path'] for item in upload.inventory(root, BINDING)['files']}
        assert set(upload.TRANSCRIPT_FILES) <= names
        (root / 'output/extra').write_bytes(b'x')
        with pytest.raises(ValueError, match='budget'):
            box_remote.collect()
    fresh = fixture(tmp_path / 'fresh')
    (fresh / 'output/extra').write_bytes(b'x')
    with pytest.raises(ValueError, match='byte budget'):
        upload.inventory(fresh, BINDING)
