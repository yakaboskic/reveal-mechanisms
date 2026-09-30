"""Failure injection for bounded execution, replay, cancellation and paid review."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reveal_backend import jobs, workflow_state as state, durable_review
from reveal_backend.repository import Repository, Transaction, digest
from reveal_backend.workflow_execution import WorkflowExecution
from reveal_backend.workflow_routes import dispatch_pending, reconcile_stale, mount_workflow
from reveal_backend.box_adapter import BoxTransportError
from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import canonical_json
from reveal_backend.scientific_grounding import ScientificReviewUnavailable


class MemoryStore:
    def __init__(self): self.values = {}; self.fail = False
    def snapshot(self, root):
        if self.fail: raise OSError('snapshot unavailable')
        files = {str(p.relative_to(root)): p.read_bytes().hex() for p in root.rglob('*') if p.is_file()}
        key = digest(files); self.values[key] = files
        return {'sha256': key, 'store': 'test-only'}
    def restore(self, ref, root, *, cancelled=None):
        for name, data in self.values[ref['sha256']].items():
            path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(bytes.fromhex(data))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name)/'db.sqlite'); self.repo.migrate()
        self.env = patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'test',
            'REVEAL_ENVIRONMENT': 'test', 'QSTASH_TOKEN': 'test-token', 'QSTASH_CURRENT_SIGNING_KEY': 'test-current',
            'QSTASH_NEXT_SIGNING_KEY': 'test-next', 'REVEAL_WORKFLOW_URL': 'http://127.0.0.1:18001/internal/workflows/research-v1'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = MemoryStore()

    def new(self, kind='deployment_probe'):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner', kind, inputs={'nonce': 'original-nonce', 'delay': 3})
            execution = tx.get('execution', job['id'])['data']
        return job, {'job_id': job['id'], 'generation': execution['generation'], 'namespace': 'test'}

    def execution(self, payload):
        with self.repo.read_transaction() as tx: return tx.get('execution', payload['job_id'])['data']

    async def test_replacement_during_durable_wait_and_replayed_step(self):
        job, payload = self.new()
        engine = WorkflowExecution(self.repo, storage=self.store)
        result = await engine.step(payload, 0)
        self.assertEqual(result, {'phase': 'probe_finish', 'index': 1, 'sleep': 3, 'done': False})
        self.assertIsNone(self.execution(payload)['fence'])
        self.assertEqual(await WorkflowExecution(self.repo, storage=self.store).step(payload, 0), result)
        result = await WorkflowExecution(self.repo, storage=self.store).step(payload, 1)
        self.assertTrue(result['done'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', job['id'])['data']['status'], 'succeeded')
            events = len(tx.list('event'))
        self.assertEqual(await WorkflowExecution(self.repo, storage=self.store).step(payload, 1), result)
        with self.repo.read_transaction() as tx: self.assertEqual(len(tx.list('event')), events)

    async def test_production_probe_requires_exact_isolated_qa_configuration(self):
        configs = [
            ({'SERVICE_ENV': 'qa', 'REVEAL_JOB_NAMESPACE': 'reveal-workflow-qa', 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_qa'}, True),
            ({'SERVICE_ENV': 'prod', 'REVEAL_JOB_NAMESPACE': 'reveal-workflow-qa', 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_qa'}, False),
            ({'SERVICE_ENV': 'qa', 'REVEAL_JOB_NAMESPACE': 'reveal-workflow-prod', 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_qa'}, False),
            ({'SERVICE_ENV': 'qa', 'REVEAL_JOB_NAMESPACE': 'reveal-workflow-qa', 'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_workflow_prod'}, False),
        ]
        for config, allowed in configs:
            with patch.dict('os.environ', {**config, 'REVEAL_ENVIRONMENT': 'production'}):
                job, payload = self.new(); payload['namespace'] = config['REVEAL_JOB_NAMESPACE']
                result = await WorkflowExecution(self.repo, storage=self.store).step(payload, 0)
                self.assertEqual(result['phase'], 'probe_finish' if allowed else 'complete')
                if not allowed:
                    with self.repo.read_transaction() as tx:
                        self.assertEqual(tx.get('job', job['id'])['data']['status'], 'failed')

    async def test_legacy_claimers_never_take_workflow_jobs(self):
        self.new()
        self.assertIsNone(jobs.claim(self.repo, 'legacy'))
        with patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'database'}):
            self.assertIsNone(jobs.claim(self.repo, 'legacy'))

    async def test_fence_generation_and_namespace_reject_stale_writes(self):
        _, payload = self.new()
        job, execution, _ = state.acquire(self.repo, payload, 0)
        with self.assertRaises(state.StepBusy): state.acquire(self.repo, payload, 0)
        with self.assertRaises(state.StaleExecution): state.save(self.repo, {**payload, 'namespace': 'other'}, execution['fence'])
        with self.repo.transaction() as tx:
            value = tx.get('execution', job['id'])['data']; value['generation'] += 1
            tx.put('execution', job['id'], 'owner', value)
        with self.assertRaises(state.StaleExecution): state.complete(self.repo, payload, execution['fence'], next_phase='create')

    async def test_cancel_queued_and_durable_wait_prevents_acceptance(self):
        for started in (False, True):
            job, payload = self.new()
            if started: await WorkflowExecution(self.repo, storage=self.store).step(payload, 0)
            with self.repo.transaction() as tx: jobs.cancel(tx, tx.get('job', job['id'])['data'])
            result = await WorkflowExecution(self.repo, storage=self.store).step(payload, 1 if started else 0)
            self.assertTrue(result['done'])
            with self.repo.read_transaction() as tx:
                self.assertEqual(tx.get('job', job['id'])['data']['status'], 'cancelled')
                self.assertIsNotNone(tx.get('workflow_control', job['id']))

    async def test_dispatch_gap_retries_same_stable_identity(self):
        job, payload = self.new()
        fake = Mock(); fake.http.request = AsyncMock(side_effect=[TimeoutError(), [{'messageId': 'ok'}]])
        first = await dispatch_pending(self.repo, qstash=fake)
        self.assertEqual(first['failed'], 1)
        with self.repo.transaction() as tx:
            row = tx.get('workflow_dispatch', job['id']); row['data']['next_attempt_at'] = ''
            tx.put('workflow_dispatch', job['id'], 'owner', row['data'])
        second = await dispatch_pending(self.repo, qstash=fake)
        self.assertEqual(second['delivered'], 1)
        self.assertEqual(fake.http.request.call_args_list[0].kwargs['body'], fake.http.request.call_args_list[1].kwargs['body'])
        with self.repo.read_transaction() as tx: self.assertIsNone(tx.get('workflow_dispatch', job['id']))

    async def test_continuations_keep_timeout_and_stable_provider_dedup_identity(self):
        from reveal_backend.workflow_transport import WorkflowHttp
        from upstash_workflow import AsyncWorkflowContext
        from upstash_workflow.error import WorkflowAbort
        delegate = Mock(); delegate.request = AsyncMock(return_value=[])
        qstash = Mock(); qstash.http = WorkflowHttp(delegate)
        def context(run='run-1'):
            return AsyncWorkflowContext(qstash_client=qstash, workflow_run_id=run,
                headers={}, steps=[], url='https://example.test/workflow', failure_url=None, initial_payload={})
        for _ in range(2):
            with self.assertRaises(WorkflowAbort): await context().run('phase-0', lambda: {'index': 1})
        first, replay = [json.loads(call.kwargs['body'])[0] for call in delegate.request.call_args_list]
        self.assertEqual(first['headers']['Upstash-Timeout'], '420s')
        self.assertEqual(first['headers']['Upstash-Deduplication-Id'], replay['headers']['Upstash-Deduplication-Id'])
        with self.assertRaises(WorkflowAbort): await context('run-2').run('phase-0', lambda: {'index': 1})
        newer = json.loads(delegate.request.call_args.kwargs['body'])[0]
        self.assertNotEqual(first['headers']['Upstash-Deduplication-Id'], newer['headers']['Upstash-Deduplication-Id'])
        with self.assertRaises(WorkflowAbort): await context().sleep('wait-1', 5)
        sleep = json.loads(delegate.request.call_args.kwargs['body'])[0]
        self.assertEqual(sleep['headers']['Upstash-Timeout'], '420s')
        self.assertNotEqual(first['headers']['Upstash-Deduplication-Id'], sleep['headers']['Upstash-Deduplication-Id'])

    async def test_reconciler_fences_stale_owner_and_preserves_paid_intent(self):
        job, payload = self.new()
        with self.repo.transaction() as tx:
            tx.remove('workflow_dispatch', job['id'])
            execution = tx.get('execution', job['id'])['data']
            execution.update(phase='create', phase_index=4, creation_intent='paid-allocation', capacity_reserved=True,
                             lease_until='2000-01-01T00:00:00Z', expected_at='2000-01-01T00:00:00Z')
            tx.put('execution', job['id'], 'owner', execution)
        self.assertEqual(reconcile_stale(self.repo), 1)
        execution = self.execution({**payload, 'generation': 2})
        self.assertEqual(execution['generation'], 2)
        self.assertEqual(execution['creation_intent'], 'paid-allocation')
        self.assertTrue(execution['capacity_reserved'])
        with self.assertRaises(state.StaleExecution): await WorkflowExecution(self.repo, storage=self.store).step(payload, 4)

    async def test_lost_creation_response_never_allocates_again(self):
        job, payload = self.new('paragraph')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root/'input.json'; path.write_text('{}')
            snapshot = {'path': 'input.json', 'sha256': __import__('hashlib').sha256(b'{}').hexdigest(), 'model': 'test', 'mode': 'box'}
            ref = self.store.snapshot(root)
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']; execution.update(phase='create', workspace=ref)
            tx.put('execution', job['id'], 'owner', execution)
            queue = tx.get('queue', job['id'])['data']; queue['dispatch_input'] = snapshot
            tx.put('queue', job['id'], 'owner', queue)
        adapter = Mock(); adapter.create_once = AsyncMock(side_effect=BoxTransportError('response lost'))
        engine = WorkflowExecution(self.repo, storage=self.store, adapter=adapter)
        with self.assertRaises(BoxTransportError): await engine.step(payload, 0)
        with self.assertRaises(state.RecoveryRequired): await engine.step(payload, 0)
        self.assertEqual(adapter.create_once.await_count, 1)
        self.assertEqual(self.execution(payload)['disposition'], 'recovery_required')
        self.assertTrue(self.execution(payload)['capacity_reserved'])

    async def test_capture_storage_failure_never_deletes_box(self):
        job, payload = self.new('paragraph')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'input.json').write_text('{}'); ref = self.store.snapshot(root)
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']; execution.update(phase='capture', workspace=ref,
                box={'box_id': 'box-1', 'phase': 'terminal'}, capacity_reserved=True)
            tx.put('execution', job['id'], 'owner', execution)
            queue = tx.get('queue', job['id'])['data']; queue['dispatch_input'] = {
                'path': 'input.json', 'sha256': __import__('hashlib').sha256(b'{}').hexdigest()}
            tx.put('queue', job['id'], 'owner', queue)
        adapter = Mock(); adapter.capture_once = AsyncMock(return_value={'box_id': 'box-1', 'phase': 'captured'})
        adapter.delete_once = AsyncMock(); self.store.fail = True
        with self.assertRaises(OSError): await WorkflowExecution(self.repo, storage=self.store, adapter=adapter).step(payload, 0)
        adapter.delete_once.assert_not_awaited()
        self.assertEqual(self.execution(payload)['phase'], 'capture')
        self.assertTrue(self.execution(payload)['capacity_reserved'])

    async def test_creation_ack_loss_reuses_saved_box_without_second_allocation(self):
        job, payload = self.new('paragraph')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'input.json').write_text('{}'); ref=self.store.snapshot(root)
        with self.repo.transaction() as tx:
            execution=tx.get('execution',job['id'])['data']; execution.update(phase='create',workspace=ref)
            tx.put('execution',job['id'],'owner',execution)
            queue=tx.get('queue',job['id'])['data']; queue['dispatch_input']={
                'path':'input.json','sha256':__import__('hashlib').sha256(b'{}').hexdigest()}
            tx.put('queue',job['id'],'owner',queue)
        adapter=Mock(); adapter.create_once=AsyncMock(return_value={'box_id':'box-1','phase':'created'})
        engine=WorkflowExecution(self.repo,storage=self.store,adapter=adapter)
        with patch('reveal_backend.workflow_state.complete',side_effect=OSError('lost step acknowledgement')):
            with self.assertRaises(OSError): await engine.step(payload,0)
        result=await engine.step(payload,0)
        self.assertEqual(result['phase'],'bootstrap'); self.assertEqual(adapter.create_once.await_count,1)

    async def test_job_events_follow_authoritative_transferred_owner(self):
        job,payload=self.new()
        _,execution,_=state.acquire(self.repo,payload,0)
        with self.repo.transaction() as tx:
            # transfer keeps payload owner_user_id historical, as the real repository does.
            for kind in ('job','queue','execution'):
                row=tx.get(kind,job['id']); tx.put(kind,job['id'],'new-owner',row['data'])
        engine=WorkflowExecution(self.repo,storage=self.store)
        engine.activity(payload,execution['fence'],'stage',{'stage':'validating','message':'Source validation'})
        current,_,_=engine.context(payload)
        self.assertEqual(current['owner_user_id'],'new-owner')
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job',job['id'])['owner'],'new-owner')
            self.assertEqual(tx.list('event')[0]['owner'],'new-owner')

    async def test_observation_batch_replay_and_duplicate_events_are_atomic_with_cursor(self):
        job, payload = self.new()
        _, execution, _ = state.acquire(self.repo, payload, 0)
        engine = WorkflowExecution(self.repo, storage=self.store)
        event = ('agent_message', {'remote_stream_id': 'stream', 'remote_sequence': 1, 'message': 'First'})
        events = [event, event, ('agent_message', {
            'remote_stream_id': 'stream', 'remote_sequence': 2, 'message': 'Second'})]
        handle = {'box_id': 'box', 'cursor': 2}
        engine.observe_commit(payload, execution['fence'], handle, events)
        engine.observe_commit(payload, execution['fence'], handle, events)
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('remote_event')), 2)
            self.assertEqual(len(tx.list('event')), 3)  # queued plus two remote events
            self.assertEqual(tx.get('job', job['id'])['data']['last_event_id'], '3')
            self.assertEqual(tx.get('execution', job['id'])['data']['box']['cursor'], 2)
            self.assertEqual(tx.get('queue', job['id'])['data']['remote_handle']['cursor'], 2)
        original = Transaction.update_existing
        def fail_cursor(tx, kind, *args, **kwargs):
            if kind == 'queue': raise OSError('Cursor commit failed after event inserts')
            return original(tx, kind, *args, **kwargs)
        later = [('agent_message', {'remote_stream_id': 'stream', 'remote_sequence': 3, 'message': 'Third'})]
        with patch.object(Transaction, 'update_existing', fail_cursor):
            with self.assertRaises(OSError):
                engine.observe_commit(payload, execution['fence'], {**handle, 'cursor': 3}, later)
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('remote_event')), 2)
            self.assertEqual(len(tx.list('event')), 3)
            self.assertEqual(tx.get('job', job['id'])['data']['last_event_id'], '3')
            self.assertEqual(tx.get('execution', job['id'])['data']['box']['cursor'], 2)
            self.assertEqual(tx.get('queue', job['id'])['data']['remote_handle']['cursor'], 2)
        engine.observe_commit(payload, execution['fence'], {**handle, 'cursor': 3}, later)
        with self.repo.read_transaction() as tx:
            self.assertEqual(len(tx.list('remote_event')), 3)
            self.assertEqual(len(tx.list('event')), 4)
            self.assertEqual(tx.get('queue', job['id'])['data']['remote_handle']['cursor'], 3)

    async def test_environment_namespace_is_checked_against_service_configuration(self):
        _,payload=self.new()
        with patch.dict('os.environ',{'REVEAL_JOB_NAMESPACE':'other'}):
            with self.assertRaises(state.StaleExecution): state.acquire(self.repo,payload,0)

    async def test_stage_capacity_returns_durable_delay_instead_of_holding_handler(self):
        _,first=self.new(); _,second=self.new()
        with patch.dict('os.environ',{'REVEAL_MAX_PREPARATION_STEPS':'1'}):
            state.acquire(self.repo,first,0)
            result=await WorkflowExecution(self.repo,storage=self.store).step(second,0)
        self.assertEqual(result['phase'],'prepare'); self.assertEqual(result['sleep'],10)
        self.assertIsNone(self.execution(second)['fence'])

    async def test_scratch_limit_is_shared_across_preparation_review_and_capture(self):
        _, first = self.new(); _, second = self.new(); _, third = self.new()
        with self.repo.transaction() as tx:
            for payload, phase in ((second, 'review_call'), (third, 'capture')):
                row = tx.get('execution', payload['job_id']); row['data']['phase'] = phase
                tx.put('execution', payload['job_id'], row['owner'], row['data'])
        with patch.dict('os.environ', {'REVEAL_MAX_SCRATCH_STEPS': '2'}):
            state.acquire(self.repo, first, 0)
            state.acquire(self.repo, second, 0)
            with patch('reveal_backend.workflow_execution.tempfile.TemporaryDirectory', side_effect=AssertionError('Deferred phase allocated scratch')):
                result = await WorkflowExecution(self.repo, storage=self.store).step(third, 0)
        self.assertEqual(result['phase'], 'capture'); self.assertEqual(result['sleep'], 10)
        self.assertIsNone(self.execution(third)['fence'])

    async def test_restore_timeout_drains_writer_before_releasing_scratch_fence(self):
        import time
        from threading import Event
        _, payload = self.new()
        with self.repo.transaction() as tx:
            row = tx.get('execution', payload['job_id']); row['data']['workspace'] = {'sha256': 'saved'}
            tx.put('execution', payload['job_id'], row['owner'], row['data'])
        drained = Event(); paths = []
        def restore(reference, root, *, cancelled):
            paths.append(root)
            time.sleep(1.1)  # Stand in for one bounded in-flight S3 response.
            self.assertTrue(cancelled())
            self.assertIsNotNone(self.execution(payload)['fence'])
            drained.set()
            raise OSError('Restore stopped before writing received bytes')
        self.store.restore = restore
        engine = WorkflowExecution(self.repo, storage=self.store); engine.operate = AsyncMock()
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_STEP_TIMEOUT_SECONDS': '1'}):
            with self.assertRaises(TimeoutError): await engine.step(payload, 0)
        engine.operate.assert_not_awaited()
        self.assertTrue(drained.is_set()); self.assertFalse(paths[0].exists())
        self.assertIsNone(self.execution(payload)['fence'])

    async def test_phase_timeout_drains_sync_writer_before_workspace_cleanup(self):
        import time
        from threading import Event
        from reveal_backend.workflow_execution import run_sync
        _, payload = self.new(); drained = Event(); paths = []
        engine = WorkflowExecution(self.repo, storage=self.store)
        def writer(root):
            paths.append(root); time.sleep(1.1)
            (root/'late-file').write_text('Completed before scratch was released')
            self.assertIsNotNone(self.execution(payload)['fence'])
            drained.set()
        async def operation(_payload, token, job, execution, root):
            await run_sync(writer, root)
            raise AssertionError('A timed-out phase must not continue')
        engine.operate = operation
        with patch.dict('os.environ', {'REVEAL_WORKFLOW_STEP_TIMEOUT_SECONDS': '1'}):
            with self.assertRaises(TimeoutError): await engine.step(payload, 0)
        self.assertTrue(drained.is_set()); self.assertFalse(paths[0].exists())
        self.assertIsNone(self.execution(payload)['fence'])

    async def test_repeated_cancellation_keeps_scratch_and_fence_until_writer_finishes(self):
        from threading import Event
        from reveal_backend.workflow_execution import run_sync
        for restoring in (False, True):
            _, payload = self.new(); started = Event(); release = Event(); paths = []
            if restoring:
                with self.repo.transaction() as tx:
                    row = tx.get('execution', payload['job_id']); row['data']['workspace'] = {'sha256': 'saved'}
                    tx.put('execution', payload['job_id'], row['owner'], row['data'])
            def writer(root, cancelled=None):
                paths.append(root); started.set(); release.wait(3)
                self.assertTrue(root.exists())
                if restoring: self.assertTrue(cancelled())
                else: (root/'late-write').write_text('still owned')
            engine = WorkflowExecution(self.repo, storage=self.store)
            if restoring:
                self.store.restore = lambda reference, root, *, cancelled: writer(root, cancelled)
            else:
                async def operation(_payload, token, job, execution, root): await run_sync(writer, root)
                engine.operate = operation
            task = asyncio.create_task(engine.step(payload, 0))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel(); await asyncio.sleep(.01)
                task.cancel(); await asyncio.sleep(.01)
                self.assertFalse(task.done())
                self.assertTrue(paths[0].exists())
                self.assertIsNotNone(self.execution(payload)['fence'])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(paths[0].exists())

    async def test_cancellation_winning_finish_race_is_terminal_before_workflow_retires(self):
        job,payload=self.new()
        engine=WorkflowExecution(self.repo,storage=self.store)
        await engine.step(payload,0)
        finish=jobs.finish
        def racing_finish(repository,identity,token,status,**kwargs):
            with repository.transaction() as tx: jobs.cancel(tx,tx.get('job',identity)['data'])
            return finish(repository,identity,token,status,**kwargs)
        with patch('reveal_backend.jobs.finish',side_effect=racing_finish):
            result=await engine.step(payload,1)
        self.assertTrue(result['done'])
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job',job['id'])['data']['status'],'cancelled')
            self.assertEqual(tx.get('execution',job['id'])['data']['disposition'],'complete')

    async def test_operator_recovery_keeps_unknown_allocation_counted_and_requires_identity(self):
        from reveal_backend.workflow_admin import resume,inspect_execution
        job,payload=self.new()
        with self.repo.transaction() as tx:
            value=tx.get('execution',job['id'])['data']; value.update(disposition='recovery_required',creation_intent='allocation',capacity_reserved=True)
            tx.put('execution',job['id'],'owner',value)
        with self.assertRaisesRegex(ValueError,'verified existing Box'): resume(self.repo,job['id'],1)
        value=resume(self.repo,job['id'],1,recovered_box='operator-verified-box')
        self.assertTrue(value['capacity_reserved']); self.assertEqual(value['box_id'],'operator-verified-box')
        self.assertEqual(value['phase'],'bootstrap'); self.assertEqual(value['generation'],2)
        with self.assertRaisesRegex(ValueError,'generation changed'): resume(self.repo,job['id'],1)

    async def test_review_retry_adopts_original_legacy_capture_without_authoring_attempt(self):
        job,payload=self.new('paragraph')
        with self.repo.transaction() as tx:
            tx.remove('execution',job['id'])
            queue=tx.get('queue',job['id'])['data']
            queue.update(review_source={'attempt':4,'box_id':'historical-box','capture_sha256':'original'},workspace={'sha256':'original-workspace'})
            jobs.dispatch(tx,job,queue)
            execution=tx.get('execution',job['id'])['data']
        self.assertEqual(execution['authoring_attempt'],4)
        self.assertEqual(execution['phase'],'validate')
        self.assertEqual(execution['box']['box_id'],'historical-box')
        self.assertTrue(execution['cleanup_complete']); self.assertTrue(execution['capture_complete'])
        self.assertFalse(execution['capacity_reserved'])

    async def test_observe_and_launch_only_use_rds_handle_without_restoring_s3(self):
        for phase in ('launch','observe'):
            job,payload=self.new('paragraph')
            workspace={'sha256':'must-remain-original'}
            handle={'box_id':'same-box','phase':'prepared' if phase=='launch' else 'running','cursor':0}
            with self.repo.transaction() as tx:
                execution=tx.get('execution',job['id'])['data']; execution.update(phase=phase,workspace=workspace,box=handle,capacity_reserved=True)
                tx.put('execution',job['id'],'owner',execution)
            adapter=Mock(); adapter.launch_once=AsyncMock(return_value={**handle,'phase':'running'})
            adapter.inspect_once=AsyncMock(return_value=({**handle,'phase':'running'},[],False))
            storage=Mock(); storage.restore.side_effect=AssertionError('No S3 reads for status or launch')
            storage.snapshot.side_effect=AssertionError('Handle updates must not replace original workspace')
            result=await WorkflowExecution(self.repo,storage=storage,adapter=adapter).step(payload,0)
            self.assertEqual(result['phase'],'observe')
            self.assertEqual(self.execution(payload)['workspace'],workspace)
            storage.restore.assert_not_called(); storage.snapshot.assert_not_called()

    async def test_create_and_abandoned_cleanup_preserve_workspace_without_s3_reads(self):
        job, payload = self.new('paragraph'); workspace = {'sha256': 'preserved'}
        with self.repo.transaction() as tx:
            row = tx.get('execution', job['id']); row['data'].update(phase='create', workspace=workspace)
            tx.put('execution', job['id'], row['owner'], row['data'])
        adapter = Mock(); adapter.create_once = AsyncMock(return_value={'box_id': 'box', 'phase': 'created'})
        adapter.delete_once = AsyncMock(return_value={'box_id': 'box', 'phase': 'deleted'})
        adapter.cancel_once = AsyncMock(side_effect=AssertionError('Unlaunched Box has no runner to cancel'))
        storage = Mock(); storage.restore.side_effect = AssertionError('Phase must not restore workspace')
        storage.snapshot.side_effect = AssertionError('Phase must preserve workspace')
        engine = WorkflowExecution(self.repo, storage=storage, adapter=adapter)
        self.assertEqual((await engine.step(payload, 0))['phase'], 'bootstrap')
        adapter.create_once.assert_awaited_once_with(job['id'], 1)
        with self.repo.transaction() as tx:
            row = tx.get('execution', job['id']); row['data'].update(phase='cleanup', abandoned=True)
            tx.put('execution', job['id'], row['owner'], row['data'])
            jobs.cancel(tx, tx.get('job', job['id'])['data'])
        self.assertTrue((await engine.step(payload, 1))['done'])
        self.assertEqual(self.execution(payload)['workspace'], workspace)
        self.assertFalse(self.execution(payload)['capacity_reserved'])
        adapter.cancel_once.assert_not_awaited()
        storage.restore.assert_not_called(); storage.snapshot.assert_not_called()

    async def test_unsigned_workflow_control_and_reconcile_rejected(self):
        from fastapi import FastAPI
        from httpx import AsyncClient, ASGITransport
        app=FastAPI(); mount_workflow(app, self.repo)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as web:
            for suffix in ('control-v1', 'reconcile-v1'):
                response = await web.post('/internal/workflows/' + suffix, json={})
                self.assertEqual(response.status_code, 401)


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_network_policy_response_reuses_trusted_bootstrap_sentinel(self):
        import re
        from reveal_backend.box_adapter import BoxExecutionAdapter
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.runtime_config import ROOT
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'input.json'; path.write_text('{}')
            request=ExecutionRequest('bootstrap',1,'paragraph',path,Path(temp)/'output')
            box=Mock(); box.files.write=AsyncMock(); box.update_network_policy=AsyncMock(side_effect=[OSError('response lost'),None])
            adapter=BoxExecutionAdapter(ROOT,environ={'ANTHROPIC_API_KEY':'fake'})
            sentinel=''
            async def command(_box,cmd):
                nonlocal sentinel
                if 'if [ -f' in cmd: return sentinel
                if "printf" in cmd:
                    sentinel=re.search(r'[a-f0-9]{64}',cmd).group()
                return ''
            adapter.command=AsyncMock(side_effect=command)
            with self.assertRaises(OSError): await adapter.prepare(box,request,b'pinned harness')
            writes=box.files.write.await_count
            await adapter.prepare(box,request,b'pinned harness')
            self.assertEqual(box.files.write.await_count,writes)
            self.assertEqual(box.update_network_policy.await_count,2)
            self.assertEqual(sum('sh /tmp/reveal-bootstrap.sh'==call.args[1] for call in adapter.command.call_args_list),1)


class ReviewTests(unittest.TestCase):
    def initial(self):
        package = {'selection': {'knowledge_gap_id': 'gap:1'}, 'dismech': {'observation': 'Only association was measured'},
                   'external_evidence': {'selected_graphs': []}}
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'test', 1); ledger.freeze()
            return durable_review.initial('analysis', {'claims': [{'id': 'claim:1'}]}, package,
                ledger_path=Path(temp)/'manifest.json', budget=1)

    def response(self, identity, name, value):
        response = Mock(status_code=200)
        response.json.return_value = {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': identity, 'name': name, 'input': value}],
            'usage': {'input_tokens': 1000, 'output_tokens': 100}}
        return response

    def test_process_restart_restores_conversation_read_coverage_and_cost(self):
        state_ = self.initial(); saves = []
        client = Mock(); client.post.return_value = self.response('read-1', 'read_evidence', {'pointer': '/package/dismech/observation'})
        durable_review.call_one(state_, 'fake', lambda value: saves.append(deepcopy(value)), client=client)
        self.assertTrue(saves[0]['pending']['reserved_max_usd'] > 0)
        state_ = durable_review.process_response(json.loads(json.dumps(saves[-1])))
        self.assertEqual(state_['turn'], 1); self.assertEqual(len(state_['reads']), 1)
        # Resume after checkpoint but before workflow step acknowledgment.
        self.assertEqual(durable_review.process_response(deepcopy(state_)), state_)
        verdict = {'verdict': 'supported', 'finding': 'Preserves observational uncertainty.', 'source_refs': ['/package/dismech/observation']}
        client.post.return_value = self.response('final', 'submit_review', {'claims': [{**verdict, 'claim_id': 'claim:1'}], 'synthesis': verdict})
        state_ = durable_review.call_one(state_, 'fake', lambda value: saves.append(deepcopy(value)), client=client)
        state_ = durable_review.process_response(state_)
        self.assertTrue(state_['result']['accepted']); self.assertEqual(len(state_['session']['calls']), 2)
        self.assertEqual(durable_review.process_response(deepcopy(state_)), state_)
        self.assertGreater(state_['session']['spent'], saves[1]['session']['spent'])
        self.assertEqual(client.post.call_args.kwargs['json']['messages'][1]['content'][0]['id'], 'read-1')

    def test_lost_paid_response_keeps_reservation_and_cannot_call_again(self):
        saved=[]; client=Mock(); client.post.side_effect=TimeoutError('paid response lost')
        with self.assertRaises(ScientificReviewUnavailable):
            durable_review.call_one(self.initial(), 'fake', lambda value: saved.append(deepcopy(value)), client=client)
        self.assertIsNotNone(saved[-1]['pending'])
        with self.assertRaises(ScientificReviewUnavailable):
            durable_review.call_one(saved[-1], 'fake', lambda value: saved.append(deepcopy(value)), client=client)
        self.assertEqual(client.post.call_count, 1)

    def test_paid_call_is_not_made_if_reservation_checkpoint_fails(self):
        client=Mock()
        with self.assertRaises(ScientificReviewUnavailable):
            durable_review.call_one(self.initial(), 'fake', Mock(side_effect=OSError('S3 down')), client=client)
        client.post.assert_not_called()

    def test_frozen_evidence_tamper_is_rejected(self):
        value=self.initial(); value['document']['claims'].append({'id': 'injected'})
        client=Mock()
        with self.assertRaises(ValueError): durable_review.call_one(value, 'fake', Mock(), client=client)
        client.post.assert_not_called()


if __name__ == '__main__': unittest.main()
