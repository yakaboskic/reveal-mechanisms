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

from reveal_backend import agent_limits, box_agent, box_remote, jobs, workflow_execution
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


TRIPLE = {'subject_entity': 'HGNC.SYMBOL:LEPR', 'relation': 'biolink:genetically_associated_with', 'object_entity': 'MONDO:0005148'}


def fake_account(question, claims):
    """Stands in for agent_account.build, which mints with the pinned DAPPER snapshot."""
    gap = {'id': 'dapper:KnowledgeGap.' + 'G' * 32, 'text': question, 'gap_description': 'test'}
    propositions = [{'id': 'dapper:Proposition.' + format(index, '032d'), **claim} for index, claim in enumerate(claims)]
    return {'question': question,
            'claims': [{'statement': node['statement'], 'proposition_id': node['id'], **{slot: node[slot] for slot in box_agent.TRIPLE_SLOTS if slot in node}}
                       for node in propositions],
            'package': {'package_version': 'reveal.evidence-package/0.2-draft', 'dapper_pin': {'snapshot_sha256': 'a' * 64},
                        'selection': {'knowledge_gap_id': gap['id']}, 'source_artifacts': {},
                        'dapper_context': {'knowledge_gaps': [gap], 'prefixes': {'biolink': 'https://w3id.org/biolink/vocab/'},
                                           **({'propositions': propositions} if propositions else {})}}}


def account_job(**body):
    return box_agent.freeze_input({'kind': 'agent', 'prompt': 'Build the account.', 'deliverable': 'scientific_account',
                                   'question': 'Does loss of LEPR signalling cause hepatic insulin resistance?', **body},
                                  budget_cap=5, account_builder=fake_account)


def frozen(**body):
    return box_agent.freeze_input({'kind': 'agent', 'prompt': 'Summarize the sandbox.', **body}, budget_cap=5)


# Frozen input -----------------------------------------------------------------------------------------------

def test_freeze_fills_every_default_so_the_frozen_bytes_say_what_runs():
    value = frozen()
    assert value == {'format': 'reveal.agent-input/4', 'prompt': 'Summarize the sandbox.', 'system_prompt': None,
                     'tools': list(box_agent.DEFAULT_TOOLS), 'graphs': [], 'network': 'restricted', 'allowed_domains': [],
                     'effort': 'medium', 'model': box_agent.DEFAULT_MODEL,
                     'limits': {'timeout_seconds': 900, 'max_turns': 50, 'max_budget_usd': 2.0, 'tool_calls': 30},
                     'deliverable': 'answer', 'account': None, 'claims_repository': False}
    # The default budget never exceeds a lower server cap.
    assert box_agent.freeze_input({'kind': 'agent', 'prompt': 'x'}, budget_cap=0.5)['limits']['max_budget_usd'] == 0.5


def test_freeze_orders_tools_and_graphs_canonically():
    value = frozen(tools=['Bash', 'Read'], graphs=['prokn', 'biomarkerkg'], effort='high', model='claude-sonnet-5-5')
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
    ({'limits': {'max_turns': 101}}, 'max_turns must be a whole number from 1 to 100'),
    ({'limits': {'tool_calls': 301}}, 'tool_calls must be a whole number from 1 to 300'),
    ({'limits': {'tool_calls': 2.5}}, 'tool_calls'),
    ({'limits': {'tool_calls': True}}, 'tool_calls'),
    ({'limits': {'max_reads': 9}}, 'limits has no max_reads'),
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


def test_a_request_may_raise_a_limit_up_to_its_ceiling_and_the_policy_sets_both():
    value = frozen(limits={'tool_calls': 300, 'max_turns': 100})
    assert (value['limits']['tool_calls'], value['limits']['max_turns']) == (300, 100)
    policy = box_agent.default_policy()
    policy['tool_calls'] = {'default': 40, 'ceiling': 200}
    policy['max_turns'] = {'default': 80, 'ceiling': 400}
    body = {'kind': 'agent', 'prompt': 'x'}
    # A client that sends no limits gets the operator's defaults.
    limits = box_agent.freeze_input(body, policy=policy)['limits']
    assert (limits['tool_calls'], limits['max_turns'], limits['timeout_seconds']) == (40, 80, 900)
    assert box_agent.freeze_input({**body, 'limits': {'tool_calls': 200, 'max_turns': 400}}, policy=policy)['limits']['max_turns'] == 400
    with pytest.raises(box_agent.AgentInputError, match='tool_calls must be a whole number from 1 to 200'):
        box_agent.freeze_input({**body, 'limits': {'tool_calls': 201}}, policy=policy)
    for broken in ({**policy, 'max_turns': {'default': 80, 'ceiling': 50}}, {**policy, 'max_turns': {'default': 80, 'ceiling': 5000}},
                   {key: value for key, value in policy.items() if key != 'tool_calls'}):
        with pytest.raises(box_agent.AgentInputError, match='not configured'):
            box_agent.freeze_input(body, policy=broken)


def test_the_literature_switch_and_its_limits_are_refused_and_earlier_formats_do_not_validate():
    for literature in (True, False):
        with pytest.raises(box_agent.AgentInputError, match='literature is not an agent job setting'):
            frozen(literature=literature)
        with pytest.raises(box_agent.AgentInputError, match='literature is not an agent job setting'):
            account_job(literature=literature)
    for key in ('literature_searches', 'literature_search_results', 'literature_papers', 'literature_characters'):
        assert key not in box_agent.LIMITS
        with pytest.raises(box_agent.AgentInputError, match=f'limits has no {key}; it accepts timeout_seconds, max_turns, max_budget_usd, tool_calls'):
            frozen(limits={key: 4})
        with pytest.raises(box_agent.AgentInputError, match=f'limits has no {key}'):
            account_job(limits={key: 4})
    value = frozen()
    # An input frozen in an earlier format, or carrying the removed key or limits, does not run.
    for change in ({'format': 'reveal.agent-input/1'}, {'format': 'reveal.agent-input/2'}, {'format': 'reveal.agent-input/3'},
                   {'literature': False}, {'limits': {**value['limits'], 'literature_papers': 4}}):
        with pytest.raises(box_agent.AgentInputError):
            box_agent.validate_input({**value, **change})
    # Every key of the one format is required, deliverable included.
    for key in box_agent.INPUT_KEYS - {'format'}:
        with pytest.raises(box_agent.AgentInputError, match='Invalid agent input format'):
            box_agent.validate_input({name: item for name, item in value.items() if name != key})
    with pytest.raises(box_agent.AgentInputError, match='limits'):
        box_agent.validate_input({**value, 'limits': {key: value['limits'][key] for key in ('timeout_seconds', 'max_turns', 'max_budget_usd')}})
    # Bounds the Box enforces follow the registry: 1,000 turns pass, 1,001 do not.
    assert box_agent.validate_input({**value, 'limits': {**value['limits'], 'max_turns': 1000}})
    with pytest.raises(box_agent.AgentInputError, match='max_turns'):
        box_agent.validate_input({**value, 'limits': {**value['limits'], 'max_turns': 1001}})


def test_an_account_job_freezes_the_question_claims_and_package_and_the_answer_job_sandbox_with_bash_and_the_open_network():
    value = account_job(claims=['  LEPR loss reduces hepatic AKT signalling. ', 'Leptin suppresses hepatic glucose production.'])
    assert value['deliverable'] == 'scientific_account'
    assert value['tools'] == ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch'] == list(box_agent.ACCOUNT_DEFAULT_TOOLS)
    assert (value['network'], value['allowed_domains'], value['graphs']) == ('open', [], [])
    account = value['account']
    assert account['question'] == 'Does loss of LEPR signalling cause hepatic insulin resistance?'
    assert [claim['statement'] for claim in account['claims']] == ['LEPR loss reduces hepatic AKT signalling.', 'Leptin suppresses hepatic glucose production.']
    assert set(account['package']) == box_agent.ACCOUNT_PACKAGE_KEYS
    assert account['package']['selection']['knowledge_gap_id'] == account['package']['dapper_context']['knowledge_gaps'][0]['id']
    assert box_agent.validate_input(json.loads(canonical_json(value))) == value
    # Authoring takes many turns and a stopped run returns nothing: an account job gets the run-time and turn ceilings.
    assert (value['limits']['timeout_seconds'], value['limits']['max_turns'], value['limits']['max_budget_usd']) == (3600, 100, 2.0)
    assert account_job(limits={'timeout_seconds': 600, 'max_turns': 20})['limits']['max_turns'] == 20
    assert account_job()['account']['claims'] == [] and 'propositions' not in account_job()['account']['package']['dapper_context']
    settings = box_agent.bootstrap_settings(value)
    assert settings == {'network': 'open', 'allowed_domains': [], 'deliverable': 'scientific_account'}
    assert box_agent.validate_bootstrap_settings(settings) == settings
    assert box_agent.network_policy([], 'open', [], 'scientific_account') == {'mode': 'allow-all'}
    assert box_agent.deliverable_of(value) == 'scientific_account' and box_agent.deliverable_of(frozen()) == 'answer'
    # Bootstrap settings carry a deliverable and no literature switch.
    for broken in ({'network': 'restricted', 'allowed_domains': []}, {**settings, 'literature': True}):
        with pytest.raises(box_agent.AgentInputError, match='Invalid agent bootstrap settings'):
            box_agent.validate_bootstrap_settings(broken)


def test_an_account_job_takes_the_same_tools_network_hosts_and_graphs_as_an_answer_job():
    # On a network it restricts, the default leaves out WebFetch, which needs the open network.
    restricted = account_job(network='restricted', allowed_domains=['www.ebi.ac.uk', 'eutils.ncbi.nlm.nih.gov'], graphs=['prokn'])
    assert restricted['tools'] == ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch']
    assert (restricted['network'], restricted['allowed_domains'], restricted['graphs']) == (
        'restricted', ['eutils.ncbi.nlm.nih.gov', 'www.ebi.ac.uk'], ['prokn'])
    assert box_agent.validate_input(json.loads(canonical_json(restricted))) == restricted
    # The Box clones the pinned DAPPER release for the account tools, so a restricted account job's egress also reaches
    # GitHub; Europe PMC is reached only when the request names it.
    assert box_agent.network_policy(['prokn'], 'restricted', restricted['allowed_domains'], 'scientific_account') == {
        'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us', 'github.com', 'eutils.ncbi.nlm.nih.gov', 'www.ebi.ac.uk']}
    assert box_agent.network_policy([], 'restricted', [], 'scientific_account') == {
        'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'github.com']}
    assert account_job(tools=['Read', 'Write', 'Bash'])['tools'] == ['Read', 'Write', 'Bash']
    assert account_job(tools=['Read', 'WebFetch'], network='open')['tools'] == ['Read', 'WebFetch']
    for body, message in (({'tools': ['WebFetch'], 'network': 'restricted'}, 'needs network "open"'),
                          ({'allowed_domains': ['eutils.ncbi.nlm.nih.gov']}, 'already reaches every host')):
        with pytest.raises(box_agent.AgentInputError, match=message):
            account_job(**body)


@pytest.mark.parametrize('body, message', [
    ({'question': None}, 'question must be nonblank'),
    ({'question': 'x' * 2001}, 'question must be nonblank'),
    ({'claims': 'one claim'}, 'claims must be a list'),
    ({'claims': ['x'] * 21}, 'at most 20 claims'),
    ({'claims': ['same', ' same ']}, 'claims must be distinct'),
    ({'claims': ['same', {'statement': 'same', **TRIPLE}]}, 'claims must be distinct'),
    ({'claims': ['']}, 'each claim must be nonblank'),
    ({'claims': ['x' * 1001]}, 'each claim must be nonblank'),
    ({'claims': [{'subject_entity': 'HGNC.SYMBOL:LEPR'}]}, 'each claim must be nonblank'),
    ({'claims': [7]}, 'each claim is a statement, or an object'),
    ({'claims': [{'statement': 'c', 'subject': 'HGNC.SYMBOL:LEPR'}]}, 'each claim is a statement, or an object'),
    ({'claims': [{'statement': 'c', 'subject_entity': 'HGNC.SYMBOL:LEPR', 'relation': 'biolink:affects'}]}, 'give all three or none'),
    ({'claims': [{'statement': 'c', **TRIPLE, 'object_entity': 'type 2 diabetes'}]}, 'object_entity must be a CURIE or an absolute URI'),
    ({'claims': [{'statement': 'c', **TRIPLE, 'subject_entity': 'LEPR'}]}, 'subject_entity must be a CURIE or an absolute URI'),
    ({'claims': [{'statement': 'c', **TRIPLE, 'relation': 'biolink:' + 'x' * 300}]}, 'relation must be a CURIE or an absolute URI'),
    ({'deliverable': 'report'}, 'deliverable must be'),
])
def test_account_jobs_reject_what_would_break_the_account(body, message):
    with pytest.raises(box_agent.AgentInputError, match=message):
        account_job(**body)


def test_account_fields_are_refused_on_an_answer_job_and_frozen_accounts_are_checked_for_agreement():
    for body in ({'question': 'q'}, {'claims': ['c']}, {'attribute_requester': True}):
        with pytest.raises(box_agent.AgentInputError, match='belong to deliverable'):
            frozen(**body)
    with pytest.raises(box_agent.AgentInputError, match='not available on this deployment'):
        box_agent.freeze_input({'kind': 'agent', 'prompt': 'x', 'deliverable': 'scientific_account', 'question': 'q'}, budget_cap=5)
    value = account_job(claims=['A caller claim.'])
    account = value['account']
    other_gap = {**account['package'], 'selection': {'knowledge_gap_id': 'dapper:KnowledgeGap.' + 'X' * 32}}
    for change in ({'account': None}, {'account': {**account, 'question': 'another question'}},
                   {'account': {**account, 'claims': []}}, {'account': {**account, 'package': other_gap}},
                   {'account': {**account, 'package': {**account['package'], 'source_artifacts': {'x': {}}}}},
                   {'account': {**account, 'claims': [{'statement': 'A caller claim.', 'proposition_id': 'urn:not-minted'}]}},
                   {'account': {**account, 'attribute_requester': 'yes'}}, {'account': {**account, 'requester': 'someone'}},
                   {'literature': True}, {'deliverable': 'answer'}):
        with pytest.raises(box_agent.AgentInputError):
            box_agent.validate_input({**value, **change})
    # The requester is named only on request, and all that is frozen is that yes or no.
    assert account['attribute_requester'] is False and account_job(attribute_requester=True)['account']['attribute_requester'] is True
    with pytest.raises(box_agent.AgentInputError, match='attribute_requester must be true or false'):
        account_job(attribute_requester='yes')
    box_agent.validate_input({**value, 'account': {key: item for key, item in account.items() if key != 'attribute_requester'}})
    with pytest.raises(box_agent.AgentInputError):
        box_agent.validate_input({**frozen(), 'account': account})


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
    assert box_agent.network_policy([], 'restricted', value['allowed_domains']) == {'mode': 'custom', 'allowed_domains': [
        'api.anthropic.com', 'api.platform.opentargets.org', 'eutils.ncbi.nlm.nih.gov']}
    note = box_remote.agent_system_prompt(value)
    assert 'these hosts over HTTPS: api.platform.opentargets.org, eutils.ncbi.nlm.nih.gov' in note


def test_network_policy_opens_only_what_the_chosen_tools_call():
    assert box_agent.network_policy([], 'restricted') == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com']}
    assert box_agent.network_policy(['prokn'], 'restricted') == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us']}
    assert box_agent.network_policy(['prokn'], 'open') == {'mode': 'allow-all'}


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
    value = value or frozen(graphs=['prokn'])
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
    assert config['model'] == 'claude-sonnet-5-5' and config['agent'] == {'network': 'restricted', 'allowed_domains': [], 'deliverable': 'answer'}
    validate_config(config)
    assert egress_policy(config) == {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us']}
    for changed in ({**config, 'kind': 'paragraph', 'selected_graphs': []}, {k: v for k, v in config.items() if k != 'agent'},
                    {**config, 'agent': {'network': 'everything', 'allowed_domains': [], 'deliverable': 'answer'}},
                    {**config, 'agent': {'network': 'restricted', 'allowed_domains': ['1.2.3.4'], 'deliverable': 'answer'}},
                    {**config, 'agent': {**config['agent'], 'literature': True}}):
        with pytest.raises(BoxTransportError):
            validate_config(changed)


def test_research_egress_is_unchanged():
    assert egress_policy({'kind': 'paragraph', 'selected_graphs': []}) == {
        'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us', 'github.com', 'www.ebi.ac.uk']}


# In-Box runner setup --------------------------------------------------------------------------------------

def runner_setup(tmp_path, value, workspace=False, **request_overrides):
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
    assert protected == [root / 'bundle', root / 'input', *([root / 'workspace'] if workspace else []), root / 'trusted']
    return work, runtime, prompt


def test_runner_setup_uses_the_callers_prompt_without_a_dapper_release(tmp_path):
    value = frozen(system_prompt='Answer in French.', tools=['Read', 'Write'])
    with patch('reveal_backend.dapper_release.clone_release', side_effect=AssertionError('No DAPPER clone')):
        work, runtime, prompt = runner_setup(tmp_path, value)
    assert prompt == 'Summarize the sandbox.'
    assert runtime['runtime_version'] == 'reveal.agent-runtime/1' and 'dapper' not in runtime
    assert runtime['agent'] == {'tools': ['Read', 'Write'], 'graphs': [], 'network': 'restricted',
                                'allowed_domains': [], 'effort': 'medium', 'limits': value['limits'], 'claims_url': None}
    assert (work / 'agent-input.json').read_bytes() == canonical_json(value)
    # The repository address reaches the runner from the frozen bootstrap alone, and with it the tool.
    asked = frozen(claims_repository=True)
    _, wired, _ = runner_setup(tmp_path / 'asked', asked, agent=box_agent.bootstrap_settings(asked, 'https://claims.example.org'))
    assert wired['agent']['claims_url'] == 'https://claims.example.org'
    ledger = box_remote.Ledger(tmp_path / 'ledger', 'agent-job', 1)
    offered = [tool['name'] for tool in box_remote.agent_tools([], ledger, wired['agent']).definitions()]
    assert offered == ['query_claims'] and box_remote.agent_tools([], ledger, runtime['agent']).definitions() == []
    system = box_remote.agent_system_prompt(value)
    assert system.endswith('\n\nAnswer in French.') and 'output/' in system and 'restricted' in system
    assert 'WebSearch still works' not in system and 'literature' not in system


def test_runner_setup_refuses_a_request_that_differs_from_the_frozen_input(tmp_path):
    with pytest.raises(ValueError, match='model'):
        runner_setup(tmp_path / 'a', frozen(), model='claude-other')
    with pytest.raises(ValueError, match='limits'):
        runner_setup(tmp_path / 'b', frozen(), max_turns=99)
    with pytest.raises(ValueError, match='limits'):
        runner_setup(tmp_path / 'c', frozen(), max_budget_usd=4.0)


def test_runner_gives_the_scoped_tools_the_jobs_own_frozen_limits_and_no_paper_tools(tmp_path):
    from reveal_backend.box_mcp import Ledger
    value = frozen(graphs=['prokn'], limits={'tool_calls': 120})
    _, runtime, _ = runner_setup(tmp_path / 'run', value)
    assert 'literature' not in runtime['agent'] and runtime['agent']['limits'] == value['limits']
    tools = box_remote.agent_tools(['prokn'], Ledger(tmp_path / 'ledger', 'agent-job', 1), runtime['agent'])
    assert (tools.max_calls, tools.soft_budget, tools.literature) == (120, True, None)
    names = {tool['name'] for tool in tools.definitions()}
    assert names and not {'search_papers', 'read_paper'} & names
    refused = tools.call('read_paper', {'source': 'PMC', 'id': 'PMC3491803'})
    assert refused['isError']
    # Nor does an account job: it fetches papers with its own commands.
    account = account_job()
    settings = {'tools': account['tools'], 'graphs': [], 'limits': account['limits']}
    account_tools = box_remote.agent_tools([], Ledger(tmp_path / 'account-ledger', 'agent-job', 1), settings,
                                           lint=lambda filename: {'content': []}, write_draft=lambda filename, document: {'content': []})
    assert account_tools.literature is None
    assert {tool['name'] for tool in account_tools.definitions()} == {'lint_account', 'write_account_draft'}


def run_main(tmp_path, request, runtime):
    """box_remote.main up to the MCP server, with the scoped tools it would serve. Nothing runs past serve()."""
    root = tmp_path / 'reveal'; state = root / 'state'; output = root / 'output'
    root.mkdir(parents=True)
    (root / 'request.json').write_text(json.dumps(request))
    (root / 'credentials.json').write_text('{"ANTHROPIC_API_KEY":"offline-placeholder"}')
    served = []
    def serve(tools):
        served.append(tools)
        raise RuntimeError('stop after serving')
    user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(root))
    with patch.multiple(box_remote, BASE=root, STATE=state, OUTPUT=output, SECRETS=(), RESEARCH=None), \
            patch.object(box_remote.os, 'getuid', return_value=0), patch.object(box_remote.pwd, 'getpwnam', return_value=user), \
            patch.object(box_remote, 'setup', return_value=(root, runtime, 'prompt')), patch.object(box_remote, 'serve', side_effect=serve), \
            patch.object(box_remote.subprocess, 'run', return_value=SimpleNamespace(stdout='test version')):
        box_remote.main()
    assert len(served) == 1
    return served[0]


def test_agent_jobs_are_served_no_paper_tools_while_research_runs_still_are(tmp_path):
    from reveal_backend.box_literature import LiteratureClient
    base = {'job_id': 'job', 'attempt': 1, 'selected_graphs': [], 'claude_version': 'test', 'model': 'test',
            'max_turns': 3, 'max_budget_usd': .1, 'timeout_seconds': 900}
    research = run_main(tmp_path / 'research', {**base, 'kind': 'research'}, {'dapper_root': str(tmp_path)})
    assert isinstance(research.literature, LiteratureClient)
    assert {'search_papers', 'read_paper'} <= {tool['name'] for tool in research.definitions()}
    for deliverable, value in (('answer', frozen()), ('scientific_account', account_job())):
        runtime = {'dapper_root': str(tmp_path), 'agent': {'tools': value['tools'], 'graphs': [], 'limits': value['limits']},
                   **({'deliverable': deliverable} if deliverable == 'scientific_account' else {})}
        agent = run_main(tmp_path / deliverable, {**base, 'kind': 'agent'}, runtime)
        assert agent.literature is None, deliverable
        assert not {'search_papers', 'read_paper'} & {tool['name'] for tool in agent.definitions()}, deliverable


def test_a_job_over_the_old_hundred_turn_bound_passes_bootstrap_and_adapter_validation(tmp_path):
    policy = {**box_agent.default_policy(), 'max_turns': {'default': 50, 'ceiling': 400}}
    value = box_agent.freeze_input({'kind': 'agent', 'prompt': 'x', 'limits': {'max_turns': 400}}, policy=policy)
    source = tmp_path / 'agent-input.json'; source.write_bytes(canonical_json(value))
    request = ExecutionRequest(job_id='agent-job', attempt=1, kind='agent', input_path=source, output_dir=tmp_path / 'output',
                               timeout_seconds=900, max_budget_usd=2.0, max_turns=400)
    config = BoxExecutionAdapter(ROOT, environ={'UPSTASH_BOX_API_KEY': 'box', 'ANTHROPIC_API_KEY': 'key'}).request_config(request)
    assert config['max_turns'] == 400
    validate_config(config)
    with pytest.raises(Exception):
        validate_config({**config, 'max_turns': 1001})


def test_an_input_frozen_in_an_earlier_format_fails_at_runner_setup(tmp_path):
    for index, version in enumerate((1, 2, 3)):
        with pytest.raises(box_agent.AgentInputError, match='Invalid agent input format'):
            runner_setup(tmp_path / str(index), {**frozen(), 'format': f'reveal.agent-input/{version}'})


def test_runner_sets_up_an_account_job_with_the_pinned_release_package_skill_and_request(tmp_path):
    value = account_job(claims=['Hepatocyte LEPR loss reduces insulin-stimulated AKT phosphorylation.'])
    bundle = tmp_path / 'reveal/bundle'
    for relative in ('services/backend/agent-skills/construct-literature-account/SKILL.md',
                     'services/backend/agent-runtime/authoring-schema-excerpt.yaml', 'services/backend/agent-runtime/dapper-release.json',
                     'services/backend/agent-runtime/authoring-schema-dependencies.json', 'services/backend/agent-runtime/linkml-types-1.11.1.yaml'):
        (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
        (bundle / relative).write_bytes((ROOT / relative).read_bytes())
    def clone(target, lock):
        Path(target).mkdir(parents=True)
        return {'tag': '0.2.0', 'commit': json.loads(Path(lock).read_bytes())['commit']}
    (tmp_path / 'reveal/input').mkdir(parents=True); (tmp_path / 'reveal/state').mkdir()
    root = tmp_path / 'reveal'
    (root / 'input/agent-input.json').write_bytes(canonical_json(value))
    request = {'kind': 'agent', 'job_id': 'agent-job', 'attempt': 1, 'selected_graphs': [], 'model': value['model'],
               'claude_version': '2.1.282', 'input_sha256': 'frozen', **value['limits']}
    protected = []
    with patch.multiple(box_remote, BASE=root, STATE=root / 'state', OUTPUT=root / 'output'), \
            patch.object(box_remote, 'protect', protected.append), patch.object(box_remote, 'verify_writable_output'), \
            patch.object(box_remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())), \
            patch('reveal_backend.dapper_release.clone_release', side_effect=clone):
        work, runtime, prompt = box_remote.setup(request)
    assert protected == [root / 'bundle', root / 'input', root / 'workspace', root / 'trusted']
    assert prompt == 'Build the account.' and runtime['deliverable'] == 'scientific_account'
    assert runtime['dapper']['commit'] == json.loads((ROOT / 'services/backend/agent-runtime/dapper-release.json').read_bytes())['commit']
    assert runtime['dapper_root'] == str(root / 'workspace/dapper')
    # The run lints against exactly the package frozen when the job was queued.
    assert json.loads(Path(runtime['evidence_package']).read_bytes()) == value['account']['package']
    request_view = json.loads((work / 'account-request.json').read_text())
    assert request_view['question'] == value['account']['question'] and request_view['claims'] == value['account']['claims']
    assert request_view['question_id'] == value['account']['package']['selection']['knowledge_gap_id']
    assert request_view['limits'] == value['limits']
    assert (work / 'account/SKILL.md').read_text().startswith('# Construct a literature Scientific Account')
    assert (work / 'account/authoring-schema-excerpt.yaml').stat().st_size > 10_000
    assert set(runtime['derived_input_views']) == {'account/SKILL.md', 'account/authoring-schema-excerpt.yaml', 'account-request.json'}
    # Drafts are attributed to the runtime inside the Box; the backend replaces that with the requester afterwards.
    assert runtime['draft_attribution']['activities'][0]['software_name'] == 'Claude Code'
    system = box_remote.agent_system_prompt(value)
    assert 'account/SKILL.md' in system and 'output/account-1.json' in system and 'account-request.json' in system
    assert 'account/SKILL.md' not in box_remote.agent_system_prompt(frozen())
    # The open network is stated as open, and the agent is told to fetch papers itself into output/sources/.
    assert 'The network is open' in system and 'restricted' not in system and 'literature' not in system
    assert 'with a Bash command and save it under output/sources/' in system and 'package installs' not in system
    restricted = box_remote.agent_system_prompt(account_job(network='restricted', allowed_domains=['www.ebi.ac.uk']))
    assert 'restricted to the Anthropic API and these hosts over HTTPS: www.ebi.ac.uk' in restricted
    assert 'output/sources/; only the hosts named above can be reached' in restricted
    shell_less = box_remote.agent_system_prompt(account_job(tools=['Read', 'Write']))
    assert 'output/sources/' not in shell_less and 'no Bash tool, so it cannot fetch papers' in shell_less
    # The requester is nowhere the agent can read.
    assert 'attribution' not in (work / 'agent-input.json').read_text()


def test_account_tools_do_not_spend_the_call_budget_and_an_answer_job_has_none(tmp_path):
    from reveal_backend.box_mcp import Ledger
    saved, linted = [], []
    settings = {'tools': ['Read'], 'graphs': [], 'limits': account_job(limits={'tool_calls': 2})['limits']}
    ledger = Ledger(tmp_path / 'ledger', 'agent-job', 1)
    tools = box_remote.agent_tools([], ledger, settings, lint=lambda filename: linted.append(filename) or {'content': []},
                                   write_draft=lambda filename, document: saved.append(filename) or {'content': []})
    names = {tool['name'] for tool in tools.definitions()}
    assert names == {'lint_account', 'write_account_draft'}
    for _ in range(5):
        assert not tools.call('write_account_draft', {'filename': 'account-1.json', 'document': {'scientific_accounts': [{}]}}).get('isError')
        assert not tools.call('lint_account', {'filename': 'account-1.json'}).get('isError')
    assert (len(saved), len(linted), tools.tool_calls, tools.authoring_calls) == (5, 5, 0, 10)
    # One account, one file: the gate reads account-1.json, so no other name can be drafted or linted. The refusal is an
    # ordinary tool error the agent can act on, never a policy denial (which would fail the whole run's ledger check).
    for tool, arguments in (('write_account_draft', {'filename': 'account-2.json', 'document': {'scientific_accounts': [{}]}}),
                            ('lint_account', {'filename': 'account-2.json'})):
        other = tools.call(tool, arguments)
        assert other['isError'] and 'account-1.json' in other['content'][0]['text'] and ledger.entries[-1]['status'] == 'failed'
    assert (len(saved), len(linted)) == (5, 5)
    schemas = {tool['name']: tool['inputSchema']['properties']['filename'] for tool in tools.definitions() if tool['name'] in ('lint_account', 'write_account_draft')}
    assert schemas == {'lint_account': {'type': 'string', 'enum': ['account-1.json']}, 'write_account_draft': {'type': 'string', 'enum': ['account-1.json']}}
    assert tools.budget()['tool_calls'] == {'used': 0, 'allowed': 2}
    plain = box_remote.agent_tools([], Ledger(tmp_path / 'plain', 'agent-job', 1), settings)
    assert not {'lint_account', 'write_account_draft'} & {tool['name'] for tool in plain.definitions()}
    refused = plain.call('lint_account', {'filename': 'account-1.json'})
    assert refused['isError'] and 'not authorized' in refused['content'][0]['text']


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


def test_an_account_jobs_ledger_check_requires_the_pinned_dapper_release(workflow):
    workflow.value = account_job()
    workflow.capture()
    result = captured_result(workflow.request, workflow.execution['box'], json.loads((workflow.request.output_dir / CAPTURE_MARKER).read_bytes()))
    with pytest.raises(EvidenceBuildError, match='different DAPPER release'):
        validate_execution_ledger(result, workflow.request, workflow.value['model'])
    lock = json.loads((ROOT / 'services/backend/agent-runtime/dapper-release.json').read_bytes())
    workflow.root.joinpath('attempt-1').rename(workflow.root / 'attempt-old')
    workflow.capture(dapper={'commit': lock['commit']})
    result = captured_result(workflow.request, workflow.execution['box'], json.loads((workflow.request.output_dir / CAPTURE_MARKER).read_bytes()))
    validate_execution_ledger(result, workflow.request, workflow.value['model'])


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


# A scientific_account job: gate in validate, deposit and storage in commit ------------------------------------

def checked_account(workflow):
    """What agent_account.accept leaves behind: the deposit-form document and one paper text under validated/."""
    directory = workflow.root / 'validated/papers'; directory.mkdir(parents=True)
    document = {'scientific_accounts': [{'id': 'dapper:ScientificAccount.' + 'A' * 32}], 'claims': [{'id': 'dapper:Claim.' + 'C' * 32}]}
    data = canonical_json(document); (workflow.root / 'validated/account.dapper.json').write_bytes(data)
    text = 'Glucose production rose in knockout mice.'.encode(); (directory / sha256(text)).write_bytes(text)
    return {'document': {'path': 'validated/account.dapper.json', 'sha256': sha256(data), 'size_bytes': len(data)},
            'account_id': document['scientific_accounts'][0]['id'], 'dapper_version': '0.2.1-a1', 'objects': 9, 'edges': 3, 'claims': 1,
            'value_locators': 1, 'lint_warnings': 1,
            'requested_claims': [{'statement': 'A claim.', 'assessed': True, 'claim_ids': [document['claims'][0]['id']]}],
            'papers': [{'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'reference': 'PMID:99999', 'text_sha256': sha256(text),
                        'text_bytes': len(text), 'media_type': 'text/plain', 'filename': 'PMC12345.fulltext.txt', 'evidence_items': 1,
                        'file_id': 'dapper:File.' + 'F' * 32, 'path': 'validated/papers/' + sha256(text)}]}


def commit_account_job(workflow, deposit, *, status='running', monkeypatch=None):
    workflow.capture()
    workflow.execution['validated_paths'] = ['attempt-1/output/ledger/agent-response.json']
    if 'account' not in workflow.execution: workflow.execution['account'] = checked_account(workflow)
    workflow.engine.context = Mock(return_value=({'status': status}, None, None))
    send = Mock(return_value=deposit)
    with patch('reveal_backend.worker.Worker.begin_persistence', AsyncMock(return_value=True)), \
            patch('reveal_backend.workflow_execution.jobs.finish') as finish, \
            patch('reveal_backend.workflow_execution.jobs.fenced', return_value=({'owner_user_id': 'owner', 'status': 'running'}, {})), \
            patch('reveal_backend.workflow_execution.state.owned', return_value=('owner', workflow.execution)), \
            patch('reveal_backend.claims_deposit.send', send):
        result = asyncio.run(workflow.engine.commit(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                    workflow.execution, workflow.root, workflow.request, workflow.value))
    assert result == {'next_phase': 'complete', 'done': True}
    puts = workflow.engine.repository.transaction.return_value.__enter__.return_value.put.call_args_list
    return send, finish, puts


def test_account_validation_runs_the_gate_once_and_checkpoints_what_commit_reads(workflow):
    workflow.value = account_job(claims=['A claim.'])
    workflow.queue.update(inputs=workflow.value, attribution={'user_id': 'owner', 'principal_kind': 'registered'})
    lock = json.loads((ROOT / 'services/backend/agent-runtime/dapper-release.json').read_bytes())
    workflow.capture(dapper={'commit': lock['commit']})
    record = {'document': {'path': 'validated/account.dapper.json'}}
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'), \
            patch('reveal_backend.agent_account.accept', return_value=record) as accept:
        result = asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                      workflow.execution, workflow.root, workflow.request, workflow.value))
    assert result == {'next_phase': 'commit'}
    root, job, attempt, account, attribution, captured, output = accept.call_args.args
    # The requester comes from the queue record, beside the frozen input and never inside it.
    assert (root, attempt, account, output) == (workflow.root, 1, workflow.value['account'], workflow.request.output_dir)
    assert attribution == workflow.queue['attribution'] and 'attribution' not in workflow.value
    saved = workflow.engine.checkpoint.call_args.kwargs
    assert saved == {'validated_paths': ['attempt-1/output/ledger/agent-response.json'], 'account': record}


def test_an_account_that_fails_the_gate_fails_validation_and_nothing_is_checkpointed(workflow):
    workflow.value = account_job()
    workflow.queue.update(inputs=workflow.value, attribution={'user_id': 'owner', 'principal_kind': 'registered'})
    lock = json.loads((ROOT / 'services/backend/agent-runtime/dapper-release.json').read_bytes())
    workflow.capture(dapper={'commit': lock['commit']})
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'), \
            pytest.raises(EvidenceBuildError, match='returns its account as output/account-1.json'):   # the capture holds none
        asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                             workflow.execution, workflow.root, workflow.request, workflow.value))
    workflow.engine.checkpoint.assert_not_called()


def test_account_commit_deposits_the_checked_bytes_once_then_stores_and_reports(workflow, monkeypatch):
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(workflow.root / 'kept'))
    receipt = {'status': 'deposited', 'deposit_id': 'dep_1', 'objects': 9, 'stored_differs': 0, 'unresolved_references': 0}
    send, finish, puts = commit_account_job(workflow, receipt)
    record = workflow.execution['account']
    send.assert_called_once_with((workflow.root / record['document']['path']).read_bytes())
    # The receipt is recorded before anything else can fail, so a retried commit does not deposit again.
    assert workflow.execution['account_deposit'] == receipt and puts[0].args[:2] == ('execution', 'agent-job')
    kind, identity, owner, stored = puts[1].args
    assert (kind, identity, owner) == ('agent_account', 'agent-job', 'owner')
    assert stored['document']['scientific_accounts'][0]['id'] == record['account_id'] and stored['account']['deposit'] == receipt
    text = stored['texts'][record['papers'][0]['text_sha256']]
    assert Path(text['path']).read_bytes() == b'Glucose production rose in knockout mice.' and Path(text['path']).is_relative_to(workflow.root / 'kept')
    outcome = finish.call_args.kwargs['result']
    assert finish.call_args.args[3] == 'succeeded' and outcome['response'] == 'Done. See notes.md.'
    assert outcome['account'] == stored['account'] and outcome['account']['requested_claims'][0]['assessed'] is True
    assert 'path' not in outcome['account']['papers'][0]
    again, _, _ = commit_account_job(AgentWorkflowRetry(workflow), receipt)
    again.assert_not_called()


class AgentWorkflowRetry:
    """The same workflow delivered again after its deposit was recorded."""
    def __init__(self, first):
        self.__dict__.update(first.__dict__)
        self.engine = WorkflowExecution(repository=MagicMock()); self.engine.activity = Mock(); self.engine.checkpoint = Mock()

    def capture(self): pass


@pytest.mark.parametrize('outcome', [{'status': 'failed', 'reason': 'unavailable'}, {'status': 'not_configured'},
                                     {'status': 'failed', 'reason': 'rejected', 'http_status': 400, 'detail': 'malformed deposit'}])
def test_a_deposit_that_does_not_happen_is_reported_and_never_fails_the_job(workflow, monkeypatch, outcome):
    monkeypatch.setenv('REVEAL_ARTIFACT_STORE', 'filesystem'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(workflow.root / 'kept'))
    send, finish, puts = commit_account_job(workflow, outcome)
    assert finish.call_args.args[3] == 'succeeded' and finish.call_args.kwargs['result']['account']['deposit'] == outcome
    # Nothing was recorded as deposited, so a later delivery of this step would try again.
    assert 'account_deposit' not in workflow.execution and [call.args[0] for call in puts] == ['agent_account']


def test_a_cancellation_pending_at_commit_stops_the_deposit(workflow):
    send, finish, puts = commit_account_job(workflow, {'status': 'deposited'}, status='cancel_requested')
    send.assert_not_called()
    assert finish.call_args.args[3] == 'cancelled' and puts == []


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


def test_console_budget_ceiling_bounds_a_frozen_budget_before_the_environment(tmp_path, monkeypatch):
    repository = Repository(str(tmp_path / 'limits.sqlite')); repository.migrate()
    engine = WorkflowExecution(repository=repository)
    value = box_agent.freeze_input({'kind': 'agent', 'prompt': 'x', 'limits': {'max_budget_usd': 40}},
                                   policy={**box_agent.default_policy(), 'max_budget_usd': {'default': 2.0, 'ceiling': 50.0}})
    path = tmp_path / 'agent-input.json'; path.write_bytes(canonical_json(value))
    queue = {'dispatch_input': {'path': 'agent-input.json', 'sha256': sha256(path.read_bytes())}}
    job = {'id': 'agent-job', 'kind': 'agent'}
    monkeypatch.setenv('REVEAL_AGENT_JOB_MAX_BUDGET_USD', '3')
    assert engine.request(job, queue, {'authoring_attempt': 1}, tmp_path)[0].max_budget_usd == 3
    with repository.transaction() as tx:
        agent_limits.change(tx, {'expected_version': None, 'values': {'max_budget_usd': {'ceiling': 25}}}, 'admin@example.org')
    assert engine.request(job, queue, {'authoring_attempt': 1}, tmp_path)[0].max_budget_usd == 25
    # prepare reads the ceiling off the event loop and passes it in; a passed ceiling is the one applied.
    assert engine.request(job, queue, {'authoring_attempt': 1}, tmp_path, 10.0)[0].max_budget_usd == 10
    assert engine.request(job, queue, {'authoring_attempt': 1}, tmp_path, agent_limits.budget_ceiling(repository))[0].max_budget_usd == 25


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


def test_an_account_job_is_queued_with_minted_nodes_and_its_requester_beside_the_input(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    created = post_agent(service, prompt='Build the account from open-access papers.', deliverable='scientific_account',
                         question='Does loss of LEPR signalling cause hepatic insulin resistance?',
                         claims=['Hepatocyte LEPR loss reduces insulin-stimulated AKT phosphorylation.'])
    assert created.status_code == 202, created.text
    with service.repo.read_transaction() as tx:
        queue = tx.get('queue', created.json()['id'])['data']
    inputs = queue['inputs']
    assert inputs == box_agent.validate_input(inputs) and inputs['deliverable'] == 'scientific_account'
    package, claims = inputs['account']['package'], inputs['account']['claims']
    gap = package['dapper_context']['knowledge_gaps'][0]
    assert gap['text'] == inputs['account']['question'] and 'not a knowledge gap curated' in gap['gap_description']
    assert package['dapper_context']['propositions'] == [{'id': claims[0]['proposition_id'], 'statement': claims[0]['statement']}]
    assert set(claims[0]) == {'statement', 'proposition_id'}     # a plain statement gets no structured form
    # The ids are real DAPPER addresses under the pinned snapshot, and the same request mints the same nodes.
    from reveal_backend import agent_account
    from reveal_backend.acceptance import public_runtime
    runtime = public_runtime()
    assert runtime.compute_id({k: v for k, v in gap.items() if k != 'id'}, 'KnowledgeGap', runtime.schema) == gap['id']
    assert package['dapper_pin']['snapshot_sha256'] == runtime.manifest['snapshot_sha256']
    assert {**agent_account.build(inputs['account']['question'], [{'statement': claims[0]['statement']}]), 'attribute_requester': False} == inputs['account']
    assert package['dapper_context']['prefixes'] == agent_account.PREFIXES
    # Who asked is frozen beside the input, never in it: the input is what the Box, and so the agent, can read.
    assert queue['attribution']['user_id'] == service.users['registered'] and queue['attribution']['principal_kind'] == 'registered'
    assert 'attribution' not in inputs and service.users['registered'] not in canonical_json(inputs).decode()
    # An account job gets the answer-job sandbox plus Bash, WebSearch and WebFetch on the open network by default.
    assert inputs['tools'] == ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch']
    assert (inputs['network'], inputs['allowed_domains'], inputs['graphs']) == ('open', [], [])
    plain = post_agent(service)
    with service.repo.read_transaction() as tx:
        assert 'attribution' not in tx.get('queue', plain.json()['id'])['data']


def test_an_account_job_may_choose_its_tools_network_hosts_and_graphs_like_an_answer_job(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    shell = post_agent(service, deliverable='scientific_account', question='q', tools=['Read', 'Write', 'Bash'], network='restricted',
                       allowed_domains=['www.ebi.ac.uk'], graphs=['prokn'])
    assert shell.status_code == 202, shell.text
    with service.repo.read_transaction() as tx:
        asked = tx.get('queue', shell.json()['id'])['data']['inputs']
    assert (asked['tools'], asked['network'], asked['allowed_domains'], asked['graphs']) == (
        ['Read', 'Write', 'Bash'], 'restricted', ['www.ebi.ac.uk'], ['prokn'])


def test_only_its_owner_reads_a_jobs_account_and_the_text_it_quotes(service, monkeypatch, tmp_path):
    from reveal_backend import agent_account
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*'); monkeypatch.setenv('REVEAL_ARTIFACTS_DIR', str(tmp_path / 'kept'))
    created = post_agent(service, deliverable='scientific_account', question='Does loss of LEPR signalling cause hepatic insulin resistance?')
    assert created.status_code == 202, created.text
    job_id, headers = created.json()['id'], {'Authorization': created.request.headers['Authorization']}
    assert service.client.get(f'/v1/jobs/{job_id}/account', headers=headers).status_code == 404        # nothing accepted yet
    text = 'Glucose production rose in knockout mice.'.encode(); checksum = sha256(text)
    document = {'scientific_accounts': [{'id': 'dapper:ScientificAccount.' + 'A' * 32}]}
    record = {'document': {'sha256': sha256(canonical_json(document)), 'size_bytes': 10}, 'account_id': document['scientific_accounts'][0]['id'],
              'dapper_version': '0.2.1-a1', 'objects': 5, 'edges': 2, 'claims': 1, 'value_locators': 1, 'lint_warnings': 0, 'requested_claims': [],
              'papers': [{'source': 'PMC', 'id': 'PMC12345', 'section': 'full_text', 'reference': 'PMID:99999', 'text_sha256': checksum,
                          'text_bytes': len(text), 'media_type': 'text/plain', 'filename': 'PMC12345.fulltext.txt', 'evidence_items': 1,
                          'file_id': 'dapper:File.' + 'F' * 32}]}
    retained = {checksum: agent_account.retain(job_id, checksum, text, 'text/plain')}
    with service.repo.transaction() as tx:
        agent_account.save(tx, service.users['registered'], job_id, record, document, retained, {'status': 'not_configured'}, 'now')
    found = service.client.get(f'/v1/jobs/{job_id}/account', headers=headers)
    assert found.status_code == 200 and found.headers['cache-control'] == 'private, no-store'
    assert found.json() == {'job_id': job_id, 'account': agent_account.public(record, {'status': 'not_configured'}), 'document': document}
    paper = service.client.get(f'/v1/jobs/{job_id}/account/texts/{checksum}', headers=headers)
    assert paper.status_code == 200 and paper.content == text and paper.headers['content-disposition'].startswith('attachment')
    assert service.client.get(f'/v1/jobs/{job_id}/account/texts/{"0" * 64}', headers=headers).status_code == 404
    # Another workspace sees neither, and neither is public.
    claims = {'sub': service.users['anonymous'], 'principal_kind': 'anonymous', 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
              'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid()}
    other = {'Authorization': 'Bearer ' + jwt.encode(claims, 's' * 40, algorithm='HS256')}
    for path in (f'/v1/jobs/{job_id}/account', f'/v1/jobs/{job_id}/account/texts/{checksum}'):
        assert service.client.get(path, headers=other).status_code == 404
        assert service.client.get(path).status_code in (401, 403, 404)


def test_invalid_agent_requests_are_rejected_before_queueing(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    for body, status in (({'tools': ['WebFetch']}, 422), ({'limits': {'max_budget_usd': 99}}, 422),
                         ({'surprise': True}, 422), ({'prompt': ''}, 422),
                         # The literature switch and its limits are gone: a request that still sends them is invalid.
                         ({'literature': True}, 422), ({'literature': False}, 422),
                         ({'deliverable': 'scientific_account', 'question': 'q', 'literature': True}, 422),
                         *(({'limits': {key: 4}}, 422) for key in ('literature_searches', 'literature_search_results',
                                                                   'literature_papers', 'literature_characters'))):
        response = post_agent(service, **body)
        assert response.status_code == status, (body, response.text)
        assert response.json()['code'] == 'INVALID_REQUEST', response.text
    with service.repo.read_transaction() as tx:
        assert tx.list('job') == []


# Admin console limits --------------------------------------------------------------------------------------

def admin_headers(purpose='admin_settings', token='t' * 40, **claims):
    body = {'sub': 'admin-console', 'principal_kind': 'registered', 'purpose': purpose, 'iss': 'reveal-nextjs', 'aud': 'reveal-api',
            'iat': int(time.time()), 'exp': int(time.time()) + 120, 'jti': uid(), **claims}
    return {'Authorization': 'Bearer ' + token, 'X-Reveal-Admin-Assertion': jwt.encode(body, 's' * 40, algorithm='HS256')}


def test_admin_reads_and_changes_limits_and_the_next_job_is_frozen_with_them(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    path = '/internal/v1/admin/agent-limits'
    view = service.client.get(path, headers=admin_headers(admin_email='admin@example.org'))
    assert view.status_code == 200 and view.headers['cache-control'] == 'private, no-store'
    body = view.json()
    assert body['version'] is None and [limit['key'] for limit in body['limits']] == list(box_agent.LIMITS)
    calls = next(limit for limit in body['limits'] if limit['key'] == 'tool_calls')
    assert calls['default'] == {'value': 30, 'source': 'code', 'environment': 'REVEAL_AGENT_JOB_DEFAULT_TOOL_CALLS'}
    assert (calls['minimum'], calls['maximum'], calls['console']) == (1, 5000, {'default': None, 'ceiling': None})

    change = {'expected_version': None, 'values': {'tool_calls': {'default': 250, 'ceiling': 2000},
                                                   'timeout_seconds': {'default': 1200}}}
    saved = service.client.put(path, json=change, headers=admin_headers(admin_email='admin@example.org'))
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 1 and saved.json()['updated_by'] == 'admin@example.org'
    assert saved.json()['history'][0]['changes']['tool_calls'] == {'default': [None, 250], 'ceiling': [None, 2000]}

    # The client that sends no limits now gets the console's defaults, with no change on its side.
    created = post_agent(service)
    assert created.status_code == 202, created.text
    with service.repo.read_transaction() as tx:
        limits = tx.get('queue', created.json()['id'])['data']['inputs']['limits']
    assert (limits['tool_calls'], limits['timeout_seconds'], limits['max_turns']) == (250, 1200, 50)
    refused = post_agent(service, limits={'tool_calls': 2001})
    assert refused.status_code == 422 and 'from 1 to 2000' in refused.json()['detail']
    # JSON has one number type: 30.0 passes the API schema and is the whole number 30.
    assert frozen(limits={'tool_calls': 30.0, 'max_turns': 7.0})['limits']['tool_calls'] == 30
    assert post_agent(service, limits={'tool_calls': 2000}).status_code == 202


def test_admin_limit_changes_are_validated_fenced_and_need_their_own_authority(service):
    path = '/internal/v1/admin/agent-limits'
    headers = lambda: admin_headers(admin_email='admin@example.org')
    for values, message in (({'tool_calls': {'default': 301}}, 'above its ceiling 300'),
                            ({'max_turns': {'ceiling': 1001}}, 'from 1 to 1000'),
                            ({'max_budget_usd': {'ceiling': 0}}, 'above 0'),
                            ({'tool_calls': {'default': 4.5}}, 'whole number'),
                            ({'reads': {'default': 4}}, 'not an agent job limit'),
                            ({'literature_papers': {'default': 4}}, 'not an agent job limit'),
                            ({'max_turns': {'floor': 4}}, 'default and ceiling only')):
        refused = service.client.put(path, json={'expected_version': None, 'values': values}, headers=headers())
        assert refused.status_code == 422 and refused.json()['code'] == 'INVALID_LIMITS' and message in refused.json()['detail'], refused.text
    assert service.client.put(path, content=b'{', headers=headers()).status_code == 422
    assert service.client.put(path, content=b' ' * 20_000, headers=headers()).status_code == 413
    assert service.client.put(path, json={'expected_version': None, 'values': {'max_turns': {'ceiling': 400}}}, headers=headers()).status_code == 200
    stale = service.client.put(path, json={'expected_version': None, 'values': {}}, headers=headers())
    assert stale.status_code == 409 and stale.json()['code'] == 'LIMITS_CHANGED'
    # Clearing every console value falls back to the environment and the code.
    cleared = service.client.put(path, json={'expected_version': 1, 'values': {}}, headers=headers())
    assert cleared.status_code == 200 and cleared.json()['version'] == 2
    assert next(limit for limit in cleared.json()['limits'] if limit['key'] == 'max_turns')['ceiling']['source'] == 'code'
    # Telemetry authority cannot read or change limits, and neither can a missing service credential.
    for request in (service.client.get, lambda *a, **k: service.client.put(*a, json={'expected_version': 2, 'values': {}}, **k)):
        assert request(path, headers=admin_headers('admin_telemetry')).status_code == 401
        assert request(path, headers=admin_headers(token='wrong' * 8)).status_code == 403
    with service.repo.read_transaction() as tx:
        assert tx.get('agent_limits', 'current')['version'] == 2


def test_a_claim_may_carry_its_triple_and_it_is_frozen_on_the_claim_and_its_proposition():
    value = account_job(claims=['A statement only.', {'statement': ' LEPR is associated with type 2 diabetes. ', **TRIPLE}])
    plain, structured = value['account']['claims']
    assert set(plain) == {'statement', 'proposition_id'}
    assert structured == {'statement': 'LEPR is associated with type 2 diabetes.', 'proposition_id': structured['proposition_id'], **TRIPLE}
    node = value['account']['package']['dapper_context']['propositions'][1]
    assert node == {'id': structured['proposition_id'], 'statement': structured['statement'], **TRIPLE}
    assert box_agent.validate_input(value) == value
    # The frozen claim and the package node must agree on the triple, and a frozen triple is all three terms.
    for change in (lambda v: v['account']['claims'][1].update(object_entity='MONDO:0000001'),
                   lambda v: v['account']['claims'][1].pop('relation'),
                   lambda v: v['account']['package']['dapper_context']['propositions'][0].update(TRIPLE),
                   lambda v: v['account']['package']['dapper_context'].update(prefixes={'biolink': 'not a url'})):
        broken = json.loads(json.dumps(value)); change(broken)
        with pytest.raises(box_agent.AgentInputError):
            box_agent.validate_input(broken)


def test_a_requested_triple_is_minted_into_the_proposition_and_an_unknown_prefix_is_refused():
    from reveal_backend import agent_account
    from reveal_backend.acceptance import public_runtime
    runtime = public_runtime()
    plain = box_agent.claim_entry('LEPR is associated with type 2 diabetes.')
    structured = box_agent.claim_entry({'statement': 'LEPR is associated with type 2 diabetes.', **TRIPLE})
    built = agent_account.build('Is LEPR a type 2 diabetes gene?', [structured])
    node = built['package']['dapper_context']['propositions'][0]
    assert {slot: node[slot] for slot in box_agent.TRIPLE_SLOTS} == TRIPLE == {slot: built['claims'][0][slot] for slot in box_agent.TRIPLE_SLOTS}
    assert runtime.compute_id({k: v for k, v in node.items() if k != 'id'}, 'Proposition', runtime.schema) == node['id']
    # The triple is part of what the id addresses: the same statement without it is another Proposition.
    assert agent_account.build('Is LEPR a type 2 diabetes gene?', [plain])['claims'][0]['proposition_id'] != node['id']
    assert built['package']['dapper_context']['prefixes'] == agent_account.PREFIXES
    for slot, value in (('subject_entity', 'GENE:LEPR'), ('relation', 'associated_with:x'), ('object_entity', 'T2D:1')):
        with pytest.raises(box_agent.AgentInputError, match=slot + '.*prefix REVEAL knows'):
            agent_account.build('q?', [box_agent.claim_entry({'statement': 's', **TRIPLE, slot: value})])
    # Absolute URIs and the pinned release's own prefixes need no declaration.
    agent_account.build('q?', [box_agent.claim_entry({'statement': 's', 'subject_entity': 'https://identifiers.org/hgnc:6554',
                                                      'relation': 'obo:RO_0002610', 'object_entity': 'KPN.TRAIT:0000505'})])


def test_the_lint_asks_for_a_triple_where_a_proposition_has_none_and_never_fails_for_it():
    from reveal_backend import box_remote
    requested = [{'id': 'dapper:Proposition.' + 'a' * 32, 'statement': 'Requested, text only.'},
                 {'id': 'dapper:Proposition.' + 'b' * 32, 'statement': 'Requested, structured.', **TRIPLE}]
    package = {'dapper_context': {'propositions': requested}}
    document = {'propositions': [{'id': 'urn:draft:prop-1', 'statement': 'Mine, text only.'},
                                 {'id': 'urn:draft:prop-2', 'statement': 'Mine, structured.', **TRIPLE},
                                 {'id': 'urn:draft:prop-3', 'statement': 'Requested, text only.', **TRIPLE}],
                'claims': [{'id': 'urn:draft:claim-1', 'proposition': requested[0]['id']},
                           {'id': 'urn:draft:claim-2', 'proposition': requested[0]['id']},
                           {'id': 'urn:draft:claim-3', 'proposition': requested[1]['id']},
                           {'id': 'urn:draft:claim-4', 'proposition': 'urn:draft:prop-2'}]}
    found = box_remote.triple_findings(document, package)
    assert [(f['where'], f['message']) for f in found] == [('propositions[0]', box_remote.TRIPLE_OWN),
                                                           ('claims[0].proposition', box_remote.TRIPLE_REQUESTED)]
    assert all(f['severity'] == 'suggestion' and f['check'] == 'proposition-triple' for f in found)
    # Fixed text: nothing from the draft is repeated, so the message reaches the agent as written.
    assert not any(node['statement'] in f['message'] for f in found for node in document['propositions'])
    assert box_remote.triple_findings({'propositions': 'not a list', 'claims': None}, {}) == []
    from reveal_backend.authoring_structure import _normalized_finding
    assert _normalized_finding(found[0])['message'] == box_remote.TRIPLE_OWN


def test_a_requested_claim_restated_with_a_triple_still_counts_as_assessed():
    from reveal_backend import agent_account
    requested = [{'statement': 'Requested, text only.', 'proposition_id': 'dapper:Proposition.' + 'a' * 32},
                 {'statement': 'Requested, never assessed.', 'proposition_id': 'dapper:Proposition.' + 'b' * 32}]
    restated = {'id': 'dapper:Proposition.' + 'c' * 32, 'statement': 'Requested, text only.', **TRIPLE}
    document = {'propositions': [restated], 'claims': [{'id': 'dapper:Claim.' + 'd' * 32, 'proposition': restated['id']}]}
    assert agent_account.requested_claims(requested, document) == [
        {'statement': 'Requested, text only.', 'assessed': True, 'claim_ids': ['dapper:Claim.' + 'd' * 32]},
        {'statement': 'Requested, never assessed.', 'assessed': False, 'claim_ids': []}]


def test_the_api_accepts_structured_claims_and_refuses_a_partial_or_unresolvable_triple(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', '*')
    ask = dict(prompt='Build the account.', deliverable='scientific_account', question='Is LEPR a type 2 diabetes gene?')
    created = post_agent(service, **ask, claims=['A statement only.', {'statement': 'LEPR is associated with type 2 diabetes.', **TRIPLE}])
    assert created.status_code == 202, created.text
    with service.repo.read_transaction() as tx:
        claims = tx.get('queue', created.json()['id'])['data']['inputs']['account']['claims']
    assert set(claims[0]) == {'statement', 'proposition_id'} and {slot: claims[1][slot] for slot in box_agent.TRIPLE_SLOTS} == TRIPLE
    for bad in ({'statement': 's', 'subject_entity': 'HGNC.SYMBOL:LEPR'}, {'statement': 's', **TRIPLE, 'object_entity': 'T2D:1'},
                {'statement': 's', **TRIPLE, 'subject': 'x'}, {**TRIPLE}):
        refused = post_agent(service, **ask, claims=[bad])
        assert refused.status_code == 422, (bad, refused.text)
