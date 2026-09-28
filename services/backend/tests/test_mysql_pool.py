"""Exclusive bounded leases must never reuse unresolved or contaminated sessions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from reveal_backend.mysql_pool import Pool
from reveal_backend.mysql_database import reset_application_session
from reveal_backend.repository import Repository, Transaction
from reveal_backend import runtime_config


class FakeCursor:
    def __init__(self, connection): self.connection = connection; self.closed = False
    def execute(self, sql, params=()): self.connection.sql.append((sql, params)); return 1
    def fetchone(self): return (1,)
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class FakeConnection:
    def __init__(self):
        self.sql = []; self.closed = False; self.reset_count = 0; self.uncommitted = False
        self.variables = {}; self.database = 'cyaka_expected'; self.pid_close = False
        self.commit_error = self.rollback_error = self.reset_error = None
    def cursor(self): return FakeCursor(self)
    def commit(self):
        if self.commit_error: raise self.commit_error
        self.uncommitted = False
    def rollback(self):
        if self.rollback_error: raise self.rollback_error
        self.uncommitted = False
    def close(self): self.closed = True
    def _force_close(self): self.pid_close = True; self.closed = True
    def _execute_command(self, code, payload):
        if self.reset_error: raise self.reset_error
        assert code == 0x1F and payload == b''
        self.reset_count += 1; self.variables = {}; self.uncommitted = False
    def _read_ok_packet(self): pass
    def select_db(self, database): self.database = database


class PoolTests(unittest.TestCase):
    def setUp(self): self.created = []
    def factory(self):
        connection = FakeConnection(); self.created.append(connection); return connection
    def pool(self, **kwargs):
        pool = Pool(self.factory, lambda c: reset_application_session(c, 'cyaka_expected'), **kwargs)
        self.addCleanup(pool.close); return pool

    def test_reuse_resets_session_database_charset_and_closes_stale_cursors(self):
        pool = self.pool(); lease = pool.acquire(); cursor = lease.cursor(); execute = cursor.execute
        raw = self.created[0]; raw.variables['private'] = 'first borrower'; raw.database = 'cyaka_other'
        lease.commit(); lease.close(); lease.close()
        next_lease = pool.acquire()
        self.assertEqual(len(self.created), 1); self.assertEqual(raw.variables, {})
        self.assertEqual(raw.database, 'cyaka_expected'); self.assertEqual(raw.reset_count, 1)
        settings = raw.sql[-1][0]
        for term in ("autocommit=0", "REPEATABLE-READ", "time_zone='+00:00'", "character_set_client='utf8mb4'"):
            self.assertIn(term, settings)
        self.assertTrue(next_lease.reveal_session_defaults)
        with self.assertRaisesRegex(RuntimeError, 'no longer active'): execute('SELECT 1')
        with self.assertRaisesRegex(RuntimeError, 'no longer active'): lease.cursor()
        next_lease.rollback(); next_lease.close()

    def test_uncommitted_and_uncertain_commit_or_rollback_are_never_reused(self):
        for failure in ('uncommitted', 'commit', 'rollback'):
            with self.subTest(failure=failure):
                pool = self.pool(); lease = pool.acquire(); raw = self.created[-1]
                raw.uncommitted = True
                if failure != 'uncommitted':
                    setattr(raw, failure + '_error', RuntimeError('uncertain'))
                    with self.assertRaisesRegex(RuntimeError, 'uncertain'): getattr(lease, failure)()
                    # A successful later rollback cannot prove a lost commit was
                    # not committed, or make an uncertain session reusable.
                    raw.rollback_error = None; lease.rollback()
                lease.close(); self.assertTrue(raw.closed); self.assertEqual(raw.reset_count, 0)
                another = pool.acquire(); self.assertIsNot(another._entry.connection, raw); another.close()

    def test_reset_failure_discards_socket_without_changing_successful_commit(self):
        pool = self.pool(); lease = pool.acquire(); raw = self.created[-1]
        lease.commit(); raw.reset_error = RuntimeError('server does not support reset')
        lease.close()  # The durable action succeeded; no false retry is reported.
        self.assertTrue(raw.closed)
        next_lease = pool.acquire(); self.assertIsNot(next_lease._entry.connection, raw); next_lease.close()

    def test_interrupted_reset_releases_capacity_and_discards_session(self):
        pool = self.pool(maximum=1); lease = pool.acquire(); raw = self.created[-1]
        lease.commit()
        with patch.object(pool, 'reset', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): lease.close()
        self.assertTrue(raw.closed)
        next_lease = pool.acquire(); self.assertIsNot(next_lease._entry.connection, raw); next_lease.close()

    def test_interrupted_cursor_close_does_not_leak_checkout_or_allow_reconnect(self):
        pool = self.pool(maximum=1); lease = pool.acquire(); cursor = lease.cursor(); raw = self.created[-1]
        self.assertIs(cursor.connection, lease)
        with self.assertRaises(ValueError): lease.ping(reconnect=True)
        with self.assertRaises(AttributeError): lease.connect()
        lease.commit()
        with patch.object(cursor.cursor, 'close', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): lease.close()
        self.assertTrue(raw.closed)
        next_lease = pool.acquire(); next_lease.close()

    def test_checkout_is_exclusive_and_capacity_wait_is_finite(self):
        pool = self.pool(maximum=1, wait_seconds=.05); first = pool.acquire()
        with ThreadPoolExecutor(max_workers=1) as executor:
            start = time.monotonic()
            with self.assertRaises(TimeoutError): executor.submit(pool.acquire).result(timeout=1)
            self.assertLess(time.monotonic() - start, .5)
            waiting = executor.submit(pool.acquire)
            time.sleep(.01); self.assertFalse(waiting.done())
            first.commit(); first.close(); second = waiting.result(timeout=1)
        self.assertEqual(len(self.created), 1); second.rollback(); second.close()

    def test_parallel_checkouts_do_not_share_connections(self):
        pool = self.pool(maximum=2); barrier = threading.Barrier(2)
        def borrow():
            lease = pool.acquire(); raw = lease._entry.connection; barrier.wait(timeout=1)
            lease.commit(); lease.close(); return raw
        with ThreadPoolExecutor(max_workers=2) as executor:
            a, b = list(executor.map(lambda _: borrow(), range(2)))
        self.assertIsNot(a, b); self.assertEqual(len(self.created), 2)

    def test_rotation_after_release_publication_never_closes_the_new_borrower(self):
        pool = self.pool(maximum=1); first = pool.acquire(); raw = self.created[-1]; first.commit()
        published, finish_release = threading.Event(), threading.Event()
        condition = pool.condition
        class GateCondition:
            def __enter__(self): return condition.__enter__()
            def __exit__(self, *args):
                result = condition.__exit__(*args)
                if threading.current_thread().name == 'old-release':
                    published.set()
                    if not finish_release.wait(2): raise AssertionError('Test release gate timed out')
                return result
            def __getattr__(self, name): return getattr(condition, name)
        pool.condition = GateCondition()
        thread = threading.Thread(target=first.close, name='old-release'); thread.start()
        try:
            self.assertTrue(published.wait(2))
            second = pool.acquire()  # Takes the just-returned clean connection.
            pool.close()  # Config rotation closes idle entries, not this lease.
            self.assertFalse(raw.closed)
        finally:
            finish_release.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertFalse(raw.closed)
        second.cursor().execute('SELECT 1'); second.commit(); second.close()
        self.assertTrue(raw.closed)

    def test_idle_expiry_lifetime_and_failed_connect_release_capacity(self):
        clock = [0.0]; pool = self.pool(maximum=1, idle_seconds=5, lifetime_seconds=20, clock=lambda: clock[0])
        lease = pool.acquire(); raw = self.created[-1]; lease.commit(); lease.close()
        clock[0] = 5.0; next_lease = pool.acquire(); self.assertTrue(raw.closed)
        second = self.created[-1]; clock[0] = 25.0; next_lease.commit(); next_lease.close(); self.assertTrue(second.closed)
        with patch.object(pool, 'factory', side_effect=RuntimeError('connect failed')):
            with self.assertRaisesRegex(RuntimeError, 'connect failed'): pool.acquire()
        final = pool.acquire(); final.close()

    def test_forked_pool_discards_inherited_sockets_without_sending_quit(self):
        pool = self.pool(maximum=2); active = pool.acquire(); idle = pool.acquire(); idle.commit(); idle.close()
        inherited = list(self.created)
        with patch('reveal_backend.mysql_pool.os.getpid', return_value=pool.pid + 1):
            child = pool.acquire()
            self.assertTrue(all(connection.pid_close for connection in inherited))
            with self.assertRaisesRegex(RuntimeError, 'no longer active'): active.cursor()
            child.commit(); child.close()
        active.close()

    def test_repository_preserves_original_error_when_rollback_loses_transport(self):
        repo = Repository(); pool = self.pool(); lease = pool.acquire()
        self.created[-1].rollback_error = RuntimeError('secondary rollback failure')
        with patch.object(repo, 'connect', return_value=lease):
            with self.assertRaisesRegex(ValueError, 'original failure'):
                with repo.read_transaction(): raise ValueError('original failure')
        self.assertTrue(self.created[-1].closed)

    def test_repository_uses_canonical_pool_defaults_and_read_only_snapshot(self):
        repo = Repository(); pool = self.pool(); first = pool.acquire(); first.commit(); first.close()
        lease = pool.acquire(); raw = self.created[-1]; raw.sql.clear()
        with patch.object(repo, 'connect', return_value=lease):
            with repo.read_transaction() as tx: tx.execute('SELECT 1')
        queries = [sql for sql, _ in raw.sql]
        self.assertEqual(queries[:2], ['START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', 'SELECT 1'])
        self.assertNotIn('FOR UPDATE', ' '.join(queries))

    def test_migration_uses_direct_connection_and_pool_can_be_disabled(self):
        repo = Repository(); connection = FakeConnection()
        with patch('reveal_backend.repository.mysql_connection', return_value=connection), \
                patch.object(repo, 'connect', side_effect=AssertionError('Migration must not pool')):
            repo.migrate()
        self.assertTrue(connection.closed)
        with patch.dict('os.environ', {'REVEAL_MYSQL_POOL_SIZE': '0'}), \
                patch.object(runtime_config, 'mysql_connection', return_value=connection):
            self.assertIs(runtime_config.application_mysql_connection(), connection)

    def test_config_rotation_closes_old_idle_pool_and_never_reuses_previous_identity(self):
        old_pool, old_key = runtime_config._application_pool, runtime_config._application_pool_key
        self.addCleanup(setattr, runtime_config, '_application_pool', old_pool)
        self.addCleanup(setattr, runtime_config, '_application_pool_key', old_key)
        runtime_config._application_pool = None; runtime_config._application_pool_key = None
        with patch.dict('os.environ', {'REVEAL_MYSQL_POOL_SIZE': '1', 'REVEAL_MYSQL_PASSWORD': 'test-only-a', 'REVEAL_MYSQL_CA_FILE': ''}), \
                patch.object(runtime_config, 'mysql_connection', side_effect=self.factory):
            first = runtime_config.application_mysql_connection(); first.commit(); first.close(); raw = self.created[-1]
            with patch.dict('os.environ', {'REVEAL_MYSQL_PASSWORD': 'test-only-b'}):
                second = runtime_config.application_mysql_connection()
                self.assertTrue(raw.closed); self.assertIsNot(second._entry.connection, raw)
                second.commit(); second.close()
            runtime_config._application_pool.close()


class ExactFetchTests(unittest.TestCase):
    def test_heterogeneous_fetch_only_returns_exact_pairs_and_binds_all_values(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(str(Path(directory) / 'test.sqlite')); repo.migrate()
            with repo.transaction() as tx:
                for kind, identity, owner in [('account','same','alice'), ('publication','same','alice'), ('account','other','bob'), ('publication','other','bob')]:
                    tx.put(kind, identity, owner, {'id': identity})
            with repo.read_transaction() as tx:
                queries=[]; execute=tx.execute
                def observed(sql, params=()): queries.append((sql, params)); return execute(sql, params)
                with patch.object(tx, 'execute', side_effect=observed):
                    self.assertEqual(tx.get_records([]), {})
                    rows=tx.get_records([('account','same'),('publication','other'),('account','same'),('account', "x' OR 1=1--")])
                    self.assertEqual(set(rows), {('account','same'), ('publication','other')})
                    self.assertEqual(rows[('account','same')]['owner'], 'alice')
                    self.assertEqual(len(queries), 1); self.assertNotIn("x' OR", queries[0][0])
                    queries.clear(); tx.get_records([('account', str(i)) for i in range(501)])
                    self.assertEqual(len(queries), 3)
                    self.assertTrue(all(len(params) <= 500 for _, params in queries))


if __name__ == '__main__': unittest.main()
