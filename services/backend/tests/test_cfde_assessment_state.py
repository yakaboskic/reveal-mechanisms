"""Owned-input projections against isolated SQL fixtures; no provider calls."""
from copy import deepcopy
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from reveal_backend.auth import Problem
from reveal_backend.catalog import Catalog
from reveal_backend import cfde_assessment_state as assessment
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.reference_generation import KPN_MODEL, LEGACY_MODEL
import test_research_data as references


class SelectedCatalog:
    validate_composer = Catalog.validate_composer
    selected = Catalog.selected

    def __init__(self):
        self.lock = threading.Lock(); self.loaded = True
        self.reference_generation_id = references.GEN; self.model = KPN_MODEL
        self.eaggl_import = references.IMP; self.dismech_import = 'd' * 64; self.source_commit = 'f' * 40
        self.factors = {references.FACTOR: {'source_id': references.FACTOR, 'source_revision': 'e' * 64,
            'object': {'id': 'dapper:Mechanism.selected', 'name': 'selected factor'},
            'cfde_anchor': {'label': 'insulin secretion'}, 'kpn_trait': {'id': 'KPN.TRAIT:0000398',
                'name': 'Type 2 diabetes', 'trait_type': 'disease', 'embedding': [666], 'token': 'private-secret'}}}
        raw = {'prompt': 'Does insulin secretion explain the gap?', 'rationale': 'A meaningful disease rationale.'}
        self.record = {'object': {'id': 'dapper:KnowledgeGap.gap', 'text': raw['prompt'],
            'gap_description': raw['rationale'], 'about_entities': ['http://purl.obolibrary.org/obo/MONDO_0005148']},
            'source': {'source_id': 'dismech:gap', 'source_revision': 'b' * 64, 'disease_label': 'Type 2 diabetes'},
            'source_detail': {'raw': raw, 'payload_sha256': sha256(canonical_json(raw)),
                'source_file': 'kb/disorders/T2D.yaml', 'source_pointer': '/discussions/0'}, 'attachments': []}
        self.mechanisms = {}

    def load(self): pass
    def gap(self, identity):
        if identity != self.record['object']['id']: raise Problem(404, 'GAP_NOT_FOUND', 'missing')
        return self.record


class AssessmentStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = references.ReferenceQueryTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.catalog = SelectedCatalog()
        self.composer = {'source_gap': {'id': self.catalog.record['object']['id'], **self.catalog.record['source']},
            'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': references.FACTOR,
                'source_revision': 'e' * 64, 'dapper_id': 'dapper:Mechanism.selected'}}],
            'model': KPN_MODEL, 'research_direction': 'Investigate this question.', 'upload_ids': []}
        self.inputs = {'format': 'reveal.user-inputs/1', 'research_direction': self.composer['research_direction'],
            'context': '', 'hypotheses': '', 'uploads': []}
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE cfde_gene_set_collections SET payload=?', (json.dumps({'collection': {'id': 'collection',
                'name': 'Human source signatures', 'organism': 'human'}, 'document_sha256': 'a' * 64,
                'provenance': {'activities': [{'id': 'activity:1', 'name': 'Source assay',
                    'perturbation_target': 'GENE_Z', 'api_key': 'private-secret'}]}}),))

    def build(self):
        return assessment.build_state(self.catalog, self.composer, self.inputs,
            connection_factory=lambda: references.Connection(self.fixture.path, self.fixture.queries))

    def test_authoritative_selection_exact_values_and_batched_metadata(self):
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            built = self.build()
        state = built['state']; factor = state['factors'][0]
        self.assertEqual(state['gap']['rationale']['text'], 'A meaningful disease rationale.')
        self.assertEqual([item['id'] for item in state['factors']], [references.FACTOR])
        self.assertEqual(factor['genes']['rows'], [['GENE_A', 0, .8], ['GENE_B', 1, .8], ['literal%_', 2, .1]])
        self.assertEqual(factor['gene_sets']['rows'][0], [0, .4, .2, '0.4', '0.2', 1, 1250, 1])
        self.assertEqual(state['gene_sets']['rows'][0][0], references.SET)
        self.assertEqual(state['source_pins']['generation_id'], references.GEN)
        self.assertEqual(state['source_pins']['eaggl_import_id'], references.IMP)
        self.assertTrue(built['coverage']['complete'])
        self.assertEqual(built['coverage']['gene_loading_count'], 3)
        self.assertEqual(built['coverage']['unique_gene_set_count'], 1)
        self.assertTrue(all(sql.startswith('SELECT ') for sql, _ in self.fixture.queries))
        self.assertEqual(sum('FROM cfde_gene_sets WHERE' in sql for sql, _ in self.fixture.queries), 1)
        self.assertEqual(sum('FROM cfde_gene_set_collections WHERE' in sql for sql, _ in self.fixture.queries), 1)
        encoded = canonical_json(state).decode()
        self.assertNotIn('private-secret', encoded); self.assertNotIn('embedding', encoded)
        self.assertIn('GENE_Z', encoded)
        self.assertIn('not necessarily a signature member', state['interpretation'])

    def test_reproducible_state_and_exact_decimal_text(self):
        text = '0.4000000000000000000000000000000001'
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE factor_gene_set_projections SET joint_loading_text=?', (text,))
        first = self.build(); second = self.build()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first['state']['factors'][0]['gene_sets']['rows'][0][3], text)

    def test_top_fifty_and_ties_never_truncate_numeric_rows(self):
        with sqlite3.connect(self.fixture.path) as c:
            for i in range(3, 65):
                c.execute('INSERT INTO eaggl_genes VALUES(?,?,?)', (references.IMP, i, f'GENE_{i:03}'))
                c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)', (references.IMP, 1, i, .12345678901234567))
                identity = 'dapper:GeneSet.' + str(i).zfill(32)
                node = {'id': identity, 'name': f'set{i}', 'members': []}
                c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)', (references.GEN, identity, 'collection',
                    f'set{i}', 'GO', 0, 0, json.dumps({'dapper_gene_set': node})))
                c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (references.GEN, 'per_trait', references.KEY, identity, .3, .7, '0.30', '0.70', i, i + 20, 0))
        value = self.build(); factor = value['state']['factors'][0]
        self.assertEqual(len(factor['genes']['rows']), 50); self.assertTrue(factor['genes']['has_more'])
        self.assertEqual(len(factor['gene_sets']['rows']), 50); self.assertTrue(factor['gene_sets']['has_more'])
        self.assertEqual(factor['genes']['rows'][2][2], .12345678901234567)
        self.assertEqual(value['coverage']['unique_gene_set_count'], 50)
        self.assertEqual(canonical_json(value), canonical_json(self.build()))

    def test_source_revision_and_generation_changes_rejected(self):
        self.composer['eaggl_anchors'][0]['reference']['source_revision'] = '0' * 64
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')
        self.composer['eaggl_anchors'][0]['reference']['source_revision'] = 'e' * 64
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE reference_factors SET source_revision=? WHERE generation_id=?', ('f' * 64, references.GEN))
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')

    def test_five_factor_state_deduplicates_shared_genesets_and_fits_budget(self):
        with sqlite3.connect(self.fixture.path) as c:
            for number in range(2, 6):
                identity = references.FACTOR.replace('Factor1', 'Factor' + str(number))
                key = references.KEY.replace('Factor1', 'Factor' + str(number))
                record = deepcopy(self.catalog.factors[references.FACTOR]); record['source_id'] = identity
                record['object']['id'] = 'dapper:Mechanism.' + str(number)
                self.catalog.factors[identity] = record
                reference = {'source': 'eaggl', 'source_id': identity, 'source_revision': 'e' * 64,
                    'dapper_id': record['object']['id']}
                self.composer['eaggl_anchors'].append({'reference': reference})
                c.execute('INSERT INTO reference_factors VALUES(?,?,?,?,?,?,?,?)',
                    (references.GEN, key, identity, 'T2D::Factor' + str(number), 'KPN.TRAIT:0000398', 'factor', 'e' * 64, '{}'))
                c.execute('INSERT INTO eaggl_factors VALUES(?,?,?,?,?,?,?,?)',
                    (references.IMP, number, 'T2D::Factor' + str(number), 'T2D', 'factor', 'e' * 64, '{}', sha256(('T2D::Factor' + str(number)).encode())))
            for index in range(3, 53):
                c.execute('INSERT INTO eaggl_genes VALUES(?,?,?)', (references.IMP, index, f'GENE_{index}'))
                identity = 'dapper:GeneSet.' + str(index).zfill(32)
                c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)', (references.GEN, identity, 'collection',
                    f'Verified source signature {index}', 'GO', 42, 37, json.dumps({'dapper_gene_set': {
                        'id': identity, 'name': f'Signature {index}', 'members': ['GENE_A']}})))
                for factor_index in range(1, 6):
                    c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)', (references.IMP, factor_index, index, .1234567890123456))
                    c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (references.GEN, 'per_trait', references.KEY.replace('Factor1', 'Factor' + str(factor_index)),
                         identity, .1, .2, '0.10000000000000000001', '0.20000000000000000001', index, index + 1, 0))
        built = self.build()
        self.assertEqual(built['coverage']['factor_count'], 5)
        self.assertEqual(built['coverage']['gene_loading_count'], 250)
        self.assertEqual(built['coverage']['gene_set_loading_count'], 250)
        self.assertEqual(len(built['state']['gene_sets']['rows']), 51)
        self.assertEqual(len(built['state']['collections']), 1)
        self.assertLess(len(canonical_json(built['state'])), 50_000)

    def add_factors(self, count, genes=60):
        """count more KPN anchors (the first with over 50 genes and sets), sharing gene sets."""
        with sqlite3.connect(self.fixture.path) as c:
            for number in range(2, count + 2):
                identity = references.FACTOR.replace('Factor1', 'Factor' + str(number))
                key = references.KEY.replace('Factor1', 'Factor' + str(number))
                record = deepcopy(self.catalog.factors[references.FACTOR]); record['source_id'] = identity
                record['object']['id'] = 'dapper:Mechanism.' + str(number); self.catalog.factors[identity] = record
                self.composer['eaggl_anchors'].append({'reference': {'source': 'eaggl', 'source_id': identity,
                    'source_revision': 'e' * 64, 'dapper_id': record['object']['id']}})
                c.execute('INSERT INTO reference_factors VALUES(?,?,?,?,?,?,?,?)',
                    (references.GEN, key, identity, 'T2D::Factor' + str(number), 'KPN.TRAIT:0000398', 'factor', 'e' * 64, '{}'))
                c.execute('INSERT INTO eaggl_factors VALUES(?,?,?,?,?,?,?,?)',
                    (references.IMP, number, 'T2D::Factor' + str(number), 'T2D', 'factor', 'e' * 64, '{}', sha256(('T2D::Factor' + str(number)).encode())))
                for index in range(3, 3 + (genes if number == 2 else 5)):
                    c.execute('INSERT OR IGNORE INTO eaggl_genes VALUES(?,?,?)', (references.IMP, index, f'GENE_{index:03}'))
                    c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)', (references.IMP, number, index, round(1 / (index + number), 6)))
                    identity_set = 'dapper:GeneSet.' + str(index).zfill(32)
                    c.execute('INSERT OR IGNORE INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)', (references.GEN, identity_set, 'collection',
                        f'set {index}', 'GO', 10, 9, json.dumps({'dapper_gene_set': {'id': identity_set, 'name': f'set {index}', 'members': ['GENE_A']}})))
                    c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)', (references.GEN, 'per_trait', key,
                        identity_set, .5 if index % 7 else .25, .1, '0.5', '0.1', index, index + 1, 0))

    def test_batched_anchor_reads_build_the_identical_state_in_six_selects(self):
        self.add_factors(4)
        with patch.object(assessment, 'BATCHED_MODELS', set()):
            per_anchor = self.build(); per_anchor_queries = len(self.fixture.queries)
        del self.fixture.queries[:]
        batched = self.build()
        self.assertEqual(canonical_json(batched), canonical_json(per_anchor))
        self.assertTrue(batched['state']['factors'][1]['genes']['has_more'])
        self.assertEqual(len(self.fixture.queries), 6)  # generation, factors, genes, gene sets, definitions, collections
        self.assertGreaterEqual(per_anchor_queries, 6 + 2 * 5)  # memoized per-anchor reads: two loading reads per anchor
        self.assertFalse(any(' OR ' in sql for sql, _ in self.fixture.queries))

    def test_batched_anchor_errors_match_the_per_anchor_reads(self):
        self.add_factors(2)
        second = self.composer['eaggl_anchors'][1]['reference']['source_id']
        with sqlite3.connect(self.fixture.path) as c:  # a third factor whose EAGGL id is the second anchor's public id
            c.execute('INSERT INTO reference_factors VALUES(?,?,?,?,?,?,?,?)',
                (references.GEN, 'KPN.TRAIT:0000398::Factor9', references.FACTOR.replace('Factor1', 'Factor9'), second, 'KPN.TRAIT:0000398', 'x', 'e' * 64, '{}'))
            c.execute('INSERT INTO eaggl_factors VALUES(?,?,?,?,?,?,?,?)', (references.IMP, 9, second, 'T2D', 'x', 'e' * 64, '{}', sha256(second.encode())))
        for models in (set(), {KPN_MODEL}):
            with self.subTest(batched=bool(models)), patch.object(assessment, 'BATCHED_MODELS', models):
                with self.assertRaises(Problem) as caught: self.build()
                self.assertEqual(caught.exception.code, 'AMBIGUOUS_IDENTITY')
        with sqlite3.connect(self.fixture.path) as c: c.execute("DELETE FROM reference_factors WHERE factor_key='KPN.TRAIT:0000398::Factor9'")
        self.composer['eaggl_anchors'][2]['reference']['source_revision'] = '0' * 64
        self.catalog.factors[self.composer['eaggl_anchors'][2]['reference']['source_id']]['source_revision'] = '0' * 64
        for models in (set(), {KPN_MODEL}):
            with self.subTest(batched=bool(models)), patch.object(assessment, 'BATCHED_MODELS', models):
                with self.assertRaises(Problem) as caught: self.build()
                self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')

    def test_pooled_borrower_takes_the_deadline_and_is_dropped_once_it_passes(self):
        class Borrowed(references.Connection):
            def __init__(self, *args): super().__init__(*args); self.limits, self.ended = [], []
            def limit(self, seconds): self.limits.append(seconds)
            def close(self): self.ended.append('close'); super().close()
            def discard(self): self.ended.append('discard'); super().close()
        borrowed = Borrowed(self.fixture.path, self.fixture.queries)
        with patch.object(assessment, 'reference_mysql_connection', return_value=borrowed):
            assessment.build_state(self.catalog, self.composer, self.inputs, deadline=time.monotonic() + 5)
        self.assertTrue(borrowed.limits and all(0 < seconds <= 5 for seconds in borrowed.limits))
        self.assertEqual(borrowed.ended, ['close'])  # settled: the adaptor rolls back and returns it to the pool
        expiring = Borrowed(self.fixture.path, self.fixture.queries)
        with patch.object(assessment, 'reference_mysql_connection', return_value=expiring), \
             patch.object(assessment.time, 'monotonic', side_effect=[0, 0, 0, 0, 0] + [100] * 50):
            with self.assertRaises(Problem) as caught:
                assessment.build_state(self.catalog, self.composer, self.inputs, deadline=10)
        self.assertEqual((caught.exception.code, expiring.ended), ('CFDE_ASSESSMENT_TIMEOUT', ['discard']))

    def test_concurrent_cutover_cannot_mix_catalogs(self):
        original = assessment.ReferenceQueryService.factor_tops
        def factor_tops(reader, *args, **kwargs):
            result = original(reader, *args, **kwargs)
            self.catalog.reference_generation_id = references.OTHER
            return result
        with patch.object(assessment.ReferenceQueryService, 'factor_tops', factor_tops):
            with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')

    def test_duplicate_or_unselected_anchor_is_not_expanded(self):
        self.composer['eaggl_anchors'].append(deepcopy(self.composer['eaggl_anchors'][0]))
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'DUPLICATE_ANCHOR')
        self.composer['eaggl_anchors'] = self.composer['eaggl_anchors'][:1]
        self.composer['eaggl_anchors'][0]['reference']['source_id'] = 'unknown'
        with self.assertRaises(Problem): self.build()

    def test_missing_species_definition_and_collection_are_explicit(self):
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('DELETE FROM cfde_gene_set_collections')
            c.execute('UPDATE cfde_gene_sets SET metadata=?', ('{}',))
        value = self.build()
        self.assertFalse(value['coverage']['complete'])
        self.assertIn('gene_sets:species_unknown:1', value['coverage']['missing'])
        self.assertIn('gene_set:' + references.SET + ':definition', value['coverage']['missing'])
        self.assertIn('collection:collection:metadata', value['coverage']['missing'])
        self.assertEqual(len(value['state']['factors'][0]['gene_sets']['rows']), 1)

    def test_wrong_geneset_object_identity_fails_closed(self):
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=?', ('{"dapper_gene_set":{"id":"different"}}',))
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_NOT_READY')

    def test_legacy_does_not_substitute_ranked_labels_for_numeric_projections(self):
        self.catalog.model = self.composer['model'] = LEGACY_MODEL
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE reference_generations SET model=?,legacy_mapping_run_id=? WHERE generation_id=?',
                (LEGACY_MODEL, 'mapping', references.GEN))
            c.execute('INSERT INTO eaggl_cfde_factor_links VALUES(?,?,?,?)',
                ('mapping', 1, references.FACTOR, '{"raw":{"label":"legacy"}}'))
            c.execute('INSERT INTO eaggl_cfde_gene_set_links VALUES(?,?,?,?,?)',
                ('mapping', 1, 'legacy-label', 'label-only', 1))
        state = self.build()
        self.assertEqual(state['state']['factors'][0]['gene_sets']['rows'], [])
        self.assertIn(references.FACTOR + ':numeric_gene_set_loadings', state['coverage']['missing'])
        self.assertNotIn('label-only', canonical_json(state).decode())

    def attachment(self):
        raw = {'id': 'dismech:mechanism', 'description': 'Stored attachment description.',
               'raw': {'description': 'Pinned scientific body.', 'notes': 'Interpret cautiously.'}}
        record = {'source_id': raw['id'], 'source_revision': '9' * 64, 'object': {'id': 'dapper:Mechanism.attachment',
            'name': 'DisMech pathway'}, 'source_detail': {'raw': raw, 'payload_sha256': sha256(canonical_json(raw)),
            'source_file': 'kb/disorders/T2D.yaml', 'source_pointer': '/pathophysiology/0'}}
        self.catalog.mechanisms[raw['id']] = record
        self.catalog.record['attachments'] = [{'target': {'source_id': raw['id'], 'source_revision': '9' * 64}},
                                               {'target': None, 'resolution': 'unresolved'}]
        return record

    def test_attachment_uses_exact_pinned_body_and_reports_unresolved(self):
        self.attachment()
        value = self.build(); detail = value['state']['dismech']
        self.assertEqual(detail['mechanisms'][0]['description']['text'], 'Pinned scientific body.')
        self.assertEqual(detail['unresolved_count'], 1)
        self.assertIn('dismech:unresolved_attachments:1', value['coverage']['missing'])
        self.assertEqual(detail['mechanisms'][0]['source']['source_pointer'], '/pathophysiology/0')

    def test_attachment_corruption_fails_and_wrong_revision_never_substitutes(self):
        record = self.attachment(); record['source_detail']['raw']['raw']['description'] = 'changed'
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_NOT_READY')
        record['source_revision'] = '8' * 64
        self.assertEqual(self.build()['state']['dismech']['mechanisms'], [])

    def upload(self, segments):
        document = {'format': 'reveal.upload-text/1', 'original_sha256': '1' * 64, 'segments': segments}
        raw = canonical_json(document)
        ref = {'store': 'filesystem', 'key': '/secret/location/private-upload', 'sha256': sha256(raw), 'size_bytes': len(raw)}
        self.inputs['uploads'] = [{'id': 'upload-1', 'sha256': '1' * 64, 'filename': 'private-file-name',
            'owner_user_id': 'private-owner', 'storage': {'key': '/private/original'}, 'extraction': {'storage': ref}}]
        self.composer['upload_ids'] = ['upload-1']
        return raw

    def test_unsaved_user_context_and_uploads_bounded_with_hashes_and_locators(self):
        self.inputs['context'] = self.composer['context'] = 'private research ' * 500
        raw = self.upload([{'locator': 'page:1', 'text': 'a' * 1500}, {'locator': 'page:2', 'text': 'b' * 1500}])
        with patch.object(assessment.user_inputs, 'read', return_value=raw) as read:
            result = self.build()
        read.assert_called_once()
        inputs = result['state']['user_inputs']; upload = inputs['uploads'][0]
        self.assertEqual(len(inputs['context']['text']), 2000)
        self.assertEqual(inputs['context']['sha256'], sha256(self.composer['context'].encode()))
        self.assertEqual(upload['excerpt_chars'], 2000); self.assertEqual(upload['total_chars'], 3000)
        self.assertEqual(upload['segments'][1]['locator'], 'page:2')
        self.assertEqual(upload['segments'][1]['pointer'], '/segments/1/text')
        self.assertEqual(upload['extraction_sha256'], sha256(raw))
        self.assertFalse(result['coverage']['complete'])
        for excluded in ('private-owner', '/secret/location', '/private/original', 'private-file-name'):
            self.assertNotIn(excluded, canonical_json(result).decode())

    def test_upload_checksum_or_resolved_composer_mismatch_fails_closed(self):
        raw = self.upload([{'locator': 'line:1', 'text': 'context'}])
        with patch.object(assessment.user_inputs, 'read', return_value=raw + b' '):
            with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'SOURCE_NOT_READY')
        self.inputs['context'] = 'unbound context'
        with self.assertRaises(Problem) as caught: self.build()
        self.assertEqual(caught.exception.code, 'INPUT_REVISION_CHANGED')

    def test_large_source_text_is_explicitly_bounded_and_not_numeric_data(self):
        record = self.attachment(); record['source_detail']['raw']['raw']['description'] = 'z' * 3000
        record['source_detail']['payload_sha256'] = sha256(canonical_json(record['source_detail']['raw']))
        result = self.build()
        self.assertEqual(len(result['state']['dismech']['mechanisms'][0]['description']['text']), 1200)
        self.assertTrue(any(item['total_chars'] == 3000 for item in result['coverage']['truncations']))
        self.assertEqual(result['coverage']['gene_loading_count'], 3)
        self.assertEqual(result['coverage']['gene_set_loading_count'], 1)

    def test_expired_deadline_does_no_catalog_or_source_work(self):
        with patch.object(self.catalog, 'load') as load, patch.object(assessment, 'reference_mysql_connection') as connect:
            with self.assertRaises(Problem) as caught:
                assessment.build_state(self.catalog, self.composer, self.inputs, deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, 'CFDE_ASSESSMENT_TIMEOUT')
        load.assert_not_called(); connect.assert_not_called()
        self.assertEqual(self.fixture.queries, [])

    def test_connection_timeouts_are_local_and_bounded_by_remaining_time(self):
        connections = []
        def factory():
            connection = references.Connection(self.fixture.path, self.fixture.queries)
            connection._read_timeout = connection._write_timeout = 120
            connections.append(connection)
            return connection
        assessment.build_state(self.catalog, self.composer, self.inputs,
            connection_factory=factory, deadline=time.monotonic() + 2)
        self.assertEqual(len(connections), 1)
        self.assertTrue(all(0 < c._read_timeout <= 2 and 0 < c._write_timeout <= 2 for c in connections))

    def tracked_connection(self):
        class Tracked(references.Connection):
            def __init__(self, *args):
                super().__init__(*args); self.closes = 0; self.rollbacks = 0
            def close(self):
                self.closes += 1
                return super().close()
            def rollback(self):
                self.rollbacks += 1
                return super().rollback()
        return Tracked(self.fixture.path, self.fixture.queries)

    def test_all_readers_share_one_connection_and_release_once(self):
        connection = self.tracked_connection()
        with patch.object(assessment, 'reference_mysql_connection', return_value=connection) as factory:
            built = assessment.build_state(self.catalog, self.composer, self.inputs, deadline=time.monotonic() + 10)
        factory.assert_called_once()
        self.assertGreater(factory.call_args.kwargs['timeout_seconds'], 0)
        self.assertLessEqual(factory.call_args.kwargs['timeout_seconds'], 10)
        self.assertEqual((connection.closes, connection.rollbacks), (1, 1))
        self.assertEqual(built['coverage']['gene_loading_count'], 3)
        self.assertEqual(built['coverage']['gene_set_loading_count'], 1)
        self.assertTrue(any('FROM cfde_gene_sets WHERE' in sql for sql, _ in self.fixture.queries))
        self.assertTrue(any('FROM cfde_gene_set_collections WHERE' in sql for sql, _ in self.fixture.queries))

    def test_failed_late_source_validation_still_releases_shared_connection_once(self):
        with sqlite3.connect(self.fixture.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=?', ('{"dapper_gene_set":{"id":"wrong"}}',))
        connection = self.tracked_connection()
        with self.assertRaises(Problem) as caught:
            assessment.build_state(self.catalog, self.composer, self.inputs, connection_factory=lambda: connection)
        self.assertEqual(caught.exception.code, 'SOURCE_NOT_READY')
        self.assertEqual((connection.closes, connection.rollbacks), (1, 1))

    def test_borrower_refuses_mutations_before_sql(self):
        connection = self.tracked_connection()
        with assessment._reference_connection(lambda: connection, None) as borrower:
            with borrower.cursor() as cursor:
                with self.assertRaisesRegex(ValueError, 'SELECT statements only'):
                    cursor.execute('DELETE FROM reference_generations')
            borrower.rollback(); borrower.close()
            self.assertEqual((connection.closes, connection.rollbacks), (0, 0))
        self.assertEqual(self.fixture.queries, [])
        self.assertEqual((connection.closes, connection.rollbacks), (1, 1))

    def test_deadline_checked_after_connection_before_first_query(self):
        with patch.object(assessment.time, 'monotonic', return_value=10) as clock:
            def factory():
                clock.return_value = 30
                return references.Connection(self.fixture.path, self.fixture.queries)
            with self.assertRaises(Problem) as caught:
                assessment.build_state(self.catalog, self.composer, self.inputs, connection_factory=factory, deadline=20)
        self.assertEqual(caught.exception.code, 'CFDE_ASSESSMENT_TIMEOUT')
        self.assertEqual(self.fixture.queries, [])

    def test_referenced_provenance_precedes_unrelated_activities(self):
        with sqlite3.connect(self.fixture.path) as c:
            node = {'id': references.SET, 'name': 'signature', 'was_generated_by': ['dapper:Activity.related'],
                    'sample': {'organism': 'mouse', 'cell_type': 'beta cell', 'api_key': 'private-secret'}}
            c.execute('UPDATE cfde_gene_sets SET metadata=?', (json.dumps({'dapper_gene_set': node}),))
            values = [{'id': 'dapper:Activity.unrelated' + str(i), 'name': 'Other assay'} for i in range(6)]
            values.append({'id': 'dapper:Activity.related', 'perturbation_target': 'EXACT_TARGET', 'species': 'mouse'})
            c.execute('UPDATE cfde_gene_set_collections SET payload=?', (json.dumps({'collection': {'organism': 'mouse'},
                'provenance': {'activities': values}}),))
        result = self.build(); state = result['state']
        self.assertEqual(state['collections']['collection']['provenance']['activities'][0]['id'], 'dapper:Activity.related')
        context = state['gene_sets']['rows'][0][-1]
        self.assertEqual(context['sample']['organism'], 'mouse')
        self.assertNotIn('private-secret', canonical_json(result).decode())
        self.assertIn('EXACT_TARGET', canonical_json(result).decode())
        self.assertTrue(result['coverage']['truncations'])


if __name__ == '__main__': unittest.main()
