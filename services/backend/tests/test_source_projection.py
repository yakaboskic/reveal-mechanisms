"""Source verification uses bounded import scans without a text-row join."""
import unittest

from reveal_backend.dismech_embeddings import _database_inputs, context_input
from test_eaggl import SQLiteMySQLAdapter, SQLiteCursor


class SourceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = SQLiteMySQLAdapter(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.db.executescript('''
            CREATE TABLE dismech_imports(import_id TEXT,status TEXT);
            CREATE TABLE dismech_documents(import_id TEXT,id_sha256 TEXT,source_sha256 TEXT);
            CREATE TABLE dismech_mechanisms(import_id TEXT,source_id TEXT,name TEXT,description TEXT,document_sha256 TEXT);
            CREATE TABLE dismech_discussions(import_id TEXT,id_sha256 TEXT,source_id TEXT,prompt TEXT,document_sha256 TEXT,is_gap INTEGER);
            CREATE TABLE dismech_gap_attachments(import_id TEXT,gap_sha256 TEXT,target_mechanism_sha256 TEXT);
            INSERT INTO dismech_imports VALUES ('selected','complete');
            INSERT INTO dismech_documents VALUES ('selected','doc-a','rev-a'),('selected','doc-b','rev-b'),('other','doc-a','wrong-rev');
            INSERT INTO dismech_mechanisms VALUES ('selected','mech-a','Name A','same text','doc-a'),('selected','mech-b','same text',NULL,'doc-b'),('other','wrong-mech','wrong','wrong','doc-a');
            INSERT INTO dismech_discussions VALUES ('selected','g1','gap-linked','linked question','doc-a',1),('selected','g2','gap-fallback','unlinked question','doc-b',1),('selected','g3','non-gap','other discussion','doc-a',0);
            INSERT INTO dismech_gap_attachments VALUES ('selected','g1','mech-a'),('selected','g1','mech-b'),('selected','g2',NULL),('other','g2','other-mechanism');
        ''')

    def test_clustered_scans_preserve_native_revision_name_fallback_and_unlinked_gaps(self):
        queries = []
        connection = self.connection
        class RecordingCursor(SQLiteCursor):
            def execute(self, sql, values=()):
                queries.append((sql, values))
                return super().execute(sql, values)
        connection.cursor = lambda: RecordingCursor(connection)
        expected = [context_input('mech-a','mechanism','rev-a','same text'),
                    context_input('mech-b','mechanism','rev-b','same text'),
                    context_input('gap-fallback','knowledge_gap','rev-b','unlinked question')]
        expected = [{key:value for key,value in row.items() if key != 'input_text'} for row in expected]
        self.assertEqual(_database_inputs(connection,'selected'), sorted(expected,key=lambda row:row['source_id']))
        self.assertEqual(len(queries),5)
        self.assertTrue(all('JOIN' not in query and 'NOT EXISTS' not in query for query,_ in queries))
        self.assertTrue(all('FORCE INDEX(PRIMARY)' in query for query,_ in queries[1:]))
        self.assertTrue(all(values == ('selected',) for _,values in queries))

    def test_missing_document_revision_fails_without_omitting_native_source(self):
        self.connection.db.execute("DELETE FROM dismech_documents WHERE import_id='selected' AND id_sha256='doc-b'")
        with self.assertRaisesRegex(ValueError,'missing document revision'):
            _database_inputs(self.connection,'selected')

    def test_incomplete_source_import_is_rejected_before_projection(self):
        self.connection.db.execute("UPDATE dismech_imports SET status='loading'")
        with self.assertRaisesRegex(ValueError,'not complete'):
            _database_inputs(self.connection,'selected')
