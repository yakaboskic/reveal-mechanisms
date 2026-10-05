"""Deterministic acceptance gates remain mandatory without a second AI reviewer."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.scientific_account_lint import AccountValidationError
from reveal_backend.worker import Worker


class WorkerValidationTests(unittest.TestCase):
    def run_account(self, *, lint_error=None, ledger_error=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = Repository(str(root/'app.db')); repo.migrate()
            owner, request_id = uid(), uid()
            with repo.transaction() as tx:
                tx.put('request', request_id, owner, {'composer': {'selected_kgs': []}, 'attribution': {'user_id': owner}})
                tx.put('request_binding', request_id, owner, {})
                job = jobs.enqueue(tx, owner, 'analysis', request_id=request_id)
            source = root/job['id']/'evidence/package.json'; source.parent.mkdir(parents=True); source.write_text('{}')
            output = root/job['id']/'attempt-1/output'; output.mkdir(parents=True)
            raw = output/'account-1.json'; raw.write_text('{}')
            adapter = type('Adapter', (), {})()
            adapter.execute = AsyncMock(return_value=ExecutionResult('succeeded', output,
                account_paths=(raw,), ledger_manifest_path=output/'ledger.json'))
            worker = Worker(repo, adapter)
            async def accept(current, token, *args):
                self.assertTrue(jobs.finish(repo, current['id'], token, 'succeeded'))
            worker.accept_accounts = AsyncMock(side_effect=accept)
            report = {'valid': True, 'findings': []}
            with (patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': temp,
                                           'REVEAL_GROUNDING_MAX_BUDGET_USD': 'invalid-unused-setting'}),
                  patch('reveal_backend.worker.collect', return_value=(source, {})),
                  patch('reveal_backend.worker.fit_input_budget', return_value=(source, {}, {})),
                  patch('reveal_backend.worker.validate_execution_ledger', side_effect=ledger_error) as ledger,
                  patch('reveal_backend.worker.assemble_account', side_effect=lint_error,
                        return_value=({'claims': []}, report)) as assemble,
                  patch('reveal_backend.scientific_grounding.review_account', side_effect=AssertionError('No second model')) as review):
                asyncio.run(worker.process(*jobs.claim(repo, 'validation-test')))
            ledger.assert_called_once(); review.assert_not_called()
            with repo.read_transaction() as tx:
                result = tx.get('job', job['id'])['data']
                events = [row['data'] for row in tx.list('event', owner)]
                self.assertIsNotNone(tx.get('request', request_id))
            directory = root/job['id']/'attempt-1'
            self.assertFalse((directory/'grounding-1.json').exists())
            if ledger_error:
                assemble.assert_not_called()
                self.assertFalse((directory/'validation-1.json').exists())
            else:
                assemble.assert_called_once()
                saved = json.loads((directory/'validation-1.json').read_text())
                self.assertEqual(saved, lint_error.report if lint_error else report)
            if lint_error or ledger_error:
                worker.accept_accounts.assert_not_awaited()
                self.assertFalse(any(event['stage'] == 'persisting' for event in events))
                return result, json.loads((directory/'failure.json').read_text())
            worker.accept_accounts.assert_awaited_once()
            self.assertTrue(any(event['stage'] == 'persisting' for event in events))
            self.assertFalse((directory/'failure.json').exists())
            return result, None

    def test_lint_valid_account_advances_directly_to_persistence(self):
        result, _ = self.run_account()
        self.assertEqual(result['status'], 'succeeded')
        self.assertIsNone(result.get('failure'))

    def test_failed_source_lint_report_is_retained_and_never_saved(self):
        report = {'valid': False, 'findings': [{'severity': 'error', 'check': 'evidence-snippet',
            'where': 'evidence', 'message': 'Not a verbatim excerpt'}]}
        result, diagnostic = self.run_account(lint_error=AccountValidationError(report))
        self.assertEqual(result['failure']['code'], 'VALIDATION_FAILED')
        self.assertEqual(diagnostic['error_type'], 'AccountValidationError')

    def test_runtime_and_tool_ledger_gate_remains_before_assembly(self):
        result, diagnostic = self.run_account(ledger_error=EvidenceBuildError('Tool ledger artifact checksum mismatch'))
        self.assertEqual(result['failure']['code'], 'VALIDATION_FAILED')
        self.assertEqual(diagnostic['message'], 'Tool ledger artifact checksum mismatch')

    def test_invalid_paragraph_citations_are_rejected_without_an_ai_reviewer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = Repository(str(root/'app.db')); repo.migrate()
            owner, account_id, claim_id = uid(), 'account:accepted', 'claim:accepted'
            document = {'scientific_accounts': [{'id': account_id, 'component_claims': [claim_id]}]}
            with repo.transaction() as tx:
                tx.put('account', digest([owner, account_id]), owner,
                    {'result': {'document': document, 'citation_metadata': [{'target_id': claim_id, 'metadata_revision': 1}]}, 'summary': {}})
                job = jobs.enqueue(tx, owner, 'paragraph', account_id=account_id)
            output = root/job['id']/'attempt-1/output'; output.mkdir(parents=True)
            raw = output/'paragraph.json'
            raw.write_text(json.dumps({'format': 'reveal.paragraph-output/1', 'segments': [
                {'text': 'Observation.', 'citations': [{'target_id': claim_id, 'citation_metadata_revision': 2}]}]}))
            adapter = type('Adapter', (), {})()
            adapter.execute = AsyncMock(return_value=ExecutionResult('succeeded', output, paragraph_path=raw))
            worker = Worker(repo, adapter)
            with (patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': temp}),
                  patch('reveal_backend.worker.validate_execution_ledger') as ledger,
                  patch('reveal_backend.dapper_release.verify_release'),
                  patch('reveal_backend.worker.validate_paragraph_document') as final_lint,
                  patch('reveal_backend.scientific_grounding.review_paragraph', side_effect=AssertionError('No second model')) as review):
                asyncio.run(worker.process(*jobs.claim(repo, 'paragraph-validation')))
            ledger.assert_called_once(); final_lint.assert_not_called(); review.assert_not_called()
            with repo.read_transaction() as tx:
                result = tx.get('job', job['id'])['data']
                self.assertEqual(result['failure']['code'], 'VALIDATION_FAILED')
                self.assertEqual(tx.list('paragraph', owner), [])
                self.assertFalse(any(row['data']['stage'] == 'persisting' for row in tx.list('event', owner)))
            diagnostic = json.loads((output.parent/'failure.json').read_text())
            self.assertIn('Unavailable or duplicate citation target/revision', diagnostic['message'])
