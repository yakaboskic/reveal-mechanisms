"""Connection cleanup must not replace the actionable original import error."""
from unittest.mock import patch
from pymysql.err import InterfaceError, OperationalError

import test_dismech_embeddings as fixtures
from test_eaggl import SQLiteCursor
from reveal_backend import dismech_embeddings as embeddings


class LoaderCleanupTests(fixtures.CaptureFixture):
    connect = fixtures.DatabaseLoadingTests.connect

    def test_disconnect_survives_failed_rollback_and_lock_cleanup(self):
        self.complete()
        connection=self.connect()
        original=OperationalError(2013,'original transport disconnect')
        execute=SQLiteCursor.execute
        def release_fails(cursor,sql,values=()):
            if 'RELEASE_LOCK(' in sql: raise InterfaceError(0,'secondary lock cleanup failure')
            return execute(cursor,sql,values)
        with patch.object(connection,'rollback',side_effect=InterfaceError(0,'secondary rollback failure')), \
             patch.object(SQLiteCursor,'execute',release_fails), \
             patch.object(embeddings,'insert_batch',side_effect=original), \
             self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(OperationalError) as caught:
                embeddings.load_capture(connection,self.output)
        self.assertIs(caught.exception,original)
        self.assertEqual(len(logs.output),2)
        self.assertTrue(all('preserving original OperationalError' in line for line in logs.output))

    def test_original_disconnect_still_releases_lock_when_rollback_alone_fails(self):
        self.complete()
        connection=self.connect()
        original=ConnectionError('original insert failure')
        with patch.object(connection,'rollback',side_effect=RuntimeError('rollback failed')), \
             patch.object(embeddings,'insert_batch',side_effect=original), self.assertLogs(level='WARNING'):
            with self.assertRaises(ConnectionError) as caught:
                embeddings.load_capture(connection,self.output)
        self.assertIs(caught.exception,original)
        self.assertTrue(connection.lock_released)

    def test_cleanup_failure_without_original_error_is_not_ignored(self):
        self.complete()
        connection=self.connect()
        cleanup=ConnectionError('lock release transport failure')
        execute=SQLiteCursor.execute
        def release_fails(cursor,sql,values=()):
            if 'RELEASE_LOCK(' in sql: raise cleanup
            return execute(cursor,sql,values)
        with patch.object(SQLiteCursor,'execute',release_fails):
            with self.assertRaises(ConnectionError) as caught:
                embeddings.load_capture(connection,self.output)
        self.assertIs(caught.exception,cleanup)
        self.assertEqual(connection.db.execute('SELECT status FROM dismech_embedding_runs').fetchone(),('complete',))
        self.assertEqual(embeddings.load_capture(connection,self.output)['status'],'complete')
