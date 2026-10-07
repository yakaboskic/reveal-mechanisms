"""single_read() costs one statement plus ROLLBACK; at most two writers per prefix hold sessions on the fence."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pymysql

from reveal_backend import repository
from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
from reveal_backend.mysql_pool import DatabaseBusy, Pool
from reveal_backend.repository import Repository
import test_application as application
import test_mysql_pool as wire


class SingleReadTests(unittest.TestCase):
    setUp = wire.WireProtocolTests.setUp
    client = wire.WireProtocolTests.client

    def leased(self, connection):
        repo = Repository(); repo.connect = lambda: connection; return repo

    def test_proven_pooled_session_sends_one_select_and_a_rollback_and_releases_clean(self):
        created = []
        def factory():
            connection = wire.StatusConnection(); created.append(connection); return connection
        pool = Pool(factory, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=application_session_unchanged)
        self.addCleanup(pool.close); repo = Repository(); repo.connect = pool.acquire
        with pool.acquire() as warm: warm.commit()
        raw = created[0]; raw.sql.clear(); raw.trips = 0
        with repo.single_read() as tx: self.assertEqual(tx.execute('SELECT 1').fetchone(), (1,))
        self.assertEqual(([sql for sql, _ in raw.sql], raw.trips, raw.reset_count, len(created)), (['SELECT 1'], 2, 0, 1))

    def test_real_pymysql_framing_is_two_flights(self):
        pool = Pool(self.client, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=application_session_unchanged)
        self.addCleanup(pool.close); repo = Repository(); repo.connect = pool.acquire
        with pool.acquire() as warm: warm.commit()
        before = self.servers[0].flights
        with repo.single_read() as tx: tx.execute('SELECT 1').fetchall()
        time.sleep(.05)
        self.assertEqual(self.servers[0].flights - before, 2)   # SELECT, ROLLBACK; release sends nothing
        self.assertEqual([payload for command, payload in self.servers[0].commands[-2:]], [b'SELECT 1', b'ROLLBACK'])
        self.assertEqual(len(self.servers), 1)

    def test_unproven_sessions_open_an_explicit_read_only_snapshot_first(self):
        for status in (0x1, 0x2, None):
            with self.subTest(status=status):
                raw = wire.StatusConnection(); raw.server_status = status
                with self.leased(raw).single_read() as tx: tx.execute('SELECT 1')
                self.assertEqual([sql for sql, _ in raw.sql], ['START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', 'SELECT 1'])
        raw = wire.FakeConnection()   # unpooled: server session defaults
        with self.leased(raw).single_read() as tx: tx.execute('SELECT 1')
        self.assertEqual([sql for sql, _ in raw.sql], ['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',
                                                      'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY', 'SELECT 1'])

    def test_anything_but_one_plain_select_is_rejected_before_it_is_sent(self):
        for sql in ('UPDATE reveal_records SET version=1', 'SELECT 1 FOR UPDATE', 'SELECT 1 FOR SHARE', 'SELECT @x:=1',
                    'SELECT 1 INTO @x', "SELECT GET_LOCK('a',0)", 'SHOW SESSION STATUS', 'SELECT SLEEP(1)', b'SELECT 1'):
            with self.subTest(sql=sql):
                raw = wire.StatusConnection()
                with self.assertRaisesRegex(RuntimeError, 'plain SELECT'):
                    with self.leased(raw).single_read() as tx: tx.execute(sql)
                self.assertEqual((raw.sql, raw.closed), ([], True))
        class Empty(wire.FakeCursor):
            def fetchone(self): return None
        raw = wire.StatusConnection(); raw.cursor = lambda *args: Empty(raw)
        with self.assertRaisesRegex(RuntimeError, 'use read_transaction'):
            with self.leased(raw).single_read() as tx: tx.get('job', 'a'); tx.get('queue', 'a')
        self.assertEqual((len(raw.sql), raw.trips, raw.closed), (1, 2, True))   # SELECT, then ROLLBACK

    def test_readiness_is_one_read_on_a_verified_pooled_session(self):
        raw = wire.StatusConnection(); raw.reveal_verified_tls = True
        self.assertEqual(self.leased(raw).readiness(), {'database': 'aurora-mysql', 'tls': True})
        self.assertEqual(([sql for sql, _ in raw.sql], raw.trips), (['SELECT 1 FROM reveal_records LIMIT 1'], 2))
        class Cipher(wire.FakeCursor):
            def fetchone(self): return ('Ssl_cipher', 'TLS_AES_256_GCM_SHA384')
        raw = wire.StatusConnection(); raw.cursor = lambda *args: Cipher(raw)
        self.assertEqual(self.leased(raw).readiness(), {'database': 'aurora-mysql', 'tls': True})  # not from connect(): SHOW

    def test_sqlite_single_read_sees_committed_rows_and_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(str(Path(directory) / 'app.sqlite')); repo.migrate()
            with repo.transaction() as tx: tx.put('vector_active', 'scope', 'system', {'snapshot_id': 's1'})
            with repo.single_read() as tx: self.assertEqual(tx.get('vector_active', 'scope')['data'], {'snapshot_id': 's1'})
            with self.assertRaises(RuntimeError):
                with repo.single_read() as tx: tx.put('vector_active', 'scope', 'system', {'snapshot_id': 's2'})
            with repo.single_read() as tx: self.assertEqual(tx.get('vector_active', 'scope')['data'], {'snapshot_id': 's1'})
            self.assertEqual(repo.readiness(), {'database': 'sqlite-test', 'tls': False})


class WriterGateTests(unittest.TestCase):
    def setUp(self):
        for item in (patch.object(repository, '_writer_gates', {}), patch.object(repository, 'SESSION_LOCK_WAIT_SECONDS', 0.2)):
            item.start(); self.addCleanup(item.stop)
        self.connects = 0

    def repo(self, prefix=None):
        repo = Repository(table_prefix=prefix)
        def connect():
            self.connects += 1; return wire.StatusConnection()
        repo.connect = connect; return repo

    def hold(self, repo, count):
        entered, release = threading.Barrier(count + 1), threading.Event()
        def writer():
            with repo.transaction(): entered.wait(2); release.wait(2)
        executor = ThreadPoolExecutor(max_workers=count); futures = [executor.submit(writer) for _ in range(count)]
        entered.wait(2)
        def finish():
            release.set(); [future.result(2) for future in futures]; executor.shutdown()
        self.addCleanup(finish); return finish

    def test_third_writer_waits_in_process_then_gets_a_retryable_timeout_without_a_session(self):
        repo = self.repo(); finish = self.hold(repo, 2)
        started = time.monotonic()
        with self.assertRaises(DatabaseBusy) as caught:
            with repo.transaction(): pass
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertLess(time.monotonic() - started, 1); self.assertEqual(self.connects, 2)
        with self.repo('reveal_other').transaction(): pass      # gates are per table prefix
        finish()
        with repo.transaction(): pass
        self.assertEqual(self.connects, 4)

    def test_writers_queued_behind_an_external_fence_holder_longer_than_the_pool_wait_still_succeed(self):
        lock_wait, fence, counter = 1.5, threading.Lock(), threading.Lock()   # scaled 15 s innodb_lock_wait_timeout
        open_sessions, peak = [0], [0]
        class Fenced(wire.FakeCursor):
            def execute(self, sql, params=()):
                if sql.endswith('FOR UPDATE'):
                    if not fence.acquire(timeout=lock_wait): raise pymysql.err.OperationalError(1205, 'Lock wait timeout exceeded')
                    self.connection.holding = True
                return super().execute(sql, params)
        class Session(wire.StatusConnection):
            holding = False
            def __init__(self):
                super().__init__()
                with counter: open_sessions[0] += 1; peak[0] = max(peak[0], open_sessions[0])
            def cursor(self, *args): return Fenced(self)
            def end(self):
                if self.holding: self.holding = False; fence.release()
            def commit(self): super().commit(); self.end()
            def rollback(self): super().rollback(); self.end()
            def close(self):
                if not self.closed:
                    with counter: open_sessions[0] -= 1
                super().close(); self.end()
        repo = Repository(); repo.connect = Session
        fence.acquire(); holder = threading.Timer(0.6, fence.release)   # another process holds the fence 0.6 s
        holder.start(); self.addCleanup(holder.cancel)
        def writer(index):
            time.sleep(0.02 * index)
            with repo.transaction(): time.sleep(0.05)
            return 'ok'
        with patch.object(repository, 'SESSION_LOCK_WAIT_SECONDS', lock_wait), \
                patch.dict(os.environ, {'REVEAL_MYSQL_POOL_WAIT_SECONDS': '0.1'}), ThreadPoolExecutor(3) as executor:
            self.assertEqual(list(executor.map(writer, range(3))), ['ok'] * 3)
        self.assertEqual((peak[0], open_sessions[0]), (repository.WRITERS, 0))   # the third never parked a session

    def test_nowait_writer_never_queues_on_the_fence_or_the_gate(self):
        class Held(wire.FakeCursor):   # another process holds the fence row
            def execute(self, sql, params=()):
                if sql.endswith('FOR UPDATE NOWAIT'):
                    self.connection.trips += 1; self.connection.sql.append((sql, params))
                    raise pymysql.err.OperationalError(3572, 'Statement aborted because lock(s) could not be acquired immediately and NOWAIT is set.')
                return super().execute(sql, params)
        sessions = []
        def connect():
            session = wire.StatusConnection(); session.cursor = lambda *args: Held(session); sessions.append(session); return session
        repo = Repository(); repo.connect = connect
        with self.assertRaises(repository.FenceBusy) as caught:
            with repo.transaction(nowait=True): self.fail('entered without the fence')
        self.assertIsInstance(caught.exception, DatabaseBusy)   # still the retryable 503 if it ever reached a route
        self.assertEqual(([sql for sql, _ in sessions[0].sql], sessions[0].closed), ([repository.FENCE + ' NOWAIT'], True))
        self.assertEqual(repository.writer_gate(repo.table_prefix)._value, repository.WRITERS)
        with repo.transaction(): pass   # ordinary writers still wait on the plain fence
        self.assertEqual(sessions[1].sql[0][0], repository.FENCE)
        finish = self.hold(repo, 2); started = time.monotonic()
        with self.assertRaises(repository.FenceBusy):   # this process's writer slots are full: no wait either
            with repo.transaction(nowait=True): pass
        self.assertLess(time.monotonic() - started, 0.1); self.assertEqual(len(sessions), 4)
        finish()

    def test_gate_is_released_on_failures_and_before_publication(self):
        repo = self.repo()
        for _ in range(3):
            with self.assertRaises(LookupError):
                with repo.transaction(): raise LookupError('handler failed')
        with patch.object(repo, 'connect', side_effect=DatabaseBusy('Application database connection pool is busy')):
            for _ in range(3):
                with self.assertRaises(DatabaseBusy):
                    with repo.transaction(): pass
        self.assertEqual(repository.writer_gate(repo.table_prefix)._value, repository.WRITERS)
        published = []
        def publish(owner, pending):   # publication opens its own fenced transaction
            with owner.transaction(): published.append(pending)
        with patch.object(repository, 'WRITERS', 1), patch.object(repository, '_writer_gates', {}), \
                patch('reveal_backend.workspace_events.prepare_commit', side_effect=[['pending'], []]), \
                patch('reveal_backend.workspace_events.publish_committed', side_effect=lambda owner, pending: publish(owner, pending) if pending else None):
            with repo.transaction(): pass
        self.assertEqual(published, [['pending']])

    def test_fork_and_sqlite_are_not_gated_by_inherited_state(self):
        gate = repository.writer_gate('reveal'); gate.acquire(); gate.acquire()
        with patch.object(repository, '_writer_lock', threading.Lock()):
            repository._after_fork()
            self.assertIsNot(repository.writer_gate('reveal'), gate)
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(str(Path(directory) / 'app.sqlite')); repo.migrate()
            with patch.object(repository, 'writer_gate', side_effect=AssertionError('SQLite is serialized by BEGIN IMMEDIATE')):
                with repo.transaction() as tx: tx.put('job', 'a', 'alice', {})


class BusyResponseTests(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token

    def test_database_busy_is_the_existing_retryable_503(self):
        user = self.provision()
        for message in ('Application database writers are busy', 'Application database connection pool is busy'):
            with self.subTest(message=message), patch.object(self.repo, 'transaction', side_effect=DatabaseBusy(message)):
                response = self.client.get('/v1/me', headers={'Authorization': 'Bearer ' + self.token(user)})
                self.assertEqual(response.status_code, 503)
                self.assertEqual((response.json()['code'], response.json()['retryable']), ('SERVICE_UNAVAILABLE', True))
                self.assertNotIn('busy', response.text)


if __name__ == '__main__': unittest.main()
