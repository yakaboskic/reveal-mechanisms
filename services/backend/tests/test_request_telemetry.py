"""Pool wait, connects, resets, COMMIT and the global-lock wait are timed; each request logs one JSON line."""
import io
import json
import logging
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from reveal_backend import app as api, runtime_config, runtime_metrics
from reveal_backend.mysql_database import application_session_unchanged, reset_application_session
from reveal_backend.mysql_pool import DatabaseBusy, Pool
from reveal_backend.repository import Repository
import test_application as application
import test_mysql_pool as wire


class DatabaseTimingTests(unittest.TestCase):
    def pool(self, observer, **kwargs):
        self.created = []
        def factory():
            connection = wire.StatusConnection(); self.created.append(connection); return connection
        pool = Pool(factory, lambda c: reset_application_session(c, 'cyaka_expected'), unchanged=application_session_unchanged,
                    observer=observer, **kwargs)
        self.addCleanup(pool.close); return pool

    def test_pool_reports_queue_wait_skipped_resets_and_timeouts(self):
        seen = []; pool = self.pool(lambda name, ms, failed: seen.append((name, failed)), maximum=1, wait_seconds=.05)
        lease = pool.acquire(); lease.commit(); lease.close()                          # cold, then a clean release
        lease = pool.acquire(); lease.cursor().execute('SET @x=1'); lease.commit()     # warm; dirty, so reset
        with self.assertRaises(DatabaseBusy): pool.acquire()
        lease.close()
        self.assertEqual(seen, [('ACQUIRE', False), ('RESET_SKIPPED', False), ('ACQUIRE', False), ('ACQUIRE', True)])
        self.assertEqual((len(self.created), self.created[0].reset_count), (1, 1))

    def test_a_failing_observer_never_changes_a_lease_decision(self):
        pool = self.pool(MagicMock(side_effect=RuntimeError('metrics down')))
        lease = pool.acquire(); lease.commit(); lease.close()
        again = pool.acquire(); self.assertIs(again._entry.connection, self.created[0]); again.close()
        self.assertEqual(self.created[0].reset_count, 0)

    def test_runtime_pool_and_every_connect_feed_runtime_metrics(self):
        seen = []
        record = lambda category, name, ms, failed=False, extra=None: seen.append((category, name, failed))
        settings = wire.RuntimePoolSettingsTests('test_bounds_keep_idle_expiry_below_the_server_kill')
        settings.setUp(); self.addCleanup(settings.doCleanups); self.addCleanup(settings.tearDown)
        with patch.object(runtime_metrics, 'observe', record):
            lease = settings.lease(); lease.commit(); lease.close()
            with patch.dict(os.environ, {'REVEAL_MYSQL_PASSWORD': 'test-only'}), \
                    patch('reveal_backend.mysql_database.connect', side_effect=[object(), OSError('refused')]):
                runtime_config.mysql_connection()
                with self.assertRaises(OSError): runtime_config.mysql_connection()
        self.assertEqual(seen, [('database', 'ACQUIRE', False), ('database', 'RESET_SKIPPED', False),
                                ('database', 'CONNECT', False), ('database', 'CONNECT', True)])

    def test_repository_names_the_fence_commit_lock_hold_and_writer_wait(self):
        seen = []; repo = Repository(); repo.connect = wire.StatusConnection
        with patch.object(runtime_metrics, 'observe', lambda category, name, ms, failed=False, extra=None: seen.append((name, failed))):
            with repo.transaction() as tx: tx.execute('SELECT 1')
            with self.assertRaises(LookupError):
                with repo.read_transaction() as tx: raise LookupError('404')
            with repo.single_read() as tx: tx.execute('SELECT 1')
        self.assertEqual(seen, [('WRITER_WAIT', False), ('FENCE', False), ('SELECT', False), ('COMMIT', False),
                                ('LOCK_HOLD', False), ('START', False), ('ROLLBACK', False), ('SELECT', False), ('ROLLBACK', False)])

    def test_request_cost_attributes_database_rows_by_kind(self):
        cost, token = runtime_metrics.begin_request()
        try:
            for name, ms in (('ACQUIRE', 2), ('WRITER_WAIT', 1), ('CONNECT', 100), ('FENCE', 30), ('SELECT', 1),
                             ('COMMIT', 1), ('RESET', 1), ('RESET_SKIPPED', 0), ('LOCK_HOLD', 50), ('PING', 1)):
                runtime_metrics.observe('database', name, ms)
            runtime_metrics.observe('research_query', 'source', 999)
        finally: runtime_metrics.end_request(token)
        runtime_metrics.observe('database', 'SELECT', 5)   # outside any request
        self.assertEqual(cost, {'statements': 3, 'db_ms': 134, 'pool_wait_ms': 3, 'lock_wait_ms': 30, 'connects': 1, 'resets': 1})


class RequestLogTests(unittest.TestCase):
    setUp = application.ApplicationTests.setUp
    provision = application.ApplicationTests.provision
    token = application.ApplicationTests.token
    headers = application.ApplicationTests.headers

    def capture(self):
        stream = io.StringIO(); handler = logging.StreamHandler(stream); logger = logging.getLogger('reveal.access')
        logger.addHandler(handler); self.addCleanup(logger.removeHandler, handler); return stream

    @staticmethod
    def lines(stream): return [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_one_line_per_request_with_the_route_template_and_its_database_cost(self):
        user = self.provision()
        draft = self.client.post('/v1/drafts', json={'composer': application.COMPOSER}, headers=self.headers(user)).json()
        stream = self.capture()
        response = self.client.get('/v1/drafts/' + draft['id'] + '?probe=private-value', headers=self.headers(user))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/v1/nothing-here/' + draft['id']).status_code, 404)
        first, missing = self.lines(stream)
        self.assertEqual((first['event'], first['route'], first['status']), ('http', 'GET /v1/drafts/{draft_id}', 200))
        self.assertEqual(set(first), {'event', 'route', 'status', 'ms'} | set(runtime_metrics.REQUEST_FIELDS))
        self.assertGreaterEqual(first['statements'], 3)   # principal, draft and COMMIT at least
        self.assertEqual((first['connects'], first['resets'], first['lock_wait_ms']), (0, 0, 0))   # SQLite
        self.assertEqual((missing['route'], missing['status'], missing['statements']), ('GET unmatched', 404, 0))
        for private in (draft['id'], user, 'private-value', 'probe'): self.assertNotIn(private, stream.getvalue())
        row = next(r for r in runtime_metrics.metrics()['rows'] if (r['category'], r['name']) == ('http', 'GET /v1/drafts/{draft_id}'))
        self.assertGreater(row['mean_statements'], 0); self.assertIn('mean_db_ms', row)

    def test_failures_are_logged_with_their_status_and_the_log_can_be_disabled(self):
        user = self.provision(); stream = self.capture(); client = TestClient(api.app, raise_server_exceptions=False)
        with patch.object(self.repo, 'transaction', side_effect=DatabaseBusy('Application database writers are busy')):
            self.assertEqual(client.get('/v1/me', headers={'Authorization': 'Bearer ' + self.token(user)}).status_code, 503)
        with patch.object(self.repo, 'transaction', side_effect=RuntimeError('unexpected')):
            self.assertEqual(client.get('/v1/me', headers={'Authorization': 'Bearer ' + self.token(user)}).status_code, 503)
        self.assertEqual([(line['route'], line['status']) for line in self.lines(stream)], [('GET /v1/me', 503), ('GET /v1/me', 500)])
        with patch.dict(os.environ, {'REVEAL_ACCESS_LOG': '0'}): self.client.get('/v1/me')
        self.assertEqual(len(self.lines(stream)), 2)


if __name__ == '__main__': unittest.main()
