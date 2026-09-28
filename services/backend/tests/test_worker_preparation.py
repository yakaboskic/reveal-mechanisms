"""Offline preparation query, transaction, recovery and cancellation boundaries."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.worker import Worker, persist_dispatch_input, read_preparation_inputs


class RecordingRepository(Repository):
    def __init__(self, path):
        super().__init__(path)
        self.operations = []

    @contextmanager
    def record(self, context, kind):
        statements = []
        self.operations.append((kind, statements))
        with context as tx:
            execute = tx.execute

            def traced(sql, params=()):
                statements.append((sql, params))
                return execute(sql, params)

            tx.execute = traced
            yield tx

    def transaction(self):
        return self.record(super().transaction(), 'write')

    def read_transaction(self, **kwargs):
        return self.record(super().read_transaction(**kwargs), 'read')


class WorkerPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = RecordingRepository(str(self.root / 'app.sqlite'))
        self.repository.migrate()
        self.owner, self.request_id = uid(), uid()
        self.frozen = {'composer': {'selected_kgs': []}, 'exact_source': 'unchanged'}
        self.binding = {'source_revision': 'exact-pinned-revision'}
        with self.repository.transaction() as tx:
            tx.put('request', self.request_id, self.owner, self.frozen)
            tx.put('request_binding', self.request_id, self.owner, self.binding)
            jobs.enqueue(tx, self.owner, 'analysis', request_id=self.request_id)
        self.job, self.queue = jobs.claim(self.repository, 'preparation-test')
        self.path = self.root / self.job['id'] / 'evidence/package.json'
        self.path.parent.mkdir(parents=True)
        self.package = {'exact': {'metric': 0.25, 'ids': ['source:one']}}
        self.path.write_bytes(canonical_json(self.package))
        self.snapshot = {'path': 'evidence/package.json', 'sha256': sha256(self.path.read_bytes()),
                         'kind': 'analysis', 'mode': 'box', 'model': 'pinned-model'}
        self.repository.operations.clear()

    def test_frozen_request_and_binding_share_one_read_only_query(self):
        self.assertEqual(read_preparation_inputs(self.repository, self.job), (self.frozen, self.binding))
        self.assertEqual(len(self.repository.operations), 1)
        kind, statements = self.repository.operations[0]
        self.assertEqual(kind, 'read')
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0][1], ('request', self.request_id, 'request_binding', self.request_id))

    def test_paragraph_input_uses_current_owner_from_the_same_read_only_snapshot(self):
        account_id = 'dapper:ScientificAccount.' + 'a' * 32
        current_owner = uid()
        stored = {'result': {'document': {'exact': 'accepted'}, 'citation_metadata': []}, 'summary': {}}
        with self.repository.transaction() as tx:
            tx.put('account', digest([current_owner, account_id]), current_owner, stored)
            job = jobs.enqueue(tx, current_owner, 'paragraph', account_id=account_id)
        job['owner_user_id'] = 'stale-owner-before-transfer'
        self.repository.operations.clear()
        inputs = read_preparation_inputs(self.repository, job)
        self.assertEqual(inputs['result']['document'], stored['result']['document'])
        self.assertEqual(inputs['result']['research_statement']['job_id'], job['id'])
        self.assertEqual(len(self.repository.operations), 1)
        self.assertEqual(self.repository.operations[0][0], 'read')
        self.assertEqual(len(self.repository.operations[0][1]), 2)

    def test_slow_input_and_checkpoint_storage_do_not_block_async_progress(self):
        ticks = []
        progress = []

        def delayed(function):
            def invoke(*args):
                before = len(ticks)
                time.sleep(0.035)
                result = function(*args)
                progress.append(len(ticks) - before)
                return result
            return invoke

        class Adapter:
            async def execute(inner, request, *args):
                return ExecutionResult('insufficient_evidence', request.output_dir, reason='Offline test')

        async def exercise():
            async def watch():
                while True:
                    ticks.append(True)
                    await asyncio.sleep(0.001)
            watcher = asyncio.create_task(watch())
            try:
                await Worker(self.repository, Adapter()).process(self.job, self.queue)
            finally:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

        with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': str(self.root)}), \
                patch('reveal_backend.worker.collect', return_value=(self.path, self.package)), \
                patch('reveal_backend.worker.fit_input_budget', return_value=(self.path, self.package, {})), \
                patch('reveal_backend.worker.read_preparation_inputs', side_effect=delayed(read_preparation_inputs)), \
                patch('reveal_backend.worker.persist_dispatch_input', side_effect=delayed(persist_dispatch_input)):
            asyncio.run(exercise())
        self.assertEqual(len(progress), 2)
        self.assertTrue(all(count > 0 for count in progress), 'Lease and cancellation tasks must remain schedulable during storage waits')

    def test_evidence_and_dispatch_commit_in_one_fenced_transaction(self):
        self.assertTrue(persist_dispatch_input(self.repository, self.job['id'], self.queue['token'], self.snapshot, self.package))
        self.assertEqual(len(self.repository.operations), 1)
        kind, statements = self.repository.operations[0]
        self.assertEqual(kind, 'write')
        self.assertEqual([sql.split()[0] for sql, _ in statements], ['SELECT', 'SELECT', 'SELECT', 'INSERT', 'UPDATE'])
        with self.repository.read_transaction() as tx:
            evidence = tx.get('evidence', self.job['id'])['data']
            dispatch = tx.get('queue', self.job['id'])['data']['dispatch_input']
        self.assertEqual(evidence['package'], self.package)
        self.assertEqual(evidence['package_sha256'], dispatch['sha256'])
        self.assertEqual(dispatch, self.snapshot)

    def test_checkpoint_failure_rolls_back_evidence_as_well(self):
        with patch('reveal_backend.repository.Transaction.update_existing', side_effect=OSError('Injected checkpoint failure')):
            with self.assertRaisesRegex(OSError, 'checkpoint failure'):
                persist_dispatch_input(self.repository, self.job['id'], self.queue['token'], self.snapshot, self.package)
        with self.repository.read_transaction() as tx:
            self.assertIsNone(tx.get('evidence', self.job['id']))
            self.assertNotIn('dispatch_input', tx.get('queue', self.job['id'])['data'])

    def test_stale_token_or_expired_lease_cannot_publish_evidence_or_dispatch(self):
        self.assertFalse(persist_dispatch_input(self.repository, self.job['id'], 'stale', self.snapshot, self.package))
        with self.repository.transaction() as tx:
            queue = tx.get('queue', self.job['id'])['data']
            queue['lease_until'] = '2000-01-01T00:00:00Z'
            tx.put('queue', self.job['id'], self.owner, queue)
        self.assertFalse(persist_dispatch_input(self.repository, self.job['id'], self.queue['token'], self.snapshot, self.package))
        with self.repository.read_transaction() as tx:
            self.assertIsNone(tx.get('evidence', self.job['id']))
            self.assertNotIn('dispatch_input', tx.get('queue', self.job['id'])['data'])

    def test_recovery_reuses_exact_committed_input_and_cancellation_reads_use_snapshots(self):
        persist_dispatch_input(self.repository, self.job['id'], self.queue['token'], self.snapshot, self.package)
        handle = {'box_id': 'existing-offline-fixture', 'cursor': 7, 'phase': 'running'}
        with self.repository.transaction() as tx:
            queue = tx.get('queue', self.job['id'])['data']
            queue['remote_handle'] = handle
            tx.put('queue', self.job['id'], self.owner, queue)
        captured = []

        class Adapter:
            async def execute(inner, request, emit, cancelled, checkpoint):
                captured.append(request)
                self.assertFalse(await cancelled())
                return ExecutionResult('insufficient_evidence', request.output_dir, reason='Offline test')

        self.repository.operations.clear()
        with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': str(self.root)}), \
                patch('reveal_backend.worker.collect', side_effect=AssertionError('Recovery must not collect')), \
                patch('reveal_backend.worker.fit_input_budget', side_effect=AssertionError('Recovery must not prepare again')):
            asyncio.run(Worker(self.repository, Adapter()).process(deepcopy(self.job), queue))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].input_path, self.path.resolve())
        self.assertEqual(captured[0].input_path.read_bytes(), canonical_json(self.package))
        self.assertEqual(captured[0].remote_handle, handle)
        cancellation_reads = [statements for kind, statements in self.repository.operations
                              if kind == 'read' and [params for _, params in statements] == [('job', self.job['id']), ('queue', self.job['id'])]]
        self.assertGreaterEqual(len(cancellation_reads), 3)
        self.assertFalse(any(kind == 'write' and all(sql.startswith('SELECT') for sql, _ in statements)
                             for kind, statements in self.repository.operations))
        with self.repository.read_transaction() as tx:
            self.assertEqual(tx.get('evidence', self.job['id'])['data']['package'], self.package)
            self.assertEqual(tx.get('queue', self.job['id'])['data']['dispatch_input'], self.snapshot)

    def test_cancel_between_collection_and_checkpoint_never_launches_adapter(self):
        called = []

        def collected(*args):
            with self.repository.transaction() as tx:
                jobs.cancel(tx, tx.get('job', self.job['id'])['data'])
            return self.path, self.package

        class Adapter:
            async def execute(inner, *args):
                called.append(True)
                raise AssertionError('Cancelled work must not launch')

        with patch.dict('os.environ', {'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': str(self.root)}), \
                patch('reveal_backend.worker.collect', side_effect=collected), \
                patch('reveal_backend.worker.fit_input_budget', return_value=(self.path, self.package, {})):
            asyncio.run(Worker(self.repository, Adapter()).process(self.job, self.queue))
        self.assertEqual(called, [])
        with self.repository.read_transaction() as tx:
            self.assertEqual(tx.get('job', self.job['id'])['data']['status'], 'cancelled')
            self.assertIsNone(tx.get('queue', self.job['id'])['data']['remote_handle'])


if __name__ == '__main__':
    unittest.main()
