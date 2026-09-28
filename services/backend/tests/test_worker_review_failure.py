"""Operational review failures must not masquerade as rejected science."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.repository import Repository, uid
from reveal_backend.scientific_grounding import ScientificReviewUnavailable
from reveal_backend.worker import Worker


class WorkerReviewFailureTests(unittest.TestCase):
    def run_review(self, verdict):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = Repository(str(root / 'app.db')); repo.migrate()
            owner, request_id = uid(), uid()
            with repo.transaction() as tx:
                tx.put('request', request_id, owner, {'composer': {'selected_kgs': []}, 'attribution': {'user_id': owner}})
                tx.put('request_binding', request_id, owner, {})
                job = jobs.enqueue(tx, owner, 'analysis', request_id=request_id)
            source = root / job['id'] / 'evidence/package.json'; source.parent.mkdir(parents=True); source.write_text('{}')
            output = root / job['id'] / 'attempt-1/output'; output.mkdir(parents=True)
            raw = output / 'account-1.json'; raw.write_text('{}')
            adapter = type('Adapter', (), {})(); adapter.execute = AsyncMock(return_value=ExecutionResult('succeeded', output, account_paths=(raw,), ledger_manifest_path=output / 'ledger.json'))
            review_patch = {'side_effect': verdict} if isinstance(verdict, Exception) else {'return_value': verdict}
            with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': temp}), \
                    patch('reveal_backend.worker.collect', return_value=(source, {})), \
                    patch('reveal_backend.worker.fit_input_budget', return_value=(source, {}, {})), \
                    patch('reveal_backend.worker.validate_execution_ledger'), \
                    patch('reveal_backend.worker.assemble_account', return_value=({'claims': []}, {'errors': [], 'warnings': []})), \
                    patch('reveal_backend.scientific_grounding.review_account', **review_patch):
                asyncio.run(Worker(repo, adapter).process(*jobs.claim(repo, 'review-test')))
            with repo.transaction() as tx:
                result = tx.get('job', job['id'])['data']
                self.assertEqual(tx.list('account', owner), [])
                self.assertIsNotNone(tx.get('request', request_id))
                self.assertFalse(any(row['data']['stage']=='persisting' for row in tx.list('event',owner)),
                    'An unavailable or rejecting review must not announce saving')
            directory = root / job['id'] / 'attempt-1'
            self.assertTrue((directory / 'validation-1.json').exists(), 'Successful deterministic lint must survive an unavailable reviewer')
            return result, json.loads((directory / 'failure.json').read_text())

    def test_budget_or_transport_failure_is_operational_and_preserves_audit(self):
        result, diagnostic = self.run_review(ScientificReviewUnavailable('Review remaining budget is insufficient', {'reads': [{'pointer': '/package/coverage'}], 'actual_cost_usd': .04}))
        self.assertEqual(result['failure']['code'], 'REVIEW_UNAVAILABLE')
        self.assertIn('no scientific verdict', result['failure']['message'])
        self.assertNotIn('failed scientific validation', result['failure']['message'])
        self.assertEqual(diagnostic['review_audit']['actual_cost_usd'], .04)
        self.assertEqual(diagnostic['phase'], 'scientific_validation')

    def test_explicit_unsupported_verdict_remains_scientific_rejection(self):
        result, diagnostic = self.run_review({'accepted': False, 'review': {'finding': 'Unsupported causal direction'}})
        self.assertEqual(result['failure']['code'], 'VALIDATION_FAILED')
        self.assertNotIn('review_audit', diagnostic)
