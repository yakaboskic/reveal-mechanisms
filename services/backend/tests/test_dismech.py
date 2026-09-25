import json
import tempfile
import unittest
from pathlib import Path

from dismech_fixture import records, rewrite, write_fixture
from reveal_backend.dismech_import import digest, open_export, stages


class DismechValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source, self.gaps = write_fixture(self.temp.name)

    def test_complete_projection_preserves_questions_and_uncertainty(self):
        export = open_export(self.source, self.gaps)
        projected = {s.name: [dict(zip(s.columns, r)) for r in s.rows] for s in stages(export)}
        self.assertEqual(len(projected['discussions']), 3)
        self.assertEqual(sum(r['is_gap'] for r in projected['discussions']), 2)
        self.assertIsNone(projected['discussions'][1]['status'])
        resolved, ambiguous, section = projected['gap_attachments']
        self.assertEqual(resolved['target_mechanism_sha256'], digest('dismech:modules/B#/pathophysiology/0'))
        self.assertIsNone(ambiguous['target_mechanism_sha256'])
        self.assertIsNone(ambiguous['target_id'])
        self.assertEqual(json.loads(ambiguous['payload'])['match_count'], 2)
        self.assertIsNone(section['target_mechanism_sha256'])
        self.assertEqual(projected['causal_edges'][0]['target_resolution'], 'unresolved_source_reference')
        self.assertEqual(json.loads(projected['discussions'][0]['payload']), export.rows['knowledge_gaps'][0])
        self.assertEqual(export.import_id, open_export(self.source, self.gaps).import_id)

    def test_checksum_failure(self):
        path = self.gaps / 'knowledge-gaps.jsonl.gz'
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            open_export(self.source, self.gaps)

    def test_source_commit_and_file_hash_mismatch(self):
        path = self.gaps / 'manifest.json'
        original = path.read_text(); manifest = json.loads(original)
        manifest['source_commit'] = 'b' * 40; path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'commits differ'):
            open_export(self.source, self.gaps)
        path.write_text(original)
        path = self.gaps / 'source-files.json'; inventory = json.loads(path.read_text())
        inventory[0]['sha256'] = 'f' * 64; path.write_text(json.dumps(inventory))
        with self.assertRaisesRegex(ValueError, 'hashes differ'):
            open_export(self.source, self.gaps)

    def test_incomplete_capture_and_row_count_mismatch(self):
        path = self.source / 'manifest.json'; original = path.read_text(); manifest = json.loads(original)
        manifest['complete'] = False; path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            open_export(self.source, self.gaps)
        manifest = json.loads(original); manifest['files']['entities.jsonl.gz']['rows'] += 1
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'row count'):
            open_export(self.source, self.gaps)

    def test_gap_subset_mismatch(self):
        rows = records(self.gaps, 'knowledge-gaps.jsonl.gz')
        rows[0]['raw']['prompt'] = 'Different question'
        rewrite(self.gaps, 'knowledge-gaps.jsonl.gz', rows)
        with self.assertRaisesRegex(ValueError, 'exact gap subset'):
            open_export(self.source, self.gaps)

    def test_duplicate_record(self):
        rows = records(self.source, 'mechanisms.jsonl.gz')
        rewrite(self.source, 'mechanisms.jsonl.gz', rows + rows)
        with self.assertRaisesRegex(ValueError, 'Duplicate mechanism'):
            open_export(self.source, self.gaps)

    def test_missing_attachment_and_fabricated_resolution(self):
        rows = records(self.gaps, 'gap-attachments.jsonl.gz')
        rewrite(self.gaps, 'gap-attachments.jsonl.gz', rows[:-1])
        with self.assertRaisesRegex(ValueError, 'coverage'):
            open_export(self.source, self.gaps)
        rows[1]['target_id'] = 'invented'
        rewrite(self.gaps, 'gap-attachments.jsonl.gz', rows)
        with self.assertRaisesRegex(ValueError, 'invent a target'):
            open_export(self.source, self.gaps)

    def test_dangling_document_and_edge_references(self):
        rows = records(self.source, 'mechanisms.jsonl.gz')
        original = json.loads(json.dumps(rows))
        rows[0]['document_id'] = 'missing'
        rewrite(self.source, 'mechanisms.jsonl.gz', rows)
        with self.assertRaisesRegex(ValueError, 'document'):
            open_export(self.source, self.gaps)
        rewrite(self.source, 'mechanisms.jsonl.gz', original)
        rows = records(self.source, 'causal-edges.jsonl.gz'); rows[0]['source_id'] = 'missing'
        rewrite(self.source, 'causal-edges.jsonl.gz', rows)
        with self.assertRaisesRegex(ValueError, 'invalid parent'):
            open_export(self.source, self.gaps)

    def test_different_capture_has_different_identity(self):
        before = open_export(self.source, self.gaps).import_id
        path = self.source / 'schema-vocabularies.json'; path.write_text('{"enums":["new"]}')
        self.assertNotEqual(before, open_export(self.source, self.gaps).import_id)


if __name__ == '__main__':
    unittest.main()
