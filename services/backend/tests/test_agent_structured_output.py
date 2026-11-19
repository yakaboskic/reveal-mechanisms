"""Structured output for answer agent jobs: output_schema in the request, --json-schema in the run, structured_output in
the result, checked by the backend against the frozen schema."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from reveal_backend import agent_output, box_agent, box_remote, telemetry
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, sha256
from test_agent_jobs import (AgentWorkflow, account_job, fake_account, frozen, post_agent, runner_setup,  # noqa: F401
                             service, workflow)

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['genes'],
          'properties': {'genes': {'type': 'array', 'items': {'$ref': '#/$defs/gene'}}, 'note': {'type': 'string'}},
          '$defs': {'gene': {'type': 'object', 'required': ['symbol', 'evidence'], 'additionalProperties': False,
                             'properties': {'symbol': {'type': 'string', 'pattern': '^[A-Z0-9-]+$'},
                                            'evidence': {'enum': ['strong', 'weak', 'none']}}}}}
ANSWER = {'genes': [{'symbol': 'NKX2-3', 'evidence': 'strong'}, {'symbol': 'JARID2', 'evidence': 'none'}]}


def structured(**body):
    return box_agent.freeze_input({'kind': 'agent', 'prompt': 'Grade these genes.', 'output_schema': SCHEMA, **body},
                                  budget_cap=5, schema_checker=agent_output.check_schema)


def test_an_answer_job_freezes_its_schema_as_an_optional_key_of_the_one_input_format():
    value = structured()
    assert value['format'] == box_agent.INPUT_FORMAT == 'reveal.agent-input/4' and value['output_schema'] == SCHEMA
    assert box_agent.schema_of(value) == SCHEMA and box_agent.validate_input(value) is value
    assert set(value) == box_agent.INPUT_KEYS | {'output_schema'}
    # Without output_schema the input is the same format with the key left out.
    plain = frozen()
    assert plain['format'] == box_agent.INPUT_FORMAT and 'output_schema' not in plain and box_agent.schema_of(plain) is None
    assert set(plain) == box_agent.INPUT_KEYS
    assert box_agent.validate_input({k: v for k, v in value.items() if k != 'output_schema'})
    assert box_agent.validate_input({**plain, 'output_schema': SCHEMA})['output_schema'] == SCHEMA
    # The formats that split these two cases are gone.
    for version in ('reveal.agent-input/2', 'reveal.agent-input/3'):
        with pytest.raises(box_agent.AgentInputError, match='Invalid agent input format'):
            box_agent.validate_input({**value, 'format': version})
    # The schema is checked again on validation, and still belongs only to an answer job.
    with pytest.raises(box_agent.AgentInputError, match='"type": "object"'):
        box_agent.validate_input({**plain, 'output_schema': {'type': 'array'}})
    with pytest.raises(box_agent.AgentInputError, match='belongs to deliverable "answer"'):
        box_agent.validate_input({**account_job(), 'output_schema': SCHEMA})


@pytest.mark.parametrize('schema, message', [
    ([], 'must be a JSON Schema object'),
    ({'type': 'array', 'items': {}}, 'its top level needs "type": "object"'),
    ({'properties': {}}, 'its top level needs "type": "object"'),
    ({'type': 'object', 'properties': {'x': {'$ref': 'https://example.org/schema.json'}}}, 'only local references'),
    ({'type': 'object', 'description': 'x' * 40_000}, 'the most is 32,000'),
    ({'type': 'object', 'properties': {'x': {'type': 'strng'}}}, 'not a valid JSON Schema at properties/x/type'),
    ({'type': 'object', 'required': 'genes'}, 'not a valid JSON Schema at required'),
])
def test_an_invalid_schema_is_refused_with_where_it_is_wrong(schema, message):
    with pytest.raises(box_agent.AgentInputError, match=message):
        structured(output_schema=schema)


def test_a_schema_is_refused_on_an_account_job_and_without_a_checker():
    with pytest.raises(box_agent.AgentInputError, match='belongs to deliverable "answer"'):
        box_agent.freeze_input({'kind': 'agent', 'prompt': 'Build it.', 'deliverable': 'scientific_account', 'question': 'Q?',
                                'output_schema': SCHEMA}, budget_cap=5, account_builder=fake_account, schema_checker=agent_output.check_schema)
    with pytest.raises(box_agent.AgentInputError, match='not available on this deployment'):
        box_agent.freeze_input({'kind': 'agent', 'prompt': 'Grade.', 'output_schema': SCHEMA}, budget_cap=5)
    # A schema that names another draft is checked against that draft.
    old = {'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object', 'properties': {'n': {'type': 'integer'}}}
    assert structured(output_schema=old)['output_schema'] == old


def test_the_runner_passes_the_schema_to_claude_code_and_tells_the_agent(tmp_path):
    value = structured()
    work, runtime, prompt = runner_setup(tmp_path, value)
    assert prompt == 'Grade these genes.'
    assert box_remote.structured_output_arguments(value) == ['--json-schema', json.dumps(SCHEMA, separators=(',', ':'))]
    assert box_remote.structured_output_arguments(frozen()) == []
    assert 'structured output' in box_remote.agent_system_prompt(value)
    assert 'structured output' not in box_remote.agent_system_prompt(frozen())
    reason = box_remote.provider_failure_reason({'kind': 'agent', 'max_turns': 5, 'max_budget_usd': 1},
                                                {'subtype': 'error_max_structured_output_retries'})
    assert 'output_schema' in reason


def test_the_response_record_keeps_the_object_and_scrubs_credentials_from_its_strings():
    result = {'result': 'Graded.', 'structured_output': {**ANSWER, 'note': 'key sk-secret-1 seen'}, 'num_turns': 2}
    record = box_agent.response_record('agent-job', 1, 'succeeded', result, secrets=('sk-secret-1',))
    assert record['structured_output'] == {**ANSWER, 'note': 'key [REDACTED_CREDENTIAL] seen'}
    assert 'structured_output' not in box_agent.response_record('agent-job', 1, 'succeeded', {'result': 'Plain.'})
    out = box_agent.job_result(record, {}, job_id='agent-job', attempt=1)
    assert out['structured_output'] == record['structured_output'] and out['response'] == 'Graded.'
    assert 'structured_output' not in box_agent.job_result(box_agent.response_record('agent-job', 1, 'succeeded', {'result': 'x'}), {},
                                                            job_id='agent-job', attempt=1)


def test_the_backend_checks_the_returned_object_against_the_frozen_schema():
    assert agent_output.check_result({'structured_output': ANSWER}, SCHEMA) == ANSWER
    for record, message in (({}, 'without structured output'),
                            ({'structured_output': {'genes': [{'symbol': 'NKX2-3', 'evidence': 'maybe'}]}},
                             'at genes/0/evidence: it fails the "enum" rule'),
                            ({'structured_output': {'genes': [], 'extra': 1}}, 'fails the "additionalProperties" rule'),
                            ({'structured_output': {'genes': [{'symbol': 'x' * 10, 'evidence': 'weak'}] * 9000}}, 'a job result holds at most')):
        with pytest.raises(EvidenceBuildError, match=message) as refused:
            agent_output.check_result(record, SCHEMA)
        assert 'maybe' not in str(refused.value)                 # the message names the rule, never the value


def run_validate(workflow, structured_output):
    workflow.value = structured()
    workflow.queue['inputs'] = workflow.value
    workflow.capture()
    path = workflow.request.output_dir / box_agent.RESPONSE_CAPTURE_PATH
    record = json.loads(path.read_bytes())
    if structured_output is not None: record['structured_output'] = structured_output
    path.write_bytes(canonical_json(record))
    from reveal_backend.box_adapter import CAPTURE_MARKER
    marker = json.loads((workflow.request.output_dir / CAPTURE_MARKER).read_bytes())
    marker['files'][box_agent.RESPONSE_CAPTURE_PATH] = {'sha256': sha256(path.read_bytes()), 'size_bytes': path.stat().st_size}
    (workflow.request.output_dir / CAPTURE_MARKER).write_bytes(canonical_json(marker))
    with patch('reveal_backend.workflow_execution.WorkflowExecution.record_agent_metrics'):
        return asyncio.run(workflow.engine.validate(workflow.payload, 'fence', workflow.job, workflow.queue,
                                                    workflow.execution, workflow.root, workflow.request, workflow.value))


def test_validation_accepts_a_conforming_object_and_commit_returns_it(workflow):
    assert run_validate(workflow, ANSWER) == {'next_phase': 'commit'}
    workflow.execution['validated_paths'] = ['attempt-1/output/ledger/agent-response.json']
    workflow.engine.context = Mock(return_value=({'status': 'running'}, None, None))
    with patch('reveal_backend.worker.Worker.begin_persistence', AsyncMock(return_value=True)), \
            patch('reveal_backend.workflow_execution.jobs.finish') as finish:
        asyncio.run(workflow.engine.commit(workflow.payload, 'fence', workflow.job, workflow.queue,
                                           workflow.execution, workflow.root, workflow.request, workflow.value))
    assert finish.call_args.kwargs['result']['structured_output'] == ANSWER


@pytest.mark.parametrize('returned', [None, {'genes': [{'symbol': 'NKX2-3'}]}])
def test_validation_fails_a_run_that_returned_no_object_or_a_nonconforming_one(workflow, returned):
    with pytest.raises(EvidenceBuildError, match='structured output'):
        run_validate(workflow, returned)
    workflow.engine.checkpoint.assert_not_called()


def test_the_api_queues_a_structured_job_and_refuses_an_invalid_schema(service, monkeypatch):
    monkeypatch.setenv('REVEAL_AGENT_JOB_USERS', service.users['registered'])
    created = post_agent(service, output_schema=SCHEMA)
    assert created.status_code == 202, created.text
    with service.repo.read_transaction() as tx:
        inputs = tx.get('queue', created.json()['id'])['data']['inputs']
    assert inputs['format'] == box_agent.INPUT_FORMAT and inputs['output_schema'] == SCHEMA
    refused = post_agent(service, output_schema={'type': 'object', 'properties': {'x': {'type': 'strng'}}})
    assert refused.status_code == 422 and 'properties/x/type' in refused.json()['detail']
    with service.repo.read_transaction() as tx:
        run = telemetry.agent_runs(tx, [created.json()['id']])[created.json()['id']]
    assert run['output_schema_characters'] > 100
    assert run['result']['structured_output_characters'] is None
    assert 'NKX' not in json.dumps(run) and 'evidence' not in json.dumps(run)   # sizes only, never the schema
