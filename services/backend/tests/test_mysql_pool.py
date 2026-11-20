"""Exclusive bounded leases must never reuse unresolved or contaminated sessions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pymysql

from reveal_backend import mysql_database
from reveal_backend.mysql_pool import Pool, session_neutral
from reveal_backend.mysql_database import (APPLICATION_SESSION_SQL, application_session_unchanged, reset_application_session,
    reset_application_session_sequential, validate_idle_session)
from reveal_backend.repository import Repository, Transaction
from reveal_backend import runtime_config


class FakeCursor:
    def __init__(self, connection): self.connection = connection; self.closed = False
    def execute(self, sql, params=()):
        self.connection.trips += 1
        if self.connection.execute_error: raise self.connection.execute_error
        self.connection.sql.append((sql, params)); return 1
    def executemany(self, sql, rows): return self.execute(sql, rows)
    def fetchone(self): return (1,)
    def callproc(self, name, args=()): self.connection.trips += 1; return args
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class FakeConnection:
    """PyMySQL-shaped session. No server_status, so release cannot prove it clean and always resets."""
    def __init__(self):
        self.sql = []; self.closed = False; self.reset_count = 0; self.uncommitted = False
        self.variables = {}; self.database = 'cyaka_expected'; self.pid_close = False
        self.commit_error = self.rollback_error = self.reset_error = self.execute_error = self.ping_error = None
        self.trips = 0; self.writes = []; self.pending = []; self.pings = 0
        self._sock = object(); self._result = None; self._next_seq_id = 0; self._read_timeout = self._write_timeout = 120
    def cursor(self, *args): return FakeCursor(self)
    def commit(self):
        self.trips += 1
        if self.commit_error: raise self.commit_error
        self.uncommitted = False
    def rollback(self):
        self.trips += 1
        if self.rollback_error: raise self.rollback_error
        self.uncommitted = False
    def close(self): self.closed = True
    def _force_close(self): self.pid_close = True; self.closed = True
    def _execute_command(self, code, payload):
        if self.reset_error: raise self.reset_error
        self.trips += 1; self.pending.append((code, payload.encode() if isinstance(payload, str) else payload))
    def _write_bytes(self, data):
        if self.reset_error: raise self.reset_error
        self.trips += 1; self.writes.append(data); offset = 0
        while offset < len(data):  # PyMySQL first packets: 3-byte length, sequence 0, command
            length = int.from_bytes(data[offset:offset + 3], 'little'); assert data[offset + 3] == 0
            self.pending.append((data[offset + 4], data[offset + 5:offset + 4 + length])); offset += 4 + length
    def _read_ok_packet(self):
        code, payload = self.pending.pop(0)
        if code == 0x1F: self.reset_count += 1; self.variables = {}; self.uncommitted = False
        elif code == 0x02: self.database = payload.decode()
        elif code == 0x03: self.sql.append((payload.decode(), ())); self.on_settings()
        elif code == 0x0E: self.pings += 1
        else: raise AssertionError(hex(code))
    def on_settings(self): pass
    def select_db(self, database): self._execute_command(0x02, database); self._read_ok_packet()
    def ping(self, reconnect=True):
        assert reconnect is False; self.observed_timeouts = (self._read_timeout, self._write_timeout)
        if self.ping_error: raise self.ping_error
        self._execute_command(0x0E, b''); self._read_ok_packet()


class StatusConnection(FakeConnection):
    """Reports server_status from the final OK packet like PyMySQL, after init_command applied the defaults."""
    status_after_end = 0
    def __init__(self):
        super().__init__(); self.server_status = 0; self.reveal_session_defaults = True
        self.client_flag = pymysql.constants.CLIENT.CAPABILITIES
    def commit(self): super().commit(); self.server_status = self.status_after_end
    def rollback(self): super().rollback(); self.server_status = self.status_after_end
    def on_settings(self): self.server_status = 0


class PoolTests(unittest.TestCase):
    def setUp(self): self.created = []; self.factory_kwargs = []
    def factory(self, **kwargs):
        connection = FakeConnection(); self.created.append(connection); self.factory_kwargs.append(kwargs); return connection
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
            self.assertEqual(self.factory_kwargs[-1], {'application_session': True})  # init_command, pooled only
            with patch.dict('os.environ', {'REVEAL_MYSQL_PASSWORD': 'test-only-b'}):
                second = runtime_config.application_mysql_connection()
                self.assertTrue(raw.closed); self.assertIsNot(second._entry.connection, raw)
                second.commit(); second.close()
            runtime_config._application_pool.close()


class CleanReleaseTests(unittest.TestCase):
    """A settled lease that ran only session-neutral SQL returns to idle with no round trip."""
    def setUp(self): self.created = []
    def factory(self):
        connection = StatusConnection(); self.created.append(connection); return connection
    def pool(self, **kwargs):
        pool = Pool(self.factory, lambda c: reset_application_session(c, 'cyaka_expected'),
                    unchanged=application_session_unchanged, **kwargs)
        self.addCleanup(pool.close); return pool
    def cycle(self, statements=('SELECT 1',), end='commit', after=None, status=0, many=False):
        pool = self.pool(maximum=1); lease = pool.acquire(); raw = self.created[-1]; raw.status_after_end = status
        for sql in statements:
            cursor = lease.cursor()
            if many: cursor.executemany(sql, [(1,)])
            else: cursor.execute(sql)
        getattr(lease, end)()
        if after: after(lease)
        raw.trips = 0; lease.close(); return pool, raw

    def assert_reset_once(self, raw):
        self.assertEqual((raw.reset_count, len(raw.writes), raw.trips), (1, 1, 1))  # one pipelined write = 1 RT
        self.assertEqual(raw.database, 'cyaka_expected'); self.assertEqual(raw.sql[-1][0], APPLICATION_SESSION_SQL)

    def test_clean_read_and_write_leases_release_with_zero_commands(self):
        for statements in (('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', 'SELECT owner_id FROM reveal_records WHERE kind=%s'),
                           ('SELECT revision FROM reveal_transaction_lock WHERE id=1 FOR UPDATE', 'UPDATE reveal_records SET payload=%s',
                            'INSERT INTO reveal_records VALUES (%s)', 'DELETE FROM reveal_records WHERE id=%s'),
                           ('SELECT /*+ MAX_EXECUTION_TIME(5000) */ 1', 'WITH x AS (SELECT 1) SELECT * FROM x', '(SELECT 1) UNION (SELECT 2)')):
            for end in ('commit', 'rollback'):  # rollback = the handler raised after neutral SQL
                with self.subTest(statements=statements[0], end=end):
                    pool, raw = self.cycle(statements, end)
                    self.assertEqual((raw.reset_count, raw.writes, raw.trips, raw.closed), (0, [], 0, False))
                    again = pool.acquire(); self.assertIs(again._entry.connection, raw); again.close()

    def test_stateful_or_unknown_statements_force_one_pipelined_full_reset(self):
        for sql in ('SET @x=1', 'SELECT @x:=1', 'SELECT 1 INTO @x', 'SELECT GET_LOCK(%s,0)', 'SELECT RELEASE_ALL_LOCKS()',
                    'CREATE TEMPORARY TABLE t (a int)', 'USE cyaka_other', 'SET SESSION time_zone=%s', 'SET TRANSACTION READ ONLY',
                    'LOCK TABLES reveal_records READ', 'PREPARE s FROM %s', 'SHOW SESSION STATUS', 'CALL p()', 'DO SLEEP(0)',
                    'SAVEPOINT a', 'SELECT LAST_INSERT_ID(5)', '/* hint */ SELECT 1', 'COMMIT', 'ROLLBACK AND CHAIN',
                    "SELECT 1 INTO OUTFILE '/tmp/x'", 'SELECT SQL_CALC_FOUND_ROWS 1', b'SET @y=2', None):
            with self.subTest(sql=sql):
                pool, raw = self.cycle(('SELECT 1', sql)); self.assert_reset_once(raw)
        pool, raw = self.cycle(('SET @z=%s',), many=True); self.assert_reset_once(raw)

    def test_statement_after_settle_raw_calls_failures_and_cursor_calls_force_reset(self):
        for after in (lambda lease: lease.cursor().execute('SELECT 1'),       # snapshot reopened after COMMIT
                      lambda lease: lease.select_db('cyaka_other'),           # raw connection method
                      lambda lease: lease.cursor().callproc('p'),             # non-fetch cursor method
                      lambda lease: lease.cursor(dict)):                      # custom/unbuffered cursor class
            with self.subTest(after=after):
                pool, raw = self.cycle(after=after); self.assert_reset_once(raw)
        pool = self.pool(maximum=1); lease = pool.acquire(); raw = self.created[-1]
        raw.execute_error = pymysql.err.OperationalError(1205, 'Lock wait timeout exceeded')
        with self.assertRaises(pymysql.err.OperationalError): lease.cursor().execute('SELECT 1')
        raw.execute_error = None; lease.rollback(); raw.trips = 0; lease.close(); self.assert_reset_once(raw)

    def test_server_status_session_marker_and_capabilities_veto_the_skip(self):
        for status in (0x1, 0x2, 0x8, 0x2000, 0x2001, None):  # chained txn, autocommit=1, more results, read-only txn
            with self.subTest(status=status):
                pool, raw = self.cycle(status=status); self.assert_reset_once(raw)
        for attribute, value in (('reveal_session_defaults', False), ('client_flag', pymysql.constants.CLIENT.MULTI_STATEMENTS)):
            with self.subTest(attribute=attribute):
                pool = self.pool(maximum=1); lease = pool.acquire(); raw = self.created[-1]
                setattr(raw, attribute, value); lease.commit(); raw.trips = 0; lease.close()
                self.assertEqual(raw.reset_count, 1)
        pool = Pool(self.factory, lambda c: reset_application_session(c, 'cyaka_expected'),
                    unchanged=MagicMock(side_effect=RuntimeError('predicate failed')))
        self.addCleanup(pool.close); lease = pool.acquire(); raw = self.created[-1]; lease.commit(); lease.close()
        self.assertEqual((raw.reset_count, raw.closed), (1, False))

    def test_unsettled_uncertain_interrupted_or_failed_reset_leases_are_discarded(self):
        pool = self.pool(maximum=1); lease = pool.acquire(); raw = self.created[-1]
        lease.cursor().execute('SELECT 1'); lease.close()
        self.assertEqual((raw.closed, raw.reset_count), (True, 0))
        lease = pool.acquire(); raw = self.created[-1]; cursor = lease.cursor()
        with patch.object(cursor.cursor, 'execute', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): cursor.execute('SELECT 1')
        lease.rollback(); lease.close()  # a stale reply may be unread: never reuse
        self.assertEqual((raw.closed, raw.reset_count), (True, 0))
        lease = pool.acquire(); raw = self.created[-1]; lease.cursor().execute('SET @x=1'); lease.commit()
        raw.reset_error = ConnectionError('lost'); lease.close(); self.assertTrue(raw.closed)
        self.assertIsNot(pool.acquire()._entry.connection, raw)

    def test_repository_read_and_write_leases_cost_three_round_trips_with_no_release(self):
        pool = self.pool(maximum=1); repo = Repository(); repo.connect = pool.acquire
        with pool.acquire() as warm: warm.commit()
        raw = self.created[0]
        with patch('reveal_backend.workspace_events.prepare_commit', return_value=[]), \
                patch('reveal_backend.workspace_events.publish_committed'):
            for method in ('read_transaction', 'transaction'):
                raw.trips = 0
                with getattr(repo, method)() as tx: tx.execute('SELECT 1')
                with self.subTest(method=method):
                    self.assertEqual(raw.trips, 3)  # OPEN + statement + COMMIT; release 0
                    self.assertEqual((raw.reset_count, len(self.created)), (0, 1))
            with self.assertRaises(LookupError):
                with repo.read_transaction() as tx: tx.execute('SELECT 1'); raise LookupError('404 after read')
        self.assertEqual(raw.reset_count, 0)  # ROLLBACK ended the snapshot; nothing stateful ran

    def test_classifier_is_allowlist_first(self):
        self.assertTrue(session_neutral(' select 1')); self.assertTrue(session_neutral('START  TRANSACTION READ WRITE'))
        for sql in ('', 'EXPLAIN SELECT 1', 'REPLACE INTO t VALUES (1)', 'HANDLER t OPEN', 'XA START 1', 'SELECT is_used_lock(%s)', 42):
            with self.subTest(sql=sql): self.assertFalse(session_neutral(sql))


class PipelinedResetTests(unittest.TestCase):
    def test_reset_is_one_write_in_order_and_clears_contamination(self):
        raw = FakeConnection(); raw.database = 'cyaka_other'; raw.variables['x'] = 1
        reset_application_session(raw, 'cyaka_expected')
        self.assertEqual(raw.trips, 1); self.assertEqual(len(raw.writes), 1)
        frames = raw.writes[0]
        self.assertTrue(frames.startswith(b'\x01\x00\x00\x00\x1f'))
        self.assertIn(b'\x02cyaka_expected', frames); self.assertTrue(frames.endswith(b'\x03' + APPLICATION_SESSION_SQL.encode()))
        self.assertEqual((raw.variables, raw.database, raw.reset_count, raw.reveal_session_defaults), ({}, 'cyaka_expected', 1, True))
        for term in ("autocommit=0", "completion_type='NO_CHAIN'", "REPEATABLE-READ", "time_zone='+00:00'",
                     "innodb_lock_wait_timeout=15", "wait_timeout=300"):
            self.assertIn(term, raw.sql[-1][0])

    def test_unsafe_preconditions_raise_before_writing(self):
        raw = FakeConnection(); raw._sock = None
        with self.assertRaises(pymysql.err.InterfaceError): reset_application_session(raw, 'cyaka_expected')
        raw = FakeConnection(); raw._result = MagicMock(unbuffered_active=True, has_next=False)
        with self.assertRaises(pymysql.err.InterfaceError): reset_application_session(raw, 'cyaka_expected')
        with self.assertRaises(ValueError): reset_application_session(FakeConnection(), 'other; DROP')
        self.assertEqual(raw.writes, [])

    def test_sequential_kill_switch_variant_has_the_same_effect_in_three_round_trips(self):
        raw = FakeConnection(); raw.database = 'cyaka_other'; raw.variables['x'] = 1
        reset_application_session_sequential(raw, 'cyaka_expected')
        self.assertEqual((raw.trips, raw.variables, raw.database, raw.sql[-1][0]), (3, {}, 'cyaka_expected', APPLICATION_SESSION_SQL))

    def test_private_pymysql_primitives_are_pinned(self):
        self.assertEqual(pymysql.VERSION_STRING, '1.1.2')  # review reset/TLS/ping internals before upgrading
        self.assertFalse(pymysql.constants.CLIENT.CAPABILITIES & pymysql.constants.CLIENT.MULTI_STATEMENTS)


class WireServer(threading.Thread):
    """In-process MySQL command-phase peer for REAL PyMySQL framing; counts client flights (round trips)."""
    def __init__(self, sock, fail_index=None):
        super().__init__(daemon=True); self.sock, self.fail_index = sock, fail_index
        self.commands, self.flights = [], 0; self.state = {'db': 'cyaka_expected', 'autocommit': 0, 'trans': 0, 'ro': 0}
    def status(self):
        s = self.state; return (0x1 if s['trans'] else 0) | (0x2 if s['autocommit'] else 0) | (0x2000 if s['trans'] and s['ro'] else 0)
    @staticmethod
    def packets(seq, *bodies):
        return b''.join(struct.pack('<I', len(body))[:3] + bytes([seq + index]) + body for index, body in enumerate(bodies))
    def ok(self): return b'\x00\x00\x00' + struct.pack('<HH', self.status(), 0)
    def eof(self): return b'\xfe\x00\x00' + struct.pack('<H', self.status())
    def run(self):
        buffer = b''; index = 0
        while True:
            try: data = self.sock.recv(65536)
            except OSError: return
            if not data: return
            buffer += data; replies = b''; counted = False
            while len(buffer) >= 4 and len(buffer) >= 4 + int.from_bytes(buffer[:3], 'little'):
                length = int.from_bytes(buffer[:3], 'little'); body = buffer[4:4 + length]; buffer = buffer[4 + length:]
                command, payload = body[0], body[1:]
                if command == 0x01: return  # COM_QUIT
                if not counted: self.flights += 1; counted = True
                self.commands.append((command, payload)); s = self.state; text = payload.decode().upper()
                if index == self.fail_index: reply = self.packets(1, b'\xff' + struct.pack('<H', 1047) + b'#08S01Unknown')
                elif command == 0x1F: s.update(autocommit=1, trans=0, ro=0, db=s['db']); reply = self.packets(1, self.ok())
                elif command == 0x02: s['db'] = payload.decode(); reply = self.packets(1, self.ok())
                elif command == 0x0E: reply = self.packets(1, self.ok())
                elif text.startswith('START TRANSACTION'): s.update(trans=1, ro=int('READ ONLY' in text)); reply = self.packets(1, self.ok())
                elif text in ('COMMIT', 'ROLLBACK'): s.update(trans=0, ro=0); reply = self.packets(1, self.ok())
                elif text.startswith('SET SESSION'): s['autocommit'] = 0; reply = self.packets(1, self.ok())
                elif text.startswith('SELECT'):
                    s['trans'] = 1; column = b'\x03def\x00\x00\x00\x011\x00\x0c' + struct.pack('<HIBHB', 63, 1, 8, 0, 0) + b'\x00\x00'
                    reply = self.packets(1, b'\x01', column, self.eof(), self.eof())  # one column, zero rows
                else: s['trans'] = 1; reply = self.packets(1, self.ok())
                replies += reply; index += 1
            if replies: self.sock.sendall(replies)


class WireProtocolTests(unittest.TestCase):
    """Real PyMySQL 1.1.2 Connection objects over a socketpair: exact round trips per pooled lease."""
    def client(self, fail_index=None):
        a, b = socket.socketpair(); self.addCleanup(b.close)
        connection = pymysql.connections.Connection(defer_connect=True, autocommit=False, read_timeout=5, write_timeout=5)
        connection._sock = a; connection._rfile = a.makefile('rb'); connection._next_seq_id = 0
        connection.server_status = 0; connection.reveal_session_defaults = True  # as init_command leaves it
        server = WireServer(b, fail_index); server.start(); self.servers.append(server); return connection
    def setUp(self): self.servers = []
    def pool(self, unchanged=application_session_unchanged):
        pool = Pool(self.client, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=unchanged,
                    validate=lambda c: validate_idle_session(c, 1))
        self.addCleanup(pool.close); return pool
    def flights(self, run):
        before = self.servers[0].flights; run(); time.sleep(.05); return self.servers[0].flights - before

    def test_warm_read_and_write_leases_cost_statements_plus_two_and_release_free(self):
        pool = self.pool(); repo = Repository(); repo.connect = pool.acquire
        with pool.acquire() as warm: warm.commit()
        def read():
            with repo.read_transaction() as tx: tx.execute('SELECT 1').fetchall()
        def write():
            with patch('reveal_backend.workspace_events.prepare_commit', return_value=[]), \
                    patch('reveal_backend.workspace_events.publish_committed'):
                with repo.transaction() as tx: tx.execute("UPDATE reveal_records SET version=version WHERE kind='x'")
        self.assertEqual(self.flights(read), 3)    # START, SELECT, COMMIT (was 6 with the 3-step reset)
        self.assertEqual(self.flights(write), 3)   # FOR UPDATE, UPDATE, COMMIT
        self.assertEqual(len(self.servers), 1)     # one socket, reused
        self.assertNotIn(0x1F, [command for command, _ in self.servers[0].commands])

    def test_dirty_lease_reset_is_one_flight_and_kill_switch_resets_every_lease(self):
        pool = self.pool()
        with pool.acquire() as warm: warm.commit()
        def dirty():
            with pool.acquire() as lease: lease.cursor().execute('SET @x=1'); lease.commit()
        self.assertEqual(self.flights(dirty), 3)   # SET, COMMIT, pipelined RESET+INIT_DB+SET SESSION
        self.assertEqual([command for command, _ in self.servers[0].commands[-3:]], [0x1F, 0x02, 0x03])
        self.assertEqual(self.servers[0].state['autocommit'], 0)
        pool = self.pool(unchanged=None); self.servers.clear()
        def clean():
            with pool.acquire() as lease:
                cursor = lease.cursor(); cursor.execute('SELECT 1'); cursor.fetchall(); lease.commit()
        with pool.acquire() as warm: warm.commit()
        self.assertEqual(self.flights(clean), 3)   # SELECT, COMMIT, one pipelined reset

    def test_err_on_any_pipelined_reply_raises_and_the_pool_discards(self):
        for fail in (0, 1, 2):
            with self.subTest(fail=fail):
                connection = self.client(fail)
                with self.assertRaises(pymysql.err.MySQLError): reset_application_session(connection, 'cyaka_expected')
        pool = Pool(lambda: self.client(1), lambda c: reset_application_session(c, 'cyaka_expected'))
        self.addCleanup(pool.close); lease = pool.acquire(); raw = lease._entry.connection; lease.commit()
        lease.close(); self.assertIsNone(raw._sock)
        self.assertIsNot(pool.acquire()._entry.connection, raw)

    def test_commit_ok_packet_proves_the_session_reusable(self):
        connection = self.client()
        with connection.cursor() as cursor: cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        self.assertEqual(connection.server_status, 0x2001); self.assertFalse(application_session_unchanged(connection))
        connection.commit(); self.assertEqual(connection.server_status, 0)
        self.assertTrue(application_session_unchanged(connection))

    def test_idle_validation_is_one_ping_with_a_short_deadline(self):
        connection = self.client(); before = self.servers[0].flights
        validate_idle_session(connection, 1); time.sleep(.05)
        self.assertEqual(self.servers[0].flights - before, 1); self.assertEqual(self.servers[0].commands[-1][0], 0x0E)
        self.assertEqual((connection._read_timeout, connection._write_timeout), (5, 5))


class IdlePolicyTests(unittest.TestCase):
    def setUp(self): self.created = []; self.clock = [0.0]; self.wall = [1000.0]
    def factory(self):
        connection = StatusConnection(); self.created.append(connection); return connection
    def pool(self, **kwargs):
        options = dict(unchanged=application_session_unchanged, validate=lambda c: validate_idle_session(c, 3),
                       idle_seconds=240, lifetime_seconds=3600, validate_after_seconds=60,
                       clock=lambda: self.clock[0], wall=lambda: self.wall[0])
        options.update(kwargs)
        pool = Pool(self.factory, lambda c: reset_application_session(c, 'cyaka_expected'), **options)
        self.addCleanup(pool.close); return pool
    def borrow(self, pool):
        lease = pool.acquire(); lease.commit(); lease.close(); return lease._entry.connection
    def advance(self, seconds, wall=None):
        self.clock[0] += seconds; self.wall[0] += seconds if wall is None else wall

    def test_recent_entries_skip_ping_long_idle_entries_get_one_and_expired_ones_reconnect(self):
        pool = self.pool(); raw = self.borrow(pool)
        self.advance(60); self.assertIs(self.borrow(pool), raw); self.assertEqual(raw.pings, 0)  # 0 RT hot path
        self.advance(61); self.assertIs(self.borrow(pool), raw); self.assertEqual(raw.pings, 1)
        self.assertEqual(raw.observed_timeouts, (3, 3)); self.assertEqual((raw._read_timeout, raw._write_timeout), (120, 120))
        self.advance(240); fresh = self.borrow(pool)
        self.assertIsNot(fresh, raw); self.assertTrue(raw.closed); self.assertEqual(raw.pings, 1)

    def test_host_sleep_counts_as_idle_even_when_the_monotonic_clock_paused(self):
        pool = self.pool(); raw = self.borrow(pool)
        self.advance(0, wall=600)  # laptop slept; the server's wait_timeout kept counting
        self.assertIsNot(self.borrow(pool), raw); self.assertTrue(raw.closed)
        raw = self.created[-1]; self.advance(0, wall=90); self.borrow(pool); self.assertEqual(raw.pings, 1)

    def test_failed_ping_discards_frees_capacity_and_connects_anew(self):
        pool = self.pool(maximum=1); raw = self.borrow(pool); raw.ping_error = pymysql.err.OperationalError(2013, 'lost')
        self.advance(90); lease = pool.acquire()
        self.assertTrue(raw.closed); self.assertIsNot(lease._entry.connection, raw); self.assertEqual(len(self.created), 2)
        lease.commit(); lease.close()
        raw = self.created[-1]; raw.ping_error = KeyboardInterrupt(); self.advance(90)
        with self.assertRaises(KeyboardInterrupt): pool.acquire()
        self.assertTrue(raw.closed); self.assertEqual(pool.entries, set())

    def test_pool_close_during_validation_never_hands_out_the_entry(self):
        pool = self.pool(); raw = self.borrow(pool); self.advance(90)
        pool.validate = lambda connection: pool.close()  # config rotation while the ping is in flight
        with self.assertRaisesRegex(RuntimeError, 'closed'): pool.acquire()
        self.assertTrue(raw.closed); self.assertEqual(pool.entries, set())

    def test_lifetime_is_jittered_per_connection_and_enforced_on_release(self):
        draws = iter([0.0, 1.0]); pool = self.pool(maximum=2, lifetime_jitter=0.1, rand=lambda: next(draws))
        first, second = pool.acquire(), pool.acquire()
        self.assertEqual((first._entry.expires, second._entry.expires), (3600, 3240))
        self.advance(3300); first.commit(); second.commit(); first.close(); second.close()
        self.assertFalse(first._entry.connection.closed); self.assertTrue(second._entry.connection.closed)


class RuntimePoolSettingsTests(unittest.TestCase):
    def setUp(self):
        old_pool, old_key = runtime_config._application_pool, runtime_config._application_pool_key
        self.addCleanup(setattr, runtime_config, '_application_pool', old_pool)
        self.addCleanup(setattr, runtime_config, '_application_pool_key', old_key)
        runtime_config._application_pool = None; runtime_config._application_pool_key = None
        self.created = []; environment = patch.dict('os.environ'); environment.start(); self.addCleanup(environment.stop)
        for name in [name for name in runtime_config.os.environ if name.startswith('REVEAL_MYSQL_POOL_')]:
            del runtime_config.os.environ[name]
    def tearDown(self):
        if runtime_config._application_pool is not None: runtime_config._application_pool.close()
    def factory(self, **kwargs):
        connection = StatusConnection(); self.created.append(connection); return connection
    def lease(self, **env):
        values = {'REVEAL_MYSQL_PASSWORD': 'test-only', 'REVEAL_MYSQL_CA_FILE': '', **env}
        with patch.dict('os.environ', values), patch.object(runtime_config, 'mysql_connection', side_effect=self.factory):
            return runtime_config.application_mysql_connection()

    def test_defaults_size_ten_idle_below_session_wait_timeout_and_jittered_hour_lifetime(self):
        lease = self.lease()
        pool = runtime_config._application_pool
        self.assertEqual((pool.maximum, pool.wait_seconds, pool.idle_seconds, pool.lifetime_seconds), (10, 5, 240, 3600))
        self.assertEqual((pool.lifetime_jitter, pool.validate_after_seconds), (0.1, 60))
        self.assertLessEqual(pool.idle_seconds, mysql_database.SESSION_IDLE_KILL_SECONDS - 60)
        self.assertIn('wait_timeout=300', APPLICATION_SESSION_SQL); self.assertIn('innodb_lock_wait_timeout=15', APPLICATION_SESSION_SQL)
        raw = self.created[-1]; lease.commit(); lease.close(); self.assertEqual(raw.reset_count, 0)  # clean release on

    def test_kill_switches_force_reset_and_sequential_reset_and_rotate_the_pool(self):
        lease = self.lease(REVEAL_MYSQL_POOL_CLEAN_RELEASE='0'); raw = self.created[-1]; raw.trips = 0
        lease.commit(); lease.close(); self.assertEqual((raw.reset_count, len(raw.writes)), (1, 1))
        lease = self.lease(REVEAL_MYSQL_POOL_CLEAN_RELEASE='0', REVEAL_MYSQL_POOL_PIPELINED_RESET='0')
        self.assertTrue(raw.closed); raw = self.created[-1]; lease.commit(); raw.trips = 0; lease.close()
        self.assertEqual((raw.reset_count, raw.writes, raw.trips), (1, [], 3))
        for env in ({'REVEAL_MYSQL_POOL_IDLE_SECONDS': '120'}, {'REVEAL_MYSQL_POOL_LIFETIME_SECONDS': '1800'}, {'REVEAL_MYSQL_POOL_SIZE': '12'}):
            with self.subTest(env=env):
                before = runtime_config._application_pool; lease = self.lease(**env); lease.rollback(); lease.close()
                self.assertIsNot(runtime_config._application_pool, before)

    def test_bounds_keep_idle_expiry_below_the_server_kill(self):
        for env in ({'REVEAL_MYSQL_POOL_IDLE_SECONDS': '241'}, {'REVEAL_MYSQL_POOL_IDLE_SECONDS': '0'},
                    {'REVEAL_MYSQL_POOL_LIFETIME_SECONDS': '3601'}, {'REVEAL_MYSQL_POOL_LIFETIME_SECONDS': '100'},
                    {'REVEAL_MYSQL_POOL_SIZE': '33'}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, 'pool bounds'): self.lease(**env)


class ReferenceReadTests(RuntimePoolSettingsTests):
    """Reference reads borrow application sessions: capped, settled by ROLLBACK, deadlines restored, never replayed."""
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime_config, '_reference_slots', runtime_config._reference_slots)
        runtime_config._reference_slots = None
    def read(self, **env):
        values = {'REVEAL_MYSQL_PASSWORD': 'test-only', 'REVEAL_MYSQL_CA_FILE': '', **env}
        with patch.dict('os.environ', values), patch.object(runtime_config, 'mysql_connection', side_effect=self.factory):
            return runtime_config.reference_mysql_connection()
    test_defaults_size_ten_idle_below_session_wait_timeout_and_jittered_hour_lifetime = None
    test_kill_switches_force_reset_and_sequential_reset_and_rotate_the_pool = None
    test_bounds_keep_idle_expiry_below_the_server_kill = None

    def test_close_settles_with_one_rollback_and_a_clean_release(self):
        read = self.read(); raw = self.created[-1]
        with read.cursor() as cursor: cursor.execute('SELECT symbol FROM eaggl_genes WHERE import_id=%s', ('x',))
        raw.trips = 0; read.close(); read.close()
        self.assertEqual((raw.trips, raw.reset_count, raw.closed), (1, 0, False))  # ROLLBACK, then zero-command release
        again = self.read(); self.assertEqual(len(self.created), 1)
        again.rollback(); raw.trips = 0; again.close(); self.assertEqual(raw.trips, 0)  # the reader already settled
        self.assertFalse(hasattr(again, '_read_timeout') or hasattr(again, '_force_close') or hasattr(again, 'autocommit'))

    def test_failed_rollback_or_discard_drops_the_session_and_frees_its_slot(self):
        small = {'REVEAL_MYSQL_POOL_SIZE': '4', 'REVEAL_MYSQL_POOL_WAIT_SECONDS': '0.05'}  # two reference slots
        read = self.read(**small); raw = self.created[-1]; raw.rollback_error = pymysql.err.OperationalError(2013, 'lost')
        read.close(); self.assertTrue(raw.closed)
        read = self.read(**small); raw = self.created[-1]
        with read.cursor() as cursor: cursor.execute('SELECT 1')
        raw.trips = 0; read.discard(); self.assertEqual((raw.trips, raw.closed), (0, True))  # past a deadline: no round trip
        reads = [self.read(**small), self.read(**small)]  # both slots came back
        for read in reads: read.close()

    def test_reads_hold_at_most_half_the_pool_and_release_on_every_path(self):
        reads = [self.read(REVEAL_MYSQL_POOL_SIZE='4', REVEAL_MYSQL_POOL_WAIT_SECONDS='0.05') for _ in range(2)]
        with self.assertRaises(runtime_config_busy()): self.read(REVEAL_MYSQL_POOL_SIZE='4', REVEAL_MYSQL_POOL_WAIT_SECONDS='0.05')
        writer = self.lease(REVEAL_MYSQL_POOL_SIZE='4'); writer.rollback(); writer.close()  # Repository keeps its share
        reads.pop().close()
        with patch.object(runtime_config, 'application_mysql_connection', side_effect=OSError('connect failed')):
            with self.assertRaises(OSError): self.read(REVEAL_MYSQL_POOL_SIZE='4')
        reads.append(self.read(REVEAL_MYSQL_POOL_SIZE='4', REVEAL_MYSQL_POOL_WAIT_SECONDS='0.05'))  # the failed connect freed it
        for read in reads: read.close()

    def test_deadline_applies_to_the_socket_and_is_restored_before_release(self):
        read = self.read(); raw = self.created[-1]; seen = []
        read.limit(2.5); self.assertEqual((raw._read_timeout, raw._write_timeout), (2.5, 2.5))
        with patch.object(runtime_config._application_pool, 'release', side_effect=lambda entry, *a, **k: seen.append(
                (entry.connection._read_timeout, entry.connection._write_timeout))):
            read.close()
        self.assertEqual(seen, [(120, 120)])

    def test_request_path_reference_readers_borrow_pooled_sessions(self):
        from types import SimpleNamespace
        from reveal_backend import catalog, cfde_assessment_state, factor_details, research_execution, research_public, worker
        self.assertIs(research_public._reader(SimpleNamespace(data_service=None)).connection_factory, runtime_config.reference_mysql_connection)
        for module in (research_execution, factor_details, catalog, cfde_assessment_state):
            self.assertIs(module.reference_mysql_connection, runtime_config.reference_mysql_connection, module.__name__)
        # Worker evidence collection and the catalog cold load keep their own direct connections.
        self.assertIs(worker.mysql_connection, runtime_config.mysql_connection)

    def test_unpooled_configurations_connect_directly(self):
        for size in ('0', '1'):
            with self.subTest(size=size):
                connection = self.read(REVEAL_MYSQL_POOL_SIZE=size)
                self.assertIs(connection, self.created[-1]); self.assertIsNone(runtime_config._application_pool)


def runtime_config_busy():
    from reveal_backend.mysql_pool import DatabaseBusy
    return DatabaseBusy


class HandshakeResult:
    def __init__(self, sock, secure=None):
        self._sock = sock; self._secure = isinstance(sock, ssl.SSLSocket) if secure is None else secure; self.closes = 0
    def close(self): self.closes += 1
    def cursor(self): raise AssertionError('No post-handshake TLS query')


class ConnectTests(unittest.TestCase):
    """Cold connects: TLS proven client-side and pooled session settings sent as init_command."""
    def tls(self, version='TLSv1.3'):
        sock = MagicMock(spec=ssl.SSLSocket); sock.version.return_value = version
        sock.cipher.return_value = ('TLS_AES_256_GCM_SHA384', 'TLSv1.3', 256); return sock
    def connect(self, connection, **kwargs):
        factory = MagicMock(return_value=connection)
        with patch.dict('os.environ', {'REVEAL_MYSQL_PASSWORD': 'test-only'}), patch.object(pymysql, 'connect', factory):
            return mysql_database.connect(**kwargs), factory.call_args.kwargs

    def test_pooled_connect_sends_settings_as_init_command_and_no_tls_query(self):
        result, kwargs = self.connect(HandshakeResult(self.tls()), application_session=True)
        self.assertEqual((kwargs['init_command'], kwargs['autocommit']), (APPLICATION_SESSION_SQL, False))
        self.assertNotIn('collation', kwargs); self.assertIsInstance(kwargs['ssl'], ssl.SSLContext)
        self.assertIs(result.reveal_verified_tls, True); self.assertIs(result.reveal_session_defaults, True)

    def test_unpooled_connect_keeps_server_session_defaults(self):
        result, kwargs = self.connect(HandshakeResult(self.tls()))
        self.assertIsNone(kwargs['init_command']); self.assertIs(kwargs['autocommit'], False)
        self.assertIs(result.reveal_verified_tls, True); self.assertFalse(hasattr(result, 'reveal_session_defaults'))

    def test_plaintext_or_unverified_sessions_fail_closed(self):
        for connection in (HandshakeResult(MagicMock(spec=socket.socket)), HandshakeResult(None),
                           HandshakeResult(self.tls(), secure=False), HandshakeResult(self.tls(version=None))):
            with self.subTest(sock=connection._sock):
                with self.assertRaisesRegex(ValueError, 'Verified TLS is required'): self.connect(connection)
                self.assertEqual(connection.closes, 1)
        context = ssl.create_default_context(); context.check_hostname = False
        with self.assertRaisesRegex(ValueError, 'Verified TLS'):
            mysql_database.require_verified_tls(HandshakeResult(self.tls()), context)


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
