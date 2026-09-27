"""Reduced-transfer verification must still compare exact stored source/vector bytes."""
import re

import test_dismech_embeddings as fixtures
from reveal_backend import dismech_embeddings as embeddings
from reveal_backend.dismech_import import digest


class TransferVerificationTests(fixtures.CaptureFixture):
    connect = fixtures.DatabaseLoadingTests.connect

    def loaded(self):
        self.complete()
        connection = self.connect()
        embeddings.load_capture(connection, self.output, batch_size=1)
        return connection

    def test_bulk_verification_returns_computed_hashes_and_lengths_not_source_or_vector_payloads(self):
        connection = self.loaded()
        statements = []
        connection.db.set_trace_callback(statements.append)
        result = embeddings.verify_capture(connection, self.output)
        self.assertTrue(result['verified'])
        selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
        vectors = [sql for sql in selects if 'FROM dismech_embedding_vectors ' in sql]
        self.assertTrue(vectors)
        for sql in vectors:
            projection = sql.split(' FROM ')[0]
            self.assertIn('SHA2(input_text,256)', projection)
            self.assertIn('SHA2(vector,256)', projection)
            self.assertIn('OCTET_LENGTH(vector)', projection)
            self.assertIn('vector_sha256', projection)
            self.assertIsNone(re.search(r'(?:SELECT|,)\s*(?:vector|input_text)\s*(?:,|$)', projection))
        source = [sql for sql in selects if 'FROM dismech_mechanisms ' in sql or 'FROM dismech_discussions ' in sql]
        self.assertEqual(len(source), 2)
        self.assertTrue(all('SHA2(' in sql for sql in source))
        self.assertTrue(all('JOIN' not in sql.upper() for sql in source))

    def test_changed_same_length_vector_bytes_fail_even_when_stored_checksum_is_unchanged(self):
        connection = self.loaded()
        identity, original, checksum = connection.db.execute(
            'SELECT input_sha256,vector,vector_sha256 FROM dismech_embedding_vectors LIMIT 1').fetchone()
        changed = bytes([original[0] ^ 1]) + original[1:]
        self.assertEqual(len(changed), len(original))
        connection.db.execute('UPDATE dismech_embedding_vectors SET vector=? WHERE input_sha256=?', (changed, identity))
        connection.commit()
        self.assertEqual(connection.db.execute('SELECT vector_sha256 FROM dismech_embedding_vectors WHERE input_sha256=?',
                                               (identity,)).fetchone()[0], checksum)
        with self.assertRaises(ValueError):
            embeddings.verify_capture(connection, self.output)
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)

    def test_recomputed_database_checksum_cannot_replace_trusted_local_vector(self):
        connection = self.loaded()
        identity, original = connection.db.execute('SELECT input_sha256,vector FROM dismech_embedding_vectors LIMIT 1').fetchone()
        changed = bytes([original[0] ^ 1]) + original[1:]
        connection.db.execute('UPDATE dismech_embedding_vectors SET vector=?,vector_sha256=? WHERE input_sha256=?',
                              (changed, digest(changed), identity))
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.verify_capture(connection, self.output)
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)

    def test_stored_text_hash_uses_exact_utf8_content_even_at_equal_byte_length(self):
        connection = self.loaded()
        changed = fixtures.GAP_TEXT.replace('β', 'γ')
        self.assertEqual(len(changed.encode('utf-8')), len(fixtures.GAP_TEXT.encode('utf-8')))
        connection.db.execute('UPDATE dismech_embedding_vectors SET input_text=? WHERE input_sha256=?',
                              (changed, digest(fixtures.GAP_TEXT)))
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.verify_capture(connection, self.output)
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)

    def test_empty_description_name_fallback_is_exact_and_whitespace_is_not_absence(self):
        connection = self.loaded()
        _, inputs = embeddings.open_capture(self.output)
        expected = [{key: value for key, value in row.items() if key != 'input_text'} for row in inputs]
        self.assertEqual(embeddings._database_inputs(connection, self.export.import_id), expected)
        connection.db.execute("UPDATE dismech_mechanisms SET description=''")
        connection.commit()
        self.assertEqual(embeddings._database_inputs(connection, self.export.import_id), expected)
        self.assertTrue(embeddings.verify_capture(connection, self.output)['verified'])
        connection.db.execute("UPDATE dismech_mechanisms SET description=' '")
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.verify_capture(connection, self.output)
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)

    def test_source_prompt_unicode_mutation_is_detected_before_a_new_run_is_created(self):
        self.complete()
        connection = self.connect()
        connection.db.execute('UPDATE dismech_discussions SET prompt=? WHERE source_id=?',
                              (fixtures.GAP_TEXT.replace('α', 'Α'), 'dismech:disorders/A#discussion:mismatch'))
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM dismech_embedding_runs').fetchone()[0], 0)
