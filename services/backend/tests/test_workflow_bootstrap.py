"""Frozen bootstrap survives replacement without a second API workspace copy."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reveal_backend import jobs, workflow_state as state
from reveal_backend.box_lifecycle import BoxLifecycle
from reveal_backend.evidence_package import canonical_json
from reveal_backend.repository import Repository, Transaction, digest
from reveal_backend.runtime_config import ROOT
from reveal_backend.workflow_execution import WorkflowExecution
from test_durable_workflow import MemoryStore


class BootstrapStore(MemoryStore):
    def __init__(self):
        super().__init__(); self.objects = {}; self.puts = 0

    def put(self, content, content_type):
        self.puts += 1
        checksum = hashlib.sha256(content).hexdigest()
        self.objects[checksum] = content
        return {'store': 's3', 'bucket': 'offline-bootstrap-test',
            'key': 'test/artifacts/sha256/' + checksum[:2] + '/' + checksum,
            'version_id': 'immutable-' + checksum, 'sha256': checksum,
            'size_bytes': len(content), 'content_type': content_type}

    def replace_workspace_files(self, reference, updates):
        files = {**self.values[reference['sha256']], **{name: raw.hex() for name, raw in updates.items()}}
        key = digest(files); self.values[key] = files
        return {'sha256': key, 'store': reference['store']}


class BootstrapWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'db.sqlite'); self.repo.migrate()
        self.env = patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'workflow',
            'REVEAL_JOB_NAMESPACE': 'bootstrap-test', 'REVEAL_ENVIRONMENT': 'test',
            'REVEAL_EXECUTION_MODE': 'box', 'REVEAL_AGENT_TIMEOUT_SECONDS': '120',
            'REVEAL_AGENT_MAX_BUDGET_USD': '0.75', 'REVEAL_AGENT_MAX_TURNS': '12'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = BootstrapStore()
        self.adapter = BoxLifecycle(ROOT, environ={'REVEAL_CLAUDE_MODEL': 'frozen-model'})
        self.adapter.freeze_bootstrap = Mock(wraps=self.adapter.freeze_bootstrap)
        self.adapter.create_once = AsyncMock(side_effect=lambda job, attempt: {
            'box_id': 'assigned-box', 'job_id': job, 'attempt': attempt, 'phase': 'created'})
        self.adapter.prepare_from_store = AsyncMock(side_effect=lambda descriptor, box, store: {
            **box, 'phase': 'prepared', 'capture_protocol': 's3-v1'})
        self.adapter.prepare_once = AsyncMock(side_effect=lambda request, box: {**box, 'phase': 'prepared'})
        self.adapter.launch_once = AsyncMock(side_effect=lambda box: {**box, 'phase': 'running'})
        self.adapter.delete_once = AsyncMock(side_effect=lambda box: {**box, 'phase': 'deleted'})
        self.source = patch('reveal_backend.workflow_execution.read_preparation_inputs',
            return_value={'result': {'citation_metadata': [], 'document': {'@id': 'account'}}})
        self.source.start(); self.addCleanup(self.source.stop)
        self.model = patch('reveal_backend.workflow_execution.setting', side_effect=self.settings)
        self.model.start(); self.addCleanup(self.model.stop)
        self.engine = WorkflowExecution(self.repo, storage=self.store, adapter=self.adapter)

    @staticmethod
    def settings(name, default=None):
        from reveal_backend.runtime_config import setting
        return 'frozen-model' if name == 'REVEAL_CLAUDE_MODEL' else setting(name, default)

    def new(self):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner', 'paragraph', account_id='account')
        return {'job_id': job['id'], 'generation': 1, 'namespace': 'bootstrap-test'}

    def rows(self, payload):
        with self.repo.read_transaction() as tx:
            return {kind: tx.get(kind, payload['job_id'])['data'] for kind in ('job', 'queue', 'execution')}

    async def prepared_input(self):
        payload = self.new()
        result = await self.engine.step(payload, 0)
        self.assertEqual(result['phase'], 'create')
        return payload, result

    async def assigned(self):
        payload, result = await self.prepared_input()
        result = await self.engine.step(payload, result['index'])
        self.assertEqual(result['phase'], 'bootstrap')
        return payload, result

    async def test_prepare_persists_bundle_and_exact_config_before_any_box_allocation(self):
        payload, result = await self.prepared_input()
        rows = self.rows(payload); descriptor = rows['queue']['dispatch_input']
        self.assertEqual(descriptor, rows['execution']['dispatch_input'])
        bootstrap = descriptor['bootstrap']; config = bootstrap['config']
        content = self.store.objects[bootstrap['bundle']['sha256']]
        self.assertEqual(hashlib.sha256(content).hexdigest(), bootstrap['bundle']['sha256'])
        self.assertEqual(config['job_id'], payload['job_id']); self.assertEqual(config['attempt'], 1)
        self.assertEqual(config['input_sha256'], descriptor['sha256'])
        self.assertEqual(config['model'], 'frozen-model'); self.assertEqual(config['max_budget_usd'], .75)
        self.assertEqual(config['timeout_seconds'], 120); self.assertEqual(config['max_turns'], 12)
        self.assertEqual(config['validation_feedback'], []); self.assertEqual(config['selected_graphs'], [])
        self.assertEqual(rows['execution']['workspace'], rows['queue']['workspace'])
        self.adapter.create_once.assert_not_awaited(); self.assertEqual(self.store.puts, 1)
        self.assertEqual(await self.engine.step(payload, 0), result)
        self.assertEqual(self.adapter.freeze_bootstrap.call_count, 1)

    async def test_lost_prepare_ack_reuses_committed_bundle_after_environment_change(self):
        payload = self.new()
        with patch('reveal_backend.workflow_state.complete', side_effect=OSError('lost prepare acknowledgment')):
            with self.assertRaises(OSError): await self.engine.step(payload, 0)
        saved = deepcopy(self.rows(payload))
        with patch.dict('os.environ', {'REVEAL_AGENT_TIMEOUT_SECONDS': '999',
                'REVEAL_AGENT_MAX_BUDGET_USD': '9', 'REVEAL_AGENT_MAX_TURNS': '99'}):
            result = await self.engine.step(payload, 0)
        self.assertEqual(result['phase'], 'create'); self.assertEqual(self.store.puts, 1)
        self.assertEqual(self.adapter.freeze_bootstrap.call_count, 1)
        self.assertEqual(self.rows(payload)['queue']['dispatch_input'], saved['queue']['dispatch_input'])
        self.assertEqual(self.rows(payload)['execution']['workspace'], saved['execution']['workspace'])
        self.adapter.create_once.assert_not_awaited()

    async def test_upload_or_checkpoint_failure_never_authorizes_box_allocation(self):
        payload = self.new()
        with patch.object(self.store, 'put', side_effect=OSError('S3 unavailable')):
            with self.assertRaises(OSError): await self.engine.step(payload, 0)
        rows = self.rows(payload)
        self.assertNotIn('dispatch_input', rows['queue']); self.assertEqual(rows['execution']['phase'], 'prepare')
        self.adapter.create_once.assert_not_awaited()
        original = Transaction.put
        def fail_commit(tx, kind, identity, owner, data, expected=None):
            if kind == 'queue' and data.get('dispatch_input'): raise OSError('checkpoint commit lost')
            return original(tx, kind, identity, owner, data, expected)
        with patch.object(Transaction, 'put', fail_commit):
            with self.assertRaises(OSError): await self.engine.step(payload, 0)
        rows = self.rows(payload)
        self.assertNotIn('dispatch_input', rows['queue']); self.assertNotIn('dispatch_input', rows['execution'])
        self.assertIsNone(rows['execution']['workspace']); self.adapter.create_once.assert_not_awaited()

    async def test_invalid_scientific_bundle_retains_diagnostics_without_bootstrap_upload_or_box_allocation(self):
        with self.repo.transaction() as tx: job = jobs.enqueue(tx, 'owner', 'analysis', request_id='request')
        payload = {'job_id': job['id'], 'generation': 1, 'namespace': 'bootstrap-test'}
        incomplete = {'external_evidence': {'selected_graphs': []}}
        def collect(_job, _frozen, _binding, _budgets, directory):
            directory.mkdir(); path = directory / 'evidence-package.json'
            path.write_bytes(canonical_json(incomplete))
            return path, incomplete
        with (patch('reveal_backend.workflow_execution.read_preparation_inputs', return_value=({}, {})),
              patch('reveal_backend.workflow_execution.collect', side_effect=collect),
              patch('reveal_backend.workflow_execution.fit_input_budget', side_effect=lambda path, mode, budget: (path, incomplete, {}))):
            result = await self.engine.step(payload, 0)
        rows = self.rows(payload)
        self.assertTrue(result['done']); self.assertEqual(rows['job']['failure']['code'], 'EVIDENCE_PREPARATION_FAILED')
        self.assertNotIn('dispatch_input', rows['queue'])
        saved = self.store.values[rows['execution']['workspace']['sha256']]
        diagnostic = json.loads(bytes.fromhex(saved['attempt-1/failure.json']))
        self.assertEqual(diagnostic['phase'], 'prepare')
        self.assertEqual(diagnostic['error_type'], 'ValidationError')
        self.assertEqual(self.store.puts, 0); self.adapter.create_once.assert_not_awaited()

    async def test_bootstrap_uses_saved_bundle_without_scratch_and_keeps_frozen_launch_limits(self):
        payload, result = await self.assigned(); saved = deepcopy(self.rows(payload))
        descriptor = saved['queue']['dispatch_input']['bootstrap']
        with (patch('reveal_backend.workflow_execution.tempfile.TemporaryDirectory', side_effect=AssertionError('no bootstrap scratch')),
              patch.object(self.engine, 'request', side_effect=AssertionError('no API input materialization')),
              patch.object(self.store, 'restore', side_effect=AssertionError('no workspace restore')),
              patch.object(self.store, 'snapshot', side_effect=AssertionError('no workspace snapshot')),
              patch.dict('os.environ', {'REVEAL_AGENT_TIMEOUT_SECONDS': '999',
                'REVEAL_AGENT_MAX_BUDGET_USD': '9', 'REVEAL_AGENT_MAX_TURNS': '99'})):
            result = await self.engine.step(payload, result['index'])
            self.assertEqual(result['phase'], 'launch')
            with patch('reveal_backend.workflow_execution.time.time', return_value=1000):
                await self.engine.step(payload, result['index'])
        self.adapter.prepare_once.assert_not_awaited()
        self.assertEqual(self.adapter.prepare_from_store.call_args.args, (
            descriptor, saved['execution']['box'], self.store))
        rows = self.rows(payload)
        self.assertEqual(rows['execution']['workspace'], saved['execution']['workspace'])
        self.assertEqual(rows['execution']['deadline'], 1540)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.store.restore(rows['execution']['workspace'], root)
            with patch.dict('os.environ', {'REVEAL_AGENT_TIMEOUT_SECONDS': '999',
                    'REVEAL_AGENT_MAX_BUDGET_USD': '9', 'REVEAL_AGENT_MAX_TURNS': '99'}):
                request, _ = self.engine.request(rows['job'], rows['queue'], rows['execution'], root)
        self.assertEqual((request.timeout_seconds, request.max_budget_usd, request.max_turns), (120, .75, 12))

    async def test_lost_bootstrap_commit_retries_same_descriptor_and_lost_phase_ack_skips_remote(self):
        payload, result = await self.assigned(); saved = deepcopy(self.rows(payload))
        with patch.object(self.engine, 'commit_checkpoint', side_effect=OSError('lost bootstrap checkpoint')):
            with self.assertRaises(OSError): await self.engine.step(payload, result['index'])
        with patch('reveal_backend.workflow_state.complete', side_effect=OSError('lost phase acknowledgment')):
            with self.assertRaises(OSError): await self.engine.step(payload, result['index'])
        final = await self.engine.step(payload, result['index'])
        self.assertEqual(final['phase'], 'launch')
        self.assertEqual(self.adapter.prepare_from_store.await_count, 2)
        first, second = self.adapter.prepare_from_store.call_args_list
        self.assertEqual(first.args[:2], second.args[:2])
        self.assertEqual(first.args[0], saved['queue']['dispatch_input']['bootstrap'])
        self.assertEqual(self.adapter.freeze_bootstrap.call_count, 1)
        self.adapter.launch_once.assert_not_awaited()

    async def test_cancel_before_or_during_bootstrap_never_launches(self):
        for during in (False, True):
            with self.subTest(during=during):
                payload, result = await self.assigned()
                def cancel():
                    with self.repo.transaction() as tx: jobs.cancel(tx, tx.get('job', payload['job_id'])['data'])
                if during:
                    async def prepare(descriptor, box, storage):
                        cancel()
                        return {**box, 'phase': 'prepared'}
                    self.adapter.prepare_from_store.side_effect = prepare
                else: cancel()
                with patch('reveal_backend.workflow_execution.tempfile.TemporaryDirectory', side_effect=AssertionError('no prelaunch scratch')):
                    result = await self.engine.step(payload, result['index'])
                    if result['phase'] == 'launch': result = await self.engine.step(payload, result['index'])
                    self.assertEqual(result['phase'], 'cleanup')
                    await self.engine.step(payload, result['index'])
                self.assertEqual(self.rows(payload)['job']['status'], 'cancelled')
                self.adapter.launch_once.assert_not_awaited()
                if not during: self.adapter.prepare_from_store.assert_not_awaited()

    async def test_legacy_descriptor_keeps_existing_restore_and_prepare_path(self):
        payload, result = await self.assigned()
        with self.repo.transaction() as tx:
            for kind in ('queue', 'execution'):
                row = tx.get(kind, payload['job_id']); row['data']['dispatch_input'].pop('bootstrap')
                tx.put(kind, payload['job_id'], row['owner'], row['data'])
        with patch.object(self.store, 'restore', wraps=self.store.restore) as restore:
            result = await self.engine.step(payload, result['index'])
        self.assertEqual(result['phase'], 'launch'); restore.assert_called_once()
        self.adapter.prepare_once.assert_awaited_once(); self.adapter.prepare_from_store.assert_not_awaited()

    async def test_metadata_bootstrap_does_not_consume_scratch_capacity(self):
        payload, result = await self.assigned(); other = self.new()
        with patch.dict('os.environ', {'REVEAL_MAX_SCRATCH_STEPS': '1', 'REVEAL_MAX_PREPARATION_STEPS': '2'}):
            state.acquire(self.repo, other, 0)
            result = await self.engine.step(payload, result['index'])
        self.assertEqual(result['phase'], 'launch'); self.adapter.prepare_from_store.assert_awaited_once()

    async def test_metadata_bootstrap_rejects_rebound_config_or_returned_box(self):
        for corrupt in ('config', 'box'):
            with self.subTest(corrupt=corrupt):
                payload, result = await self.assigned()
                if corrupt == 'config':
                    with self.repo.transaction() as tx:
                        for kind in ('queue', 'execution'):
                            row = tx.get(kind, payload['job_id'])
                            row['data']['dispatch_input']['bootstrap']['config']['input_sha256'] = '0' * 64
                            tx.put(kind, payload['job_id'], row['owner'], row['data'])
                else:
                    self.adapter.prepare_from_store.side_effect = lambda descriptor, box, storage: {
                        **box, 'box_id': 'different-box', 'phase': 'prepared'}
                with self.assertRaisesRegex(ValueError, 'differs from its dispatch binding|replace its assigned Box'):
                    await self.engine.step(payload, result['index'])
                rows = self.rows(payload)
                self.assertEqual(rows['execution']['box']['box_id'], 'assigned-box')
                self.assertEqual(rows['execution']['disposition'], 'recovery_required')
                self.assertTrue(rows['execution']['capacity_reserved'])
                self.adapter.launch_once.assert_not_awaited()
