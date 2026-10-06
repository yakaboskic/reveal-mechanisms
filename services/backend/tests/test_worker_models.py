"""Frozen authoring models remain verified without launching another model."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.evidence_package import sha256
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.worker import Worker, persist_dispatch_input


class WorkerModelTests(unittest.TestCase):
    def exercise(self, kind, author_model, *, recovering=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = Repository(str(root / 'app.sqlite'))
            repository.migrate()
            owner, request_id = uid(), uid()
            account_id = 'dapper:ScientificAccount.' + 'a' * 32
            with repository.transaction() as tx:
                if kind == 'analysis':
                    tx.put('request', request_id, owner, {
                        'composer': {'selected_kgs': []}, 'attribution': {'user_id': owner}})
                    tx.put('request_binding', request_id, owner, {})
                    jobs.enqueue(tx, owner, kind, request_id=request_id)
                else:
                    tx.put('account', digest([owner, account_id]), owner, {
                        'result': {'document': {}, 'citation_metadata': []}, 'summary': {}})
                    jobs.enqueue(tx, owner, kind, account_id=account_id)
            job, queue = jobs.claim(repository, 'model-test')
            source = root / job['id'] / 'evidence/package.json'
            source.parent.mkdir(parents=True)
            source.write_text('{}')
            output = root / job['id'] / 'attempt-1/output'
            output.mkdir(parents=True)
            raw = output / 'result.json'
            raw.write_text('{}')
            if recovering:
                snapshot = {'path': 'evidence/package.json', 'sha256': sha256(source.read_bytes()),
                            'kind': kind, 'mode': 'box', 'model': author_model}
                self.assertTrue(persist_dispatch_input(repository, job['id'], queue['token'], snapshot))
                with repository.read_transaction() as tx:
                    queue = tx.get('queue', job['id'])['data']
            result = ExecutionResult('succeeded', output,
                account_paths=(raw,) if kind == 'analysis' else (),
                paragraph_path=raw if kind == 'paragraph' else None,
                ledger_manifest_path=output / 'ledger.json')
            adapter = SimpleNamespace(execute=AsyncMock(return_value=result))
            worker = Worker(repository)
            worker.accept_accounts = AsyncMock()
            worker.accept_paragraph = AsyncMock()
            review_name = 'review_account' if kind == 'analysis' else 'review_paragraph'
            with patch.dict('os.environ', {
                    'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': temp,
                    'REVEAL_CLAUDE_MODEL': 'changed-after-dispatch' if recovering else author_model,
                    'ANTHROPIC_API_KEY': 'test-only'}), \
                    patch('reveal_backend.worker.collect', return_value=(source, {})) as collect, \
                    patch('reveal_backend.worker.fit_input_budget', return_value=(source, {}, {})), \
                    patch('reveal_backend.box_adapter.BoxExecutionAdapter', return_value=adapter) as factory, \
                    patch('reveal_backend.worker.validate_execution_ledger') as validate, \
                    patch('reveal_backend.worker.assemble_account', return_value=({}, {'errors': [], 'warnings': []})), \
                    patch('reveal_backend.scientific_grounding.' + review_name,
                          side_effect=AssertionError('A second model must not run')) as review:
                asyncio.run(worker.process(job, queue))
            self.assertEqual(factory.call_args.kwargs['environ']['REVEAL_CLAUDE_MODEL'], author_model)
            validate.assert_called_once()
            self.assertEqual(validate.call_args.args[2], author_model)
            review.assert_not_called()
            accepted = worker.accept_accounts if kind == 'analysis' else worker.accept_paragraph
            accepted.assert_awaited_once()
            if recovering:
                collect.assert_not_called()
            report = output.parent / ('grounding-1.json' if kind == 'analysis' else 'paragraph-grounding.json')
            self.assertFalse(report.exists())
            with repository.read_transaction() as tx:
                self.assertEqual(tx.get('queue', job['id'])['data']['dispatch_input']['model'], author_model)

    def test_configured_author_models_save_without_an_independent_reviewer(self):
        for kind in ('analysis', 'paragraph'):
            for model in ('claude-opus-5-5', 'claude-fable-5-1'):
                with self.subTest(kind=kind, model=model):
                    self.exercise(kind, model)

    def test_recovery_preserves_author_model_after_environment_change(self):
        for kind in ('analysis', 'paragraph'):
            with self.subTest(kind=kind):
                self.exercise(kind, 'claude-opus-5-5', recovering=True)
