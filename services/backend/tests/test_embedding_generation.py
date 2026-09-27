"""Generator attribution and mixed-run recovery, without GPU or network access."""
from copy import deepcopy
import json
import sqlite3

from test_dismech_embeddings import CALIBRATION_TEXTS, CaptureFixture, embed
from reveal_backend import dismech_embeddings as embeddings
from reveal_backend.dismech_import import canonical, digest


LOCAL_METADATA = {'backend': 'local_sentence_transformers',
                  'resolved_revision': 'a' * 40, 'device': 'synthetic-cpu',
                  'dtype': 'float32', 'max_seq_length': 100,
                  'reference_bundle_sha256': 'b' * 64}
REMOTE_METADATA = {'backend': 'remote_embedding_service'}


class GenerationEvidenceTests(CaptureFixture):
    def connect_cache(self):
        return sqlite3.connect(self.output / 'embeddings.sqlite3')

    def evidence(self):
        with self.connect_cache() as connection:
            report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])
        return report['generation']

    def assignments(self):
        with self.connect_cache() as connection:
            return connection.execute('SELECT input_sha256,vector_sha256,session_id FROM generation_vectors ORDER BY input_sha256').fetchall()

    def make_legacy_fixture(self):
        with self.connect_cache() as connection:
            report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])
            report.pop('generation', None)
            connection.execute("UPDATE metadata SET value=? WHERE key='calibration'", (canonical(report),))
            connection.execute("DELETE FROM metadata WHERE key='generation_format'")
            connection.execute('DROP TABLE generation_vectors')
            connection.execute('DROP TABLE generation_sessions')

    def test_remote_partial_then_local_resume_preserves_each_vector_generator(self):
        self.prepare()
        first = embeddings.embed_capture(self.output, batch_size=1, max_workers=1,
                                         max_batches=1, generation_metadata=REMOTE_METADATA, embedder=embed)
        self.assertEqual(first['status'], 'embedding')
        _, _, remote_rows = embeddings.read_local_vectors(self.output, require_complete=False)
        remote_assignments = self.assignments()
        self.assertEqual(len(remote_rows), 1)
        self.assertEqual(len(remote_assignments), 1)
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        result = embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA, embedder=embed)
        self.assertEqual(result['status'], 'complete')
        rows = {row[0]: row for row in embeddings.read_local_vectors(self.output)[2]}
        self.assertEqual(rows[remote_rows[0][0]], remote_rows[0])
        assignments = {row[0]: row for row in self.assignments()}
        self.assertEqual(assignments[remote_assignments[0][0]], remote_assignments[0])
        evidence = self.evidence()
        self.assertEqual(evidence['vector_count'], 2)
        sessions = {row['session_id']: row for row in evidence['sessions']}
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[remote_assignments[0][2]]['metadata']['backend'], 'remote_embedding_service')
        local = next(row for row in sessions.values() if row['metadata']['backend'] == 'local_sentence_transformers')
        self.assertEqual(local['metadata'], LOCAL_METADATA)
        self.assertEqual(local['vector_count'], 1)
        self.assertEqual(local['metadata_sha256'], digest(canonical(LOCAL_METADATA)))
        self.assertEqual(evidence['assignment_sha256'], digest(canonical([list(row) for row in self.assignments()])))

    def test_interrupted_session_is_honest_and_resume_does_not_relabel_prior_rows(self):
        self.prepare()
        source_calls = 0
        def interrupted(texts, **kwargs):
            nonlocal source_calls
            if not all(text in CALIBRATION_TEXTS for text in texts):
                source_calls += 1
                if source_calls == 2:
                    raise ConnectionError('synthetic interruption after one committed group')
            return embed(texts)
        with self.assertRaises(ConnectionError):
            embeddings.embed_capture(self.output, batch_size=1, max_workers=1,
                                     generation_metadata=LOCAL_METADATA, embedder=interrupted)
        before = self.assignments()
        self.assertEqual(len(before), 1)
        embeddings.embed_capture(self.output, generation_metadata=REMOTE_METADATA, embedder=embed)
        self.assertIn(before[0], self.assignments())
        sessions = {row['session_id']: row for row in self.evidence()['sessions']}
        old = sessions[before[0][2]]
        self.assertEqual(old['metadata'], LOCAL_METADATA)
        self.assertIn(old['status'], ('interrupted', 'failed'))
        self.assertIsNone(old['after_calibration'])
        self.assertEqual(old['vector_count'], 1)
        self.assertTrue(any(row['metadata']['backend'] == 'remote_embedding_service' for row in sessions.values()))

    def test_missing_assignment_after_format_marker_never_becomes_legacy_attribution(self):
        self.complete()
        with self.connect_cache() as connection:
            connection.execute('DELETE FROM generation_vectors WHERE input_sha256=(SELECT input_sha256 FROM generation_vectors LIMIT 1)')
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=lambda *a, **k: self.fail('Missing provenance reached embedding service'))

    def test_assignment_insert_failure_rolls_back_vector_bytes_in_the_same_transaction(self):
        self.prepare()
        # Fail the first calibration before any vectors exist, creating the
        # cache schema through the public API without depending on its helper.
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=lambda texts, **kwargs: -embed(texts))
        with self.connect_cache() as connection:
            connection.execute('''CREATE TRIGGER fail_assignment BEFORE INSERT ON generation_vectors
                                  BEGIN SELECT RAISE(ABORT, 'synthetic assignment failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA, embedder=embed)
        with self.connect_cache() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM vectors').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM generation_vectors').fetchone()[0], 0)
            connection.execute('DROP TRIGGER fail_assignment')
        result = embeddings.embed_capture(self.output, embedder=embed)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.assignments()), 2)

    def test_valid_json_metadata_corruption_is_detected_by_its_durable_hash(self):
        self.prepare()
        embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA, embedder=embed)
        changed = deepcopy(LOCAL_METADATA)
        changed['resolved_revision'] = 'c' * 40
        with self.connect_cache() as connection:
            connection.execute('UPDATE generation_sessions SET metadata=?', (canonical(changed),))
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=lambda *a, **k: self.fail('Corrupt provenance reached embedding service'))

    def test_assignment_hash_and_missing_session_corruption_are_rejected(self):
        self.complete()
        original = self.assignments()[0]
        for column, invalid in [('vector_sha256', '0' * 64), ('session_id', 'missing-session')]:
            with self.subTest(column=column):
                with self.connect_cache() as connection:
                    connection.execute(f'UPDATE generation_vectors SET {column}=? WHERE input_sha256=?', (invalid, original[0]))
                with self.assertRaises(ValueError):
                    embeddings.read_local_vectors(self.output)
                with self.connect_cache() as connection:
                    connection.execute('UPDATE generation_vectors SET vector_sha256=?,session_id=? WHERE input_sha256=?',
                                       (original[1], original[2], original[0]))

    def test_completion_summary_must_match_durable_session_and_assignment_records(self):
        self.complete()
        with self.connect_cache() as connection:
            original = json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])
        for field in ['generation', 'assignment_sha256']:
            with self.subTest(field=field):
                report = deepcopy(original)
                if field == 'generation':
                    del report[field]
                else:
                    report['generation'][field] = '0' * 64
                with self.connect_cache() as connection:
                    connection.execute("UPDATE metadata SET value=? WHERE key='calibration'", (canonical(report),))
                with self.assertRaises(ValueError):
                    embeddings.read_local_vectors(self.output)

    def test_standalone_summary_validation_rejects_invalid_finished_session_calibration(self):
        self.complete()
        with self.connect_cache() as connection:
            original = json.loads(connection.execute("SELECT value FROM metadata WHERE key='calibration'").fetchone()[0])
        mutations = []
        missing_before = deepcopy(original)
        missing_before['generation']['sessions'][0]['before_calibration'] = None
        mutations.append(missing_before)
        missing_after = deepcopy(original)
        missing_after['generation']['sessions'][0]['after_calibration'] = None
        mutations.append(missing_after)
        drift = deepcopy(original)
        drift['generation']['sessions'][0]['after_calibration']['probes'][0]['cosine_similarity'] = 0.5
        mutations.append(drift)
        invalid_status = deepcopy(original)
        invalid_status['generation']['sessions'][0]['status'] = 'invented-complete'
        mutations.append(invalid_status)
        bad_global_hash = deepcopy(original)
        bad_global_hash['generation']['assignment_sha256'] = 'not-a-sha256'
        mutations.append(bad_global_hash)
        bad_session_hash = deepcopy(original)
        bad_session_hash['generation']['sessions'][0]['vector_inventory_sha256'] = ['wrong-type']
        mutations.append(bad_session_hash)
        for index, value in enumerate(mutations):
            with self.subTest(case=index), self.assertRaises(ValueError):
                embeddings.validate_calibration_evidence(value, self.target)
        embeddings.validate_calibration_evidence(original, self.target, expected_vectors=2)
        with self.assertRaises(ValueError):
            embeddings.validate_calibration_evidence(original, self.target, expected_vectors=3)

    def test_legacy_rows_need_honest_unrecorded_attribution_and_preserve_optional_attestation(self):
        self.complete()
        original_rows = embeddings.read_local_vectors(self.output)[2]
        # Reproduce a capture written before generation tracking existed, keeping
        # its original before/after calibration but removing new-format state.
        self.make_legacy_fixture()
        attestation = {'backend': 'remote_embedding_service', 'basis': 'operator-observed legacy run'}
        embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA,
                                 legacy_generation_attestation=attestation,
                                 embedder=lambda *a, **k: self.fail('Completed legacy vectors should not be regenerated'))
        self.assertEqual(embeddings.read_local_vectors(self.output)[2], original_rows)
        sessions = self.evidence()['sessions']
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['metadata']['backend'], 'legacy_unrecorded')
        self.assertIn(attestation, sessions[0]['metadata'].values())
        self.assertEqual(sessions[0]['vector_count'], 2)
        self.assertFalse(any(row['metadata']['backend'] == 'local_sentence_transformers' for row in sessions))

    def test_partial_legacy_resume_retains_exact_bytes_and_honest_origin(self):
        self.prepare()
        embeddings.embed_capture(self.output, batch_size=1, max_workers=1, max_batches=1,
                                 generation_metadata=REMOTE_METADATA, embedder=embed)
        old_rows = embeddings.read_local_vectors(self.output, require_complete=False)[2]
        self.make_legacy_fixture()
        attestation = {'backend': 'remote_embedding_service', 'basis': 'observed before local runner existed'}
        result = embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA,
                                          legacy_generation_attestation=attestation, embedder=embed)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['embedded_now'], 1)
        self.assertIn(old_rows[0], embeddings.read_local_vectors(self.output)[2])
        sessions = self.evidence()['sessions']
        legacy = next(row for row in sessions if row['metadata']['backend'] == 'legacy_unrecorded')
        self.assertEqual(legacy['vector_count'], 1)
        self.assertIn(attestation, legacy['metadata'].values())
        self.assertIsNone(legacy['before_calibration'])
        self.assertIsNone(legacy['after_calibration'])
        local = next(row for row in sessions if row['metadata']['backend'] == 'local_sentence_transformers')
        self.assertEqual(local['vector_count'], 1)

    def test_final_local_drift_quarantines_without_relabeling_valid_remote_prefix(self):
        self.prepare()
        embeddings.embed_capture(self.output, batch_size=1, max_workers=1, max_batches=1,
                                 generation_metadata=REMOTE_METADATA, embedder=embed)
        remote = self.assignments()[0]
        calibration_calls = 0
        def drift(texts, **kwargs):
            nonlocal calibration_calls
            if all(text in CALIBRATION_TEXTS for text in texts):
                calibration_calls += 1
                return embed(texts) if calibration_calls == 1 else -embed(texts)
            return -embed(texts)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, generation_metadata=LOCAL_METADATA, embedder=drift)
        self.assertIn(remote, self.assignments())
        with self.connect_cache() as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='status'").fetchone()[0], 'quarantined')
            original = json.loads(connection.execute('SELECT metadata FROM generation_sessions WHERE session_id=?', (remote[2],)).fetchone()[0])
            self.assertEqual(original['backend'], 'remote_embedding_service')
        with self.assertRaises(ValueError):
            embeddings.read_local_vectors(self.output)
        with self.assertRaises(ValueError):
            embeddings.embed_capture(self.output, embedder=lambda *a, **k: self.fail('Quarantined mixed run reached service'))
