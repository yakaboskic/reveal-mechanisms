"""Snapshot readers must not join the worker's exclusive write lock."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.repository import Repository


class ReadTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Repository(str(Path(temporary.name) / 'application.sqlite'))
        self.repo.migrate()
        # WAL models Aurora's concurrent MVCC reader/writer behavior. This only
        # configures this isolated test database, not production write paths.
        connection = self.repo.connect()
        connection.execute('PRAGMA journal_mode=WAL')
        connection.close()
        with self.repo.transaction() as tx:
            tx.put('principal', 'owner', 'owner', {'retired': False})
            tx.put('job', 'job', 'owner', {'status': 'running'})

    def test_read_only_snapshot_keeps_authorization_and_job_consistent_while_writer_commits(self):
        def transfer():
            with self.repo.transaction() as tx:
                tx.put('principal', 'owner', 'owner', {'retired': True})
                tx.put('job', 'job', 'new-owner', {'status': 'succeeded'})
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.repo.read_transaction() as tx:
                self.assertFalse(tx.get('principal', 'owner')['data']['retired'])
                executor.submit(transfer).result(timeout=2)
                # Ownership and identity reads belong to the same old snapshot,
                # even though a concurrent transfer has already committed.
                self.assertFalse(tx.get('principal', 'owner')['data']['retired'])
                self.assertEqual(tx.get('job', 'job')['owner'], 'owner')
                self.assertEqual(tx.get('job', 'job')['data']['status'], 'running')
        with self.repo.read_transaction() as tx:
            self.assertTrue(tx.get('principal', 'owner')['data']['retired'])
            self.assertEqual(tx.get('job', 'job')['owner'], 'new-owner')

    def test_read_path_rejects_accidental_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            with self.repo.read_transaction() as tx:
                tx.put('job', 'job', 'owner', {'status': 'changed'})
        with self.repo.read_transaction() as tx:
            self.assertEqual(tx.get('job', 'job')['data']['status'], 'running')

    def test_mysql_read_path_requests_enforced_snapshot_without_global_mutex(self):
        class Connection:
            def __init__(self): self.statements=[]; self.committed=False; self.closed=False
            def cursor(self): return self
            def execute(self, sql, params=()): self.statements.append(sql)
            def commit(self): self.committed=True
            def rollback(self): raise AssertionError('Unexpected rollback')
            def close(self): self.closed=True
        connection=Connection()
        repository=Repository()
        with patch.object(repository, 'connect', return_value=connection):
            with repository.read_transaction() as tx:
                tx.execute('SELECT 1')
        self.assertEqual(connection.statements[:2], [
            'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',
            'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY'])
        self.assertNotIn('FOR UPDATE', ' '.join(connection.statements))
        self.assertTrue(connection.committed and connection.closed)
