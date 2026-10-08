"""Capacity reservations keep one fenced transaction and a constant SQL budget."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import jobs, workflow_state as state
from reveal_backend.repository import Repository, Transaction


class CapacityRoundTripBudgetTests(unittest.TestCase):
    def test_reserve_does_not_fetch_historical_execution_or_cleanup_payloads(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {
                'REVEAL_JOB_TRANSPORT': 'workflow', 'REVEAL_JOB_NAMESPACE': 'budget-test'}):
            repo = Repository(Path(temp)/'db.sqlite'); repo.migrate()
            with repo.transaction() as tx:
                job = jobs.enqueue(tx, 'owner', 'paragraph', account_id='account')
                value = tx.get('execution', job['id'])['data']; value['phase'] = 'create'
                tx.put('execution', job['id'], 'owner', value)
                for index in range(500):
                    tx.put('execution', 'historical-'+str(index), 'owner', {
                        'namespace': 'budget-test', 'capacity_reserved': False, 'private_history': 'x'*4000})
                    tx.put('workflow_cleanup_completed', 'historical-'+str(index), 'owner', {
                        'namespace': 'budget-test', 'private_history': 'x'*4000})
            payload = {'job_id': job['id'], 'namespace': 'budget-test', 'generation': 1}
            _, active, _ = state.acquire(repo, payload, 0)
            statements = []; original = Transaction.execute
            def execute(tx, sql, params=()):
                statements.append(sql)
                return original(tx, sql, params)
            with patch.object(Transaction, 'execute', execute), patch.object(Transaction, 'list',
                    side_effect=AssertionError('Capacity must not fetch full-kind JSON history')):
                state.reserve_box(repo, payload, active['fence'])
            # Four SQL statements, fence acquisition and COMMIT, plus this base's
            # three warm-pool release commands: RESET, select_db and session SET.
            # Cold connection/TLS setup is separate from the reservation budget.
            self.assertEqual(len(statements), 4, statements)
            self.assertEqual(sum('SUM(active)' in sql for sql in statements), 1)
            self.assertFalse(any('ORDER BY' in sql for sql in statements))
            self.assertEqual(len(statements)+2+3, 9)
