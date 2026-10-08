"""Recovery fences remote work before handing its reservation to cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reveal_backend import jobs, workflow_state as state
from reveal_backend.repository import Repository, Transaction
from reveal_backend.workflow_execution import WorkflowExecution
from reveal_backend.workflow_routes import reconcile_stale


class WorkflowCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name)/'db.sqlite'); self.repo.migrate()
        env = patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'capacity-test',
            'REVEAL_MAX_ACTIVE_BOXES': '1'})
        env.start(); self.addCleanup(env.stop)

    def seed(self, *, allocated=True, cancelled=False, unknown=False):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner', 'paragraph', account_id='account')
            value = tx.get('execution', job['id'])['data']
            value.update(phase='observe' if allocated else 'create', phase_index=23, recoveries=3,
                expected_at='', disposition='retry', retry_cause='step_failure',
                capacity_reserved=allocated, creation_intent='allocation' if allocated else None,
                workspace={'sha256': 'preserved'}, deadline=123, launch_intent=allocated,
                box={'box_id': 'box-'+job['id'], 'job_id': job['id'], 'attempt': 1,
                     'phase': 'running', 'cursor': 73} if allocated and not unknown else None)
            tx.put('execution', job['id'], 'owner', value)
            tx.remove('workflow_dispatch', job['id'])
            if cancelled: jobs.cancel(tx, job)
        return job, {'job_id': job['id'], 'namespace': 'capacity-test', 'generation': 1}

    def read(self, kind, identity):
        with self.repo.read_transaction() as tx: return tx.get(kind, identity)['data']

    async def test_exhaustion_frees_slot_after_handoff_and_deletes_once(self):
        job, payload = self.seed()
        self.assertEqual(reconcile_stale(self.repo), 0)
        execution = self.read('execution', job['id'])
        self.assertEqual(execution['disposition'], 'recovery_required')
        self.assertEqual(execution['generation'], 2)
        self.assertFalse(execution['capacity_reserved']); self.assertFalse(execution['capture_complete'])
        self.assertEqual((execution['phase_index'], execution['box']['cursor'], execution['deadline']), (23, 73, 123))
        self.assertEqual(execution['workspace'], {'sha256': 'preserved'})
        outcome = self.read('job', job['id'])
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(outcome['failure']['code'], 'WORKFLOW_RECOVERY_EXHAUSTED')
        with self.assertRaises(state.StaleExecution): state.acquire(self.repo, payload, 23)
        other, next_payload = self.seed(allocated=False)
        _, active, _ = state.acquire(self.repo, next_payload, 23)
        self.assertTrue(state.reserve_box(self.repo, next_payload, active['fence'])['capacity_reserved'])
        adapter = Mock(); order = []
        async def cancel(box): order.append('cancel')
        async def delete(box): order.append('delete'); return {**box, 'phase': 'deleted'}
        adapter.cancel_once = AsyncMock(side_effect=cancel); adapter.delete_once = AsyncMock(side_effect=delete)
        engine = WorkflowExecution(self.repo, adapter=adapter)
        cleanup = {'cleanup_id': execution['cleanup_id'], 'namespace': payload['namespace']}
        await engine.cleanup(cleanup); await engine.cleanup(cleanup)
        self.assertEqual(order, ['cancel', 'delete'])
        self.assertTrue(self.read('execution', job['id'])['cleanup_complete'])
        self.assertEqual(self.read('job', job['id'])['failure'], outcome['failure'])

    async def test_abandoned_cleanup_recovers_lost_delete_ack_when_cancel_finds_no_box(self):
        from upstash_box.errors import BoxError
        job, payload = self.seed(); reconcile_stale(self.repo)
        execution = self.read('execution', job['id'])
        cleanup = {'cleanup_id': execution['cleanup_id'], 'namespace': payload['namespace']}
        adapter = Mock()
        adapter.cancel_once = AsyncMock(side_effect=BoxError('Box not found', status_code=404))
        adapter.delete_once = AsyncMock(return_value={**execution['box'], 'phase': 'deleted'})
        await WorkflowExecution(self.repo, adapter=adapter).cleanup(cleanup)
        self.assertTrue(self.read('execution', job['id'])['cleanup_complete'])
        adapter.delete_once.assert_awaited_once()

    async def test_dead_cancel_requested_workflow_reports_cancelled_and_hands_off(self):
        job, payload = self.seed(cancelled=True)
        reconcile_stale(self.repo)
        execution = self.read('execution', job['id']); outcome = self.read('job', job['id'])
        self.assertEqual(outcome['status'], 'cancelled')
        self.assertEqual(outcome['failure']['code'], 'WORKFLOW_CANCELLED')
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx: self.assertTrue(state.has_cleanup_handoff(tx, execution))

    async def test_unknown_allocation_stays_reserved_until_operator_identifies_box(self):
        job, payload = self.seed(unknown=True)
        reconcile_stale(self.repo)
        execution = self.read('execution', job['id'])
        self.assertTrue(execution['capacity_reserved']); self.assertNotIn('cleanup_id', execution)
        other, next_payload = self.seed(allocated=False)
        _, active, _ = state.acquire(self.repo, next_payload, 23)
        with self.assertRaises(state.StepBusy): state.reserve_box(self.repo, next_payload, active['fence'])

    async def test_live_phase_blocks_recovery_until_its_fence_releases(self):
        job, payload = self.seed()
        _, active, _ = state.acquire(self.repo, payload, 23)
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']
            with self.assertRaises(state.StepBusy): state.require_recovery(tx, 'owner', execution, 'test')
        self.assertEqual(self.read('execution', job['id'])['generation'], 1)
        state.release(self.repo, payload, active['fence'], recovery=True, reason='phase failed')
        execution = self.read('execution', job['id'])
        self.assertEqual(execution['generation'], 2); self.assertFalse(execution['capacity_reserved'])
        with self.assertRaises(state.StaleExecution): state.save(self.repo, payload, active['fence'], box={})

    async def test_cleanup_creation_failure_rolls_back_terminal_outcome_and_capacity(self):
        job, payload = self.seed(); original = Transaction.put
        def put(tx, kind, *args, **kwargs):
            if kind == 'workflow_cleanup': raise OSError('write failed')
            return original(tx, kind, *args, **kwargs)
        with patch.object(Transaction, 'put', put), self.assertRaises(OSError): reconcile_stale(self.repo)
        self.assertTrue(self.read('execution', job['id'])['capacity_reserved'])
        self.assertEqual(self.read('execution', job['id'])['generation'], 1)
        self.assertEqual(self.read('job', job['id'])['status'], 'queued')

    async def test_sql_count_handles_released_captured_abandoned_and_legacy_reservations(self):
        job, _ = self.seed()
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']
            self.assertEqual(tx.active_box_count('capacity-test'), 1)
            execution.update(capture_complete=True, capture_sha256='a'*64)
            identity = state.enqueue_cleanup(tx, 'owner', execution, execution['workspace'], execution['box'], 'a'*64)
            execution['cleanup_id'] = identity
            tx.put('execution', job['id'], 'owner', execution)
            # Old deployments can leave the reservation set after a valid handoff.
            self.assertEqual(tx.active_box_count('capacity-test'), 0)
            cleanup = tx.get('workflow_cleanup', identity)['data']
            cleanup.update(abandoned=True, recovery_generation=99)
            execution.update(cleanup_abandoned=True, recovery_generation=1)
            tx.put('workflow_cleanup', identity, 'owner', cleanup)
            tx.put('execution', job['id'], 'owner', execution)
            # An abandoned intent must satisfy its recovery binding even when
            # it also carries an otherwise valid captured workspace/checksum.
            self.assertFalse(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.active_box_count('capacity-test'), 1)
            cleanup.pop('abandoned'); cleanup.pop('recovery_generation')
            tx.put('workflow_cleanup', identity, 'owner', cleanup)
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.active_box_count('capacity-test'), 0)
            invalid = deepcopy(execution); invalid['capture_sha256'] = 'b'*64
            tx.put('execution', job['id'], 'owner', invalid)
            self.assertEqual(tx.active_box_count('capacity-test'), 1)
            invalid['capacity_reserved'] = False
            tx.put('execution', job['id'], 'owner', invalid)
            self.assertEqual(tx.active_box_count('capacity-test'), 0)
            tx.put('queue', 'legacy', 'owner', {'namespace': 'capacity-test', 'transport': 'redis',
                'remote_handle': {'box_id': 'legacy', 'phase': 'running'}})
            self.assertEqual(tx.active_box_count('capacity-test'), 1)
            self.assertEqual(tx.active_box_count('other'), 0)

    async def test_simultaneous_claims_cannot_overbook_the_last_slot(self):
        candidates = []
        for _ in range(8):
            _, payload = self.seed(allocated=False)
            _, active, _ = state.acquire(self.repo, payload, 23)
            candidates.append((payload, active['fence']))
        def reserve(candidate):
            try: state.reserve_box(self.repo, *candidate); return True
            except state.StepBusy: return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, candidates)), 1)
        with self.repo.read_transaction() as tx: self.assertEqual(tx.active_box_count('capacity-test'), 1)
