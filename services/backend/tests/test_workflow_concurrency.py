"""Twenty-five real workflow state machines share bounded SQLite write locks."""
import asyncio
from collections import Counter
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from reveal_backend import jobs, workflow_state as state
from reveal_backend.evidence_package import sha256
from reveal_backend.repository import Repository
from reveal_backend.workflow_execution import WorkflowExecution, run_sync
from test_durable_workflow import MemoryStore


class TimedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        started = time.monotonic()
        try: return super().execute(sql, parameters)
        finally:
            if sql == 'BEGIN IMMEDIATE': self.lock_waits.append(time.monotonic() - started)


class BoundedRepository(Repository):
    def __init__(self, path):
        super().__init__(path)
        self.lock_waits = []

    def connect(self):
        connection = sqlite3.connect(self.sqlite_path, timeout=5, factory=TimedConnection)
        connection.lock_waits = self.lock_waits
        return connection


class FakeBoxes:
    def __init__(self, count):
        self.count = count
        self.created = set()
        self.observations = Counter()
        self.deleted = Counter()
        self.all_created = asyncio.Event()

    async def create_once(self, job_id, attempt):
        self.created.add(job_id)
        if len(self.created) == self.count: self.all_created.set()
        # No job can release capacity before all 25 hold an allocation.
        await asyncio.wait_for(self.all_created.wait(), 10)
        return {'box_id': 'fake-' + job_id, 'job_id': job_id, 'attempt': attempt,
                'phase': 'created', 'capture_protocol': 's3-v1', 'cursor': 0}

    async def prepare_once(self, request, box):
        await asyncio.sleep(0)
        return dict(box, phase='prepared')

    async def launch_once(self, box):
        return dict(box, phase='running')

    async def inspect_once(self, box):
        self.observations[box['job_id']] += 1
        terminal = self.observations[box['job_id']] == 3
        await asyncio.sleep(0)
        return (dict(box, phase='terminal' if terminal else 'running', cursor=box['cursor'] + 1,
                     state={'status': 'succeeded' if terminal else 'running'}), [], terminal)

    async def capture_to_store(self, binding, box, store, reference):
        return {'box': dict(box, phase='captured'), 'workspace': reference,
                'capture_sha256': sha256(box['box_id'].encode())}

    async def delete_once(self, box):
        self.deleted[box['box_id']] += 1
        await asyncio.sleep(0)
        return dict(box, phase='deleted')


class WorkflowConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_five_jobs_create_observe_capture_cleanup_without_deadlocks(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict('os.environ', {
                'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'concurrency-test',
                'REVEAL_ENVIRONMENT': 'test', 'REVEAL_MAX_ACTIVE_BOXES': '25',
                'REVEAL_MAX_SCRATCH_STEPS': '2', 'REVEAL_MAX_PREPARATION_STEPS': '2',
                'REVEAL_MAX_CAPTURE_STEPS': '2', 'REVEAL_MAX_REVIEW_STEPS': '2',
                'REVEAL_WORKFLOW_OBSERVE_INTERVAL_SECONDS': '10'}):
            repo = BoundedRepository(Path(temporary) / 'db.sqlite'); repo.migrate()
            with repo.connect() as connection: connection.execute('PRAGMA journal_mode=WAL')
            store = MemoryStore(); boxes = FakeBoxes(25)
            engine = WorkflowExecution(repo, storage=store, adapter=boxes)
            root = Path(temporary) / 'input'; root.mkdir(); (root / 'input.json').write_bytes(b'{}')
            reference = store.snapshot(root); payloads = []
            for index in range(25):
                with repo.transaction() as tx:
                    job = jobs.enqueue(tx, 'owner-' + str(index // 5), 'paragraph', account_id='synthetic-account')
                    execution = tx.get('execution', job['id'])['data']
                    execution.update(phase='create', workspace=reference, cleanup_complete=False)
                    tx.put('execution', job['id'], job['owner_user_id'], execution)
                    queue = tx.get('queue', job['id'])['data']
                    queue.update(workspace=reference, dispatch_input={
                        'path': 'input.json', 'sha256': sha256(b'{}'), 'kind': 'paragraph',
                        'mode': 'box', 'model': 'claude-sonnet-4-6', 'selected_graphs': []})
                    tx.put('queue', job['id'], job['owner_user_id'], queue)
                payloads.append({'job_id': job['id'], 'generation': 1, 'namespace': 'concurrency-test'})

            def cancel(payload):
                with repo.transaction() as tx: jobs.cancel(tx, tx.get('job', payload['job_id'])['data'])

            async def drive(payload, ordinal):
                result = {'index': 0}
                for _ in range(100):
                    result = await engine.step(payload, result['index'])
                    if result['phase'] == 'validate':
                        self.assertTrue(result.get('cleanup_id'))
                        # Stop at the capture boundary; scientific validation is tested separately.
                        await run_sync(cancel, payload)
                        terminal = result
                        for _ in range(100):
                            terminal = await engine.step(payload, terminal['index'])
                            if terminal['done']: break
                            await asyncio.sleep(0.1 + ordinal * 0.003)
                        self.assertTrue(terminal['done'])
                        cleanup = {'cleanup_id': result['cleanup_id'], 'namespace': 'concurrency-test'}
                        self.assertTrue((await engine.cleanup(cleanup))['deleted'])
                        self.assertTrue((await engine.cleanup(cleanup))['deleted'])
                        return
                    self.assertFalse(result['done'])
                    if result['phase'] == 'observe': self.assertEqual(result['sleep'], 10)
                    await asyncio.sleep(0.1 + ordinal * 0.003)
                self.fail(f'Workflow did not progress within 100 bounded phase attempts: {result}')

            started = time.monotonic()
            async with asyncio.timeout(30), asyncio.TaskGroup() as tasks:
                for ordinal, payload in enumerate(payloads): tasks.create_task(drive(payload, ordinal))
            elapsed = time.monotonic() - started
            self.assertEqual(len(boxes.created), 25)
            self.assertEqual(list(boxes.observations.values()), [3] * 25)
            self.assertEqual(list(boxes.deleted.values()), [1] * 25)
            with repo.read_transaction() as tx:
                executions = tx.list('execution')
                self.assertEqual(len(executions), 25)
                self.assertTrue(all(not row['data']['capacity_reserved'] for row in executions))
                self.assertTrue(all(row['data']['cleanup_complete'] for row in executions))
                self.assertTrue(all(row['data']['box']['cursor'] == 3 for row in executions))
                self.assertTrue(all(row['data']['fence'] is None for row in executions))
                self.assertTrue(all(row['data']['status'] == 'cancelled' for row in tx.list('job')))
                self.assertEqual(tx.list('workflow_cleanup'), [])
                self.assertEqual(len(tx.list('workflow_cleanup_completed')), 25)
            self.assertTrue(repo.lock_waits)
            self.assertLess(max(repo.lock_waits), 5)
            print(f'25 workflow jobs: {elapsed:.3f}s, {len(repo.lock_waits)} writes, '
                  f'max SQLite lock wait {max(repo.lock_waits):.3f}s')
