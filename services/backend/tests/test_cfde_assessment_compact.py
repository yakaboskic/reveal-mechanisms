"""Lossless request projection against synthetic scientific fixtures; no I/O."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from reveal_backend.cfde_assessment_compact import FORMAT, compact, reconstruct


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def fixture():
    """Five selected factors; all 143 genes and 250 GeneSet loading rows matter."""
    collections = [f'dapper:Collection.public-source-{i}' for i in range(2)]
    shared_source = {'id': 'dapper:File.original-gmt', 'url': 'https://example.org/original.gmt',
        'sha256': 'a' * 64, 'source_revision': 'b' * 64, 'description': 'Original public source; exact locators retained.'}
    state = {'source_pins': {'generation_id': 'c' * 64, 'model': 'fixture-model'},
        'gap': {'question': 'Can the selected signatures support a scientific relationship?'},
        'user_inputs': {'context': '', 'hypotheses': '', 'uploads': []},
        'collections': {identity: {'name': f'Public collection {i}',
            'provenance': {'files': [deepcopy(shared_source)], 'activities': [{'id': f'activity:{i}'}]}}
            for i, identity in enumerate(collections)},
        'gene_sets': {'columns': ['id', 'name', 'collection_id', 'library', 'n_genes',
            'n_genes_in_eaggl_universe', 'object_sha256', 'context'], 'rows': [], 'coverage': 'all selected rows'},
        'factors': [], 'unknown_future_field': {'nested': [True, 1, 1.0, None, 'λ']}}
    for i in range(250):
        identity = f'dapper:GeneSet.{i:032x}'
        name = f'PUBLIC_ASSAY_SIGNATURE_{i // 125}_GENE{i}_UP'
        context = {'gmt_entry': identity, 'alternate_identifier': [name], 'gmt_row': i + 1,
            'assay': 'An exact shared assay definition: ' + 'quantified expression; ' * 15,
            'species': 'NCBITaxon:9606', 'reference_source': deepcopy(shared_source),
            'perturbation_target': 'GENE_TARGET (does not imply signature membership)'}
        state['gene_sets']['rows'].append([identity, name, collections[i // 125], 'public_library',
            125 + i, 100 + i, hashlib.sha256(identity.encode()).hexdigest(), context])
    for f in range(5):
        state['factors'].append({'id': f'factor:{f}',
            'genes': {'columns': ['gene', 'source_index', 'loading'], 'source': deepcopy(shared_source),
                'has_more': True, 'rows': [[f'GENE{f}_{i}', i, .12345678901234567] for i in range(29 if f < 3 else 28)]},
            'gene_sets': {'columns': ['gene_set_index', 'joint_loading', 'marginal_loading',
                'joint_loading_text', 'marginal_loading_text', 'source_index'], 'source': deepcopy(shared_source),
                'has_more': True, 'rows': [[f * 50 + i, .4, .2, '0.4', '0.2', i] for i in range(50)]}})
    return state


class AssessmentCompactionTests(unittest.TestCase):
    def assertRoundTrip(self, state):
        before = canonical(state)
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            encoded = compact(state)
            decoded = reconstruct(encoded)
        self.assertEqual(canonical(decoded), before)
        self.assertEqual(canonical(state), before, 'The original state must never be mutated')
        return encoded, decoded

    def test_full_scientific_state_smaller_exact_and_independently_owned(self):
        state = fixture()
        encoded, decoded = self.assertRoundTrip(state)
        self.assertLess(len(canonical(encoded)), len(canonical(state)) * .55)
        self.assertEqual(encoded['compaction']['format'], FORMAT)
        self.assertEqual(sum(len(f['genes']['rows']) for f in encoded['factors']), 143)
        self.assertEqual(sum(len(f['gene_sets']['rows']) for f in encoded['factors']), 250)
        self.assertEqual(len(encoded['gene_sets']['rows']), 250)
        wire = canonical(encoded)
        for row in state['gene_sets']['rows']:
            self.assertIn(row[0].encode(), wire)
            self.assertIn(row[6].encode(), wire, 'Original hexadecimal hashes remain visible')
        self.assertIn('compaction.gene_sets.collection_ids', encoded['compaction']['rules'])
        self.assertIn('compaction.provenance.records', encoded['compaction']['rules'])
        encoded['compaction']['provenance']['records'][0]['id'] = 'changed'
        self.assertEqual(decoded['collections']['dapper:Collection.public-source-0']['provenance']['files'][0]['id'],
            'dapper:File.original-gmt')
        decoded['gene_sets']['rows'][0][-1]['reference_source']['id'] = 'changed too'
        self.assertEqual(state['gene_sets']['rows'][0][-1]['reference_source']['id'], 'dapper:File.original-gmt')

    def test_numeric_types_decimal_text_and_negative_zero_are_exact(self):
        state = fixture()
        for i, row in enumerate(state['gene_sets']['rows']):
            row[-1]['typed_value'] = [True, 1, 1.0][i % 3]
        rows = state['factors'][0]['gene_sets']['rows']
        rows[0][1:5] = [.4, -.0, '0.4000000000000000000000000000000001', '-0.0']
        rows[1][1:5] = [1, 1.0, '1', '1.0']
        rows[2][1:5] = [True, .2, 'true', '0.2']
        encoded, decoded = self.assertRoundTrip(state)
        self.assertIn(b'0.4000000000000000000000000000000001', canonical(encoded))
        self.assertIs(type(decoded['factors'][0]['gene_sets']['rows'][1][1]), int)
        self.assertIs(type(decoded['factors'][0]['gene_sets']['rows'][1][2]), float)
        self.assertIs(type(decoded['factors'][0]['gene_sets']['rows'][2][1]), bool)
        self.assertIn(b'-0.0', canonical(encoded))
        # Python deep equality would miss both of these regressions.
        self.assertNotEqual(canonical(True), canonical(1))
        self.assertNotEqual(canonical(1), canonical(1.0))

    def test_partial_context_and_distinct_provenance_are_not_inferred(self):
        state = fixture()
        rows = state['gene_sets']['rows']
        rows[0][-1].pop('gmt_entry')
        rows[1][-1]['alternate_identifier'] = [rows[1][1], 'original alternate name']
        rows[2][-1]['species'] = 'NCBITaxon:10090'
        rows[3][-1]['uncommon_field'] = {'count': 0, 'evidence': 'literal source statement'}
        rows[4][-1]['reference_source']['sha256'] = 'd' * 64
        encoded, _ = self.assertRoundTrip(state)
        self.assertNotIn('gmt_entry', encoded['compaction']['gene_sets']['derived_context'])
        self.assertNotIn('alternate_identifier', encoded['compaction']['gene_sets']['derived_context'])
        self.assertIn(b'NCBITaxon:10090', canonical(encoded))
        self.assertIn(('d' * 64).encode(), canonical(encoded))

    def test_unknown_context_and_reserved_column_collisions_pass_through(self):
        for field in ('collection_index', 'name_suffix'):
            with self.subTest(field=field):
                state = fixture()
                state['gene_sets']['columns'].append(field)
                for row in state['gene_sets']['rows']: row.append('original value')
                encoded, _ = self.assertRoundTrip(state)
                self.assertEqual(encoded['gene_sets'], state['gene_sets'])
        state = fixture()
        state['gene_sets']['rows'][0][-1] = None
        encoded, _ = self.assertRoundTrip(state)
        self.assertEqual(encoded['gene_sets'], state['gene_sets'])
        state = fixture()
        state['gene_sets']['columns'].append('context.gmt_row')
        for row in state['gene_sets']['rows']: row.append('original distinct column')
        self.assertRoundTrip(state)

    def test_mixed_provenance_and_nonuniform_factor_shapes_are_not_reinterpreted(self):
        state = fixture()
        records = state['collections']['dapper:Collection.public-source-0']['provenance']['files']
        records.append(0)
        state['collections']['dapper:Collection.public-source-1']['provenance']['files'].append(True)
        table = state['factors'][0]['gene_sets']
        table['columns'].append('extra_scientific_value')
        for row in table['rows']: row.append({'exact': ['0.00', 0, None]})
        encoded, _ = self.assertRoundTrip(state)
        self.assertNotIn('provenance', encoded['compaction'])
        self.assertEqual(encoded['collections'], state['collections'])

    def test_small_unknown_and_empty_values_return_owned_copies(self):
        for state in ({}, {'gene_sets': {'columns': [], 'rows': []}}, {'unknown': [True, 1, 1.0]},
                {'factors': [{'genes': {'columns': ['gene'], 'rows': [['GENE']]}}]}):
            with self.subTest(state=state):
                encoded, decoded = self.assertRoundTrip(state)
                self.assertNotIn('compaction', encoded)
                self.assertIsNot(encoded, state)
                self.assertIsNot(decoded, encoded)

    def test_idempotence_reserved_metadata_and_nonfinite_numbers(self):
        encoded, _ = self.assertRoundTrip(fixture())
        self.assertEqual(canonical(compact(encoded)), canonical(encoded))
        for value in ([], None, {'compaction': {'format': 'unknown'}}):
            with self.subTest(value=value), self.assertRaises(ValueError): compact(value)
        with self.assertRaises(ValueError): reconstruct({'compaction': {'format': 'unknown'}})
        state = fixture(); state['unknown_future_field']['value'] = float('nan')
        with self.assertRaises(ValueError): compact(state)


if __name__ == '__main__': unittest.main()
