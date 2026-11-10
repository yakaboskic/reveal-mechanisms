"""General agent jobs: frozen prompt input, key isolation, Box wiring, workflow acceptance and API admission."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import tarfile
import gzip
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient
import jwt
import pytest

from reveal_backend import box_agent, box_remote, jobs, workflow_execution
from reveal_backend import app as api
from reveal_backend.agent_execution import ExecutionRequest, authoring_stage, execution_kind
from reveal_backend.box_adapter import (CAPTURE_MARKER, BoxConfigurationError, BoxExecutionAdapter, BoxTransportError, capture_binding,
                                        captured_result, egress_policy, make_bundle)
from reveal_backend.direct_bootstrap import validate_config
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, sha256
from reveal_backend.repository import Repository, uid
from reveal_backend.worker import validate_execution_ledger
from reveal_backend.workflow_execution import WorkflowExecution

ROOT = Path(__file__).resolve().parents[3]


def frozen(**body):
    return box_agent.freeze_input({'kind': 'agent', 'prompt': 'Summarize the sandbox.', **body}, budget_cap=5)


# Frozen input -----------------------------------------------------------------------------------------------

def test_freeze_fills_every_default_so_the_frozen_bytes_say_what_runs():
    value = frozen()
    assert value == {'format': 'reveal.agent-input/1', 'prompt': 'Summarize the sandbox.', 'system_prompt': None,
                     'tools': list(box_agent.DEFAULT_TOOLS), 'graphs': [], 'literature': False, 'network': 'restricted', 'allowed_domains': [],
                     'effort': 'medium', 'model': box_agent.DEFAULT_MODEL,
                     'limits': {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0}}
    # The default budget never exceeds a lower server cap.
    assert box_agent.freeze_input({'kind': 'agent', 'prompt': 'x'}, budget_cap=0.5)['limits']['max_budget_usd'] == 0.5


def test_freeze_orders_tools_and_graphs_canonically():
    value = frozen(tools=['Bash', 'Read'], graphs=['prokn', 'biomarkerkg'], literature=True, effort='high', model='claude-sonnet-5-5')
    assert value['tools'] == ['Read', 'Bash'] and value['graphs'] == ['biomarkerkg', 'prokn']
    assert box_agent.validate_input(json.loads(canonical_json(value))) == value


@pytest.mark.parametrize('body, message', [
    ({'tools': ['WebFetch']}, 'needs network "open"'),
    ({'tools': ['Read', 'Read']}, 'tools must be distinct'),
    ({'tools': ['Task']}, 'tools must be distinct'),
    ({'graphs': ['not-a-graph']}, 'graphs must be distinct'),
    ({'limits': {'max_budget_usd': 6}}, 'at most 5'),
    ({'limits': {'max_budget_usd': 0}}, 'above 0'),
    ({'limits': {'timeout_seconds': 30}}, 'timeout_seconds'),
    ({'limits': {'max_turns': 0}}, 'max_turns'),
    ({'prompt': '   '}, 'prompt must be nonblank'),
    ({'system_prompt': ''}, 'system_prompt must be nonblank'),
    ({'network': 'lan'}, 'network must be'),
    ({'model': 'gpt-5'}, 'Claude model id'),
    ({'effort': 'max'}, 'effort must be'),
    ({'allowed_domains': ['10.0.0.1']}, 'exact hostnames'),
    ({'allowed_domains': ['*.ncbi.nlm.nih.gov']}, 'exact hostnames'),
    ({'allowed_domains': ['https://eutils.ncbi.nlm.nih.gov']}, 'exact hostnames'),
    ({'allowed_domains': ['host%d.example.org' % n for n in range(21)]}, 'at most 20'),
    ({'allowed_domains': ['api.example.org'], 'network': 'open'}, 'already reaches every host'),
    ({'allowed_domains': 'api.example.org'}, 'list of hostnames'),
])
def test_freeze_rejects_invalid_requests(body, message):
    with pytest.raises(box_agent.AgentInputError, match=message):
        frozen(**body)


def test_webfetch_is_allowed_on_the_open_network():
    assert frozen(tools=['WebFetch'], network='open')['tools'] == ['WebFetch']


def test_validate_rejects_changed_frozen_bytes():
    value = frozen()
    for change in ({'extra': 1}, {'tools': ['Bash', 'Read']}, {'limits': {**value['limits'], 'timeout_seconds': 5}}):
        with pytest.raises(box_agent.AgentInputError):
            box_agent.validate_input({**value, **change})


def test_allowed_domains_are_normalized_and_added_to_the_restricted_egress():
    value = frozen(allowed_domains=['EUTILS.ncbi.nlm.nih.gov.', 'api.platform.opentargets.org', 'eutils.ncbi.nlm.nih.gov'])
    assert value['allowed_domains'] == ['api.platform.opentargets.org', 'eutils.ncbi.nlm.nih.gov']
    assert box_agent.network_policy([], 'restricted', True, value['allowed_domains']) == {'mode': 'custom', 'allowed_domains': [
        'api.anthropic.com', 'www.ebi.ac.uk', 'api.platform.opentargets.org', 'eutils.ncbi.nlm.nih.gov']}
    note = box_remote.agent_system_prompt(value)
    assert 'these hosts over HTTPS: api.platform.opentargets.org, eutils.ncbi.nlm.nih.gov' in note


def test_network_policy_opens_only_what_the_chosen_tools_call():
    assert box_agent.network_policy([], 'restricted', False) == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com']}
    assert box_agent.network_policy(['prokn'], 'restricted', True) == {
        'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us', 'www.ebi.ac.uk']}
    assert box_agent.network_policy(['prokn'], 'open', True) == {'mode': 'allow-all'}


# Response record and job result ---------------------------------------------------------------------------

def test_response_record_redacts_credentials_and_keeps_only_safe_numbers():
    record = box_agent.response_record('job', 1, 'succeeded', {'result': 'key sk-secret done', 'subtype': 'success',
        'num_turns': 4, 'duration_ms': 1500, 'total_cost_usd': 0.12, 'is_error': False}, ('sk-secret',))
    assert record['response'] == 'key [REDACTED_CREDENTIAL] done'
    assert (record['num_turns'], record['duration_ms'], record['cost_usd']) == (4, 1500, 0.12)
    assert box_agent.response_record('job', 1, 'failed', {'num_turns': float('nan')})['num_turns'] is None


def test_job_result_lists_output_files_and_cuts_long_answers():
    answer = 'a' * (box_agent.MAX_RESULT_RESPONSE_CHARS + 5)
    record = box_agent.response_record('job', 2, 'succeeded', {'result': answer, 'duration_ms': 2500})
    files = {'output/report.md': {'sha256': 'f' * 64, 'size_bytes': 9}, 'ledger/manifest.json': {'sha256': 'e' * 64, 'size_bytes': 2},
             'runtime.json': {'sha256': 'd' * 64, 'size_bytes': 3}}
    result = box_agent.job_result(record, files, job_id='job', attempt=2)
    assert result['kind'] == 'agent' and result['response_truncated'] is True
    assert len(result['response']) == box_agent.MAX_RESULT_RESPONSE_CHARS
    assert result['response_sha256'] == hashlib.sha256(answer.encode()).hexdigest()
    assert result['files'] == [{'path': 'report.md', 'size_bytes': 9, 'sha256': 'f' * 64}]
    assert result['duration_seconds'] == 2.5
    with pytest.raises(box_agent.AgentInputError):
        box_agent.job_result(record, files, job_id='job', attempt=1)


# Loopback key proxy ---------------------------------------------------------------------------------------

class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        Upstream.seen.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for part in (b'event: one\n\n', b'event: two\n\n'):
            self.wfile.write(b'%x\r\n%s\r\n' % (len(part), part)); self.wfile.flush()
        self.wfile.write(b'0\r\n\r\n')


@pytest.fixture
def upstream():
    Upstream.seen = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown(); server.server_close()


def test_key_proxy_swaps_in_the_real_key_and_streams_responses(upstream):
    port = upstream.server_address[1]
    proxy = box_agent.KeyProxy('sk-real', port=0, connection=lambda: http.client.HTTPConnection('127.0.0.1', port, timeout=5)).start()
    try:
        client = http.client.HTTPConnection('127.0.0.1', proxy.server.server_address[1], timeout=5)
        client.request('POST', '/v1/messages?beta=true', body=b'{"stream":true}',
                       headers={'x-api-key': box_agent.KEY_PROXY_PLACEHOLDER, 'Authorization': 'Bearer nope',
                                'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'})
        response = client.getresponse()
        assert response.status == 200 and response.getheader('Content-Type') == 'text/event-stream'
        assert response.read() == b'event: one\n\nevent: two\n\n'
        seen = Upstream.seen[0]
        assert seen['path'] == '/v1/messages?beta=true' and seen['body'] == b'{"stream":true}'
        assert seen['headers']['x-api-key'] == 'sk-real' and 'authorization' not in seen['headers']
        assert seen['headers']['anthropic-version'] == '2023-06-01' and seen['headers']['accept-encoding'] == 'identity'
        other = http.client.HTTPConnection('127.0.0.1', proxy.server.server_address[1], timeout=5)
        other.request('GET', '/etc/passwd')
        assert other.getresponse().status == 404
    finally:
        proxy.shutdown()


def test_agent_environment_never_carries_the_real_key():
    proxy = SimpleNamespace(base_url='http://127.0.0.1:8766')
    env = box_agent.agent_environment('/home/agent', '/usr/bin', proxy)
    assert env['ANTHROPIC_API_KEY'] == box_agent.KEY_PROXY_PLACEHOLDER and env['ANTHROPIC_BASE_URL'] == proxy.base_url


# Box bundle and bootstrap configuration ------------------------------------------------------------------

def agent_request(tmp_path, value=None, **overrides):
    value = value or frozen(graphs=['prokn'], literature=True)
    path = tmp_path / 'agent-input.json'; path.write_bytes(canonical_json(value))
    limits = value['limits']
    fields = dict(job_id='agent-job', attempt=1, kind='agent', input_path=path, output_dir=tmp_path / 'out',
                  selected_graphs=tuple(value['graphs']), timeout_seconds=limits['timeout_seconds'],
                  max_budget_usd=limits['max_budget_usd'], max_turns=limits['max_turns'])
    return ExecutionRequest(**{**fields, **overrides})


def bundle_names(raw):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw))) as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def test_bundle_carries_the_frozen_input_and_the_shared_module(tmp_path):
    request = agent_request(tmp_path)
    files = bundle_names(make_bundle(ROOT, request))
    assert files['input/agent-input.json'] == request.input_path.read_bytes()
    assert 'bundle/services/backend/src/reveal_backend/box_agent.py' in files
    with pytest.raises(BoxConfigurationError, match='Selected graphs'):
        make_bundle(ROOT, agent_request(tmp_path, selected_graphs=()))
    bad = tmp_path / 'bad'; bad.mkdir()
    with pytest.raises(BoxConfigurationError, match='Invalid agent input'):
        make_bundle(ROOT, agent_request(bad, {**frozen(), 'format': 'other'}))


def test_bootstrap_config_freezes_model_and_egress_from_the_input(tmp_path):
    request = agent_request(tmp_path, frozen(graphs=['prokn'], model='claude-sonnet-5-5'))
    config = BoxExecutionAdapter(ROOT, environ={'REVEAL_CLAUDE_MODEL': 'claude-sonnet-4-6'}).request_config(request)
    assert config['model'] == 'claude-sonnet-5-5' and config['agent'] == {'network': 'restricted', 'literature': False, 'allowed_domains': []}
    validate_config(config)
    assert egress_policy(config) == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us']}
    for changed in ({**config, 'kind': 'paragraph', 'selected_graphs': []}, {k: v for k, v in config.items() if k != 'agent'},
                    {**config, 'agent': {'network': 'everything', 'literature': False, 'allowed_domains': []}},
                    {**config, 'agent': {'network': 'restricted', 'literature': False, 'allowed_domains': ['1.2.3.4']}}):
        with pytest.raises(BoxTransportError):
            validate_config(changed)


def test_research_egress_is_unchanged():
    assert egress_policy({'kind': 'paragraph', 'selected_graphs': []}) == {
        'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us', 'github.com', 'www.ebi.ac.uk']}


# In-Box runner setup --------------------------------------------------------------------------------------

def runner_setup(tmp_path, value, **request_overrides):
    root = tmp_path / 'reveal'; (root / 'input').mkdir(parents=True); (root / 'bundle').mkdir()
    state = root / 'state'; state.mkdir()
    (root / 'input/agent-input.json').write_bytes(canonical_json(value))
    request = {'kind': 'agent', 'job_id': 'agent-job', 'attempt': 1, 'selected_graphs': value['graphs'], 'model': value['model'],
               'claude_version': '2.1.282', 'input_sha256': 'frozen', **{k: value['limits'][k] for k in value['limits']},
               **request_overrides}
    def protect(target):
        # The real hardening chowns to root; here it only proves every hardened path exists.
        assert target.exists(), target
        protected.append(target)
    protected = []
    with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=root / 'output'), patch.object(box_remote, 'protect', protect), \
            patch.object(box_remote, 'verify_writable_output'), \
            patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())):
        work, runtime, prompt = box_remote.setup(request)
    assert protected == [root / 'bundle', root / 'input', root / 'trusted']
    return work, runtime, prompt


def test_runner_setup_uses_the_callers_prompt_without_a_dapper_release(tmp_path):
    value = frozen(system_prompt='Answer in French.', tools=['Read', 'Write'])
    with patch('reveal_backend.dapper_release.clone_release', side_effect=AssertionError('No DAPPER clone')):
        work, runtime, prompt = runner_setup(tmp_path, value)
    assert prompt == 'Summarize the sandbox.'
    assert runtime['runtime_version'] == 'reveal.agent-runtime/1' and 'dapper' not in runtime
    assert runtime['agent'] == {'tools': ['Read', 'Write'], 'graphs': [], 'literature': False, 'network': 'restricted',
                                'allowed_domains': [], 'effort': 'medium'}
    assert (work / 'agent-input.json').read_bytes() == canonical_json(value)
    system = box_remote.agent_system_prompt(value)
    assert system.endswith('\n\nAnswer in French.') and 'output/' in system and 'restricted' in system
    assert 'WebSearch still works' not in system


def test_runner_setup_refuses_a_request_that_differs_from_the_frozen_input(tmp_path):
    with pytest.raises(ValueError, match='model'):
        runner_setup(tmp_path / 'a', frozen(), model='claude-other')
    with pytest.raises(ValueError, match='limits'):
        runner_setup(tmp_path / 'b', frozen(), max_turns=99)
    with pytest.raises(ValueError, match='limits'):
        runner_setup(tmp_path / 'c', frozen(), max_budget_usd=4.0)


def test_runner_failure_messages_name_the_agent_limits():
    request = {'kind': 'agent', 'max_turns': 7, 'max_budget_usd': 1.5, 'timeout_seconds': 60}
    assert '7-turn limit' in box_remote.provider_failure_reason(request, {'subtype': 'error_max_turns'})
    assert '$1.5 budget' in box_remote.provider_failure_reason(request, {'subtype': 'error_max_budget_usd'})
    assert 'while working on the prompt' in box_remote.deadline_reason(request)


# Workflow ------------------------------------------------------------------------------------------------

def test_job_kind_mapping():
    assert [execution_kind(k) for k in ('analysis', 'paragraph', 'agent')] == ['research', 'paragraph', 'agent']
    assert [authoring_stage(k) for k in ('analysis', 'paragraph', 'agent')] == ['authoring_account', 'authoring_paragraph', 'running_agent']


class AgentWorkflow:
    def __init__(self, root):
        self.root = root
        self.value = frozen(tools=['Read', 'Write', 'Bash'])
        self.engine = WorkflowExecution(repository=MagicMock())
        self.engine.activity = Mock(); self.engine.checkpoint = Mock()
        self.payload = {'job_id': 'agent-job', 'namespace': 'test', 'generation': 1}
        self.job = {'id': 'agent-job', 'kind': 'agent', 'owner_user_id': 'owner', 'status': 'running'}
        self.queue = {'attempt': 1, 'inputs': self.value, 'dispatch_input': {'model': self.value['model']}}
        self.execution = {'box': {'box_id': 'box-1'}, 'capture_complete': True, 'cleanup_complete': True, 'authoring_attempt': 1}

    def capture(self, *, response=True, status='succeeded', dapper=None):
        source = self.root / 'agent-input.json'; source.write_bytes(canonical_json(self.value))
        output = self.root / 'attempt-1/output'
        (output / 'output').mkdir(parents=True); (output / 'ledger').mkdir()
        (output / 'output/notes.md').write_text('# notes\n')
        self.request = ExecutionRequest(job_id='agent-job', attempt=1, kind='agent', input_path=source, output_dir=output)
        runtime = {'job_id': 'agent-job', 'attempt': 1, 'input_sha256': sha256(source.read_bytes()), 'model': self.value['model']}
        if dapper: runtime['dapper'] = dapper
        (output / 'runtime.json').write_bytes(canonical_json(runtime))
        (output / 'ledger/manifest.json').write_bytes(canonical_json(
            {'format': 'reveal.tool-ledger/1', 'job_id': 'agent-job', 'attempt': 1, 'complete': True, 'calls': []}))
        names = ['output/notes.md', 'runtime.json', 'ledger/manifest.json']
        if response:
            record = box_agent.response_record('agent-job', 1, status, {'result': 'Done. See notes.md.', 'num_turns': 3,
                                                                         'duration_ms': 4000, 'total_cost_usd': 0.05})
            (output / box_agent.RESPONSE_CAPTURE_PATH).write_bytes(canonical_json(record)); names.append(box_agent.RESPONSE_CAPTURE_PATH)
        files = {name: {'sha256': sha256((output / name).read_bytes()), 'size_bytes': (output / name).stat().st_size} for name in names}
        marker = {'format': 'reveal.box-capture/1', 'binding': capture_binding(self.request, self.execution['box']),
                  'state': {'status': status}, 'cleanup_complete': True, 'files': files}
        (output / CAPTURE_MARKER).write_bytes(canonical_json(marker))


@pytest.fixture
def workflow(tmp_path):
    return AgentWorkflow(tmp_path)


def test_agent_validation_checks_the_ledger_and_requires_the_response(workflow):
    workflow.capture()
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'):
        result = asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                      workflow.execution, workflow.root, workflow.request, workflow.value))
    assert result == {'next_phase': 'commit'}
    assert workflow.engine.checkpoint.call_args.kwargs['validated_paths'] == ['attempt-1/output/ledger/agent-response.json']


def test_agent_validation_fails_without_a_response_record(workflow):
    workflow.capture(response=False)
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'), \
            pytest.raises(EvidenceBuildError, match='no response record'):
        asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                             workflow.execution, workflow.root, workflow.request, workflow.value))
    workflow.engine.checkpoint.assert_not_called()


def test_agent_ledger_check_skips_only_the_dapper_release(workflow):
    workflow.capture()
    result = captured_result(workflow.request, workflow.execution['box'], json.loads((workflow.request.output_dir / CAPTURE_MARKER).read_bytes()))
    validate_execution_ledger(result, workflow.request, workflow.value['model'])
    with pytest.raises(EvidenceBuildError, match='model'):
        validate_execution_ledger(result, workflow.request, 'claude-other')


def test_failed_agent_run_finishes_failed_with_its_reason(workflow):
    workflow.capture(status='failed')
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'), \
            patch('reveal_backend.workflow_execution.jobs.finish') as finish:
        result = asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                      workflow.execution, workflow.root, workflow.request, workflow.value))
    assert result == {'next_phase': 'complete', 'done': True}
    assert finish.call_args.args[3] == 'failed' and finish.call_args.kwargs['failure']['code'] == 'AGENT_EXECUTION_FAILED'
    assert workflow.engine.activity.call_args.args[3]['stage'] == 'running_agent'


def test_agent_commit_saves_the_answer_as_the_job_result(workflow):
    workflow.capture()
    workflow.execution['validated_paths'] = ['attempt-1/output/ledger/agent-response.json']
    workflow.engine.context = Mock(return_value=({'status': 'running'}, None, None))
    with patch('reveal_backend.worker.Worker.begin_persistence', AsyncMock(return_value=True)), \
            patch('reveal_backend.workflow_execution.jobs.finish') as finish:
        result = asyncio.run(workflow.engine.commit(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                    workflow.execution, workflow.root, workflow.request, workflow.value))
    assert result == {'next_phase': 'complete', 'done': True}
    _, job_id, token, status = finish.call_args.args
    outcome = finish.call_args.kwargs['result']
    assert (job_id, token, status) == ('agent-job', 'fence', 'succeeded')
    assert outcome['response'] == 'Done. See notes.md.' and outcome['turns'] == 3 and outcome['cost_usd'] == 0.05
    assert outcome['files'] == [{'path': 'notes.md', 'size_bytes': 8, 'sha256': sha256(b'# notes\n')}]


def test_agent_prepare_freezes_input_limits_and_model_into_the_bootstrap(workflow):
    descriptors = []
    class Adapter(BoxExecutionAdapter):
        def freeze_bootstrap(self, request, storage):
            config = self.request_config(request)
            validate_config(config)
            descriptors.append((request, config))
            return {'format': 'reveal.box-bootstrap/1', 'bundle': {'store': 's3', 'key': 'k', 'version_id': 'v'},
                    'config': config, 'fingerprint': 'f' * 64}
    workflow.value = frozen(graphs=['prokn'], model='claude-sonnet-5-5', limits={'timeout_seconds': 300, 'max_turns': 12, 'max_budget_usd': 1.25})
    queue = {'attempt': 1, 'inputs': workflow.value}
    execution = {'authoring_attempt': 1}
    workflow.engine.box_adapter = lambda _queue: Adapter(ROOT, environ={})
    workflow.engine.store = Mock()
    with patch('reveal_backend.workflow_execution.setting', side_effect=lambda key, default=None: {'REVEAL_EXECUTION_MODE': 'box'}.get(key, default)), \
            patch('reveal_backend.workflow_execution.read_preparation_inputs', side_effect=AssertionError('agent jobs read no account')), \
            patch('reveal_backend.workflow_execution.state.owned', return_value=('owner', None)), \
            patch('reveal_backend.workflow_execution.jobs.event') as event:
        result = asyncio.run(workflow.engine.prepare(workflow.payload, 'fence', workflow.job, queue, execution, workflow.root))
    assert result == {'next_phase': 'create'}
    request, config = descriptors[0]
    assert (request.timeout_seconds, request.max_turns, request.max_budget_usd) == (300, 12, 1.25)
    assert request.selected_graphs == ('prokn',) and config['model'] == 'claude-sonnet-5-5' and config['kind'] == 'agent'
    snapshot = workflow.engine.checkpoint.call_args.kwargs['dispatch_input']
    assert snapshot['model'] == 'claude-sonnet-5-5' and snapshot['selected_graphs'] == ['prokn'] and snapshot['kind'] == 'agent'
    assert json.loads((workflow.root / snapshot['path']).read_bytes()) == workflow.value
    assert 'Prompt saved' in event.call_args.args[3]


def test_server_cap_bounds_a_frozen_budget(workflow, monkeypatch):
    workflow.value = frozen(limits={'max_budget_usd': 4})
    path = workflow.root / 'agent-input.json'; path.write_bytes(canonical_json(workflow.value))
    queue = {'dispatch_input': {'path': 'agent-input.json', 'sha256': sha256(path.read_bytes())}}
    monkeypatch.setenv('REVEAL_AGENT_JOB_MAX_BUDGET_USD', '3')
    request, _ = workflow.engine.request(workflow.job, queue, {'authoring_attempt': 1}, workflow.root)
    assert request.max_budget_usd == 3 and request.timeout_seconds == 900 and request.kind == 'agent'


# API admission -------------------------------------------------------------------------------------------

@pytest.fixture
def service(monkeypatch, tmp_path):
    env = {'REVEAL_GATEWAY_SECRET': 's' * 40, 'REVEAL_GATEWAY_SERVICE_TOKEN': 't' * 40,
           'REVEAL_GATEWAY_ISSUER': 'reveal-nextjs', 'REVEAL_GATEWAY_AUDIENCE': 'reveal-api',
           'REVEAL_ARTIFACT_STORE': 'filesystem', 'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'agent-test',
           'REVEAL_MAX_ACTIVE_JOBS': '2', 'REVEAL_NOTIFICATION_NAMESPACE': 'agent-test',
           'REVEAL_NOTIFICATION_REDIS_URL': '', 'REVEAL_NOTIFICATION_REDIS_REST_URL': '',
           'UPSTASH_REDIS_REST_URL': '', 'UPSTASH_REDIS_REST_TOKEN': '', 'REVEAL_NOTIFICATION_REDIS_REST_TOKEN': ''}
    for key, value in env.items(): monkeypatch.setenv(key, value)
    monkeypatch.delenv('REVEAL_AGENT_JOB_USERS', raising=False)
    repo = Repository(str(tmp_path / 'app.sqlite')); repo.migrate()
    users = {}
    with repo.transaction() as tx:
        for kind in ('registered', 'anonymous'):
            me = api.fresh_principal(kind); users[kind] = me['user_id']
            tx.put('principal', me['user_id'], me['user_id'], {'me': me, 'retired': False})
    monkeypatch.setattr(api, 'repo', repo)
    monkeypatch.setattr(api, 'deliver_workflow_intents', AsyncMock())
    with TestClient(api.app) as client:
        yield SimpleNamespace(client=client, repo=repo, users=users)


def post_agent(service, kind='registered', **body):
    claims = {'sub': service.users[kind], 'principal_kind': kind, 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
              'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid()}
    headers = {'Authorization': 'Bearer ' + jwt.encode(claims, 's' * 40, algorithm='HS256'), 'Idempotency-Key': uid()}
    return service.client.post('/v1/jobs', json={'kind': 'agent', 'prompt': 'Say hello.', **body}, headers=headers)


def test_agent_jobs_are_off_until_users_are_named(service):
    refused = post_agent(service)
    assert refused.status_code == 403 and refused.json()['code'] == 'AGENT_JOBS_NOT_ENABLED'


def test_named_registered_user_queues_a_frozen_agent_job(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', 'someone-else, ' + service.users['registered'])
    created = post_agent(service, tools=['Read', 'Bash'], limits={'max_turns': 5})
    assert created.status_code == 202, created.text
    job = created.json()
    assert job['kind'] == 'agent' and job['status'] == 'queued' and job['research_request_id'] is None
    with service.repo.read_transaction() as tx:
        queue = tx.get('queue', job['id'])['data']
        execution = tx.get('execution', job['id'])['data']
    assert queue['inputs'] == frozen(prompt='Say hello.', tools=['Read', 'Bash'], limits={'max_turns': 5})
    assert execution['phase'] == 'prepare'
    events = service.client.get('/v1/jobs/' + job['id'] + '/events', headers={'Authorization': created.request.headers['Authorization']})
    assert events.json()['items'][0]['message'] == 'Agent run queued.'


def test_star_admits_registered_users_but_never_anonymous_ones(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    assert post_agent(service).status_code == 202
    anonymous = post_agent(service, 'anonymous')
    assert anonymous.status_code == 403 and anonymous.json()['code'] == 'AGENT_JOBS_NOT_ENABLED'


def test_an_explicitly_named_workspace_is_admitted_whatever_its_kind(service, monkeypatch):
    # An operator naming an API-key workspace chose it deliberately, even if it is anonymous.
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', service.users['anonymous'])
    assert post_agent(service, 'anonymous').status_code == 202
    assert post_agent(service).status_code == 403


def test_agent_jobs_need_the_workflow_runtime(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    monkeypatch.setenv('REVEAL_JOB_TRANSPORT', 'database')
    refused = post_agent(service)
    assert refused.status_code == 409 and refused.json()['code'] == 'AGENT_JOBS_UNAVAILABLE'


def test_invalid_agent_requests_are_rejected_before_queueing(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    for body, status in (({'tools': ['WebFetch']}, 422), ({'limits': {'max_budget_usd': 99}}, 422),
                         ({'surprise': True}, 422), ({'prompt': ''}, 422)):
        response = post_agent(service, **body)
        assert response.status_code == status, (body, response.text)
    with service.repo.read_transaction() as tx:
        assert tx.list('job') == []
