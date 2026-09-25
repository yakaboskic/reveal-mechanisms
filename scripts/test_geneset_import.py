"""GeneSet import invariants: exact scope, faithful metadata, identity and storage."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import Mock

import import_cfde_genesets as importer


class CatalogTests(unittest.TestCase):
    def test_model_scope_and_case_sensitive_keys(self):
        source = {'keys': [['b', 'cfde-inc-v2'], ['A', 'cfde-inc-v2'], ['a', 'cfde-inc-v2'], ['old', 'cfde']]}
        self.assertEqual(importer.selected_keys(source, 'cfde-inc-v2'), ['A', 'a', 'b'])

    def test_missing_malformed_and_duplicate_catalogs_fail(self):
        for source in ({}, {'keys': []}, {'keys': [['x']]},
                       {'keys': [['x', 'cfde-inc-v2'], ['x', 'cfde-inc-v2']]}):
            with self.subTest(source=source), self.assertRaises(ValueError):
                importer.selected_keys(source, 'cfde-inc-v2')

    def test_unknown_scientific_metadata_is_omitted(self):
        key = 'HuBMAP__all_signatures__A_gene_program'
        node = importer.metadata_record(key, 'cfde-inc-v2', 'urn:test:activity')
        self.assertEqual(node['term'], key)
        self.assertEqual(node['term_prefix'], 'HuBMAP')
        self.assertEqual(node['member_type'], 'gene')
        for field in ['organism', 'assay', 'data_type', 'genome_build', 'n_genes', 'members']:
            self.assertNotIn(field, node)

    def test_composite_set_does_not_get_a_single_misleading_namespace(self):
        key = 'HuBMAP__A___LINCS_L1000__B'
        node = importer.metadata_record(key, 'cfde-inc-v2', 'urn:test:activity')
        self.assertEqual(node['term'], key)
        self.assertNotIn('term_prefix', node)
        self.assertIn('gene_set:' + key, node['alternate_identifier'])

    def test_database_prefix_is_literal_and_identifier_is_safe(self):
        importer.validate_db_name('cyaka_reveal_mechanisms')
        for name in ['cyakaXother', 'cyaka_', 'other', 'cyaka_x;DROP TABLE x', 'cyaka_x`']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                importer.validate_db_name(name)

    def test_export_integrity_and_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with gzip.open(root / 'records.jsonl.gz', 'wt') as out:
                out.write('{}\n')
            (root / 'activity.json').write_text('{}')
            manifest = {'complete': True, 'encoded_rows': 1, 'expected_rows': 1,
                        'records_sha256': importer.file_hash(root / 'records.jsonl.gz'),
                        'activity_sha256': importer.file_hash(root / 'activity.json')}
            importer.write_json(root / 'manifest.json', manifest)
            importer.check_export(root)
            manifest['complete'] = False
            importer.write_json(root / 'manifest.json', manifest)
            with self.assertRaises(ValueError):
                importer.check_export(root)
            manifest['complete'] = True
            importer.write_json(root / 'manifest.json', manifest)
            (root / 'activity.json').write_text('{"changed":true}')
            with self.assertRaises(ValueError):
                importer.check_export(root)

    def test_existing_id_cannot_be_overwritten_with_different_content(self):
        cursor = Mock()
        cursor.fetchall.return_value = [('dapper:GeneSet.test', 'incorrect')]
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            importer.store_objects(cursor, [('GeneSet', {'id': 'dapper:GeneSet.test', 'term': 'A'})])


class PinnedIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = importer.ROOT / 'data/cfde-genesets/2026-09-24/dapper'
        cls.module, cls.sv = importer.identity_module(cls.snapshot)

    def test_identity_replay_and_model_scope(self):
        node = importer.metadata_record('HuBMAP__A', 'cfde-inc-v2', 'urn:test:activity')
        first = self.module.compute_id(node, 'GeneSet', self.sv)
        self.assertTrue(first.startswith('dapper:GeneSet.'))
        self.assertEqual(first, self.module.compute_id(copy.deepcopy(node), 'GeneSet', self.sv))
        other = importer.metadata_record('HuBMAP__A', 'cfde', 'urn:test:activity')
        self.assertNotEqual(first, self.module.compute_id(other, 'GeneSet', self.sv))

    def test_future_enrichment_creates_a_new_digest(self):
        node = importer.metadata_record('HuBMAP__A', 'cfde-inc-v2', 'urn:test:activity')
        first = self.module.compute_id(node, 'GeneSet', self.sv)
        node['n_genes'] = 10
        self.assertNotEqual(first, self.module.compute_id(node, 'GeneSet', self.sv))

    def test_closed_schema_rejects_invented_fields(self):
        from jsonschema import validators, ValidationError
        root = self.snapshot.parent
        schema = importer.read_json(root / 'GeneSet.schema.json')
        validate = validators.validator_for(schema)(schema).validate
        node = importer.metadata_record('HuBMAP__A', 'cfde-inc-v2', 'urn:test:activity')
        node['id'] = self.module.compute_id(node, 'GeneSet', self.sv)
        validate(node)
        node['invented_assay_confidence'] = 0.9
        with self.assertRaises(ValidationError):
            validate(node)


class BulkStagingTests(unittest.TestCase):
    def test_compressed_transport_preserves_nested_json_and_unicode(self):
        values = [('α\\N\tline\nreturn\rnull\0', '{"nested":"α\\n"}')]
        packed = importer.compressed_rows(values)
        raw = zlib.decompress(packed[4:])
        self.assertEqual(len(raw), int.from_bytes(packed[:4], 'little'))
        self.assertEqual(json.loads(raw), [list(values[0])])

    def test_staging_rejects_silent_conversion_warnings(self):
        cursor = Mock()
        cursor.fetchone.return_value = (1,)
        with self.assertRaisesRegex(ValueError, 'conversion warnings'):
            importer.bulk_stage(cursor, 'reveal_stage_objects', 'id', [('id1',)])

    def test_staging_rejects_dropped_rows(self):
        cursor = Mock()
        cursor.fetchone.side_effect = [(0,), (1,)]
        with self.assertRaisesRegex(ValueError, 'count mismatch'):
            importer.bulk_stage(cursor, 'reveal_stage_objects', 'id', [('id1',), ('id2',)])


if __name__ == '__main__':
    unittest.main()
