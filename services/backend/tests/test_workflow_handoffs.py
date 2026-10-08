"""Replay bounded workflow segments through the pinned SDK and durable state."""
import base64
from collections import deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import jwt
from qstash import AsyncQStash
from upstash_workflow.constants import WORKFLOW_PROTOCOL_VERSION

from reveal_backend import jobs, workflow_state as state, workflow_routes as routes
from reveal_backend.repository import Repository, digest, now
from reveal_backend.workflow_execution import WorkflowExecution
from reveal_backend.workflow_transport import WorkflowHttp

KEY = 'handoff-test-signing-key-32-bytes'
URL = 'https://workflow.invalid' + routes.PATH


def encoded(raw):
    return base64.b64encode(raw.encode()).decode()


class WorkflowWire:
    """Keep full QStash history envelopes, including exact SDK result JSON."""
    def __init__(self):
        self.histories = {}; self.deliveries = deque(); self.dedup = set()
        self.sizes = []; self.deleted = []; self.published = []

    async def request(self, **kwargs):
        if kwargs['method'] == 'DELETE':
            self.deleted.append(kwargs['path']); return []
        assert kwargs['path'] == '/v2/batch'
        for item in json.loads(kwargs['body']):
            headers = item['headers']; run = headers['Upstash-Workflow-RunId']
            dedup = headers['Upstash-Deduplication-Id']
            self.published.append(deepcopy(item))
            if dedup in self.dedup: continue
            self.dedup.add(dedup)
            initial = headers['Upstash-Workflow-Init'] == 'true'
            row = {'messageId': 'msg_' + digest([run, dedup])[:32],
                   'body': encoded(item['body']), 'callType': 'step',
                   'createdAt': 1791468000000}
            if initial: self.histories[run] = [row]
            else: self.histories[run].append(row)
            body = json.dumps(self.histories[run])
            self.sizes.append(len(body.encode()))
            self.deliveries.append((run, body))
        return []

    def signature(self, body, run):
        return {'Content-Type': 'application/json', 'Upstash-Workflow-Sdk-Version': WORKFLOW_PROTOCOL_VERSION,
            'Upstash-Workflow-Runid': run, 'Upstash-Signature': jwt.encode({'iss': 'Upstash', 'sub': URL,
                'exp': int(time.time()) + 60, 'nbf': int(time.time()) - 1,
                'body': base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).decode().rstrip('=')},
                KEY, algorithm='HS256')}


class WorkflowHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repo = Repository(Path(temporary.name) / 'db.sqlite'); self.repo.migrate()
        env = patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'handoff-test',
            'REVEAL_ENVIRONMENT': 'test', 'QSTASH_TOKEN': 'fake', 'QSTASH_CURRENT_SIGNING_KEY': KEY,
            'QSTASH_NEXT_SIGNING_KEY': KEY, 'REVEAL_WORKFLOW_URL': URL,
            'REVEAL_WORKFLOW_STEPS_PER_RUN': '6', 'REVEAL_WORKFLOW_MAX_RECOVERIES': '3'})
        env.start(); self.addCleanup(env.stop)

    def seed(self):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner', 'paragraph', account_id='account:original')
            row = tx.get('execution', job['id']); value = row['data']
            value.update(phase='observe', capacity_reserved=True, creation_intent='original-allocation',
                deadline='2026-10-08T18:00:00Z', workspace={'store': 'test', 'sha256': 'a' * 64}, recoveries=2,
                box={'box_id': 'test-box', 'job_id': job['id'], 'attempt': 1, 'phase': 'running', 'cursor': 31})
            tx.put('execution', job['id'], row['owner'], value)
        return job, {'job_id': job['id'], 'namespace': 'handoff-test', 'generation': 1}

    def execution(self, job):
        with self.repo.read_transaction() as tx: return tx.get('execution', job['id'])['data']

    async def drive(self, phases, limit, *, legacy=False):
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_STEPS_PER_RUN': str(limit)}):
            job, payload = self.seed()
            wire = WorkflowWire(); qstash = AsyncQStash('fake'); qstash.http = WorkflowHttp(wire)
            app = FastAPI(); effects = []
            async def operate(engine, incoming, token, current, execution, root):
                self.assertEqual(execution['box']['box_id'], 'test-box')
                self.assertEqual(execution['box']['cursor'], 31 + execution['phase_index'])
                self.assertEqual(execution['deadline'], '2026-10-08T18:00:00Z')
                self.assertEqual(execution['recoveries'], 2)
                effects.append(execution['phase_index'])
                box = dict(execution['box'], cursor=execution['box']['cursor'] + 1)
                if execution['phase_index'] == phases - 1:
                    with self.repo.transaction() as tx:
                        row = tx.get('job', current['id']); value = row['data']; value['status'] = 'succeeded'
                        tx.put('job', current['id'], row['owner'], value)
                    return {'next_phase': 'complete', 'done': True, 'capacity_reserved': False,
                            'cleanup_complete': True, 'box': dict(box, phase='deleted')}
                return {'next_phase': 'observe', 'sleep': 5, 'box': box}
            payload_for = routes.payload_for
            def dispatched(intent):
                value = payload_for(intent)
                if legacy and intent['generation'] == 1: value.pop('steps_per_run')
                return value
            with (patch.object(routes, 'client', return_value=qstash),
                  patch.object(routes, 'payload_for', side_effect=dispatched),
                  patch.object(WorkflowExecution, 'operate', operate)):
                routes.mount_workflow(app, self.repo)
                await routes.dispatch_job(self.repo, job['id'])
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://workflow.invalid') as client:
                    for delivery in range(phases * 4):
                        if not wire.deliveries: break
                        run, body = wire.deliveries.popleft()
                        if legacy and delivery < 18:
                            with patch.object(routes, 'segment_limit', return_value=5000):
                                response = await client.post(routes.PATH, content=body, headers=wire.signature(body, run))
                        else:
                            response = await client.post(routes.PATH, content=body, headers=wire.signature(body, run))
                        self.assertEqual(response.status_code, 200, response.text)
                    else: self.fail('Workflow replay loop did not finish')
            self.assertEqual(effects, list(range(phases)))
            self.assertFalse(wire.deliveries)
            self.assertEqual(len(wire.deleted), len(wire.histories))
            execution = self.execution(job)
            self.assertEqual(execution['phase_index'], phases)
            self.assertEqual(execution['recoveries'], 2)
            self.assertEqual(execution.get('handoffs', 0), (phases - 1) // limit)
            return wire

    async def test_204_phases_use_bounded_signed_sdk_deliveries_with_continuous_indexes(self):
        wire = await self.drive(204, 6)
        self.assertEqual(len(wire.histories), 34)
        self.assertLess(max(wire.sizes), 16 * 1024)
        # The SDK's null call/sleep fields and base64 encoding are part of the
        # measurement; counting only the tiny business result misses growth.
        self.assertGreater(max(wire.sizes), 6000)
        print('Bounded workflow: 204 phases, 34 runs, max callback bytes =', max(wire.sizes))

    async def test_legacy_history_beyond_new_limit_replays_before_handoff(self):
        wire = await self.drive(204, 6, legacy=True)
        first = next(iter(wire.histories.values()))
        step_names = [json.loads(base64.b64decode(row['body']))['stepName'] for row in first[1:]]
        self.assertEqual(step_names[-1], 'continue-9')
        self.assertEqual(len(wire.histories), 34)

    async def test_unbounded_baseline_replays_the_same_204_phases(self):
        wire = await self.drive(204, 5000)
        self.assertGreater(max(wire.sizes), 16 * 1024)
        self.assertEqual(len(wire.histories), 1)
        print('Unbounded workflow baseline: 204 phases, max callback bytes =', max(wire.sizes))

    async def test_handoff_replay_is_idempotent_and_old_generation_cannot_act(self):
        job, payload = self.seed()
        with self.repo.transaction() as tx: tx.remove('workflow_dispatch', job['id'])
        before = self.execution(job)
        result = routes.continue_run(self.repo, payload, 0)
        self.assertEqual(result, {'generation': 2, 'index': 0})
        self.assertEqual(routes.continue_run(self.repo, payload, 0), result)
        after = self.execution(job)
        for key in ('box', 'deadline', 'workspace', 'creation_intent', 'capacity_reserved', 'recoveries'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['handoffs'], 1)
        with self.assertRaises(state.StaleExecution): state.acquire(self.repo, payload, 0)
        wire = WorkflowWire(); qstash = AsyncQStash('fake'); qstash.http = WorkflowHttp(wire)
        await routes.dispatch_pending(self.repo, qstash=qstash)
        self.assertEqual(routes.continue_run(self.repo, payload, 0), result)
        with self.repo.read_transaction() as tx: self.assertIsNone(tx.get('workflow_dispatch', job['id']))
        self.assertEqual(len(wire.deliveries), 1)

    async def test_handoff_rejects_changed_checkpoint_or_active_lease(self):
        job, payload = self.seed()
        with self.assertRaises(state.StaleExecution): routes.continue_run(self.repo, payload, 9)
        _, execution, _ = state.acquire(self.repo, payload, 0)
        with self.assertRaises(state.StepBusy): routes.continue_run(self.repo, payload, 0)
        self.assertEqual(self.execution(job)['fence'], execution['fence'])

    async def test_delivery_stalls_do_not_spend_step_failure_budget(self):
        job, payload = self.seed()
        for _ in range(10):
            with self.repo.transaction() as tx:
                tx.remove('workflow_dispatch', job['id'])
                row = tx.get('execution', job['id']); value = row['data']; value['expected_at'] = ''
                tx.put('execution', job['id'], row['owner'], value)
            self.assertEqual(routes.reconcile_stale(self.repo), 1)
        execution = self.execution(job)
        self.assertEqual(execution['recoveries'], 2)
        self.assertEqual(execution['delivery_recoveries'], 10)
        self.assertEqual(execution['generation'], 11)
        self.assertEqual(execution['disposition'], 'ready')

    async def test_real_phase_failures_exhaust_budget_and_transfer_cleanup(self):
        job, payload = self.seed()
        for expected in (1, 0):
            current = self.execution(job); payload['generation'] = current['generation']
            _, execution, _ = state.acquire(self.repo, payload, 0)
            state.release(self.repo, payload, execution['fence'], reason='Temporary Box failure')
            with self.repo.transaction() as tx:
                tx.remove('workflow_dispatch', job['id'])
                row = tx.get('execution', job['id']); row['data']['expected_at'] = ''
                tx.put('execution', job['id'], row['owner'], row['data'])
            self.assertEqual(routes.reconcile_stale(self.repo), expected)
        execution = self.execution(job)
        self.assertEqual(execution['disposition'], 'recovery_required')
        self.assertEqual(execution['recoveries'], 3)
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx:
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')
        with self.assertRaises(state.StaleExecution): state.acquire(self.repo, payload, 0)

    async def fail_callback(self, payload):
        wire = WorkflowWire(); qstash = AsyncQStash('fake'); qstash.http = WorkflowHttp(wire)
        app = FastAPI()
        with patch.object(routes, 'client', return_value=qstash): routes.mount_workflow(app, self.repo)
        body = json.dumps({'status': 500, 'header': {}, 'body': encoded(json.dumps({'message': 'delivery failed'})),
            'url': URL, 'sourceBody': encoded(json.dumps(payload)), 'workflowRunId': 'failed-run'})
        headers = wire.signature(body, 'failed-run')
        headers.pop('Upstash-Workflow-Sdk-Version')
        headers['Upstash-Workflow-Is-Failure'] = 'true'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://workflow.invalid') as client:
            response = await client.post(routes.PATH, content=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)

    async def test_signed_failure_callback_transfers_exhausted_cleanup_and_honors_cancel(self):
        job, payload = self.seed()
        with self.repo.transaction() as tx:
            row = tx.get('execution', job['id']); row['data'].update(recoveries=3, retry_cause='step_failure')
            tx.put('execution', job['id'], row['owner'], row['data'])
            row = tx.get('job', job['id']); row['data']['status'] = 'cancel_requested'
            tx.put('job', job['id'], row['owner'], row['data'])
        await self.fail_callback(payload)
        execution = self.execution(job)
        self.assertEqual(execution['disposition'], 'recovery_required')
        self.assertFalse(execution['capacity_reserved'])
        with self.repo.read_transaction() as tx:
            self.assertTrue(state.has_cleanup_handoff(tx, execution))
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'cancelled')
            cleanup_count = len(tx.list('workflow_cleanup'))
        await self.fail_callback(payload)
        self.assertEqual(self.execution(job), execution)
        with self.repo.read_transaction() as tx: self.assertEqual(len(tx.list('workflow_cleanup')), cleanup_count)

    async def test_scheduler_failure_callback_preserves_live_lease_and_failure_budget(self):
        job, payload = self.seed()
        _, execution, _ = state.acquire(self.repo, payload, 0)
        await self.fail_callback(payload)
        current = self.execution(job)
        for key in ('generation', 'fence', 'lease_until', 'disposition', 'recoveries'):
            self.assertEqual(current[key], execution[key])
        self.assertEqual(routes.reconcile_stale(self.repo), 0)
        state.complete(self.repo, payload, execution['fence'], next_phase='observe', sleep=5)
        await self.fail_callback(payload)
        current = self.execution(job)
        self.assertEqual(current['disposition'], 'retry')
        self.assertEqual(current['recoveries'], 2)
        self.assertTrue(current['capacity_reserved'])

    async def test_dispatched_limit_is_immutable_across_configuration_change(self):
        job, payload = self.seed()
        with self.repo.read_transaction() as tx: intent = tx.get('workflow_dispatch', job['id'])['data']
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_STEPS_PER_RUN': '3'}):
            self.assertEqual(routes.payload_for(intent)['steps_per_run'], 6)
            routes.continue_run(self.repo, payload, 0)
        with self.repo.read_transaction() as tx: next_intent = tx.get('workflow_dispatch', job['id'])['data']
        self.assertEqual(routes.payload_for(next_intent)['steps_per_run'], 3)


if __name__ == '__main__': unittest.main()
