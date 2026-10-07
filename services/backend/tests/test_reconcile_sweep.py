"""Managed reconciliation probes outside the write fence and never waits on it."""
from unittest.mock import patch
import unittest

from reveal_backend import workflow_routes
from reveal_backend.repository import DatabaseBusy, FenceBusy
from reveal_backend.workflow_routes import mount_workflow, reconcile_stale, sweep
import test_durable_workflow as durable

PAST = '2000-01-01T00:00:00Z'


class SweepTests(unittest.TestCase):
    setUp = durable.WorkflowTests.setUp
    new = durable.WorkflowTests.new
    execution = durable.WorkflowTests.execution

    def change(self, kind, identity, **values):
        with self.repo.transaction() as tx:
            row = tx.get(kind, identity); row['data'].update(values); tx.put(kind, identity, row['owner'], row['data'])

    def stale(self):
        job, payload = self.new()
        with self.repo.transaction() as tx: tx.remove('workflow_dispatch', job['id'])
        self.change('execution', job['id'], lease_until=PAST, expected_at=PAST)
        return job, payload

    def expired_editor(self, owner='owner'):
        with self.repo.transaction() as tx:
            tx.put('draft', 'editor', owner, {'id': 'editor', 'lifecycle': 'temporary', 'expires_at': PAST, 'composer': {}})

    def test_nothing_due_never_takes_the_fence_even_with_work_in_progress(self):
        queued, _ = self.new()                     # dispatch still pending: delivery owns it
        leased, _ = self.new()
        with self.repo.transaction() as tx: tx.remove('workflow_dispatch', leased['id'])
        self.change('execution', leased['id'], lease_until='2999-01-01T00:00:00Z', expected_at=PAST)
        waiting, _ = self.new()
        with self.repo.transaction() as tx: tx.remove('workflow_dispatch', waiting['id'])
        self.change('execution', waiting['id'], expected_at='2999-01-01T00:00:00Z')
        with self.repo.transaction() as tx:
            tx.put('draft', 'saved', 'owner', {'id': 'saved', 'composer': {}})   # legacy: no lifecycle, no expiry
            tx.put('upload', 'live', 'owner', {'id': 'live', 'expires_at': '2999-01-01T00:00:00Z'})
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('took the write fence')):
            self.assertEqual(sweep(self.repo), (0, False))
        self.change('execution', queued['id'], expected_at=PAST)
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('took the write fence')):
            self.assertEqual(sweep(self.repo), (0, False))

    def test_stale_executions_and_expired_editors_are_decided_under_the_fence(self):
        job, payload = self.stale(); self.expired_editor()
        self.assertEqual(sweep(self.repo), (1, False))
        self.assertEqual(self.execution({**payload, 'generation': 2})['generation'], 2)
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('draft', 'editor'))
            self.assertIsNotNone(tx.get('workflow_dispatch', job['id']))
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('nothing left to fence')):
            self.assertEqual(sweep(self.repo), (0, False))   # the new dispatch excludes it from the probe

    def test_a_held_fence_defers_to_the_next_tick_without_waiting(self):
        job, _ = self.stale(); self.expired_editor()
        with patch.object(self.repo, 'transaction', side_effect=FenceBusy('held')) as fenced:
            self.assertEqual(sweep(self.repo), (0, True))
        self.assertTrue(fenced.call_args_list and all(call.kwargs == {'nowait': True} for call in fenced.call_args_list))
        self.assertEqual(self.execution({'job_id': job['id']})['generation'], 1)
        with self.repo.read_transaction() as tx: self.assertIsNotNone(tx.get('draft', 'editor'))
        self.assertEqual(reconcile_stale(self.repo), 1)
        with self.repo.read_transaction() as tx: self.assertIsNone(tx.get('draft', 'editor'))

    def test_candidates_renewed_after_the_probe_are_kept(self):
        job, _ = self.stale(); self.expired_editor()
        original = self.repo.transaction
        def renew_first(*args, **kwargs):
            # Between the snapshot and the fence: the editor is saved and the execution takes a fresh lease.
            with original() as tx:
                draft = tx.get('draft', 'editor'); draft['data'].update(lifecycle='saved', expires_at=None)
                tx.put('draft', 'editor', 'owner', draft['data'])
                execution = tx.get('execution', job['id']); execution['data']['lease_until'] = '2999-01-01T00:00:00Z'
                tx.put('execution', job['id'], execution['owner'], execution['data'])
            self.repo.transaction = original
            return original(*args, **kwargs)
        self.repo.transaction = renew_first
        self.assertEqual(sweep(self.repo), (0, False))
        with self.repo.read_transaction() as tx: self.assertIsNotNone(tx.get('draft', 'editor'))
        self.assertEqual(self.execution({'job_id': job['id']})['generation'], 1)


class ReconcileRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        durable.WorkflowTests.setUp(self)
        from fastapi import FastAPI
        self.app = FastAPI(); mount_workflow(self.app, self.repo)
        verify = patch('qstash.Receiver.verify', return_value=None); verify.start(); self.addCleanup(verify.stop)

    async def post(self):
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url='http://test') as web:
            return await web.post('/internal/workflows/reconcile-v1', json={}, headers={'Upstash-Signature': 'signed'})

    async def test_busy_database_answers_deferred_not_503(self):
        response = await self.post()
        self.assertEqual((response.status_code, response.json()['status']), (200, 'ok'), response.text)
        with patch.object(workflow_routes, 'sweep', side_effect=DatabaseBusy('pool exhausted')):
            response = await self.post()
        self.assertEqual((response.status_code, response.json()), (200, {'status': 'deferred'}))
        SweepTests.stale(self)
        with patch.object(self.repo, 'transaction', side_effect=FenceBusy('held')):
            response = await self.post()
        self.assertEqual((response.status_code, response.json()['status'], response.json()['recovered']), (200, 'deferred', 0))
    new = durable.WorkflowTests.new
    change = SweepTests.change


if __name__ == '__main__': unittest.main()
