"""A terminal workflow error retains the exact failing gate before scratch exits."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from reveal_backend import jobs, workflow_state as state
from reveal_backend.agent_execution import ExecutionRequest, ExecutionResult
from reveal_backend.artifact_store import StorageUnavailable
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.repository import Repository, digest
from reveal_backend.scientific_account_lint import AccountValidationError
from reveal_backend.scientific_grounding import ScientificReviewUnavailable
from reveal_backend.workflow_execution import WorkflowExecution, capture_message
from test_durable_workflow import MemoryStore


class DiagnosticStore(MemoryStore):
    def replace_workspace_files(self, reference, updates):
        if self.fail: raise OSError('storage unavailable')
        files = {**self.values[reference['sha256']], **{name: raw.hex() for name, raw in updates.items()}}
        key = digest(files); self.values[key] = files
        return {'sha256': key, 'store': reference['store']}


class WorkflowFailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.repo = Repository(Path(self.temporary.name)/'db.sqlite'); self.repo.migrate()
        environment = patch.dict('os.environ', {'REVEAL_JOB_TRANSPORT': 'workflow',
            'REVEAL_JOB_NAMESPACE': 'diagnostics-test', 'REVEAL_ENVIRONMENT': 'test',
            'QSTASH_TOKEN': 'test-qstash-token', 'QSTASH_CURRENT_SIGNING_KEY': 'test-current',
            'QSTASH_NEXT_SIGNING_KEY': 'test-next',
            'REVEAL_WORKFLOW_URL': 'http://127.0.0.1:18001/internal/workflows/research-v1',
            'ANTHROPIC_API_KEY': 'test-sensitive-provider-key'})
        environment.start(); self.addCleanup(environment.stop)
        self.store = DiagnosticStore(); self.engine = WorkflowExecution(self.repo, storage=self.store)

    def seed(self, phase='validate', *, remote=False):
        with self.repo.transaction() as tx:
            job = jobs.enqueue(tx, 'owner', 'analysis', request_id='request')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root/'input.json').write_bytes(b'{}')
            output = root/'attempt-2/output/output'; output.mkdir(parents=True)
            (output/'account-1.json').write_bytes(b'{"original":"draft"}')
            reference = self.store.snapshot(root)
        if remote: reference['store'] = 's3'
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']
            execution.update(phase=phase, authoring_attempt=2, workspace=reference)
            tx.put('execution', job['id'], 'owner', execution)
            queue = tx.get('queue', job['id'])['data']
            queue.update(workspace=reference, dispatch_input={'model': 'test-model'})
            tx.put('queue', job['id'], 'owner', queue)
        return job, {'job_id': job['id'], 'generation': execution['generation'], 'namespace': 'diagnostics-test'}

    def saved(self, job):
        with self.repo.read_transaction() as tx:
            current = tx.get('job', job['id'])['data']
            execution = tx.get('execution', job['id'])['data']
            queue = tx.get('queue', job['id'])['data']
        files = {name: bytes.fromhex(raw) for name, raw in self.store.values[queue['workspace']['sha256']].items()}
        return current, execution, files

    async def test_final_lint_findings_and_assembled_file_survive_scratch_cleanup(self):
        job, payload = self.seed()
        report = {'valid': False, 'findings': [{'severity': 'error', 'check': 'source-file',
            'where': 'evidence', 'message': 'Uncaptured File; test-sensitive-provider-key'}]}
        scratch = []
        async def validate(payload, token, job, execution, root):
            scratch.append(root)
            _, queue, current = self.engine.context(payload)
            request = ExecutionRequest(job_id=job['id'], attempt=2, kind='research',
                input_path=root/'input.json', output_dir=root/'attempt-2/output', selected_graphs=())
            result = ExecutionResult('succeeded', request.output_dir,
                account_paths=(request.output_dir/'output/account-1.json',), ledger_manifest_path=request.output_dir/'ledger/manifest.json')
            def assemble(raw, package, target, *args):
                target.write_bytes(b'{"assembled":"draft"}')
                raise AccountValidationError(report)
            with (patch.object(self.engine, 'verified_capture', return_value={}),
                  patch('reveal_backend.workflow_execution.captured_result', return_value=result),
                  patch('reveal_backend.workflow_execution.validate_execution_ledger'),
                  patch('reveal_backend.workflow_execution.read_preparation_inputs', return_value=({'attribution': {}}, {})),
                  patch('reveal_backend.workflow_execution.assemble_account', side_effect=assemble)):
                return await self.engine.validate(payload, token, job, queue, current, root, request, {})
        with patch.object(self.engine, 'operate', side_effect=validate):
            result = await self.engine.step(payload, 0)
        current, _, files = self.saved(job)
        self.assertTrue(result['done']); self.assertFalse(scratch[0].exists())
        self.assertEqual(current['failure']['code'], 'VALIDATION_FAILED')
        diagnostic = json.loads(files['attempt-2/failure.json'])
        self.assertEqual(diagnostic['phase'], 'validate')
        self.assertEqual(diagnostic['error_type'], 'AccountValidationError')
        findings = json.loads(files['attempt-2/validation-1.json'])
        self.assertEqual(findings['findings'][0]['check'], 'source-file')
        self.assertNotIn(b'test-sensitive-provider-key', files['attempt-2/validation-1.json'])
        self.assertEqual(files['validated/account-0.json'], b'{"assembled":"draft"}')
        self.assertEqual(files['attempt-2/output/output/account-1.json'], b'{"original":"draft"}')
        # Admin telemetry keeps the cause the public message does not name, without credentials.
        with self.repo.read_transaction() as tx:
            metrics = tx.get('job_metrics', job['id'])['data']
        self.assertEqual((metrics['error']['error_type'], metrics['error']['phase']), ('AccountValidationError', 'validate'))
        self.assertNotIn('test-sensitive-provider-key', json.dumps(metrics))

    async def test_budget_stop_records_the_runs_cost_for_admin_telemetry(self):
        job, payload = self.seed()
        async def validate(payload, token, job, execution, root):
            _, queue, current = self.engine.context(payload)
            request = ExecutionRequest(job_id=job['id'], attempt=2, kind='research', max_budget_usd=3,
                input_path=root/'input.json', output_dir=root/'attempt-2/output', selected_graphs=())
            runtime = request.output_dir/'runtime.json'
            runtime.write_text(json.dumps({'model': 'test-model', 'execution_limits': {'max_budget_usd': 3},
                'completion': {'status': 'failed', 'provider_result_subtype': 'error_max_budget_usd',
                               'cost_usd': 3.1378, 'max_budget_usd': 3, 'turns_used': 38}}))
            result = ExecutionResult('failed', request.output_dir, runtime_manifest_path=runtime, reason='Budget reached.')
            with (patch.object(self.engine, 'verified_capture', return_value={}),
                  patch('reveal_backend.workflow_execution.captured_result', return_value=result)):
                return await self.engine.validate(payload, token, job, queue, current, root, request, {})
        with patch.object(self.engine, 'operate', side_effect=validate):
            self.assertTrue((await self.engine.step(payload, 0))['done'])
        with self.repo.read_transaction() as tx:
            current = tx.get('job', job['id'])['data']; metrics = tx.get('job_metrics', job['id'])['data']
        self.assertEqual(current['failure']['code'], 'AUTHORING_BUDGET_EXCEEDED')
        self.assertEqual(current['failure']['budget']['spent_usd'], 3.1378)
        self.assertEqual((metrics['agent']['cost_usd'], metrics['agent']['turns'], metrics['agent']['budget_used']), (3.1378, 38, 1.0459))
        self.assertNotIn('error', metrics)

    async def test_agent_timeout_never_announces_scientific_validation(self):
        job, payload = self.seed()
        reason = 'Agent reached its 900-second execution limit while waiting for a durable evidence operation. No output was accepted.'
        async def validate(payload, token, job, execution, root):
            _, queue, current = self.engine.context(payload)
            request = ExecutionRequest(job_id=job['id'], attempt=2, kind='research',
                input_path=root/'input.json', output_dir=root/'attempt-2/output', selected_graphs=())
            result = ExecutionResult('failed', request.output_dir, reason=reason)
            with (patch.object(self.engine, 'verified_capture', return_value={}),
                  patch('reveal_backend.workflow_execution.captured_result', return_value=result),
                  patch('reveal_backend.workflow_execution.validate_execution_ledger', side_effect=AssertionError('Failed authoring cannot validate'))):
                return await self.engine.validate(payload, token, job, queue, current, root, request, {})
        with patch.object(self.engine, 'operate', side_effect=validate):
            result = await self.engine.step(payload, 0)
        current, _, _ = self.saved(job)
        self.assertTrue(result['done'])
        self.assertEqual(current['stage'], 'authoring_account')
        self.assertEqual(current['failure']['message'], reason)
        with self.repo.read_transaction() as tx:
            events = [row['data'] for row in tx.list('event') if row['data']['job_id'] == job['id']]
        self.assertNotIn('validating', [event['stage'] for event in events])
        self.assertTrue(any(event['stage'] == 'collecting_output' for event in events))

    async def test_known_agent_failure_is_reported_before_capture_and_survives_capture_retry(self):
        job, payload = self.seed('observe')
        handle = {'box_id': 'timed-out-box', 'job_id': job['id'], 'attempt': 2,
                  'phase': 'running', 'cursor': 0, 'capture_protocol': 's3-v1'}
        with self.repo.transaction() as tx:
            execution = tx.get('execution', job['id'])['data']
            execution.update(box=handle, capacity_reserved=True)
            tx.put('execution', job['id'], 'owner', execution)
            queue = tx.get('queue', job['id'])['data']
            queue['dispatch_input'].update(sha256='a'*64, selected_graphs=[])
            tx.put('queue', job['id'], 'owner', queue)
            current = tx.get('job', job['id'])['data']; current['status'] = 'running'
            tx.put('job', job['id'], 'owner', current)
        terminal = {**handle, 'phase': 'terminal', 'state': {'status': 'failed',
                    'reason': 'Agent execution limit reached; PRIVATE_PROVIDER_DETAIL'}}
        adapter = Mock()
        adapter.inspect_once = AsyncMock(return_value=(terminal, [], True))
        adapter.capture_to_store = AsyncMock(side_effect=StorageUnavailable('retry storage'))
        self.engine.adapter = adapter
        first = await self.engine.step(payload, 0)
        self.assertEqual(first['phase'], 'capture')
        current, execution, files = self.saved(job)
        self.assertEqual(current['status'], 'running')  # Finalization still requires durable preservation.
        self.assertIsNone(current['failure'])
        self.assertTrue(execution['capacity_reserved'])
        self.assertEqual(execution['failure_notice_attempt'], 2)
        with self.repo.read_transaction() as tx:
            events = [r['data'] for r in tx.list('event') if r['data']['job_id'] == job['id']]
        failures = [e for e in events if (e.get('detail') or {}).get('state') == 'failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['stage'], 'authoring_account')
        self.assertNotIn('PRIVATE_PROVIDER_DETAIL', json.dumps(events))
        # A lost acknowledgement does not invent a second failed agent attempt.
        self.assertEqual(await self.engine.step(payload, 0), first)
        with self.assertRaises(StorageUnavailable): await self.engine.step(payload, 1)
        current, execution, after = self.saved(job)
        self.assertEqual(current['status'], 'running')
        self.assertEqual(after, files)
        self.assertTrue(execution['capacity_reserved'])
        adapter.delete_once.assert_not_called()
        with self.repo.read_transaction() as tx:
            events = [r['data'] for r in tx.list('event') if r['data']['job_id'] == job['id']]
        self.assertEqual(sum((e.get('detail') or {}).get('state') == 'failed' for e in events), 1)
        preservation = next(e for e in events if e['stage'] == 'collecting_output')
        self.assertIn('partial output and diagnostics', preservation['message'])
        self.assertLess(int(failures[0]['id']), int(preservation['id']))

    def test_capture_messages_never_claim_a_completed_scientific_result(self):
        for status in ('failed', 'cancelled', 'succeeded', 'insufficient_evidence'):
            for restoring in (False, True):
                with self.subTest(status=status, restoring=restoring):
                    message = capture_message({'box': {'state': {'status': status}}}, restoring=restoring)
                    self.assertNotIn('completed', message)
                    self.assertIn('partial output', message) if status in ('failed', 'cancelled') else self.assertIn('validation', message)

    async def test_diagnostic_message_is_redacted_then_bounded_and_phase_is_classified(self):
        for phase, error, code in (
                ('prepare', ValueError('source preparation'), 'EVIDENCE_PREPARATION_FAILED'),
                ('launch', ValueError('provider configuration'), 'WORKER_FAILED'),
                ('validate', RuntimeError('validation implementation fault'), 'WORKER_FAILED'),
                ('validate', EvidenceBuildError('source payload changed'), 'VALIDATION_FAILED')):
            with self.subTest(phase=phase, error=type(error).__name__):
                job, payload = self.seed(phase)
                error.args = (str(error)+' test-sensitive-provider-key Bearer abc123 token=secret-value '+'x'*4000,)
                with patch.object(self.engine, 'operate', AsyncMock(side_effect=error)):
                    await self.engine.step(payload, 0)
                current, _, files = self.saved(job)
                self.assertEqual(current['failure']['code'], code)
                diagnostic = json.loads(files['attempt-2/failure.json'])
                self.assertEqual(diagnostic['phase'], phase)
                self.assertEqual(diagnostic['error_type'], type(error).__name__)
                self.assertEqual(len(diagnostic['message']), 3000)
                self.assertIn('[redacted]', diagnostic['message'])
                for secret in ('test-sensitive-provider-key', 'abc123', 'secret-value'):
                    self.assertNotIn(secret, diagnostic['message'])

    async def test_diagnostic_storage_failure_retries_same_phase_without_terminal_rejection(self):
        job, payload = self.seed(); self.store.fail = True
        with (patch.object(self.engine, 'operate', AsyncMock(side_effect=EvidenceBuildError('missing evidence'))),
              self.assertRaises(StorageUnavailable)):
            await self.engine.step(payload, 0)
        current, execution, _ = self.saved(job)
        self.assertNotEqual(current['status'], 'failed')
        self.assertIsNone(current.get('failure'))
        self.assertEqual(execution['phase'], 'validate'); self.assertIsNone(execution['fence'])
        self.store.fail = False
        with patch.object(self.engine, 'operate', AsyncMock(side_effect=EvidenceBuildError('missing evidence'))):
            await self.engine.step(payload, 0)
        self.assertEqual(self.saved(job)[0]['failure']['code'], 'VALIDATION_FAILED')

    async def test_review_failure_merges_latest_paid_response_without_restoring_scratch(self):
        job, payload = self.seed('review_call', remote=True)
        async def review(payload, token, job, execution, root):
            self.assertIsNone(root)
            reference = self.store.replace_workspace_files(execution['workspace'], {'review/latest.json': b'{"paid_response":true}'})
            self.engine.commit_checkpoint(payload, token, reference)
            raise ScientificReviewUnavailable('Reviewer did not produce a verdict')
        with (patch.object(self.engine, 'operate', side_effect=review),
              patch.object(self.engine, 'restore_workspace', AsyncMock(side_effect=AssertionError('No scratch restore')))):
            await self.engine.step(payload, 0)
        current, _, files = self.saved(job)
        self.assertEqual(current['failure']['code'], 'REVIEW_UNAVAILABLE')
        self.assertEqual(files['review/latest.json'], b'{"paid_response":true}')
        self.assertEqual(json.loads(files['attempt-2/failure.json'])['phase'], 'review_call')

    async def test_diagnostic_checkpoint_obeys_lost_fence(self):
        job, payload = self.seed()
        with (patch.object(self.engine, 'operate', AsyncMock(side_effect=EvidenceBuildError('invalid source'))),
              patch.object(self.engine, 'checkpoint', side_effect=state.StaleExecution('fence lost')),
              self.assertRaises(state.StaleExecution)):
            await self.engine.step(payload, 0)
        current, _, files = self.saved(job)
        self.assertNotEqual(current['status'], 'failed')
        self.assertNotIn('attempt-2/failure.json', files)

    async def test_failed_restore_never_replaces_original_workspace_with_partial_files(self):
        job, payload = self.seed()
        async def partial_restore(reference, root):
            (root/'partial.txt').write_text('incomplete')
            raise ValueError('Invalid workspace entry')
        with patch.object(self.engine, 'restore_workspace', side_effect=partial_restore):
            await self.engine.step(payload, 0)
        current, _, files = self.saved(job)
        self.assertEqual(current['failure']['code'], 'WORKER_FAILED')
        self.assertEqual(files['attempt-2/output/output/account-1.json'], b'{"original":"draft"}')
        self.assertNotIn('partial.txt', files)
        self.assertEqual(json.loads(files['attempt-2/failure.json'])['message'], 'Invalid workspace entry')

    async def test_review_audit_retains_only_bounded_redacted_diagnostic_scalars(self):
        job, payload = self.seed('review_tools', remote=True)
        error = ScientificReviewUnavailable('Invalid review response', {
            'response_error_type': 'ValidationError test-sensitive-provider-key '+'x'*1000,
            'actual_cost_usd': .125, 'configured_max_usd': 3,
            'blocked_call': {'reason': 'budget token=private-value '+'y'*1000,
                'limit': 100, 'observed': 101, 'reserved_max_usd': .2,
                'authorization': 'Bearer private-secret', 'response': {'private': 'provider text'}},
            'calls': [{'response': 'private-provider-response'}],
            'headers': {'authorization': 'Bearer private-secret'},
            'ambiguous_call': {'secret': 'private-reservation'}})
        with patch.object(self.engine, 'operate', AsyncMock(side_effect=error)):
            await self.engine.step(payload, 0)
        _, _, files = self.saved(job); raw = files['attempt-2/failure.json']
        audit = json.loads(raw)['review_audit']
        self.assertEqual(set(audit), {'response_error_type', 'actual_cost_usd', 'configured_max_usd', 'blocked_call'})
        self.assertEqual(audit['actual_cost_usd'], .125)
        self.assertEqual(audit['configured_max_usd'], 3)
        self.assertEqual(len(audit['response_error_type']), 160)
        self.assertEqual(set(audit['blocked_call']), {'reason', 'limit', 'observed', 'reserved_max_usd'})
        self.assertEqual(len(audit['blocked_call']['reason']), 160)
        self.assertIn('[redacted]', audit['response_error_type'])
        self.assertIn('[redacted]', audit['blocked_call']['reason'])
        self.assertLess(len(raw), 128*1024)
        for secret in (b'test-sensitive-provider-key', b'private-value', b'private-provider-response', b'private-secret', b'private-reservation'):
            self.assertNotIn(secret, raw)

    async def test_review_audit_omits_nested_nonfinite_and_unbounded_numeric_values(self):
        job, payload = self.seed('review_tools', remote=True)
        error = ScientificReviewUnavailable('Invalid review response', {
            'response_error_type': {'private': 'nested'}, 'actual_cost_usd': float('nan'),
            'configured_max_usd': float('inf'),
            'blocked_call': {'reason': ['private'], 'limit': 10**5000, 'observed': True, 'reserved_max_usd': -1}})
        with patch.object(self.engine, 'operate', AsyncMock(side_effect=error)), patch(
                'reveal_backend.job_failures.review_failure', return_value={'code': 'REVIEW_UNAVAILABLE', 'message': 'Unavailable', 'retryable': True}):
            await self.engine.step(payload, 0)
        _, _, files = self.saved(job)
        self.assertNotIn('review_audit', json.loads(files['attempt-2/failure.json']))
