"""Operator recovery cannot race an independent abandoned-Box deletion."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reveal_backend import jobs
from reveal_backend.repository import Repository, digest
from reveal_backend.workflow_admin import inspect_execution, resume


class WorkflowAdminRecoveryTests(unittest.TestCase):
    def test_abandoned_cleanup_cannot_resume_or_reset_the_audit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
                'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'admin-recovery-test'}):
            repository = Repository(Path(directory) / 'db.sqlite'); repository.migrate()
            with repository.transaction() as tx:
                job = jobs.enqueue(tx, 'owner', 'analysis', request_id='request')
                execution = tx.get('execution', job['id'])['data']
                execution.update(disposition='recovery_required', cleanup_abandoned=True,
                    cleanup_id='durable-cleanup', capacity_reserved=False, recoveries=3)
                tx.put('execution', job['id'], 'owner', execution)
            before = inspect_execution(repository, job['id'])
            with self.assertRaisesRegex(ValueError, 'Box cleanup owns'):
                resume(repository, job['id'], execution['generation'])
            self.assertEqual(inspect_execution(repository, job['id']), before)
            self.assertEqual(before['cleanup_id'], 'durable-cleanup')
            self.assertTrue(before['cleanup_abandoned'])

    def test_resume_resets_the_retry_cause_with_the_budget(self):
        from reveal_backend.workflow_routes import reconcile_stale
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
                'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'admin-recovery-test',
                'REVEAL_WORKFLOW_MAX_RECOVERIES': '3'}):
            repository = Repository(Path(directory) / 'db.sqlite'); repository.migrate()
            with repository.transaction() as tx:
                job = jobs.enqueue(tx, 'owner', 'analysis', request_id='request')
                execution = tx.get('execution', job['id'])['data']
                execution.update(disposition='retry', retry_cause='step_failure', recoveries=3, expected_at='',
                    scheduler_failure_at='2026-10-08T18:00:00Z', scheduler_failure_status=500,
                    sweep_hold={'error': 'StaleExecution', 'detail': 'Recovery cleanup binding changed'})
                tx.put('execution', job['id'], 'owner', execution)
                tx.remove('workflow_dispatch', job['id'])
            result = resume(repository, job['id'], execution['generation'])
            self.assertEqual((result['recoveries'], result['retry_cause'], result['scheduler_failure_at'],
                              result['scheduler_failure_status'], result['sweep_hold']), (0, None, None, None, None))
            with repository.read_transaction() as tx:
                audit = tx.get('workflow_recovery_audit', digest([job['id'], result['generation']]))['data']['previous_execution']
            self.assertEqual((audit['retry_cause'], audit['recoveries'], audit['scheduler_failure_status']), ('step_failure', 3, 500))
            self.assertEqual(audit['sweep_hold']['error'], 'StaleExecution')
            # The resumed run is never delivered: a delivery stall, which must not spend the fresh budget.
            with repository.transaction() as tx:
                tx.remove('workflow_dispatch', job['id'])
                row = tx.get('execution', job['id']); row['data']['expected_at'] = ''
                tx.put('execution', job['id'], row['owner'], row['data'])
            self.assertEqual(reconcile_stale(repository), 1)
            after = inspect_execution(repository, job['id'])
            self.assertEqual((after['recoveries'], after['delivery_recoveries'], after['disposition']), (0, 1, 'ready'))

    def test_resolved_terminal_allocation_transfers_to_cleanup_without_restarting_agent(self):
        from reveal_backend import workflow_state as state
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
                    'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'admin-recovery-test'}):
                repository = Repository(Path(directory) / 'db.sqlite'); repository.migrate()
                with repository.transaction() as tx:
                    job = jobs.enqueue(tx, 'owner', 'analysis', request_id='request')
                    if cancelled:
                        job['status'] = 'cancel_requested'; tx.put('job', job['id'], 'owner', job)
                    execution = tx.get('execution', job['id'])['data']
                    execution.update(creation_intent='ambiguous-allocation', capacity_reserved=True)
                    state.require_recovery(tx, 'owner', execution, 'Lost allocation response',
                        failure_code='WORKFLOW_ALLOCATION_AMBIGUOUS')
                generation = inspect_execution(repository, job['id'])['generation']
                result = resume(repository, job['id'], generation, recovered_box='verified-box')
                self.assertEqual(result['job_status'], 'cancelled' if cancelled else 'failed')
                self.assertTrue(result['cleanup_abandoned'])
                self.assertFalse(result['capacity_reserved'])
                self.assertEqual(result['generation'], generation + 1)
                with repository.read_transaction() as tx:
                    self.assertIsNone(tx.get('workflow_dispatch', job['id']))
                    audit = tx.get('workflow_recovery_audit', digest([job['id'], result['generation']]))['data']
                    self.assertEqual(audit['previous_execution']['diagnostic'], 'Lost allocation response')
                    self.assertEqual(audit['previous_execution']['generation'], generation)
                    self.assertTrue(audit['cleanup_only'])
                    self.assertEqual(tx.get('workflow_cleanup', result['cleanup_id'])['data']['box_id'], 'verified-box')
