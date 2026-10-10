"""Release-pinned trace hydration: research and Box-package captures bound to the published release's ref_* tables."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend import provenance_reference as reference
from reveal_backend.account_provenance import collect
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
            'status': 'retained_rows_loaded', 'known_factor_binding': True, 'retained_rows': 3, 'model_construction_provenance': 'not_established'}])
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
        # Without the captured release manifest the factors cannot be re-minted, so nothing binds.
        self.records = {key: value for key, value in self.records.items() if value['file']['filename'] != 'reference-release.json'}
        _, unbound = self.expand()
        self.assertEqual(unbound['retained_projection_coverage'][0]['status'], 'unresolved')
        self.assertIn('invalid_capture_binding', {row['code'] for row in unbound['issues']})

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
        self.assertEqual([(edge['object'], edge['reference_generation_id']) for edge in doc['factor_projection_edges']], [(CITED, RELEASE)])
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


if __name__ == '__main__':
    unittest.main()
