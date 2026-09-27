"""Offline source/vector integrity and resumability checks for DisMech retrieval.

Synthetic vectors exercise contracts only; these are not scientific retrieval
quality tests and make no embedding-service or real database requests.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np

from dismech_fixture import records, rewrite, write_fixture
from test_eaggl import SQLiteMySQLAdapter
from reveal_backend import dismech_embeddings as embeddings
from reveal_backend.dismech_import import canonical, digest, open_export


MECHANISM_TEXT = 'Nested source mechanism'
GAP_TEXT = 'Does α affect β?'
CALIBRATION_TEXTS = ['Known EAGGL label alpha', 'Known EAGGL label beta']


def target_run(**changes):
    config = {'import_id': 'e' * 64, 'model': 'synthetic-model',
              'model_revision': 'declared-revision-1', 'provider': 'test',
              'service_url': 'https://example.invalid', 'template': 'factor-label-v1',
              'dtype': 'float32-le', 'normalization': 'none', 'metric': 'cosine'}
    config.update(changes)
    calibration = []
    for text, value in zip(CALIBRATION_TEXTS, ([1., 0.], [0., 1.])):
        vector = np.asarray(value, dtype='<f4')
        calibration.append({'input_sha256': digest(text), 'input_text': text,
                            'vector': vector.tolist(), 'vector_sha256': digest(vector.tobytes())})
    return {'run_id': digest(canonical(config)), 'config': config,
            'dimensions': 2, 'calibration': sorted(calibration, key=lambda row: row['input_sha256'])}


def embed(texts, **kwargs):
    values = {CALIBRATION_TEXTS[0]: [1., 0.], CALIBRATION_TEXTS[1]: [0., 1.],
              MECHANISM_TEXT: [1., 2.], GAP_TEXT: [2., 1.]}
    return np.asarray([values[text] for text in texts], dtype=np.float32)


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.gaps = write_fixture(self.root)
        # Equal text may share one vector while both native identities and the
        # different source-file revisions must survive the capture.
        mechanisms = records(self.source, 'mechanisms.jsonl.gz')
        second = deepcopy(mechanisms[0])
        second.update(id='dismech:disorders/A#/pathophysiology/0',
                      document_id='dismech:disorders/A', source_file='kb/disorders/A.yaml')
        mechanisms.append(second)
        rewrite(self.source, 'mechanisms.jsonl.gz', mechanisms)
        self.export = open_export(self.source, self.gaps)
        self.target = target_run()
        self.output = self.root / 'capture'

    def prepare(self, **kwargs):
        return embeddings.prepare_capture(self.output, self.export, kwargs.get('target', self.target))

    def complete(self, **kwargs):
        self.prepare()
        return embeddings.embed_capture(self.output, embedder=kwargs.pop('embedder', embed), **kwargs)


class SourceCaptureTests(CaptureFixture):
    def test_exact_native_revision_bindings_deduplicate_text_and_include_unlinked_gap(self):
        manifest = self.prepare()
        reopened, inputs = embeddings.open_capture(self.output)
        self.assertEqual(manifest, reopened)
        self.assertEqual(len(inputs), 3)
        mechanisms = [row for row in inputs if row['source_kind'] == 'mechanism']
        gaps = [row for row in inputs if row['source_kind'] == 'knowledge_gap']
        self.assertEqual(len(mechanisms), 2)
        self.assertEqual({row['source_id'] for row in mechanisms},
                         {'dismech:disorders/A#/pathophysiology/0', 'dismech:modules/B#/pathophysiology/0'})
        self.assertEqual({row['source_revision'] for row in mechanisms},
                         {digest('kb/disorders/A.yaml'), digest('kb/modules/B.yaml')})
        self.assertEqual({row['input_text'] for row in mechanisms}, {MECHANISM_TEXT})
        self.assertEqual({row['template'] for row in mechanisms}, {'dismech-description-v1'})
        self.assertEqual(gaps[0]['source_id'], 'dismech:disorders/A#discussion:mismatch')
        self.assertEqual(gaps[0]['source_revision'], digest('kb/disorders/A.yaml'))
        self.assertEqual(gaps[0]['input_text'], GAP_TEXT)
        self.assertEqual(gaps[0]['template'], 'dismech-gap-text-v1')
        for row in inputs:
            self.assertEqual(row['input_sha256'], digest(row['input_text']))
        self.assertEqual(manifest['expected_bindings'], 3)
        self.assertEqual(manifest['expected_vectors'], 2)
        self.assertEqual(manifest['target_run'], self.target)
        self.assertEqual(self.prepare(), manifest)

    def test_capture_relocation_preserves_identity_and_changed_target_configuration_does_not(self):
        original = self.prepare()
        moved = self.root / 'moved'
        self.output.rename(moved)
        self.assertEqual(embeddings.open_capture(moved)[0], original)
        for field, value in [('model', 'other-model'), ('model_revision', 'revision-2'),
                             ('provider', 'other-provider'), ('service_url', 'https://other.invalid'),
                             ('import_id', 'f' * 64)]:
            with self.subTest(field=field):
                changed = embeddings.prepare_capture(self.root / field, self.export, target_run(**{field: value}))
                self.assertNotEqual(changed['run_id'], original['run_id'])
        with self.assertRaises(ValueError):
            embeddings.prepare_capture(moved, self.export, target_run(model_revision='revision-2'))

    def test_different_source_revision_changes_run_and_same_id_is_not_silently_reused(self):
        first = self.prepare()
        for root in (self.source, self.gaps):
            path = root / 'source-files.json'
            inventory = json.loads(path.read_text())
            inventory[0]['sha256'] = 'f' * 64
            path.write_text(canonical(inventory))
        changed = open_export(self.source, self.gaps)
        second = embeddings.prepare_capture(self.root / 'revision-2', changed, self.target)
        self.assertNotEqual(first['run_id'], second['run_id'])
        _, inputs = embeddings.open_capture(self.root / 'revision-2')
        actual = next(row for row in inputs if row['source_id'] == 'dismech:disorders/A#/pathophysiology/0')
        self.assertEqual(actual['source_revision'], 'f' * 64)
        with self.assertRaises(ValueError):
            embeddings.prepare_capture(self.output, changed, self.target)

    def test_inventory_and_calibration_source_corruption_rejected_before_embedding(self):
        self.prepare()
        path = self.output / 'inputs.jsonl'
        original = path.read_bytes()
        path.write_bytes(original.replace(MECHANISM_TEXT.encode(), b'Invented mechanism'))
        with self.assertRaises(ValueError):
            embeddings.open_capture(self.output)
        path.write_bytes(original)
        for field, value in [('input_sha256', '0' * 64), ('vector_sha256', '0' * 64),
                             ('vector', [0., 0.])]:
            with self.subTest(field=field):
                target = deepcopy(self.target)
                target['calibration'][0][field] = value
                with self.assertRaises(ValueError):
                    embeddings.prepare_capture(self.root / ('bad-' + field), self.export, target)


class LocalEmbeddingTests(CaptureFixture):
    def test_calibrated_complete_replay_never_calls_remote_and_exact_vectors_round_trip(self):
        calls = []
        def observed(texts, **kwargs):
            calls.append(list(texts))
            self.assertEqual(kwargs['model'], self.target['config']['model'])
            self.assertEqual(kwargs['provider'], self.target['config']['provider'])
            self.assertEqual(kwargs['service_url'], self.target['config']['service_url'])
            return embed(texts)
        report = self.complete(embedder=observed, batch_size=1)
        manifest, inputs, rows = embeddings.read_local_vectors(self.output)
        self.assertEqual(report['run_id'], manifest['run_id'])
        self.assertEqual({row[0] for row in rows}, {digest(MECHANISM_TEXT), digest(GAP_TEXT)})
        for checksum, text, blob, vector_sha in rows:
            self.assertEqual(checksum, digest(text))
            self.assertEqual(vector_sha, digest(blob))
            np.testing.assert_array_equal(np.frombuffer(blob, dtype='<f4'), embed([text])[0])
        for text in CALIBRATION_TEXTS + [MECHANISM_TEXT, GAP_TEXT]:
            self.assertIn(text, [text for batch in calls for text in batch])
        def forbidden(*args, **kwargs):
            self.fail('A complete, verified capture must be reused without HTTP requests')
        embeddings.embed_capture(self.output, embedder=forbidden)

    def test_interrupted_generation_resumes_missing_texts_only(self):
        self.prepare()
        source_calls = []
        def interrupted(texts, **kwargs):
            if texts and texts[0] not in CALIBRATION_TEXTS:
                source_calls.extend(texts)
                if len(source_calls) == 2:
                    raise ConnectionError('synthetic interruption')
            return embed(texts)
        with self.assertRaises(ConnectionError):
            embeddings.embed_capture(self.output, batch_size=1, max_workers=1, embedder=interrupted)
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        pending = []
        def resumed(texts, **kwargs):
            pending.extend(text for text in texts if text not in CALIBRATION_TEXTS)
            return embed(texts)
        embeddings.embed_capture(self.output, batch_size=1, max_workers=1, embedder=resumed)
        self.assertEqual(pending, source_calls[-1:])
        self.assertEqual(len(embeddings.read_local_vectors(self.output)[2]), 2)

    def test_remote_malformed_vectors_never_publish_complete(self):
        invalid = [[[0., 0.]], [[float('nan'), 1.]], [[1., float('inf')]],
                   [[1., 2., 3.]], [['1', '2']], [[True, False]], [[1., 0.], [0., 1.]]]
        for index, raw in enumerate(invalid):
            with self.subTest(raw=raw):
                output = self.root / ('invalid-' + str(index))
                embeddings.prepare_capture(output, self.export, self.target)
                def malformed(texts, **kwargs):
                    return embed(texts) if all(text in CALIBRATION_TEXTS for text in texts) else raw
                with self.assertRaises((ValueError, TypeError)):
                    embeddings.embed_capture(output, batch_size=1, max_workers=1, embedder=malformed)
                with self.assertRaises(ValueError):
                    embeddings.read_local_vectors(output)

    def test_declared_model_match_does_not_hide_calibration_direction_drift(self):
        self.prepare()
        observed = []
        def drift(texts, **kwargs):
            observed.extend(texts)
            return -embed(texts)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=drift)
        self.assertTrue(observed)
        self.assertTrue(set(observed).issubset(CALIBRATION_TEXTS))
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)

    def test_final_calibration_failure_never_becomes_accepted_stale_vectors_on_retry(self):
        self.prepare()
        calibration_calls = 0
        def switched_deployment(texts, **kwargs):
            nonlocal calibration_calls
            if all(text in CALIBRATION_TEXTS for text in texts):
                calibration_calls += 1
                return embed(texts) if calibration_calls == 1 else -embed(texts)
            return -embed(texts)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=switched_deployment)
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        # A failed calibration can require explicit recapture, or it can force
        # regeneration. Either policy must keep the known-bad vectors unavailable.
        try:
            embeddings.embed_capture(self.output, embedder=embed)
        except ValueError:
            with self.assertRaises(ValueError):
                embeddings.read_local_vectors(self.output)
        else:
            for _, text, blob, _ in embeddings.read_local_vectors(self.output)[2]:
                np.testing.assert_array_equal(np.frombuffer(blob, dtype='<f4'), embed([text])[0])

    def test_cached_vector_text_and_bytes_corruption_rejected_on_read_and_replay(self):
        self.complete()
        path = self.output / 'embeddings.sqlite3'
        with sqlite3.connect(path) as connection:
            original = connection.execute('SELECT input_sha256,input_text,vector,vector_sha256 FROM vectors LIMIT 1').fetchone()
        for column, value in [('input_text', 'invented source text'), ('vector', b'bad-vector'),
                              ('vector_sha256', '0' * 64)]:
            with self.subTest(column=column):
                with sqlite3.connect(path) as connection:
                    connection.execute(f'UPDATE vectors SET {column}=? WHERE input_sha256=?', (value, original[0]))
                with self.assertRaises(ValueError):
                    embeddings.read_local_vectors(self.output)
                with self.assertRaises(ValueError):
                    embeddings.embed_capture(self.output, embedder=lambda *args, **kwargs: self.fail('Corrupt cache reached service'))
                with sqlite3.connect(path) as connection:
                    connection.execute('UPDATE vectors SET input_text=?,vector=?,vector_sha256=? WHERE input_sha256=?',
                                       (original[1], original[2], original[3], original[0]))

    def test_completed_calibration_evidence_must_be_valid_and_bound_to_target(self):
        self.complete()
        path = self.output / 'embeddings.sqlite3'
        with sqlite3.connect(path) as connection:
            original = json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])
        wrong_run = deepcopy(original)
        wrong_run['after']['eaggl_embedding_run_id'] = '0' * 64
        bad_cosine = deepcopy(original)
        bad_cosine['before']['probes'][0]['cosine_similarity'] = 0.5
        bad_probe = deepcopy(original)
        bad_probe['after']['probes'][0]['stored_vector_sha256'] = '0' * 64
        for value in [{}, wrong_run, bad_cosine, bad_probe]:
            with self.subTest(value=value):
                with sqlite3.connect(path) as connection:
                    connection.execute("UPDATE metadata SET value=? WHERE key='calibration'", (canonical(value),))
                with self.assertRaises(ValueError):
                    embeddings.read_local_vectors(self.output)


class DatabaseLoadingTests(CaptureFixture):
    """Real SQLite transactions through the existing MySQL spelling adapter.

The minimal source projections are populated from the validated export. No
source lookup, vector decoding, or loader verification functions are mocked.
This does not claim MySQL engine or Aurora connectivity coverage.
"""
    def connect(self):
        connection = SQLiteMySQLAdapter(self.root / 'database.sqlite3')
        self.addCleanup(connection.close)
        connection.db.executescript('''
            CREATE TABLE dismech_imports (import_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE dismech_documents (import_id TEXT,id_sha256 TEXT,source_sha256 TEXT);
            CREATE TABLE dismech_mechanisms (import_id TEXT,source_id TEXT,name TEXT,description TEXT,document_sha256 TEXT);
            CREATE TABLE dismech_discussions (import_id TEXT,id_sha256 TEXT,source_id TEXT,prompt TEXT,document_sha256 TEXT,is_gap INTEGER);
            CREATE TABLE dismech_gap_attachments (import_id TEXT,gap_sha256 TEXT,target_mechanism_sha256 TEXT);
            CREATE TABLE eaggl_embedding_runs (run_id TEXT PRIMARY KEY,status TEXT,config TEXT,dimensions INTEGER);
            CREATE TABLE eaggl_name_embeddings (run_id TEXT,input_sha256 TEXT,input_text TEXT,vector BLOB,vector_sha256 TEXT);
        ''')
        db, export = connection.db, self.export
        db.execute('INSERT INTO dismech_imports VALUES (?,?)', (export.import_id, 'complete'))
        for row in export.rows['documents']:
            db.execute('INSERT INTO dismech_documents VALUES (?,?,?)',
                       (export.import_id, digest(row['id']), export.source_files['source'][row['source_file']]))
        for row in export.rows['mechanisms']:
            db.execute('INSERT INTO dismech_mechanisms VALUES (?,?,?,?,?)',
                       (export.import_id, row['id'], row['name'], row['description'], digest(row['document_id'])))
        for row in export.rows['discussions']:
            db.execute('INSERT INTO dismech_discussions VALUES (?,?,?,?,?,?)',
                       (export.import_id, digest(row['id']), row['id'], row['raw']['prompt'], digest(row['document_id']), row['is_gap']))
        mechanism_ids = {row['id'] for row in export.rows['mechanisms']}
        for row in export.rows['gap_attachments']:
            target = row.get('target_id')
            db.execute('INSERT INTO dismech_gap_attachments VALUES (?,?,?)',
                       (export.import_id, digest(row['gap_id']), digest(target) if row['resolution'] == 'resolved' and target in mechanism_ids else None))
        db.execute('INSERT INTO eaggl_embedding_runs VALUES (?,?,?,?)',
                   (self.target['run_id'], 'complete', canonical(self.target['config']), self.target['dimensions']))
        for probe in self.target['calibration']:
            db.execute('INSERT INTO eaggl_name_embeddings VALUES (?,?,?,?,?)',
                       (self.target['run_id'], probe['input_sha256'], probe['input_text'],
                        np.asarray(probe['vector'], dtype='<f4').tobytes(), probe['vector_sha256']))
        connection.commit()
        embeddings.migrate(connection)
        return connection

    def test_load_completed_replay_and_independent_source_vector_readback(self):
        self.complete()
        connection = self.connect()
        self.assertEqual(embeddings.read_target_run(connection, self.target['run_id']), self.target)
        first = embeddings.load_capture(connection, self.output, batch_size=1)
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(first['bindings'], 3)
        self.assertEqual(first['vectors'], 2)
        self.assertEqual(embeddings.load_capture(connection, self.output, batch_size=2), first)
        self.assertTrue(embeddings.verify_capture(connection, self.output)['verified'])
        self.assertTrue(connection.lock_released)
        _, inputs, _ = embeddings.read_local_vectors(self.output)
        loaded = embeddings.load_context_vectors(connection, self.export.import_id, self.target, inputs)
        self.assertEqual(set(loaded['vectors']), {row['source_id'] for row in inputs})
        for row in inputs:
            expected = embed([row['input_text']])[0].astype(np.float64)
            expected /= np.linalg.norm(expected)
            np.testing.assert_array_equal(loaded['vectors'][row['source_id']], expected)
            self.assertFalse(loaded['vectors'][row['source_id']].flags.writeable)

    def test_vector_and_binding_insert_failure_rolls_back_and_resumes_committed_prefix(self):
        self.complete()
        connection = self.connect()
        for table, checkpoint, total in [('dismech_embedding_vectors', 'loaded_vectors', 2),
                                         ('dismech_embedding_inputs', 'loaded_bindings', 3)]:
            with self.subTest(table=table):
                seen = []
                def fail(sql, rows):
                    if f'INSERT INTO {table} ' in sql:
                        seen.append(sql)
                        return len(seen) == 2
                    return False
                connection.fail_after_insert = fail
                with self.assertRaises(ConnectionError):
                    embeddings.load_capture(connection, self.output, batch_size=1)
                self.assertTrue(connection.lock_released)
                self.assertEqual(connection.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)
                self.assertEqual(connection.db.execute(f'SELECT {checkpoint},status FROM dismech_embedding_runs').fetchone(), (1, 'loading'))
                with self.assertRaises(ValueError):
                    embeddings.verify_capture(connection, self.output)
                connection.fail_after_insert = None
                if table == 'dismech_embedding_inputs':
                    result = embeddings.load_capture(connection, self.output, batch_size=1)
                    self.assertEqual(result['status'], 'complete')
                    self.assertEqual(connection.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], total)
        self.assertTrue(embeddings.verify_capture(connection, self.output)['verified'])

    def test_source_revision_drift_refuses_load_before_embedding_rows_are_created(self):
        self.complete()
        connection = self.connect()
        connection.db.execute('UPDATE dismech_documents SET source_sha256=? WHERE id_sha256=?',
                              ('f' * 64, digest('dismech:modules/B')))
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM dismech_embedding_runs').fetchone()[0], 0)

    def test_completed_vector_and_binding_corruption_fail_verify_and_resume(self):
        self.complete()
        connection = self.connect()
        embeddings.load_capture(connection, self.output)
        for table, column, key, value in [
            ('dismech_embedding_vectors', 'input_text', 'input_sha256', 'invented'),
            ('dismech_embedding_vectors', 'vector', 'input_sha256', b'bad'),
            ('dismech_embedding_inputs', 'source_revision', 'source_id_sha256', '0' * 64),
            ('dismech_embedding_inputs', 'source_id', 'source_id_sha256', 'dismech:invented')]:
            with self.subTest(table=table, column=column):
                identity, old = connection.db.execute(f'SELECT {key},{column} FROM {table} LIMIT 1').fetchone()
                connection.db.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?', (value, identity))
                connection.commit()
                with self.assertRaises(ValueError):
                    embeddings.verify_capture(connection, self.output)
                with self.assertRaises(ValueError):
                    embeddings.load_capture(connection, self.output)
                connection.db.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?', (old, identity))
                connection.commit()

    def test_stale_configuration_incomplete_source_and_lock_conflict_never_load(self):
        self.complete()
        connection = self.connect()
        connection.db.execute("UPDATE dismech_imports SET status='loading'")
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)
        connection.db.execute("UPDATE dismech_imports SET status='complete'")
        changed = deepcopy(self.target['config'])
        changed['model_revision'] = 'different-revision'
        connection.db.execute('UPDATE eaggl_embedding_runs SET config=?', (canonical(changed),))
        connection.commit()
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)
        connection.db.execute('UPDATE eaggl_embedding_runs SET config=?', (canonical(self.target['config']),))
        connection.commit()
        connection.lock_available = False
        with self.assertRaises(ValueError):
            embeddings.load_capture(connection, self.output)
        self.assertEqual(connection.db.execute('SELECT COUNT(*) FROM dismech_embedding_runs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
