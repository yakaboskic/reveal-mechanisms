"""Release-pinned trace hydration: research and Box-package captures bound to the published release's ref_* tables."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import provenance_reference as reference
from reveal_backend.account_provenance import collect, kpn_trait
from reveal_backend.acceptance import public_runtime
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.reference_generation import KPN_MODEL
from test_research_data import Connection

RELEASE, NEXT_RELEASE, LEGACY = 'e' * 64, 'f' * 64, 'a' * 64
MANIFEST = {'format': 'reveal.reference-release/1', 'files': {'traits.jsonl.gz': '0' * 64}}
FACTOR, KEY, TRAIT, RARE = 'factor:kpn:0000398:eaggl-capped-v1:Factor1', 'KPN.TRAIT:0000398::Factor1', 'KPN.TRAIT:0000398', 'KPN.TRAIT:0000500'
ARTIFACT = 'dig-open-bottom-line-analysis-stg_bottom-line_Mixed_T2D.sumstats.tsv.gz'
ACCOUNT, PROP, PROP2 = 'dapper:ScientificAccount.account', 'dapper:Proposition.factor', 'dapper:Proposition.rare'


def ref(cls, name): return f'dapper:{cls}.{name:x<32}'


CITED, PROJECTED, GLYGEN_SET = ref('GeneSet', 'cited'), ref('GeneSet', 'projected'), ref('GeneSet', 'glygen')
EXTRACT, PREPARE, GLYGEN_EXTRACT = ref('Activity', 'extract'), ref('Activity', 'prepare'), ref('Activity', 'glygenx')
DEG, READS = ref('File', 'deg'), ref('File', 'reads')
GTEX, GLYGEN, BROAD, GWU = ref('Dataset', 'gtex'), ref('Dataset', 'glygen'), ref('Organization', 'broad'), ref('Organization', 'gwu')
RESPONSES = [f'dapper:File.{index:0>32}' for index in range(6620)]


def used(activity, entity): return {'subject': activity, 'predicate': 'prov:used', 'object': entity, 'edge_role': 'data_input'}


class EmptyTx:
    def get(self, *args): raise AssertionError('a release never reads recovery supplements')


class Missing(Connection):
    """MySQL 1146 for statements naming a dropped table."""
    def __init__(self, path, queries, table):
        super().__init__(path, queries); self.table = table
    def cursor(self):
        cursor, table = super().cursor(), self.table
        execute = cursor.execute
        def guarded(sql, args=()):
            if table in sql: raise Exception(1146, f"Table '{table}' doesn't exist")
            return execute(sql, args)
        cursor.execute = guarded
        return cursor


class ReleaseHydrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal'}); environment.start(); self.addCleanup(environment.stop)
        reference._LINEAGE.items.clear(); reference._LINEAGE.release = None; self.addCleanup(reference._LINEAGE.items.clear)
        self.path, self.queries, self.raw, self.records = str(Path(temporary.name) / 'refs.sqlite'), [], {}, {}
        self.runtime = public_runtime()
        bottom = {'portal_trait_id': 'T2D', 'artifacts': [{'ancestry_code': 'Mixed', 'ancestry_name': 'Mixed', 'artifact_id': ARTIFACT}],
                  'gwas_artifact': {'ancestry_code': 'Mixed', 'ancestry_name': 'Mixed', 'artifact_id': ARTIFACT}, 'gwas_reason': None}
        gtex = {'document_sha256': '1' * 64, 'provenance': {
            'activities': [{'id': EXTRACT, 'name': 'extract'}, {'id': PREPARE, 'name': 'prepare'}],
            'files': [{'id': DEG}, {'id': READS}], 'datasets': [{'id': GTEX, 'name': 'GTEx v8', 'publisher': BROAD}],
            'organizations': [{'id': BROAD, 'name': 'Broad Institute'}],
            'used_edges': [used(EXTRACT, DEG), used(PREPARE, READS)],
            'was_generated_by_edges': [{'subject': DEG, 'predicate': 'prov:wasGeneratedBy', 'object': PREPARE}],
            'was_derived_from_edges': [{'subject': READS, 'predicate': 'prov:wasDerivedFrom', 'object': GTEX}]}}
        glygen = {'document_sha256': '2' * 64, 'provenance': {
            'activities': [{'id': GLYGEN_EXTRACT}], 'files': [{'id': identity} for identity in RESPONSES],
            'used_edges': [used(GLYGEN_EXTRACT, identity) for identity in RESPONSES]},
            'curated_provenance': {'source': 'lap/config/source_datasets.yaml', 'groups': {
                'datasets': [{'id': GLYGEN, 'name': 'GlyGen data release v1.12.1', 'publisher': GWU}],
                'organizations': [{'id': GWU, 'name': 'George Washington University'}],
                'was_derived_from_edges': [{'subject': identity, 'predicate': 'prov:wasDerivedFrom', 'object': GLYGEN} for identity in RESPONSES]}}}
        self.cited = {'id': CITED, 'name': 'cited set', 'members': ['HGNC.SYMBOL:GENE_A'], 'was_generated_by': EXTRACT}
        with sqlite3.connect(self.path) as c:
            c.executescript('''
CREATE TABLE reveal_ref_release(release_id TEXT,published_at TEXT,manifest TEXT);
CREATE TABLE reveal_ref_factors(factor_key TEXT,public_id TEXT,eaggl_factor_id TEXT,kpn_trait_id TEXT,factor_number INTEGER,label TEXT,metadata TEXT);
CREATE TABLE reveal_ref_traits(kpn_trait_id TEXT,legacy_phenotype_id TEXT,phenotype_name TEXT,metadata TEXT);
CREATE TABLE reveal_ref_phenotypes(phenotype TEXT,kpn_trait_id TEXT,phenotype_name TEXT,display_name TEXT,metadata TEXT);
CREATE TABLE reveal_ref_projections(factor_key TEXT,gene_set_id TEXT,library TEXT,joint_loading REAL,marginal_loading REAL,joint_rank INTEGER,marginal_rank INTEGER);
CREATE TABLE reveal_ref_gene_sets(gene_set_id TEXT,collection_id TEXT,gene_set_name TEXT,library TEXT,metadata TEXT);
CREATE TABLE reveal_ref_collections(collection_id TEXT,cfde_label TEXT,portal_collection_id TEXT,payload TEXT);
''')
            c.execute('INSERT INTO reveal_ref_release VALUES(?,?,?)', (RELEASE, '2026-10-10 12:00:00', json.dumps(MANIFEST)))
            c.execute('INSERT INTO reveal_ref_factors VALUES(?,?,?,?,?,?,?)', (KEY, FACTOR, 'T2D::Factor1', TRAIT, 1, 'insulin secretion', '{}'))
            c.execute('INSERT INTO reveal_ref_traits VALUES(?,?,?,?)', (TRAIT, 'T2D', 'type 2 diabetes', json.dumps({'bottom_line': bottom})))
            c.execute('INSERT INTO reveal_ref_phenotypes VALUES(?,?,?,?,?)', ('T2D', TRAIT, 'Type 2 diabetes', 'Type 2 diabetes', json.dumps({'bottom_line': bottom})))
            c.execute('INSERT INTO reveal_ref_phenotypes VALUES(?,?,?,?,?)', ('rare_x', RARE, None, 'rare x', json.dumps({'bottom_line': {
                'portal_trait_id': None, 'artifacts': [], 'gwas_artifact': None, 'gwas_reason': 'not_a_gwas_trait'}})))
            for identity, library, rank in ((CITED, 'GTEx', 1), (PROJECTED, 'GTEx', 2), (GLYGEN_SET, 'GlyGen', 1)):
                c.execute('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?)', (KEY, identity, library, .5 / rank, .2, rank, rank))
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?)', (CITED, 'gtex', 'cited set', 'GTEx', json.dumps({'dapper_gene_set': {
                **self.cited, 'in_gene_set_collection': 'dapper:GeneSetCollection.gtex'}})))
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?)', (PROJECTED, 'gtex', 'projected set', 'GTEx', json.dumps({'dapper_gene_set': {
                'id': PROJECTED, 'name': 'projected set', 'members': ['HGNC.SYMBOL:GENE_' + str(n) for n in range(500)], 'was_generated_by': EXTRACT}})))
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?)', (GLYGEN_SET, 'glygen', 'glycan set', 'GlyGen', json.dumps({'dapper_gene_set': {
                'id': GLYGEN_SET, 'name': 'glycan set', 'members': [], 'was_generated_by': GLYGEN_EXTRACT}})))
            c.execute('INSERT INTO reveal_ref_collections VALUES(?,?,?,?)', ('gtex', 'GTEx', 'gtex-portal', json.dumps(gtex)))
            c.execute('INSERT INTO reveal_ref_collections VALUES(?,?,?,?)', ('glygen', 'GlyGen', 'glygen-portal', json.dumps(glygen)))
        self.item = {'factor_key': KEY, 'public_id': FACTOR, 'eaggl_factor_id': 'T2D::Factor1', 'kpn_trait_id': TRAIT, 'label': 'insulin secretion',
                     'trait_metadata': {'kpn_trait_id': TRAIT, 'phenotype_name': 'type 2 diabetes'}}
        self.source = {'origin': 'mysql:reveal_ref_factors+reveal_ref_traits?release_id=' + RELEASE, 'kind': 'mysql',
                       'tables': ['reveal_ref_factors', 'reveal_ref_traits'], 'generation_id': RELEASE, 'release_id': RELEASE,
                       'source_kind': 'reference_release', 'model': KPN_MODEL, 'eaggl_import_id': None,
                       'generation_manifest_sha256': sha256(canonical_json(MANIFEST)), 'mechanism_identity_version': 1}
        self.mechanism = reference._compute_mechanism(self.item, self.source)
        self.document = {'scientific_accounts': [{'id': ACCOUNT, 'component_claims': ['dapper:Claim.a', 'dapper:Claim.b'], 'conclusion_claims': ['dapper:Claim.a']}],
            'claims': [{'id': 'dapper:Claim.a', 'proposition': PROP}, {'id': 'dapper:Claim.b', 'proposition': PROP2}],
            'propositions': [{'id': PROP, 'subject_entity': self.mechanism['id'], 'object_entity': CITED}, {'id': PROP2, 'subject_entity': RARE}],
            'mechanisms': [self.mechanism]}

    def capture(self, operation='get_factor', items=None, source=None):
        envelope = {'format': 'reveal.reference-query.capture/1', 'reader_version': 'reveal.reference-query/3', 'source_mode': 'imported_reference',
                    'source': source or self.source, 'operation': operation, 'arguments': {'factor_id': FACTOR, 'limit': 25, 'offset': 0},
                    'result': {'status': 'complete', 'items': items or [self.item]}}
        raw = canonical_json(envelope); checksum = sha256(raw)
        file = self.runtime.file('capture-' + checksum + '.json', raw, 'application/json')
        self.records[checksum] = {'sha256': checksum, 'file': file, 'research_source': {'sha256': checksum, 'dapper_file_id': file['id'],
            'source_mode': 'imported_reference', 'research_capture': deepcopy(envelope['source'])}}
        self.raw[checksum] = raw

    def box(self, filename, tables, data):
        raw = canonical_json({'format': reference.BOX_FORMAT, 'release_id': RELEASE, 'model': KPN_MODEL,
                              'source': {'kind': 'mysql', 'tables': tables, 'statements': []}, 'data': data})
        checksum = sha256(raw); file = self.runtime.file(filename, raw, 'application/json')
        self.records[checksum] = {'sha256': checksum, 'file': file, 'storage': {'store': 's3'}}
        self.raw[checksum] = raw

    def expand(self, connection=None, **kwargs):
        with patch.object(reference, '_read_capture', side_effect=lambda record: self.raw[record['sha256']]):
            return reference.expand(self.document, self.records, tx=EmptyTx(),
                connection_factory=connection or (lambda: Connection(self.path, self.queries)), **kwargs)

    def collect(self, document, metadata):
        document['_provenance_coverage'] = metadata
        return collect(document, ACCOUNT)

    def test_a_research_capture_binds_the_mechanism_to_the_published_release(self):
        self.capture()
        original = deepcopy(self.document)
        doc, metadata = self.expand()
        self.assertEqual(original, self.document)
        self.assertIn('SELECT release_id,published_at,manifest FROM reveal_ref_release', self.queries[0][0])
        self.assertFalse(any('reference_generations' in sql or 'per_trait' in sql for sql, _ in self.queries))
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])
        self.assertEqual({edge['object'] for edge in doc['factor_projection_edges']}, {CITED, PROJECTED, GLYGEN_SET})
        self.assertEqual(metadata['retained_projection_coverage'], [{'mechanism_id': self.mechanism['id'], 'reference_generation_id': RELEASE,
            'status': 'retained_rows_loaded', 'known_factor_binding': True, 'retained_rows': 3, 'traced_rows': 3,
            'trace_bound': reference.TRACE_BOUND, 'model_construction_provenance': 'not_established'}])
        # A projection edge observes its library and ranks; its release is its provenance_source.
        edge = next(edge for edge in doc['factor_projection_edges'] if edge['object'] == CITED)
        self.assertEqual({key: value for key, value in edge.items() if key != 'provenance_source'},
                         {'subject': self.mechanism['id'], 'predicate': 'reveal:factorProjection', 'object': CITED,
                          'library': 'GTEx', 'joint_rank': 1, 'marginal_rank': 1})
        self.assertEqual(edge['provenance_source'], {'kind': 'retained_projection', 'reference_generation_id': RELEASE})
        sets = {node['id']: node for node in doc['gene_sets']}
        self.assertEqual(sets[CITED], self.cited)  # cited: the full node, its non-identity slots removed
        self.assertEqual(sets[PROJECTED], {'id': PROJECTED, 'name': 'projected set', 'collection_id': 'gtex', 'was_generated_by': EXTRACT})
        # The GlyGen HZ2-shaped closure keeps one of 6,620 inputs, observed as collapsed.
        glygen_used = [edge for edge in doc['used_edges'] if edge['subject'] == GLYGEN_EXTRACT]
        self.assertEqual([(edge['object'], edge['collapsed_inputs']) for edge in glygen_used], [(RESPONSES[0], 6620)])
        self.assertEqual(len([node for node in doc['files'] if node['id'] in RESPONSES]), 1)
        traits = {node['id']: node for node in doc['traits']}
        self.assertEqual(traits[TRAIT], {'id': TRAIT, 'name': 'Type 2 diabetes', 'kpn_trait_id': TRAIT,
                                         'href': 'https://translator.broadinstitute.org/artifact_provenance/bottom_line/T2D'})
        self.assertEqual(traits[RARE]['href'], 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000500/')
        self.assertEqual(metadata['traits'][TRAIT]['gwas_artifact']['artifact_id'], ARTIFACT)
        self.assertEqual((metadata['traits'][RARE]['gwas_artifact'], metadata['traits'][RARE]['gwas_reason']), (None, 'not_a_gwas_trait'))
        self.assertEqual(doc['fitted_on_trait_edges'][0]['object'], TRAIT)
        result = self.collect(doc, metadata)
        self.assertEqual(result['coverage']['status'], 'complete', result['coverage']['issues'])
        reuse = {row['dataset_id']: row for row in result['dataset_reuse']}
        self.assertEqual(set(reuse), {GTEX, GLYGEN})
        self.assertEqual(reuse[GTEX]['classification_claim_counts']['proposition_reference'], 1)
        self.assertEqual(reuse[GLYGEN]['classification_claim_counts']['factor_projection'], 1)
        kinds = {row['kind'] for row in metadata['source_observations']}
        self.assertTrue({'reference_release', 'ref_factors', 'ref_projections', 'ref_gene_sets', 'ref_collections', 'ref_traits'} <= kinds)

    def test_parsed_lineage_is_cached_per_release_and_dropped_when_it_changes(self):
        self.capture()
        first, _ = self.expand()
        self.queries.clear()
        second, _ = self.expand()
        self.assertEqual(first, second)
        self.assertFalse(any('payload AS raw' in sql for sql, _ in self.queries))  # served from the cache
        self.assertIsNone(reference._LINEAGE.get(NEXT_RELEASE, 'gtex', '1' * 64))
        self.assertEqual(len(reference._LINEAGE.items), 0)

    def test_a_box_package_capture_binds_its_mechanism_like_its_builder_minted_it(self):
        self.box('reference-release.json', ['reveal_ref_release'], [{'release_id': RELEASE, 'published_at': '2026-10-10T12:00:00', 'manifest': json.dumps(MANIFEST)}])
        self.box('reference-factors.json', ['reveal_ref_factors', 'reveal_ref_traits'], [
            {key: self.item[key] for key in reference.RELEASE_FACTOR_COLUMNS} | {'factor_number': 1, 'input_sha256': '3' * 64,
             'source_revision': '4' * 64, 'metadata': {}, 'phenotype_name': 'type 2 diabetes', 'legacy_phenotype_id': 'T2D',
             'trait_group': 'kpn', 'trait_type': 'phenotype'}])
        doc, metadata = self.expand()
        self.assertEqual(metadata['retained_projection_coverage'][0]['status'], 'retained_rows_loaded')
        self.assertEqual(len(doc['factor_projection_edges']), 3)
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])
        self.assertEqual({row['kind'] for row in metadata['source_observations']} & {'box_capture'}, {'box_capture'})
        self.assertNotIn('binding_origin', metadata['retained_projection_coverage'][0])  # the capture bound it
        # Without the captured release manifest the factors cannot be re-minted: the capture binds nothing and is reported, and
        # the Mechanism binds by identity to the published release instead.
        self.records = {key: value for key, value in self.records.items() if value['file']['filename'] != 'reference-release.json'}
        _, unbound = self.expand()
        self.assertEqual({key: unbound['retained_projection_coverage'][0][key] for key in ('status', 'binding_origin')},
                         {'status': 'retained_rows_loaded', 'binding_origin': 'document_identity'})
        self.assertIn('invalid_capture_binding', {row['code'] for row in unbound['issues']})
        self.assertNotIn('box_capture', {row['kind'] for row in unbound['source_observations']})

    def test_a_superseded_release_reads_only_capture_bytes_and_archived_snapshots(self):
        self.capture()
        with sqlite3.connect(self.path) as c: c.execute('UPDATE reveal_ref_release SET release_id=?', (NEXT_RELEASE,))
        snapshot = {'generation_id': RELEASE, 'source_id': FACTOR, 'mechanism': {'id': self.mechanism['id']},
                    'top_gene_sets': [{'rank': 1, 'gene_set_id': CITED, 'library': 'GTEx', 'collection_id': 'gtex', 'joint_loading': .5, 'marginal_loading': .2}]}
        calls = []
        doc, metadata = self.expand(archived=lambda identity: calls.append(identity) or deepcopy(snapshot))
        self.assertEqual(calls, [self.mechanism['id']])
        self.assertEqual([sql for sql, _ in self.queries], ['SELECT release_id,published_at,manifest FROM reveal_ref_release'])
        self.assertIn({'code': 'reference_release_superseded'}, metadata['issues'])
        self.assertEqual([(edge['object'], edge['provenance_source']['reference_generation_id'], edge['library'], edge['joint_rank'])
                          for edge in doc['factor_projection_edges']], [(CITED, RELEASE, 'GTEx', 1)])
        self.assertEqual(metadata['retained_projection_coverage'][0]['status'], 'archived_snapshot_loaded')
        self.assertEqual({node['id']: node.get('name') for node in doc['traits']}, {TRAIT: 'type 2 diabetes'})
        self.assertEqual(metadata['traits'], {})  # no current-table GWAS metadata for superseded work
        self.assertIn({'code': 'trait_unavailable', 'object_id': RARE}, metadata['issues'])
        result = self.collect(doc, metadata)
        self.assertEqual(result['coverage']['status'], 'incomplete')
        # No snapshot: the bound factor's projections are missing, never read from the current release.
        doc, metadata = self.expand(archived=lambda identity: None)
        self.assertNotIn('factor_projection_edges', doc)
        self.assertEqual(metadata['retained_projection_coverage'][0]['status'], 'unresolved')
        self.assertIn('MISSING_FACTOR_PROJECTIONS', {row['code'] for row in self.collect(doc, metadata)['coverage']['issues']})

    def test_work_on_the_published_release_keeps_its_traits_beside_superseded_work(self):
        older = 'd' * 64
        item = {**self.item, 'factor_key': 'KPN.TRAIT:0000398::Factor2', 'public_id': 'factor:kpn:0000398:eaggl-capped-v1:Factor2',
                'eaggl_factor_id': 'T2D::Factor2', 'label': 'beta cell mass'}
        source = {**self.source, 'origin': 'mysql:reveal_ref_factors+reveal_ref_traits?release_id=' + older, 'generation_id': older, 'release_id': older}
        superseded = reference._compute_mechanism(item, source)
        self.document['mechanisms'].append(superseded)
        self.document['propositions'].append({'id': 'dapper:Proposition.older', 'subject_entity': superseded['id']})
        self.document['claims'].append({'id': 'dapper:Claim.c', 'proposition': 'dapper:Proposition.older'})
        self.document['scientific_accounts'][0]['component_claims'].append('dapper:Claim.c')
        self.capture()
        self.capture(items=[item], source=source)
        doc, metadata = self.expand(archived=lambda identity: None)
        self.assertIn({'code': 'reference_release_superseded'}, metadata['issues'])
        self.assertNotIn('trait_unavailable', {row['code'] for row in metadata['issues']})
        coverage = {row['mechanism_id']: row['status'] for row in metadata['retained_projection_coverage']}
        self.assertEqual(coverage, {self.mechanism['id']: 'retained_rows_loaded', superseded['id']: 'unresolved'})
        traits = {node['id']: node for node in doc['traits']}
        self.assertEqual(traits[TRAIT]['name'], 'Type 2 diabetes')  # named from the published release, not the superseded capture
        self.assertEqual(metadata['traits'][TRAIT]['gwas_artifact']['artifact_id'], ARTIFACT)
        self.assertEqual(metadata['traits'][RARE]['gwas_reason'], 'not_a_gwas_trait')
        self.assertEqual({(edge['subject'], edge['object']) for edge in doc['fitted_on_trait_edges']},
                         {(self.mechanism['id'], TRAIT), (superseded['id'], TRAIT)})
        self.assertEqual(metadata['reference_generation_ids'], sorted([older, RELEASE]))

    def test_a_capture_that_disagrees_with_the_release_manifest_does_not_bind(self):
        self.capture(source={**self.source, 'generation_manifest_sha256': '9' * 64})
        doc, metadata = self.expand()
        self.assertIn({'code': 'reference_generation_conflict'}, metadata['issues'])
        self.assertNotIn('factor_projection_edges', doc)

    def test_cited_traits_without_captures_are_named_from_the_published_release(self):
        self.document['propositions'][0] = {'id': PROP, 'subject_entity': 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000398/'}
        doc, metadata = self.expand()
        self.assertEqual({node['id'] for node in doc['traits']}, {'https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000398/', RARE})
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])
        self.assertEqual(metadata['traits']['https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000398/']['kpn_trait_id'], TRAIT)
        with sqlite3.connect(self.path) as c: c.execute('DELETE FROM reveal_ref_release')
        doc, metadata = self.expand()  # nothing published: the traits are unavailable, not an error
        self.assertNotIn('traits', doc)
        self.assertEqual({row['code'] for row in metadata['issues']}, {'trait_unavailable'})

    def test_release_hydration_writes_no_file(self):
        import builtins
        self.capture()
        writes, real_open, real_os_open = [], builtins.open, os.open
        def guarded(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in 'wax+'): writes.append(file); raise AssertionError(f'write to {file}')
            return real_open(file, mode, *args, **kwargs)
        def guarded_os(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC): writes.append(path); raise AssertionError(f'write to {path}')
            return real_os_open(path, flags, *args, **kwargs)
        def refused(*args, **kwargs): writes.append('tempfile'); raise AssertionError('temporary file')
        with patch('builtins.open', guarded), patch('io.open', guarded), patch('os.open', guarded_os), \
             patch.multiple(tempfile, NamedTemporaryFile=refused, TemporaryFile=refused, mkstemp=refused, mkdtemp=refused):
            doc, metadata = self.expand()
        self.assertEqual(writes, [])
        self.assertEqual(len(doc['factor_projection_edges']), 3)

    def test_dropped_reload_era_tables_are_coverage_issues(self):
        legacy = {'origin': 'mysql:reference_factors?generation_id=' + LEGACY, 'kind': 'mysql', 'tables': ['reference_factors'],
                  'generation_id': LEGACY, 'model': KPN_MODEL, 'eaggl_import_id': 'c' * 64, 'generation_manifest_sha256': '0' * 64}
        self.document['propositions'][1] = {'id': PROP2}
        self.capture(source=legacy)
        self.document['mechanisms'] = [reference._compute_mechanism(self.item, legacy)]
        self.document['propositions'][0]['subject_entity'] = self.document['mechanisms'][0]['id']
        doc, metadata = self.expand(connection=lambda: Missing(self.path, self.queries, 'reference_generations'))
        self.assertEqual({row['code'] for row in metadata['issues']}, {'reference_tables_unavailable', 'reference_generation_unavailable'})
        self.assertNotIn('factor_projection_edges', doc)

    # Content-addressed identity binding: Box-built accounts cite KPN Mechanisms and GeneSets without captures.

    def test_identity_binds_a_mechanism_without_captures_to_the_published_release(self):
        doc, metadata = self.expand()
        self.assertEqual(self.queries[0][0], 'SELECT release_id,published_at,manifest FROM reveal_ref_release')
        self.assertFalse(any('reference_generations' in sql or 'reference_active' in sql for sql, _ in self.queries))
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])
        coverage = metadata['retained_projection_coverage'][0]
        self.assertEqual((coverage['status'], coverage['binding_origin'], coverage['retained_rows']),
                         ('retained_rows_loaded', 'document_identity', 3))
        edges = {edge['object']: edge for edge in doc['factor_projection_edges']}
        self.assertEqual(set(edges), {CITED, PROJECTED, GLYGEN_SET})
        self.assertEqual(edges[CITED]['provenance_source']['binding_origin'], 'document_identity')
        identity = [row for row in metadata['source_observations'] if row['kind'] == 'document_identity']
        self.assertEqual([row['key'] for row in identity], [RELEASE])
        self.assertEqual(doc['mechanisms'], [self.mechanism])  # the document's own node, unchanged
        self.assertEqual(doc['fitted_on_trait_edges'][0]['object'], TRAIT)
        self.assertEqual(metadata['traits'][TRAIT]['gwas_artifact']['artifact_id'], ARTIFACT)
        result = self.collect(doc, metadata)
        self.assertEqual({row['dataset_id'] for row in result['dataset_reuse']}, {GTEX, GLYGEN})
        self.assertEqual(result['organization_reuse'][0]['organization_id'] in (BROAD, GWU), True)
        routes = set().union(*(row['route_classifications'] for row in result['traces']))
        self.assertTrue({'factor_projection', 'proposition_reference'} <= routes)

    def test_identity_binds_a_directly_cited_gene_set(self):
        self.document['propositions'][0].pop('subject_entity')
        doc, metadata = self.expand()
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])
        self.assertEqual({node['id']: node for node in doc['gene_sets']}[CITED], self.cited)  # metadata.dapper_gene_set
        self.assertNotIn('factor_projection_edges', doc)
        self.assertIn(('document_identity', RELEASE), {(row['kind'], row['key']) for row in metadata['source_observations']})
        self.assertEqual([row['dataset_id'] for row in self.collect(doc, metadata)['dataset_reuse']], [GTEX])
        # A cited id the release does not hold stays unbound, and is never matched by name.
        self.document['propositions'][0]['object_entity'] = ref('GeneSet', 'absent')
        doc, metadata = self.expand()
        self.assertNotIn('gene_sets', doc)
        self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_identity_requires_the_exact_minted_mechanism(self):
        self.document['propositions'][0].pop('object_entity')
        for table, column, value in [('reveal_ref_factors', 'label', 'tampered'), ('reveal_ref_traits', 'phenotype_name', 'tampered'),
                                     ('reveal_ref_release', 'manifest', json.dumps({**MANIFEST, 'mechanism_identity_version': 2}))]:
            with self.subTest(column=column):
                self.setUp(); self.document['propositions'][0].pop('object_entity')
                with sqlite3.connect(self.path) as c: c.execute(f'UPDATE {table} SET {column}=?', (value,))
                doc, metadata = self.expand()
                self.assertNotIn('factor_projection_edges', doc)
                self.assertEqual(metadata['retained_projection_coverage'][0]['identity_lookup'], 'no_match')
                self.assertEqual(self.collect(doc, metadata)['dataset_reuse'], [])

    def test_identity_names_its_lookup_when_nothing_binds(self):
        with sqlite3.connect(self.path) as c: c.execute('DELETE FROM reveal_ref_release')
        doc, metadata = self.expand()
        self.assertEqual(metadata['retained_projection_coverage'][0]['identity_lookup'], 'no_published_release')
        self.assertNotIn('factor_projection_edges', doc)
        # A Mechanism without a KPN description is never looked up; nor is one whose own lineage the document records.
        self.setUp()
        legacy = {'name': 'T2D mechanism Factor1', 'description': 'EAGGL mechanism factor:portal:T2D:cfde-inc-v2:Factor1. Source label: x.'}
        legacy['id'] = self.runtime.compute_id(legacy, 'Mechanism', self.runtime.schema)
        self.document['mechanisms'] = [legacy]
        self.document['propositions'] = [{'id': PROP, 'subject_entity': legacy['id']}, {'id': PROP2}]
        doc, metadata = self.expand()
        self.assertEqual(self.queries, [])
        self.assertEqual(metadata['retained_projection_coverage'][0]['identity_lookup'], 'not_applicable')
        self.document['mechanisms'] = [{**self.mechanism, 'was_derived_from': [GTEX]}]
        self.document['propositions'][0]['subject_entity'] = self.mechanism['id']
        doc, metadata = self.expand()
        self.assertEqual(self.queries, [])

    def test_a_capture_and_an_identity_binding_that_disagree_are_ambiguous(self):
        # A reload-era capture binds the cited set to its generation; the Mechanism binds the release by identity and projects
        # the same set, so the set is ambiguous and blocked rather than traced through either.
        legacy = {'origin': 'mysql:cfde_gene_sets?generation_id=' + LEGACY, 'kind': 'mysql', 'tables': ['cfde_gene_sets'],
                  'generation_id': LEGACY, 'model': KPN_MODEL, 'eaggl_import_id': 'c' * 64, 'generation_manifest_sha256': '0' * 64}
        envelope = {'format': 'reveal.reference-query.capture/1', 'reader_version': 'reveal.reference-query/2', 'source_mode': 'imported_reference',
                    'source': legacy, 'operation': 'get_gene_set', 'arguments': {'gene_set_id': CITED},
                    'result': {'status': 'complete', 'items': [{'gene_set_id': CITED, 'metadata': {'dapper_gene_set': self.cited}, 'payload': {}}]}}
        raw = canonical_json(envelope); checksum = sha256(raw)
        file = self.runtime.file('capture-' + checksum + '.json', raw, 'application/json')
        self.records[checksum] = {'sha256': checksum, 'file': file, 'research_source': {'sha256': checksum, 'dapper_file_id': file['id'],
            'source_mode': 'imported_reference', 'research_capture': deepcopy(legacy)}}
        self.raw[checksum] = raw
        doc, metadata = self.expand(connection=lambda: Missing(self.path, self.queries, 'reference_generations'))
        self.assertIn(CITED, metadata['blocked_reference_ids'])
        self.assertIn('ambiguous_reference_binding', {row['code'] for row in metadata['issues']})
        self.assertEqual(metadata['retained_projection_coverage'][0]['binding_origin'], 'document_identity')
        self.assertNotIn(CITED, {edge['object'] for edge in doc['factor_projection_edges']})
        result = self.collect(doc, metadata)
        through = {edge['id'] for edge in result['edges'] if CITED in (edge['subject'], edge['object'])}
        self.assertFalse(any(set(row['path_edge_ids']) & through for row in result['traces']))
        self.assertIn('CONFLICTING_OBSERVATION', {row['code'] for row in result['coverage']['issues']})

    def test_curated_release_edges_are_labelled_curated_lineage(self):
        doc, metadata = self.expand()
        curated = [edge for edge in doc['was_derived_from_edges'] if edge['object'] == GLYGEN]
        self.assertEqual(len(curated), 1)  # the collapsed GlyGen inputs' representative
        self.assertEqual(curated[0]['provenance_source'], {'kind': 'curated_lineage', 'curation_id': 'lap/config/source_datasets.yaml',
            'curation_sha256': None, 'reference_generation_id': RELEASE, 'collection_id': 'glygen'})
        producer = [edge for edge in doc['was_derived_from_edges'] if edge['object'] == GTEX]
        self.assertEqual(producer[0]['provenance_source']['kind'], 'reference_release')
        result = self.collect(doc, metadata)
        derived = next(edge for edge in result['edges'] if edge['relation'] == 'was_derived_from' and edge['object'] == GLYGEN)
        self.assertEqual(derived['provenance_source'][0]['kind'], 'curated_lineage')

    def test_identity_observations_are_independent_of_sql_row_order(self):
        first, metadata = self.expand()
        rows = reference._rows
        reference._LINEAGE.items.clear()
        with patch.object(reference, '_rows', side_effect=lambda *args: list(reversed(rows(*args)))):
            second, reordered = self.expand()
        self.assertEqual(metadata['source_observations'], reordered['source_observations'])
        content = lambda document: {group: sorted(map(canonical_json, rows)) for group, rows in document.items()}
        self.assertEqual(content(first), content(second))
        self.assertEqual(self.collect(first, metadata), self.collect(second, reordered))

    def test_identity_lookup_storage_failure_propagates(self):
        with self.assertRaises(sqlite3.OperationalError):
            reference.expand(self.document, {}, tx=EmptyTx(),
                connection_factory=lambda: (_ for _ in ()).throw(sqlite3.OperationalError('offline')))

    def test_a_verified_capture_file_ends_its_lineage_and_an_unverified_file_is_missing_lineage(self):
        self.capture()
        self.box('reference-release.json', ['reveal_ref_release'], [{'release_id': RELEASE, 'published_at': '2026-10-10T12:00:00', 'manifest': json.dumps(MANIFEST)}])
        captured, box = (record['file'] for record in sorted(self.records.values(), key=lambda record: record['file']['filename']))
        unverified = self.runtime.file('notes.json', b'{"notes": 1}', 'application/json')
        self.records[unverified['sha256']] = {'sha256': unverified['sha256'], 'file': unverified,  # its descriptor names other bytes
            'research_source': {'sha256': '0' * 64, 'dapper_file_id': unverified['id'], 'source_mode': 'imported_reference'}}
        unknown = self.runtime.file('unknown.json', b'[]', 'application/json')  # no retained record
        cited = {'captured': captured, 'box': box, 'unverified': unverified, 'unknown': unknown}
        self.document['files'] = list(cited.values())
        self.document['claims'][0]['has_evidence'] = [f'dapper:EvidenceItem.{name}' for name in cited]
        self.document['evidence_items'] = [{'id': f'dapper:EvidenceItem.{name}', 'target_proposition': PROP, 'direction': 'SUPPORTS',
                                            'was_derived_from': [file['id']]} for name, file in cited.items()]
        doc, metadata = self.expand()
        self.assertEqual(metadata['capture_file_ids'], sorted([captured['id'], box['id']]))
        result = self.collect(doc, metadata)
        kinds = {node['id']: node['kind'] for node in result['nodes']}
        self.assertEqual([kinds[file['id']] for file in cited.values()], ['capture', 'capture', 'file', 'file'])
        missing = {row['origin_id'] for row in result['coverage']['issues'] if row['code'] == 'MISSING_LINEAGE'}
        self.assertEqual(missing, {'dapper:EvidenceItem.unverified', 'dapper:EvidenceItem.unknown'})
        evidence = {wrapper['record']['id']: wrapper['provenance']['resolution'] for wrapper in result['evidence_items']}
        self.assertEqual(evidence['dapper:EvidenceItem.captured'], 'not_applicable')  # an endpoint, not a Dataset
        self.assertEqual(evidence['dapper:EvidenceItem.unverified'], 'incomplete')
        # Without the verified record the same File is missing lineage again.
        self.records ={key: value for key, value in self.records.items() if value['file']['id'] != captured['id']}
        doc, metadata = self.expand()
        self.assertNotIn(captured['id'], metadata['capture_file_ids'])
        self.assertIn('dapper:EvidenceItem.captured', {row['origin_id'] for row in self.collect(doc, metadata)['coverage']['issues']
                                                       if row['code'] == 'MISSING_LINEAGE'})

    def test_non_ascii_ids_are_unresolvable_and_never_reach_sql(self):
        digits = '٠٠٠٠٣٩٨'  # 0000398 in Arabic-Indic digits: Python's \d matches them
        trait, gene_set = 'KPN.TRAIT:' + digits, 'dapper:GeneSet.' + 'é' * 32
        self.assertIsNone(kpn_trait(trait))
        self.assertIsNone(kpn_trait('https://broadinstitute.github.io/kpn-data-models/kpn.trait/' + digits + '/'))
        self.assertEqual(kpn_trait('KPN.TRAIT:0000398'), TRAIT)
        # A Mechanism node naming a public factor id in those digits is never looked up.
        named = {'id': ref('Mechanism', 'named'), 'name': 'T2D mechanism Factor1',
                 'description': f'EAGGL mechanism factor:kpn:{digits}:eaggl-capped-v1:Factor1. KPN trait type 2 diabetes.'}
        self.document['mechanisms'].append(named)
        self.document['propositions'] = [{'id': PROP, 'subject_entity': self.mechanism['id'], 'object_entity': gene_set},
                                         {'id': PROP2, 'subject_entity': trait, 'object_entity': named['id']}]
        doc, metadata = self.expand()
        parameters = [value for _, args in self.queries for value in args]
        self.assertTrue(parameters and all(str(value).isascii() for value in parameters), parameters)
        self.assertEqual(metadata['reference_generation_ids'], [RELEASE])  # the ASCII Mechanism still binds
        coverage = {row['mechanism_id']: row for row in metadata['retained_projection_coverage']}
        self.assertEqual(coverage[named['id']]['identity_lookup'], 'not_applicable')
        self.assertEqual(coverage[self.mechanism['id']]['status'], 'retained_rows_loaded')
        self.assertNotIn(gene_set, {node['id'] for node in doc.get('gene_sets', [])})
        self.assertEqual({node['id'] for node in doc['traits']}, {TRAIT})
        # The SQL boundary itself: a non-ASCII id names no row and is never sent.
        self.queries.clear()
        rows = reference._batched(Connection(self.path, self.queries), 'SELECT gene_set_id FROM reveal_ref_gene_sets WHERE gene_set_id IN ({marks})',
                                  [], [CITED, gene_set], ('gene_set_id',))
        self.assertEqual((rows, self.queries[0][1]), ([{'gene_set_id': CITED}], (CITED,)))


LIBRARIES = ('GTEx', 'LINCS_L1000', 'GlyGen', 'IDG', 'RummaGEO', 'HuBMAP', 'MoTrPAC', 'IMPC', 'MetabolomicsWorkbench')


class Repo:
    """A public source's trace reads no application row: its artifact records are the publication's."""
    @contextmanager
    def read_transaction(self): yield None


class ProjectionBoundTests(unittest.TestCase):
    """A release Mechanism's factor_projection route follows its top gene sets per library, the account's own and the first one
    reaching each further Dataset: the Datasets reached stay those of every retained projection, and an account citing 13 T2D
    Mechanisms (about 576 retained projections each, as released) stays far inside the response and graph bounds."""
    release_setup, capture = ReleaseHydrationTests.setUp, ReleaseHydrationTests.capture

    def setUp(self):
        self.release_setup()

    def build(self, factors, per_library=64, further=True):
        """Factors 1..`factors` of T2D, each projecting in every library onto its own gene sets ranked 1..per_library. Collection
        <library>-a generated the top 10 (and without `further` all of them); with `further` the rest are <library>-b's, whose
        lineage reaches another Dataset. Each Mechanism is cited by one claim whose evidence is its get_factor capture."""
        gene_sets, projections, collections = [], [], []
        for library in LIBRARIES:
            for part in 'ab':
                activity, file, dataset = (ref(cls, library + part) for cls in ('Activity', 'File', 'Dataset'))
                collections.append((f'{library}-{part}', library, 'portal-' + library, json.dumps({'document_sha256': '3' * 64, 'provenance': {
                    'activities': [{'id': activity}], 'files': [{'id': file}], 'organizations': [{'id': BROAD, 'name': 'Broad Institute'}],
                    'datasets': [{'id': dataset, 'name': f'{library} {part} release', 'publisher': BROAD}],
                    'used_edges': [used(activity, file)],
                    'was_derived_from_edges': [{'subject': file, 'predicate': 'prov:wasDerivedFrom', 'object': dataset}]}})))
        mechanisms, factor_rows, account = [], [], {'id': ACCOUNT, 'component_claims': [], 'conclusion_claims': []}
        document = {'scientific_accounts': [account], 'claims': [], 'propositions': [], 'evidence_items': [], 'mechanisms': mechanisms, 'files': []}
        self.records, self.raw = {}, {}
        for number in range(1, factors + 1):
            key = f'{TRAIT}::Factor{number}'
            item = {**self.item, 'factor_key': key, 'public_id': f'factor:kpn:0000398:eaggl-capped-v1:Factor{number}',
                    'eaggl_factor_id': f'T2D::Factor{number}', 'label': f'program {number}'}
            factor_rows.append((key, item['public_id'], item['eaggl_factor_id'], TRAIT, number, item['label'], '{}'))
            mechanisms.append(reference._compute_mechanism(item, self.source))
            known = set(self.records); self.capture(items=[item])
            file = self.records[(set(self.records) - known).pop()]['file']
            document['files'].append(file)
            claim, prop, evidence = (ref(cls, f'f{number}') for cls in ('Claim', 'Proposition', 'EvidenceItem'))
            document['claims'].append({'id': claim, 'proposition': prop, 'has_evidence': [evidence]})
            document['propositions'].append({'id': prop, 'subject_entity': mechanisms[-1]['id'], 'object_entity': TRAIT})
            document['evidence_items'].append({'id': evidence, 'target_proposition': prop, 'direction': 'SUPPORTS', 'was_derived_from': [file['id']]})
            account['component_claims'].append(claim)
            for index, library in enumerate(LIBRARIES):
                for rank in range(1, per_library + 1):
                    identity = ref('GeneSet', f'f{number}l{index}r{rank}')
                    part = 'b' if further and rank > reference.TRACE_PROJECTIONS_PER_LIBRARY else 'a'
                    gene_sets.append((identity, f'{library}-{part}', f'{library} signature {number}.{rank}', library, json.dumps({'dapper_gene_set': {
                        'id': identity, 'name': f'{library} signature {number}.{rank}', 'members': ['HGNC.SYMBOL:GENE_A'],
                        'was_generated_by': ref('Activity', library + part)}})))
                    projections.append((key, identity, library, 1 / rank, 1 / rank, rank, rank))
        account['conclusion_claims'] = [document['claims'][0]['id']]
        with sqlite3.connect(self.path) as c:
            for table in ('reveal_ref_factors', 'reveal_ref_projections', 'reveal_ref_gene_sets', 'reveal_ref_collections'): c.execute('DELETE FROM ' + table)
            c.executemany('INSERT INTO reveal_ref_factors VALUES(?,?,?,?,?,?,?)', factor_rows)
            c.executemany('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?)', projections)
            c.executemany('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?)', gene_sets)
            c.executemany('INSERT INTO reveal_ref_collections VALUES(?,?,?,?)', collections)
        self.document = document

    def account_source(self):
        return {'document': self.document, 'public': {'id': 'publication', 'owner': 'owner', 'data': {'version': 1}}, 'publication_record': None,
                'user': None, 'root_payload': sha256(canonical_json(self.document['scientific_accounts'][0])), 'withheld_ids': set(),
                'withheld_dependencies': False, 'snapshot': {'artifact_records': deepcopy(self.records)}}

    def traced(self, endpoint=False, per_library=None):
        """The endpoint's page (portal disabled) or trace_local's result, optionally with another per-library bound."""
        from reveal_backend import provenance_api
        source = self.account_source()
        bound = patch.object(reference, 'TRACE_PROJECTIONS_PER_LIBRARY', per_library) if per_library else patch.dict(os.environ, {})
        with patch('reveal_backend.runtime_config.reference_mysql_connection', side_effect=lambda *args, **kwargs: Connection(self.path, self.queries)), \
             patch.object(reference, '_read_capture', side_effect=lambda record: self.raw[record['sha256']]), bound, \
             patch.dict(os.environ, {'REVEAL_GATEWAY_SECRET': 's' * 32, 'REVEAL_PROVENANCE_PORTAL_URL': ''}):
            if endpoint: return asyncio.run(provenance_api.trace(Repo(), ACCOUNT, lambda tx: source, {}))
            return provenance_api.trace_local(Repo(), ACCOUNT, source, provenance_api.visible_records(self.document, self.records))

    def test_thirteen_t2d_mechanisms_stay_far_inside_the_response_bounds(self):
        from reveal_backend import account_data_sources, account_provenance, provenance_api
        self.build(13)
        page = self.traced(endpoint=True)
        size = len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode())
        self.assertLess(size, provenance_api.MAX_RESPONSE_BYTES // 4)
        self.assertLess(len(page['nodes']), account_provenance.MAX_NODES // 4)
        self.assertLess(len(page['edges']), account_provenance.MAX_EDGES // 10)
        # Per Mechanism: the top 10 of each library and the first gene set of its further collection, stated in coverage.
        coverage = page['coverage']['retained_projection_coverage']
        self.assertEqual([(row['retained_rows'], row['traced_rows'], row['trace_bound']) for row in coverage],
                         [(64 * len(LIBRARIES), 11 * len(LIBRARIES), reference.TRACE_BOUND)] * 13)
        projected = [edge for edge in page['edges'] if edge['relation'] == 'factor_projection']
        self.assertEqual(len(projected), 13 * 11 * len(LIBRARIES))
        self.assertTrue(all(edge['observations'] == [{'library': edge['observations'][0]['library'], 'joint_rank': edge['observations'][0]['joint_rank'],
                                                      'marginal_rank': edge['observations'][0]['joint_rank']}] for edge in projected))
        datasets = {ref('Dataset', library + part) for library in LIBRARIES for part in 'ab'}
        self.assertEqual({row['dataset_id'] for row in page['dataset_reuse']}, datasets)
        # The capture each claim's evidence cites is where that lineage ends: no missing lineage anywhere.
        self.assertEqual(sum(node['kind'] == 'capture' for node in page['nodes']), 13)
        self.assertEqual({row['code'] for row in page['coverage']['issues']}, {'external_provenance_unavailable'})  # portal disabled
        # The stored data sources trace the same bound: the same Datasets per claim (and the trait's release-named GWAS), the same
        # coverage, and no missing lineage.
        local = self.traced()
        self.assertEqual(local['result']['coverage']['retained_projection_coverage'], coverage)
        gwas = provenance_api.GWAS_DATASET_PREFIX + ARTIFACT
        self.assertEqual({wrapper['record']['id']: wrapper['provenance']['dataset_ids'] for wrapper in local['result']['claims']},
                         {wrapper['record']['id']: sorted(wrapper['provenance']['dataset_ids'] + [gwas]) for wrapper in page['claims']})
        stored = account_data_sources.compact(local)
        self.assertEqual((stored['status'], stored['coverage']['issue_counts']), ('complete', {}))
        # Expanding every retained projection instead exceeds the response bound, and reaches no other Dataset.
        unbounded = self.traced(per_library=10 ** 6)['result']
        self.assertGreater(len(json.dumps(unbounded, ensure_ascii=False, separators=(',', ':')).encode()), provenance_api.MAX_RESPONSE_BYTES)
        reach = lambda result: {row['dataset_id']: row['claim_ids'] for row in result['dataset_reuse']}
        self.assertEqual(reach(unbounded), reach(local['result']))

    def test_datasets_reached_are_unchanged_when_projections_beyond_the_top_add_no_collection(self):
        self.build(2, further=False)
        bounded, unbounded = self.traced()['result'], self.traced(per_library=10 ** 6)['result']
        self.assertEqual([row['traced_rows'] for row in bounded['coverage']['retained_projection_coverage']], [10 * len(LIBRARIES)] * 2)
        self.assertEqual([row['traced_rows'] for row in unbounded['coverage']['retained_projection_coverage']], [64 * len(LIBRARIES)] * 2)
        reached = lambda result: {wrapper['record']['id']: wrapper['provenance']['dataset_ids'] for wrapper in result['claims']}
        self.assertEqual(reached(bounded), reached(unbounded))
        self.assertLessEqual({ref('Dataset', library + 'a') for library in LIBRARIES}, {row['dataset_id'] for row in bounded['dataset_reuse']})
        self.assertEqual(bounded['organization_reuse'], unbounded['organization_reuse'])

    def test_gene_sets_the_account_cites_are_always_followed(self):
        self.build(1, per_library=20, further=False)
        deep = ref('GeneSet', 'f1l0r20')  # GTEx rank 20: beyond the top 10, reaching nothing new
        self.document['propositions'][0]['object_entity'] = deep
        result = self.traced()['result']
        self.assertEqual(result['coverage']['retained_projection_coverage'][0]['traced_rows'], 10 * len(LIBRARIES) + 1)
        self.assertIn(deep, {edge['object'] for edge in result['edges'] if edge['relation'] == 'factor_projection'})


if __name__ == '__main__':
    unittest.main()
