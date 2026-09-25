"""Opt-in real MySQL tests, restricted to a disposable localhost test database.

REVEAL_TEST_MYSQL_PORT=<mapped container port> runs these tests. The test database
must be cyaka_dismech_test and contain no production data. Each test uses a distinct
immutable import; existing unrelated imports are not deleted.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from dismech_fixture import write_fixture
from reveal_backend import dismech_import as importer


@unittest.skipUnless(os.getenv('REVEAL_TEST_MYSQL_PORT'), 'Disposable MySQL port not configured')
class DismechMySQLTests(unittest.TestCase):
    def setUp(self):
        import pymysql
        self.connection = pymysql.connect(host='127.0.0.1', port=int(os.environ['REVEAL_TEST_MYSQL_PORT']),
            user='root', password=os.getenv('REVEAL_TEST_MYSQL_PASSWORD', 'local-test-only'),
            database='cyaka_dismech_test', charset='utf8mb4', autocommit=False)
        self.addCleanup(self.connection.close)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        source, gaps = write_fixture(temp.name)
        # Distinct content identity without destructive cleanup between tests.
        (source / 'schema-vocabularies.json').write_text(importer.canonical({'test': str(uuid.uuid4())}))
        self.export = importer.open_export(source, gaps)

    def test_full_load_replay_and_readback(self):
        first = importer.load_export(self.connection, self.export, batch_size=1)
        replay = importer.load_export(self.connection, self.export, batch_size=2)
        self.assertEqual(first, replay)
        self.assertTrue(importer.verify_export(self.connection, self.export)['verified'])
        with self.connection.cursor() as q:
            q.execute('SELECT status FROM dismech_discussions WHERE import_id=%s AND kind=%s', (self.export.import_id, 'HUMAN_MODEL_MISMATCH'))
            self.assertIsNone(q.fetchone()[0])
            q.execute('SELECT target_mechanism_sha256,target_id FROM dismech_gap_attachments WHERE import_id=%s AND resolution=%s', (self.export.import_id, 'ambiguous_target'))
            self.assertEqual(q.fetchone(), (None, None))

    def test_interrupted_batch_rolls_back_then_resumes(self):
        insert = importer.insert_batch
        calls = 0
        def fail(cursor, table, columns, batch):
            nonlocal calls
            insert(cursor, table, columns, batch)
            if table == 'dismech_discussions':
                calls += 1
                if calls == 2:
                    raise ConnectionError('simulated interruption before checkpoint')
        with patch.object(importer, 'insert_batch', side_effect=fail), self.assertRaises(ConnectionError):
            importer.load_export(self.connection, self.export, batch_size=1)
        with self.connection.cursor() as q:
            q.execute('SELECT COUNT(*) FROM dismech_discussions WHERE import_id=%s', (self.export.import_id,))
            self.assertEqual(q.fetchone()[0], 1)
            q.execute('SELECT status,progress FROM dismech_imports WHERE import_id=%s', (self.export.import_id,))
            status, progress = q.fetchone()
            self.assertEqual(status, 'loading')
        result = importer.load_export(self.connection, self.export, batch_size=1)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(importer.verify_export(self.connection, self.export)['verified'])

    def test_same_count_payload_corruption_is_detected(self):
        importer.load_export(self.connection, self.export)
        with self.connection.cursor() as q:
            q.execute("UPDATE dismech_discussions SET prompt='changed' WHERE import_id=%s", (self.export.import_id,))
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, 'read-back differs'):
            importer.load_export(self.connection, self.export)
        with self.assertRaisesRegex(ValueError, 'read-back differs'):
            importer.verify_export(self.connection, self.export)

    def test_foreign_keys_enforced(self):
        import pymysql
        importer.load_export(self.connection, self.export)
        with self.connection.cursor() as q, self.assertRaises(pymysql.IntegrityError):
            q.execute('UPDATE dismech_gap_attachments SET target_mechanism_sha256=%s WHERE import_id=%s', ('f' * 64, self.export.import_id))
        self.connection.rollback()

    def test_concurrent_loader_lock_is_respected(self):
        import pymysql
        other = pymysql.connect(host='127.0.0.1', port=int(os.environ['REVEAL_TEST_MYSQL_PORT']),
            user='root', password=os.getenv('REVEAL_TEST_MYSQL_PASSWORD', 'local-test-only'), database='cyaka_dismech_test')
        self.addCleanup(other.close)
        lock = 'dismech:' + importer.digest('cyaka_dismech_test:' + self.export.import_id)[:56]
        with other.cursor() as q:
            q.execute('SELECT GET_LOCK(%s,0)', (lock,)); self.assertEqual(q.fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'Another loader'):
            importer.load_export(self.connection, self.export)
        with other.cursor() as q:
            q.execute('SELECT RELEASE_LOCK(%s)', (lock,))


if __name__ == '__main__':
    unittest.main()
