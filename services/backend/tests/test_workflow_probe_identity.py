"""Nonpaid QA recovery proof identifies tasks without using credentials or IAM."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from reveal_backend import jobs
from reveal_backend.repository import Repository
from reveal_backend import workflow_execution as execution


URI = 'http://169.254.170.2/v4/abc-123'
TASK = 'arn:aws:ecs:us-east-1:005901288866:task/dig-qa/'


@pytest.fixture
def isolated(monkeypatch):
    for key in ('ECS_CONTAINER_METADATA_URI_V4', 'SERVICE_ENV'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('REVEAL_ENVIRONMENT', 'test')
    monkeypatch.setenv('REVEAL_JOB_TRANSPORT', 'workflow')
    monkeypatch.setenv('REVEAL_JOB_NAMESPACE', 'probe-test')


def response_stream(monkeypatch, payload, status=200):
    calls = []
    @contextmanager
    def stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        yield httpx.Response(status, content=payload, request=httpx.Request(method, url))
    monkeypatch.setattr(execution.httpx, 'stream', stream)
    return calls


def test_local_identity_has_no_network_request(isolated, monkeypatch):
    network = Mock(side_effect=AssertionError('Local probe must not request metadata'))
    monkeypatch.setattr(execution.httpx, 'stream', network)
    monkeypatch.setattr(execution.socket, 'gethostname', lambda: 'local-test')
    assert execution.probe_task_identity() == {'kind': 'hostname', 'hostname': 'local-test'}
    network.assert_not_called()


def test_ecs_identity_reads_only_bounded_task_metadata(isolated, monkeypatch):
    monkeypatch.setenv('SERVICE_ENV', 'qa'); monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', URI)
    calls = response_stream(monkeypatch, json.dumps({'TaskARN': TASK + 'a' * 32, 'Ignored': 'not returned'}).encode())
    assert execution.probe_task_identity() == {'kind': 'ecs_task', 'task_arn': TASK + 'a' * 32}
    assert calls == [('GET', URI + '/task', {'timeout': 2, 'trust_env': False, 'follow_redirects': False})]


@pytest.mark.parametrize('uri', ['http://169.254.170.2/v2/credentials/secret', 'http://example.com/v4/abc',
    'https://169.254.170.2/v4/abc', 'http://169.254.170.2:8080/v4/abc',
    'http://user@169.254.170.2/v4/abc', URI + '?query=1', URI + '#fragment', URI + '/../credentials'])
def test_metadata_endpoint_rejects_credentials_external_hosts_and_redirect_inputs(isolated, monkeypatch, uri):
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', uri)
    network = Mock(side_effect=AssertionError('Rejected endpoint must not be contacted'))
    monkeypatch.setattr(execution.httpx, 'stream', network)
    with pytest.raises(ValueError): execution.probe_task_identity()
    network.assert_not_called()


@pytest.mark.parametrize('payload,status', [(b'{}', 200), (b'x' * 65537, 200), (b'{}', 302), (b'{}', 500),
    (json.dumps({'TaskARN': TASK.replace('dig-qa/', 'dig-prod/') + 'a' * 32}).encode(), 200)])
def test_qa_fails_closed_without_valid_task_identity(isolated, monkeypatch, payload, status):
    monkeypatch.setenv('SERVICE_ENV', 'qa')
    with pytest.raises(ValueError, match='requires ECS task identity'): execution.probe_task_identity()
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', URI)
    response_stream(monkeypatch, payload, status)
    with pytest.raises(ValueError, match='could not obtain'): execution.probe_task_identity()


class MemoryStore:
    def __init__(self): self.files = {}
    def snapshot(self, root):
        self.files = {path.name: path.read_bytes() for path in Path(root).iterdir()}
        return {'key': 'probe-manifest'}
    def restore(self, reference, root, **kwargs):
        for name, content in self.files.items(): (Path(root) / name).write_bytes(content)


@pytest.mark.parametrize('qa,requested,expected_sleep', [(True, 900, 600), (True, 480, 480), (False, 480, 120)])
def test_probe_preserves_checkpoint_shape_and_records_task_transition(isolated, monkeypatch, tmp_path, qa, requested, expected_sleep):
    if qa:
        for key, value in {'SERVICE_ENV': 'qa', 'REVEAL_ENVIRONMENT': 'production',
            'REVEAL_JOB_NAMESPACE': 'reveal-workflow-qa', 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_qa'}.items():
            monkeypatch.setenv(key, value)
    repository = Repository(tmp_path / 'db.sqlite', table_prefix='reveal_workflow_qa' if qa else 'reveal_probe_test')
    repository.migrate()
    with repository.transaction() as tx:
        job = jobs.enqueue(tx, 'probe-owner', 'deployment_probe', inputs={'nonce': 'unchanged', 'delay': requested})
        saved = tx.get('execution', job['id'])['data']
    payload = {'job_id': job['id'], 'namespace': saved['namespace'], 'generation': saved['generation']}
    first = {'kind': 'ecs_task', 'task_arn': TASK + 'a' * 32}
    second = {'kind': 'ecs_task', 'task_arn': TASK + 'b' * 32}
    identity = Mock(side_effect=[first, second])
    monkeypatch.setattr(execution, 'probe_task_identity', identity)
    store = MemoryStore()
    prepared = asyncio.run(execution.WorkflowExecution(repository, storage=store).step(payload, 0))
    assert prepared['sleep'] == expected_sleep
    assert json.loads(store.files['probe.json']) == {'job_id': job['id'], 'nonce': 'unchanged'}
    with repository.read_transaction() as tx:
        assert tx.get('execution', job['id'])['data']['probe_prepared_task_identity'] == first
    # A replay reads its committed step; it must not recapture a different host.
    assert asyncio.run(execution.WorkflowExecution(repository, storage=store).step(payload, 0)) == prepared
    assert identity.call_count == 1
    assert asyncio.run(execution.WorkflowExecution(repository, storage=store).step(payload, 1))['done']
    with repository.read_transaction() as tx:
        result = tx.get('job', job['id'])['data']['result']
        assert result['prepared_task_identity'] == first and result['restored_task_identity'] == second
        assert result['restored'] is True
