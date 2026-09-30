"""Agent completion advances activity before durable capture and validation."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from reveal_backend import jobs
from reveal_backend.agent_execution import ExecutionResult
from reveal_backend.repository import Repository, digest, uid
from reveal_backend.worker import Worker, persist_activity_batch, public_activity


class WorkerCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict('os.environ', {
            'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_ARTIFACTS_DIR': temporary.name,
            'REVEAL_QUEUE_NAMESPACE': 'collection-test',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = Repository(str(self.root / 'application.sqlite'))
        self.repo.migrate()
        self.owner = uid()
        account = 'dapper:ScientificAccount.' + 'a' * 32
        with self.repo.transaction() as tx:
            tx.put('account', digest([self.owner, account]), self.owner, {
                'result': {'document': {}, 'citation_metadata': []}, 'summary': {},
            })
            jobs.enqueue(tx, self.owner, 'paragraph', account_id=account)
        self.job, self.queue = jobs.claim(self.repo, 'collection-worker')

    def events(self):
        with self.repo.read_transaction() as tx:
            return sorted((row['data'] for row in tx.list('event', self.owner)), key=lambda event: int(event['id']))

    def saved(self, kind):
        with self.repo.read_transaction() as tx:
            return tx.get(kind, self.job['id'])['data']

    def test_agent_completion_starts_collection_without_claiming_job_success(self):
        for kind, stage in [('analysis', 'authoring_account'), ('paragraph', 'authoring_paragraph')]:
            for status in ('running', 'cancel_requested'):
                with self.subTest(kind=kind, status=status):
                    job = {'kind': kind, 'stage': stage, 'status': status, 'warnings': []}
                    mapped = public_activity(job, 'agent_completed', {
                        'status': 'succeeded', 'message': 'Claude finished its work.',
                    })
                    self.assertEqual(job['stage'], 'collecting_output')
                    self.assertEqual(job['status'], status)
                    self.assertEqual(mapped[1], 'Claude finished its work.')
                    self.assertEqual(mapped[2]['state'], 'started')

    def test_completion_replay_cannot_duplicate_handoff_or_regress_validation(self):
        completion = ('agent_completed', {
            'remote_stream_id': 'box:collection:1', 'remote_sequence': 2,
            'status': 'succeeded', 'message': 'Claude finished its work.',
        })
        prose = ('agent_message', {
            'remote_stream_id': 'box:collection:1', 'remote_sequence': 1,
            'text': 'The causal question remains open.', 'delta': True,
        })
        persist_activity_batch(self.repo, self.job['id'], self.queue['token'], [prose, completion])
        self.assertEqual(self.saved('job')['stage'], 'collecting_output')
        persist_activity_batch(self.repo, self.job['id'], self.queue['token'], [
            ('stage', {'stage': 'validating', 'message': 'Checking identities and source fidelity.'}),
        ])
        last_event_id = self.saved('job')['last_event_id']
        persist_activity_batch(self.repo, self.job['id'], self.queue['token'], [completion, prose, completion])
        saved = self.saved('job')
        self.assertEqual(saved['last_event_id'], last_event_id)
        self.assertEqual(saved['stage'], 'validating')
        self.assertEqual(saved['status'], 'running')
        rows = self.events()
        self.assertEqual(sum(row['message'] == completion[1]['message'] for row in rows), 1)
        self.assertEqual(sum(row['message'] == prose[1]['text'] for row in rows), 1)
        handoff = next(row for row in rows if row['message'] == completion[1]['message'])
        self.assertEqual(handoff['stage'], 'collecting_output')
        self.assertEqual(handoff['detail']['state'], 'started')

    def exercise_capture(self, checkpoint_phases, *, mismatch=False):
        snapshots = []

        def snapshot(root):
            # Capture bytes, not references to the disposable worker filesystem.
            files = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
            snapshots.append(files)
            return {'snapshot': len(snapshots)}

        store = SimpleNamespace(snapshot=Mock(side_effect=snapshot))
        checkpoints = []

        class Adapter:
            async def execute(adapter, request, emit, cancelled, checkpoint):
                await emit('agent_completed', {'status': 'succeeded', 'message': 'Claude finished its work.'})
                request.output_dir.mkdir(parents=True, exist_ok=True)
                (request.output_dir / 'captured.json').write_bytes(b'{"captured":true}')
                handle = {'job_id': request.job_id, 'attempt': request.attempt, 'box_id': 'offline-box',
                          'phase': 'deleted', 'cursor': 3}
                for phase in checkpoint_phases:
                    handle['phase'] = phase
                    await checkpoint(deepcopy(handle))
                    checkpoints.append(deepcopy(handle))
                returned = dict(handle, cursor=4) if mismatch else deepcopy(handle)
                # No scientific result needs acceptance to exercise durable
                # capture. All Box/S3 boundaries are deterministic local doubles.
                return ExecutionResult('failed', request.output_dir, remote_handle=returned,
                                       reason='Intentional offline fixture finish')

        with patch('reveal_backend.worker.s3_enabled', return_value=True), \
                patch('reveal_backend.worker.artifact_store', return_value=store):
            worker = Worker(self.repo, Adapter())
            with patch.object(worker, 'save_workspace', wraps=worker.save_workspace) as save:
                asyncio.run(worker.process(self.job, self.queue))
        captured_snapshots = [files for files in snapshots if 'attempt-1/output/captured.json' in files]
        self.assertEqual(self.saved('job')['status'], 'failed')
        self.assertFalse((self.root / self.job['id']).exists(), 'Durable storage permits local scratch cleanup')
        self.assertEqual(len(snapshots), len(captured_snapshots) + 1, 'Only the dispatch snapshot precedes captured output')
        self.assertTrue(all(files['attempt-1/output/captured.json'] == b'{"captured":true}' for files in captured_snapshots))
        self.assertEqual(self.saved('queue')['workspace'], {'snapshot': len(snapshots)})
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.list('paragraph', self.owner), [])
        return len(captured_snapshots), save.call_count, checkpoints

    def test_confirmed_deleted_checkpoint_avoids_third_captured_output_snapshot(self):
        count, saves, checkpoints = self.exercise_capture(['captured', 'deleted'])
        self.assertEqual([handle['phase'] for handle in checkpoints], ['captured', 'deleted'])
        self.assertEqual((count, saves), (2, 2))
        self.assertEqual(self.saved('queue')['remote_handle'], checkpoints[-1])

    def test_unconfirmed_deleted_handle_still_saves_output(self):
        count, saves, checkpoints = self.exercise_capture([])
        self.assertEqual(checkpoints, [])
        self.assertEqual((count, saves), (1, 1))

    def test_captured_checkpoint_without_deleted_confirmation_still_saves_output(self):
        count, saves, _ = self.exercise_capture(['captured'])
        self.assertEqual((count, saves), (2, 2))

    def test_changed_result_handle_cannot_reuse_another_capture_confirmation(self):
        count, saves, _ = self.exercise_capture(['captured', 'deleted'], mismatch=True)
        self.assertEqual((count, saves), (3, 3))

    def test_validation_stage_is_durable_before_ledger_validation_begins(self):
        observed = []

        class Adapter:
            async def execute(adapter, request, emit, cancelled, checkpoint):
                await emit('agent_completed', {'status': 'succeeded', 'message': 'Claude finished its work.'})
                return ExecutionResult('succeeded', request.output_dir)

        def validate_ledger(*args):
            current = self.saved('job')
            observed.append((current['status'], current['stage'], self.events()[-1]['stage']))
            raise ValueError('Intentional offline validation rejection')

        with patch('reveal_backend.worker.s3_enabled', return_value=False), \
                patch('reveal_backend.worker.validate_execution_ledger', side_effect=validate_ledger) as validate:
            asyncio.run(Worker(self.repo, Adapter()).process(self.job, self.queue))
        validate.assert_called_once()
        self.assertEqual(observed, [('running', 'validating', 'validating')])
        self.assertEqual(self.saved('job')['failure']['code'], 'VALIDATION_FAILED')
        self.assertEqual(self.saved('job')['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
