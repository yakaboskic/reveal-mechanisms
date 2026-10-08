"""Managed reconciliation probes outside the write fence and never waits on it; job replies never wait for
Workflow delivery, which a committed intent and reconciliation already guarantee."""
import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch
import unittest

from reveal_backend import app as api, jobs, workflow_routes, workflow_state as state
from reveal_backend.repository import DatabaseBusy, FenceBusy, Transaction, digest
from reveal_backend.workflow_routes import dispatch_job, mount_workflow, reconcile_stale, sweep
from round_trips import count_round_trips
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

    def conflicting(self):
        """An exhausted execution whose Box already has a cleanup intent bound to a different capture."""
        job, payload = self.stale()
        box = {'box_id': 'box-' + job['id'], 'job_id': job['id'], 'attempt': 1, 'phase': 'running', 'cursor': 4}
        self.change('execution', job['id'], recoveries=3, retry_cause='step_failure', disposition='retry',
                    capacity_reserved=True, creation_intent='allocation', box=box)
        identity = digest(['box-cleanup-v1', 'test', job['id'], 1, box['box_id']])
        with self.repo.transaction() as tx:
            tx.put('workflow_cleanup', identity, 'owner', {'cleanup_id': identity, 'namespace': 'test', 'job_id': job['id'],
                'authoring_attempt': 1, 'box_id': box['box_id'], 'box': box, 'workspace': {'sha256': 'other'},
                'capture_sha256': 'b' * 64, 'status': 'pending', 'attempts': 0, 'dispatch_generation': 0,
                'next_attempt_at': '2999-01-01T00:00:00Z', 'lease_until': None, 'token': None})
        return job, identity

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
            self.assertEqual(sweep(self.repo), (0, False, []))
        self.change('execution', queued['id'], expected_at=PAST)
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('took the write fence')):
            self.assertEqual(sweep(self.repo), (0, False, []))

    def test_stale_executions_and_expired_editors_are_decided_under_the_fence(self):
        job, payload = self.stale(); self.expired_editor()
        self.assertEqual(sweep(self.repo), (1, False, []))
        self.assertEqual(self.execution({**payload, 'generation': 2})['generation'], 2)
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('draft', 'editor'))
            self.assertIsNotNone(tx.get('workflow_dispatch', job['id']))
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('nothing left to fence')):
            self.assertEqual(sweep(self.repo), (0, False, []))   # the new dispatch excludes it from the probe

    def test_a_held_fence_defers_to_the_next_tick_without_waiting(self):
        job, _ = self.stale(); self.expired_editor()
        with patch.object(self.repo, 'transaction', side_effect=FenceBusy('held')) as fenced:
            self.assertEqual(sweep(self.repo), (0, True, []))
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
        self.assertEqual(sweep(self.repo), (0, False, []))
        with self.repo.read_transaction() as tx: self.assertIsNotNone(tx.get('draft', 'editor'))
        self.assertEqual(self.execution({'job_id': job['id']})['generation'], 1)

    def test_an_undecidable_row_is_held_for_an_operator_while_the_rest_recover(self):
        held_job, cleanup_id = self.conflicting()
        other, payload = self.stale()
        with self.assertLogs('reveal.workflow', 'ERROR') as logs:
            recovered, deferred, held = sweep(self.repo)
        self.assertEqual((recovered, deferred), (1, False))
        self.assertEqual([(entry['job_id'], entry['error'], entry['detail']) for entry in held],
                         [(held_job['id'], 'StaleExecution', 'Recovery cleanup binding changed')])
        self.assertTrue(any(held_job['id'] in line for line in logs.output), logs.output)
        self.assertEqual(self.execution({**payload, 'generation': 2})['generation'], 2)
        # Nothing was released or abandoned: the reservation, Box, budget, cause, job and cleanup intent are unchanged.
        execution = self.execution({'job_id': held_job['id']})
        self.assertEqual((execution['generation'], execution['disposition'], execution['recoveries'], execution['retry_cause']),
                         (1, 'retry', 3, 'step_failure'))
        self.assertTrue(execution['capacity_reserved']); self.assertNotIn('cleanup_abandoned', execution)
        self.assertEqual((execution['sweep_hold']['error'], execution['expected_at']), ('StaleExecution', held[0]['held_until']))
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', held_job['id'])['data']['status'], 'queued')
            self.assertEqual(tx.get('workflow_cleanup', cleanup_id)['data']['capture_sha256'], 'b' * 64)
            self.assertEqual(tx.active_box_count('test'), 1)   # still counted against Box capacity
        # Held rows leave the probe until their hold ends, so they cannot occupy every tick's page.
        with patch.object(self.repo, 'transaction', side_effect=AssertionError('a held row was decided again')):
            self.assertEqual(sweep(self.repo), (0, False, []))
        # After the hold, once an operator repairs the binding, the sweep decides it normally.
        with self.repo.transaction() as tx: tx.remove('workflow_cleanup', cleanup_id)
        self.change('execution', held_job['id'], expected_at=PAST)
        self.assertEqual(sweep(self.repo), (0, False, []))
        execution = self.execution({'job_id': held_job['id']})
        self.assertEqual(execution['disposition'], 'recovery_required'); self.assertNotIn('sweep_hold', execution)
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx:
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.get('job', held_job['id'])['data']['status'], 'failed')

    def test_a_failed_hold_is_still_reported_and_retried_next_tick(self):
        held_job, _ = self.conflicting()
        with patch.object(workflow_routes, 'hold_stale', side_effect=ValueError('cannot hold')), \
                self.assertLogs('reveal.workflow', 'ERROR'):
            recovered, deferred, held = sweep(self.repo)
        self.assertEqual((recovered, deferred, [entry['job_id'] for entry in held]), (0, False, [held_job['id']]))
        self.assertIsNone(held[0]['held_until'])
        self.assertNotIn('sweep_hold', self.execution({'job_id': held_job['id']}))
        self.assertEqual(sweep(self.repo)[2][0]['job_id'], held_job['id'])   # still a candidate, decided again

    def test_a_database_failure_still_fails_the_whole_tick(self):
        import pymysql
        job, _ = self.stale(); put = Transaction.put
        def lost(tx, kind, *args, **kwargs):
            if kind == 'workflow_dispatch': raise pymysql.err.OperationalError(2013, 'Lost connection to MySQL server during query')
            return put(tx, kind, *args, **kwargs)
        with patch.object(Transaction, 'put', lost), self.assertRaises(pymysql.err.OperationalError): sweep(self.repo)
        execution = self.execution({'job_id': job['id']})
        self.assertEqual(execution['generation'], 1); self.assertNotIn('sweep_hold', execution)


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
    async def test_an_undecidable_row_never_fails_the_tick(self):
        held_job, _ = SweepTests.conflicting(self)
        with self.assertLogs('reveal.workflow', 'ERROR'):
            response = await self.post()
        self.assertEqual((response.status_code, response.json()['status']), (200, 'ok'), response.text)
        self.assertEqual([entry['job_id'] for entry in response.json()['needs_operator']], [held_job['id']])

    new = durable.WorkflowTests.new
    change = SweepTests.change
    stale = SweepTests.stale


class FakeQStash:
    def __init__(self):
        self.http = Mock(); self.http.request = AsyncMock(return_value=[{'messageId': 'run'}])
        self.message = Mock(); self.message.publish_json = AsyncMock(return_value={'messageId': 'control'})


class JobDispatchTests(unittest.IsolatedAsyncioTestCase):
    setUp = durable.WorkflowTests.setUp
    new = durable.WorkflowTests.new

    async def test_a_jobs_intents_are_one_read_and_one_client(self):
        job, _ = self.new()
        with self.repo.transaction() as tx: jobs.cancel(tx, tx.get('job', job['id'])['data'])
        clients = []
        def client(): clients.append(FakeQStash()); return clients[-1]
        with patch.object(workflow_routes, 'client', client), count_round_trips() as budget:
            self.assertEqual(await dispatch_job(self.repo, job['id']), {'delivered': 1, 'failed': 0})
        self.assertEqual(len(clients), 1)
        clients[0].http.request.assert_awaited_once(); clients[0].message.publish_json.assert_awaited_once()
        self.assertEqual(budget.leases[0], ['read', 1], budget)   # dispatch and control in one statement
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('workflow_dispatch', job['id'])); self.assertIsNone(tx.get('workflow_control', job['id']))

    async def test_without_control_the_cancellation_intent_is_left_to_reconciliation(self):
        job, _ = self.new()
        with self.repo.transaction() as tx: jobs.cancel(tx, tx.get('job', job['id'])['data'])
        fake = FakeQStash()
        with patch.object(workflow_routes, 'client', return_value=fake):
            await dispatch_job(self.repo, job['id'], control=False)
        fake.message.publish_json.assert_not_awaited()
        with self.repo.read_transaction() as tx:
            self.assertIsNone(tx.get('workflow_dispatch', job['id'])); self.assertIsNotNone(tx.get('workflow_control', job['id']))
        await workflow_routes.dispatch_pending(self.repo, qstash=fake)
        with self.repo.read_transaction() as tx: self.assertIsNone(tx.get('workflow_control', job['id']))


class JobReplyTests(unittest.IsolatedAsyncioTestCase):
    """The reply is sent before delivery starts; delivery still runs in the request, after the body."""

    async def exchange(self, path, body, transaction, transport='workflow'):
        order, released = [], asyncio.Event()
        async def dispatch(repository, job_id, *, control=True):
            await asyncio.wait_for(released.wait(), 2)
            order.append(('dispatched', job_id, control))
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': path, 'raw_path': path.encode(), 'root_path': '', 'query_string': b'', 'server': ('test', 80),
                 'client': ('test', 1), 'headers': [(b'content-type', b'application/json'), (b'authorization', b'Bearer x'),
                                                    (b'idempotency-key', b'key')]}
        delivered = []
        async def receive():
            if not delivered: delivered.append(1); return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.Event().wait()
        async def send(message):
            if message['type'] == 'http.response.start': order.append(('status', message['status']))
            if message['type'] == 'http.response.body' and not message.get('more_body'): order.append(('responded',)); released.set()
        with patch.object(api, transaction, return_value={'id': 'job-1', 'status': 'queued'}), \
                patch.object(jobs, 'transport', return_value=transport), patch.object(workflow_routes, 'dispatch_job', dispatch):
            await asyncio.wait_for(api.app(scope, receive, send), 5)
        return order

    async def test_job_replies_never_wait_for_workflow_delivery(self):
        cases = (('/v1/jobs', b'{"kind":"analysis","draft_id":"draft","draft_version":1}', 'create_job_transaction', False),
                 ('/v1/jobs/job-1/cancel', b'', 'cancel_job_transaction', True),
                 ('/v1/jobs/job-1/retry-review', b'{"expected_last_event_id":"3"}', 'retry_review_transaction', False))
        for path, body, transaction, control in cases:
            with self.subTest(path=path):
                order = await self.exchange(path, body, transaction)
                self.assertEqual(order[1:], [('responded',), ('dispatched', 'job-1', control)])
                self.assertIn(order[0][1], (200, 202))

    async def test_other_transports_schedule_no_delivery(self):
        order = await self.exchange('/v1/jobs/job-1/cancel', b'', 'cancel_job_transaction', transport='database')
        self.assertEqual(order, [('status', 200), ('responded',)])


if __name__ == '__main__': unittest.main()
