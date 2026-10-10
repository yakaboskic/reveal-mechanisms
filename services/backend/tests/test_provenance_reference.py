"""Exact-generation hydration, using real retained-query captures and SQL."""
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch

from reveal_backend.account_provenance import collect
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend import provenance_reference as reference
from reveal_backend.acceptance import public_runtime
import test_research_data as fixtures
from test_research_data import Connection, GEN, OTHER, IMP, FACTOR, KEY, SET

DATASET = 'dapper:Dataset.dataset'
ACTIVITY = 'dapper:Activity.extraction'
ORG = 'dapper:Organization.generator'
ACCOUNT = 'dapper:ScientificAccount.account'
CLAIM = 'dapper:Claim.claim'
PROP = 'dapper:Proposition.proposition'
EVIDENCE = 'dapper:EvidenceItem.evidence'


class EmptyTx:
    def get(self, *args): return None


class ProvenanceReferenceTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReferenceQueryTests.setUp(self)
        self.raw, self.records = {}, {}
        self.runtime = public_runtime()
        self.factor_capture = self.service.query('get_factor', {'factor_id': FACTOR}, generation_id=GEN)
        item = self.factor_capture.result['items'][0]
        self.mechanism = reference._compute_mechanism(item, self.factor_capture.source)
        self.record(self.factor_capture)
        self.gene_set = {'id': SET, 'name': 'stored set', 'members': ['HGNC.SYMBOL:GENE_A', 'HGNC.SYMBOL:GENE_B'],
                         'was_generated_by': ACTIVITY}
        self.graph = {'activities': [{'id': ACTIVITY, 'name': 'extraction'}],
                      'datasets': [{'id': DATASET, 'name': 'source', 'was_attributed_to': [ORG]}],
                      'organizations': [{'id': ORG, 'name': 'generator'}],
                      'used_edges': [{'subject': ACTIVITY, 'predicate': 'prov:used', 'object': DATASET, 'input_role': 'mapping'}]}
        self.store_graph()
        self.document = {'scientific_accounts': [{'id': ACCOUNT, 'component_claims': [CLAIM], 'conclusion_claims': [CLAIM]}],
            'claims': [{'id': CLAIM, 'proposition': PROP}],
            'propositions': [{'id': PROP, 'subject_entity': SET, 'object_entity': self.mechanism['id']}],
            'mechanisms': [self.mechanism]}
        self.queries.clear()

    def record(self, capture):
        checksum = sha256(capture.raw)
        file = self.runtime.file('capture-' + checksum + '.json', capture.raw, 'application/json')
        descriptor = {'sha256': checksum, 'dapper_file_id': file['id'], 'source_mode': 'imported_reference',
                      'research_capture': deepcopy(capture.source)}
        self.records[checksum] = {'sha256': checksum, 'file': file, 'research_source': descriptor}
        self.raw[checksum] = capture.raw
        return file

    def store_graph(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=? WHERE generation_id=? AND gene_set_id=?',
                (json.dumps({'dapper_gene_set': self.gene_set}), GEN, SET))
            c.execute('UPDATE cfde_gene_set_collections SET payload=? WHERE generation_id=?',
                (json.dumps({'provenance': self.graph, 'document_sha256': 'd' * 64}), GEN))

    def expand(self, **kwargs):
        with patch.object(reference, '_read_capture', side_effect=lambda record: self.raw[record['sha256']]):
            return reference.expand(self.document, self.records, tx=EmptyTx(),
                connection_factory=lambda: Connection(self.path, self.queries), **kwargs)

    def collect(self, document, metadata):
        document['_provenance_coverage'] = metadata
        return collect(document, ACCOUNT)

    def test_exact_superseded_generation_includes_marginal_only_rows(self):
        second = 'dapper:GeneSet.marginal'
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',
                (GEN, second, 'collection', 'marginal', 'GO', 0, 0, json.dumps({'dapper_gene_set': {
                    'id': second, 'name': 'marginal', 'was_generated_by': ACTIVITY}})))
            c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (GEN, 'per_trait', KEY, second, 0.001, 0.98, '0.001', '0.98', 900, 1, 0))
            c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (OTHER, 'per_trait', KEY, 'dapper:GeneSet.WRONG_ACTIVE', 1, 1, '1', '1', 1, 1, 1))
        original = deepcopy(self.document)
        doc, metadata = self.expand()
        self.assertEqual(original, self.document)
        self.assertEqual({row['object'] for row in doc['factor_projection_edges']}, {SET, second})
        self.assertEqual(metadata['reference_generation_ids'], [GEN])
        self.assertEqual(metadata['retained_projection_coverage'][0]['retained_rows'], 2)
        self.assertFalse(any('reference_active' in sql or OTHER in args for sql, args in self.queries))
        result = self.collect(doc, metadata)
        self.assertEqual(len(result['dataset_reuse']), 1)
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertTrue(any('factor_projection' in row['route_classifications'] for row in result['traces']))
        self.assertTrue(any('mapping' in row['input_roles'] for row in result['traces']))

    def test_direct_geneset_capture_binding_loads_recorded_provenance(self):
        self.document['propositions'][0].pop('object_entity')
        self.records = {}; self.record(self.service.query('get_gene_set', {'gene_set_id': SET}, generation_id=GEN))
        doc, metadata = self.expand()
        self.assertEqual(doc['gene_sets'], [self.gene_set])
        self.assertEqual(doc['datasets'][0]['id'], DATASET)
        self.assertNotIn('factor_projection_edges', doc)
        self.assertEqual(len(self.collect(doc, metadata)['dataset_reuse']), 1)

    def test_evidence_only_set_and_nested_source_claim_are_eligible(self):
        source = 'dapper:Claim.source'; source_prop = 'dapper:Proposition.source'
        self.document['propositions'][0] = {'id': PROP, 'statement': 'synthesis'}
        self.document['claims'][0]['has_evidence'] = [EVIDENCE]
        self.document['evidence_items'] = [{'id': EVIDENCE, 'target_proposition': PROP, 'direction': 'SUPPORTS',
            'was_derived_from': [SET], 'source_claims': [source]}]
        self.document['claims'].append({'id': source, 'proposition': source_prop})
        self.document['propositions'].append({'id': source_prop, 'subject_entity': self.mechanism['id']})
        doc, metadata = self.expand()
        result = self.collect(doc, metadata)
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertTrue(any('inherited_source_claim' in row['route_classifications'] for row in result['traces']))

    def test_no_capture_binding_uses_retained_native_graph_without_catalog(self):
        self.records = {}
        self.document['propositions'][0].pop('object_entity')
        self.document.update(deepcopy(self.graph)); self.document['gene_sets'] = [self.gene_set]
        doc, metadata = self.expand()
        self.assertEqual(self.queries, [])
        self.assertEqual(metadata['issues'], [])
        self.assertEqual(len(self.collect(doc, metadata)['dataset_reuse']), 1)

    def test_no_scientific_credit_from_capture_file_result_rows_or_context_candidates(self):
        capture_file = next(iter(self.records.values()))['file']
        self.document['propositions'][0] = {'id': PROP, 'subject_entity': capture_file['id']}
        self.document['files'] = [capture_file]
        self.document['gene_sets'] = [self.gene_set]
        doc, metadata = self.expand()
        self.assertEqual(self.queries, [])
        self.assertNotIn('factor_projection_edges', doc)
        self.assertNotIn('was_derived_from_edges', doc)
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_blocked_sources_are_not_restored_or_named_in_diagnostics(self):
        self.document['propositions'][0].pop('subject_entity')
        doc, metadata = self.expand(blocked_ids={SET})
        self.assertNotIn(SET, {node['id'] for node in doc.get('gene_sets', [])})
        self.assertFalse(doc.get('factor_projection_edges'))
        self.assertNotIn(SET, json.dumps(metadata))

    def test_ambiguous_mechanism_generation_does_not_fall_back(self):
        capture = deepcopy(self.factor_capture)
        source = {**capture.source, 'generation_id': OTHER}
        envelope = json.loads(capture.raw); envelope['source'] = source
        from reveal_backend.research_data import QueryCapture
        self.record(QueryCapture(capture.result, canonical_json(envelope), 'imported_reference', source))
        doc, metadata = self.expand()
        self.assertNotIn('factor_projection_edges', doc)
        self.assertIn(self.mechanism['id'], metadata['blocked_reference_ids'])
        self.assertTrue(any(row['code'] == 'ambiguous_reference_binding' for row in metadata['issues']))

    def test_capture_descriptor_must_equal_checksummed_source(self):
        record = next(iter(self.records.values()))
        record['research_source']['research_capture']['generation_id'] = OTHER
        doc, metadata = self.expand()
        self.assertEqual(self.queries, [])
        self.assertNotIn('factor_projection_edges', doc)
        self.assertTrue(any(row['code'] == 'invalid_capture_binding' for row in metadata['issues']))

    def test_manifest_or_factor_mutation_is_unresolved(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reference_factors SET label=? WHERE generation_id=?', ('tampered', GEN))
        doc, metadata = self.expand()
        self.assertNotIn('factor_projection_edges', doc)
        self.assertIn(self.mechanism['id'], metadata['blocked_reference_ids'])
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reference_generations SET manifest=? WHERE generation_id=?', ('{}', GEN))
        doc, metadata = self.expand()
        self.assertNotIn('factor_projection_edges', doc)
        self.assertTrue(any(row['code'] == 'reference_generation_conflict' for row in metadata['issues']))

    def test_database_and_capture_storage_failures_propagate(self):
        with patch.object(reference, '_read_capture', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                reference.expand(self.document, self.records, tx=EmptyTx(), connection_factory=lambda: None)
        with patch.object(reference, '_read_capture', side_effect=lambda record: self.raw[record['sha256']]):
            with self.assertRaises(sqlite3.OperationalError):
                reference.expand(self.document, self.records, tx=EmptyTx(),
                    connection_factory=lambda: (_ for _ in ()).throw(sqlite3.OperationalError('offline')))

    def test_changed_capture_bytes_are_an_error(self):
        with patch.object(reference, '_read_capture', return_value=b'{}'):
            with self.assertRaises(Problem) as raised:
                reference.expand(self.document, self.records, tx=EmptyTx())
        self.assertEqual(raised.exception.code, 'SOURCE_UNAVAILABLE')

    def test_supplement_edges_retained_without_unrelated_objects(self):
        unrelated = 'dapper:Dataset.unrelated'
        self.graph.pop('used_edges'); self.graph['datasets'].append({'id': unrelated, 'name': 'unrelated'})
        self.store_graph()
        supplement = {'registry': {'id': 'supplement-1', 'graph_sha256': 'a' * 64},
            'graph': {'used_edges': [{'subject': ACTIVITY, 'predicate': 'prov:used', 'object': DATASET}]}}
        with patch('reveal_backend.provenance_supplements.load_supplement', return_value=supplement):
            doc, metadata = self.expand()
        self.assertEqual(metadata['supplement_ids'], ['supplement-1'])
        self.assertEqual({node['id'] for node in doc['datasets']}, {DATASET})
        self.assertEqual(doc['used_edges'][0]['provenance_source']['supplement_id'], 'supplement-1')

    def test_conflicting_reference_node_blocks_lineage(self):
        self.document['gene_sets'] = [{**self.gene_set, 'was_generated_by': 'dapper:Activity.different'}]
        doc, metadata = self.expand()
        self.assertIn(SET, metadata['blocked_reference_ids'])
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_legacy_ranked_links_use_exact_mapping(self):
        legacy = '1' * 64; mapping = '2' * 64; sets = '3' * 64
        native = 'factor:portal:T2D:cfde-inc-v2:Factor1'
        with sqlite3.connect(self.path) as c:
            c.execute('ALTER TABLE eaggl_cfde_gene_set_links ADD COLUMN gene_set_import_id TEXT')
            c.execute('ALTER TABLE eaggl_cfde_gene_set_links ADD COLUMN resolved_alias_sha256 TEXT')
            c.execute('INSERT INTO reference_generations VALUES(?,?,?,?,?,?,?,?)',
                (legacy, 'legacy-cfde-inc-v2', 'cfde-inc-v2', 'superseded', IMP, mapping, sets, '{}'))
            c.execute('INSERT INTO eaggl_cfde_factor_links VALUES(?,?,?,?)',
                (mapping, 1, native, json.dumps({'raw': {'label': 'source label'}})))
            c.execute('INSERT INTO eaggl_cfde_gene_set_links VALUES(?,?,?,?,?,?,?)',
                (mapping, 1, 'gene_set:source', 'source', 123, sets, 'f' * 64))
            c.execute('INSERT INTO cfde_gene_set_aliases VALUES(?,?,?,?,?,?)',
                (sets, SET, 'source', 'gene_set:source', 'f' * 64, json.dumps(self.graph)))
            c.execute('INSERT INTO dapper_objects VALUES(?,?)', (SET, json.dumps(self.gene_set)))
        capture = self.service.query('get_factor', {'factor_id': native}, generation_id=legacy)
        node = reference._compute_mechanism(capture.result['items'][0], capture.source)
        self.records = {}; self.record(capture)
        self.document['mechanisms'] = [node]
        self.document['propositions'][0]['object_entity'] = node['id']
        doc, metadata = self.expand()
        self.assertEqual(metadata['reference_generation_ids'], [legacy])
        self.assertEqual(doc['factor_projection_edges'][0]['gene_set_rank'], 123)
        self.assertNotIn('joint_loading', doc['factor_projection_edges'][0])
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'][0]['claim_count'], 1)

    def test_ambiguous_geneset_generation_cannot_mix_lineage(self):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',
                (OTHER, SET, 'collection', 'stored set', 'GO', 2, 2, json.dumps({'dapper_gene_set': self.gene_set})))
            c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?)',
                (OTHER, 'collection', json.dumps({'provenance': self.graph})))
        self.record(self.service.query('get_gene_set', {'gene_set_id': SET}, generation_id=OTHER))
        doc, metadata = self.expand()
        self.assertIn(SET, metadata['blocked_reference_ids'])
        self.assertNotIn('factor_projection_edges', doc)
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_transitive_geneset_in_another_collection_is_pinned_and_batched(self):
        upstream = 'dapper:GeneSet.upstream'
        self.gene_set['was_derived_from'] = [upstream]
        self.gene_set.pop('was_generated_by')
        self.store_graph()
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',
                (GEN, upstream, 'upstream-collection', 'upstream', 'GO', 0, 0, json.dumps({'dapper_gene_set': {
                    'id': upstream, 'name': 'upstream', 'was_generated_by': ACTIVITY, 'was_derived_from': [SET]}})))
            c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?)',
                (GEN, 'upstream-collection', json.dumps({'provenance': self.graph})))
        self.queries.clear()
        doc, metadata = self.expand()
        self.assertEqual({node['id'] for node in doc['gene_sets']}, {SET, upstream})
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'][0]['claim_count'], 1)
        set_queries = [(sql, args) for sql, args in self.queries if ' FROM cfde_gene_sets' in sql]
        self.assertEqual(len(set_queries), 2)
        self.assertTrue(all(args[0] == GEN for sql, args in set_queries))
        self.assertEqual(len([row for row in metadata['source_observations'] if row['kind'] == 'gene_sets']), 2)

    def test_captured_inverse_membership_survives_sql_generation_retirement(self):
        file = 'dapper:File.captured'; sibling = 'dapper:File.uncited'
        self.graph['used_edges'][0]['object'] = file
        self.graph['files'] = [{'id': file}, {'id': sibling}]
        self.graph['datasets'][0]['has_file'] = [file, sibling]
        self.store_graph()
        self.records = {}
        capture = self.service.query('get_gene_set', {'gene_set_id': SET}, generation_id=GEN)
        self.record(capture)
        self.document['propositions'][0].pop('object_entity')
        self.document['gene_sets'] = [deepcopy(self.gene_set)]
        self.document['activities'] = deepcopy(self.graph['activities'])
        self.document['files'] = [{'id': file}]
        self.document['used_edges'] = deepcopy(self.graph['used_edges'])
        original = deepcopy(self.document)
        with sqlite3.connect(self.path) as c:
            c.execute('DELETE FROM cfde_gene_sets WHERE generation_id=?', (GEN,))
            c.execute('DELETE FROM cfde_gene_set_collections WHERE generation_id=?', (GEN,))
            c.execute('DELETE FROM reference_generations WHERE generation_id=?', (GEN,))
        doc, metadata = self.expand()
        self.assertEqual(self.document, original)
        self.assertEqual({node['id'] for node in doc['datasets']}, {DATASET})
        self.assertNotIn(sibling, {node['id'] for node in doc['files']})
        result = self.collect(doc, metadata)
        self.assertEqual(result['dataset_reuse'][0]['claim_count'], 1)
        self.assertEqual(result['organization_reuse'][0]['organization_id'], ORG)
        membership = next(edge for edge in result['edges'] if edge['relation'] == 'has_file')
        self.assertEqual(membership['provenance_source'][0]['kind'], 'research_capture')
        self.assertTrue(result['coverage']['counts_are_lower_bounds'])

    def test_conflicting_captured_and_catalog_observations_remain_unresolved(self):
        self.records = {}
        self.record(self.service.query('get_gene_set', {'gene_set_id': SET}, generation_id=GEN))
        self.document['propositions'][0].pop('object_entity')
        self.graph['datasets'][0]['name'] = 'mutated catalog observation'
        self.store_graph()
        doc, metadata = self.expand()
        self.assertIn(DATASET, metadata['blocked_reference_ids'])
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_observation_snapshot_is_independent_of_sql_row_order(self):
        first, metadata = self.expand()
        rows = reference._rows
        with patch.object(reference, '_rows', side_effect=lambda *args: list(reversed(rows(*args)))):
            second, reordered = self.expand()
        self.assertEqual(metadata['source_observations'], reordered['source_observations'])

    def test_file_membership_does_not_walk_siblings(self):
        file = 'dapper:File.input'; sibling = 'dapper:File.sibling'
        self.graph['used_edges'][0]['object'] = file
        self.graph['files'] = [{'id': file}, {'id': sibling}]
        self.graph['datasets'][0]['has_file'] = [file, sibling]
        self.store_graph()
        doc, metadata = self.expand()
        self.assertEqual({node['id'] for node in doc['files']}, {file})
        self.assertEqual(len(self.collect(doc, metadata)['dataset_reuse']), 1)


if __name__ == '__main__': unittest.main()
