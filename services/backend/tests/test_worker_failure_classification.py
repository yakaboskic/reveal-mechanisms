"""A preparation error must not be presented as rejected scientific output."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.repository import Repository, uid
from reveal_backend.worker import Worker


class WorkerFailureClassificationTests(unittest.TestCase):
    def run_failure(self, phase):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = Repository(str(root / 'application.db'))
            repository.migrate()
            owner, request_id = uid(), uid()
            with repository.transaction() as tx:
                tx.put('request', request_id, owner, {'composer': {'selected_kgs': []}})
                tx.put('request_binding', request_id, owner, {})
                job = jobs.enqueue(tx, owner, 'analysis', request_id=request_id)
            source = root / job['id'] / 'evidence' / 'package.json'
            source.parent.mkdir(parents=True)
            source.write_text('{}')
            adapter = type('Adapter', (), {})()
            adapter.execute = AsyncMock()
            collect_error = EvidenceBuildError('Unknown DisMech CURIE prefix: hgnc') if phase == 'evidence_preparation' else None
            if phase == 'agent_execution':
                adapter.execute.side_effect = ValueError('Invalid transport event')
            else:
                # A successful transport with no accounts must still fail validation.
                adapter.execute.return_value = ExecutionResult('succeeded', root)
            with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'deterministic', 'REVEAL_ARTIFACTS_DIR': temp}), \
                    patch('reveal_backend.worker.collect', return_value=(source, {}), side_effect=collect_error), \
                    patch('reveal_backend.worker.fit_input_budget', return_value=(source, {}, {})):
                claimed = jobs.claim(repository, 'classification-test')
                asyncio.run(Worker(repository, adapter).process(*claimed))
            with repository.transaction() as tx:
                result = tx.get('job', job['id'])['data']
                queue = tx.get('queue', job['id'])['data']
                self.assertEqual(tx.list('account', owner), [])
                self.assertIsNotNone(tx.get('request', request_id))
            diagnostic = json.loads((root / job['id'] / 'attempt-1' / 'failure.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(result['failure']['retryable'])
            self.assertEqual(diagnostic['phase'], phase)
            self.assertIsNone(queue.get('remote_handle'))
            return result['failure'], diagnostic, adapter.execute.await_count

    def test_source_identifier_error_fails_preparation_without_launching_agent(self):
        failure, diagnostic, executions = self.run_failure('evidence_preparation')
        self.assertEqual(executions, 0)
        self.assertEqual(failure['code'], 'EVIDENCE_PREPARATION_FAILED')
        self.assertIn('source evidence could not be prepared', failure['message'])
        self.assertNotIn('scientific validation', failure['message'])
        self.assertEqual(diagnostic['message'], 'Unknown DisMech CURIE prefix: hgnc')
        self.assertNotIn('hgnc', failure['message'])

    def test_transport_value_error_is_not_scientific_validation(self):
        failure, _, executions = self.run_failure('agent_execution')
        self.assertEqual(executions, 1)
        self.assertEqual(failure['code'], 'WORKER_FAILED')
        self.assertNotIn('scientific validation', failure['message'])

    def test_invalid_completed_output_retains_scientific_validation_failure(self):
        failure, diagnostic, executions = self.run_failure('scientific_validation')
        self.assertEqual(executions, 1)
        self.assertEqual(failure['code'], 'VALIDATION_FAILED')
        self.assertIn('scientific validation', failure['message'])
        self.assertIn('invalid account count', diagnostic['message'])
