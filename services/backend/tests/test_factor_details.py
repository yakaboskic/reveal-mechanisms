"""Public factor browsing reads the published reference release (reveal_ref_*), pinned per request, with bounded reads."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from reveal_backend import app as api
from reveal_backend import factor_details as details
from reveal_backend.auth import Problem
from reveal_backend.reference_generation import KPN_MODEL
from reveal_backend.repository import application_prefix, canonical
from reveal_backend.runtime_config import ROOT

CONTRACT = json.loads((ROOT / 'api/openapi.json').read_text())
DETAIL = CONTRACT['paths']['/v1/factors/{source_id}']['get']['responses']['200']['content']['application/json']['examples']['factor']['value']
FACTOR = DETAIL['factor']
RELEASE, NEXT_RELEASE = DETAIL['generation_id'], 'f' * 64
KEY = 'KPN.TRAIT:0000398::Factor1'
COLLECTION, GLYGEN_COLLECTION = 'dapper:GeneSetCollection.' + 'c' * 32, 'dapper:GeneSetCollection.' + 'g' * 32
PORTAL_COLLECTION = 'dapper:GeneSetCollection.' + 'p' * 32
SET1, SET2, SET3, GLYGEN_SET = ['dapper:GeneSet.' + c * 32 for c in ('a', 'b', 'd', 'e')]
TRAIT_LINKS = {'n_phenotypes': 6698, 'n_linked': 2, 'n_linked_kpn_traits': 1, 'n_linked_atlas_traits': 1, 'own_trait_q': 1e-9,
               'top_linked': [{'phenotype': 'T2D', 'kpn_trait_id': 'KPN.TRAIT:0000398', 'name': 'Type 2 diabetes', 'phewas_q': 1e-9},
                              {'phenotype': 'GLYCATED_HB', 'kpn_trait_id': None, 'name': 'Glycated haemoglobin', 'phewas_q': .01}]}


def ref(cls, name): return f'dapper:{cls}.{name:x<32}'


class Cursor:
    def __init__(self, connection, statements): self.cursor, self.statements = connection.cursor(), statements
    def __enter__(self): return self
    def __exit__(self, *exc): self.cursor.close()
    def execute(self, sql, args=()): self.statements.append(sql); self.cursor.execute(sql.replace('%s', '?'), args)
    def fetchall(self): return self.cursor.fetchall()


class Connection:
    def __init__(self, path, statements=None, opened=None):
        self.connection, self.statements, self.closed = sqlite3.connect(path), statements if statements is not None else [], False
        self.connection.create_function('CONCAT', -1, lambda *values: ''.join(str(v) for v in values))
        if opened is not None: opened.append(self)
    def cursor(self): return Cursor(self.connection, self.statements)
    def close(self): self.closed = True; self.connection.close()


class FactorDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'reference.sqlite')
        self.catalog = SimpleNamespace(load=lambda: None, lock=threading.Lock(), reference_generation_id=RELEASE,
            factors={FACTOR['source_id']: deepcopy(FACTOR)}, model=KPN_MODEL, geneset_import=RELEASE,
            bindings={FACTOR['source_id']: {'eaggl_import_id': None, 'eaggl_factor_id': 'T2D::Factor1', 'factor_key': KEY, 'model': KPN_MODEL,
                                            'cfde_payload': {'factor': 'Factor1', 'trait_links': TRAIT_LINKS}}})
        with sqlite3.connect(self.path) as c:
            c.executescript('''
CREATE TABLE reveal_ref_release(release_id TEXT PRIMARY KEY,published_at TEXT,manifest TEXT);
CREATE TABLE reveal_ref_factor_genes(factor_key TEXT,gene TEXT,loading REAL,PRIMARY KEY(factor_key,gene));
CREATE TABLE reveal_ref_collections(collection_id TEXT PRIMARY KEY,cfde_label TEXT,library TEXT,n_sets INTEGER,portal_collection_id TEXT,payload TEXT);
CREATE TABLE reveal_ref_gene_sets(gene_set_id TEXT PRIMARY KEY,collection_id TEXT,gene_set_name TEXT,library TEXT,n_genes INTEGER,n_genes_in_eaggl_universe INTEGER,legacy_source_key TEXT,metadata TEXT);
CREATE TABLE reveal_ref_projections(factor_key TEXT,gene_set_id TEXT,library TEXT,joint_loading REAL,marginal_loading REAL,joint_loading_text TEXT,marginal_loading_text TEXT,joint_rank INTEGER,marginal_rank INTEGER,is_joint_top_factor INTEGER,PRIMARY KEY(factor_key,gene_set_id));
CREATE TABLE gnomad_constraint_imports(import_id TEXT PRIMARY KEY,source_version TEXT,source_sha256 TEXT,source_url TEXT,selection_policy TEXT,status TEXT);
CREATE TABLE gnomad_constraint_active(table_prefix TEXT PRIMARY KEY,import_id TEXT);
CREATE TABLE gnomad_gene_constraints(import_id TEXT,gene_symbol TEXT,gene_id TEXT,transcript_id TEXT,selection_status TEXT,selection_reason TEXT,pli REAL,loeuf REAL,mis_z REAL,lof_oe REAL,constraint_flags TEXT,PRIMARY KEY(import_id,gene_symbol));
''')
            c.execute('INSERT INTO reveal_ref_release VALUES(?,?,?)', (RELEASE, '2026-10-10T00:00:00Z', json.dumps({'format': 'test'})))
            for symbol, weight in [('GABRB3', .9), ('GABRA5', .9), ('GABRG3', .4), ('LITERAL%_GENE', .01)]:
                c.execute('INSERT INTO reveal_ref_factor_genes VALUES(?,?,?)', (KEY, symbol, weight))
            self.node = {'id': SET1, 'name': 'Synaptic transmission', 'members': ['NCBIGene:2562'], 'was_generated_by': ref('Activity', 'x')}
            self.provenance = {'prefixes': {'NCBIGene': 'https://www.ncbi.nlm.nih.gov/gene/'},
                'activities': [{'id': ref('Activity', 'x'), 'description': 'Imported source processing'},
                               {'id': ref('Activity', 'y'), 'description': 'Another gene set\'s processing'}],
                'datasets': [], 'organizations': [], 'files': []}
            payload = {'collection': {'id': COLLECTION, 'name': 'GO BP'}, 'document_sha256': 'f' * 64, 'provenance': self.provenance}
            c.execute('INSERT INTO reveal_ref_collections VALUES(?,?,?,?,?,?)', (COLLECTION, 'GO_BP', 'GO', 2, PORTAL_COLLECTION, json.dumps(payload)))
            for identity, name, joint, marginal, jr, mr in [(SET1, 'Synaptic transmission', .4, .1, 1, 2), (SET2, 'Brain function', .2, .7, 1250, 1)]:
                c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?,?,?,?)', (identity, COLLECTION, name, 'GO', 100, 80, None, json.dumps(
                    {'dapper_gene_set': self.node if identity == SET1 else {'id': identity, 'name': name}, 'cfde_snapshot': '2026-09-28'})))
                c.execute('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?,?,?,?)', (KEY, identity, 'GO', joint, marginal, str(joint), str(marginal), jr, mr, 0))
            # Another factor's projections never leak into this one's.
            c.execute('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?,?,?,?)', ('KPN.TRAIT:0000398::Factor2', SET1, 'GO', 9., 9., '9', '9', 1, 1, 1))
        self.statements, self.opened = [], []
        details.clear_caches(); self.addCleanup(details.clear_caches)
        self.db_patch = patch.object(details, 'reference_mysql_connection', lambda: Connection(self.path, self.statements, self.opened))
        self.db_patch.start(); self.addCleanup(self.db_patch.stop)
        self.clock = [1000.0]
        clock = patch.object(details, 'monotonic', lambda: self.clock[0]); clock.start(); self.addCleanup(clock.stop)
        self.cat_patch = patch.object(api, 'catalog', self.catalog)
        self.cat_patch.start(); self.addCleanup(self.cat_patch.stop)
        self.client = TestClient(api.app)

    def validate(self, result, schema):
        Draft202012Validator({'$ref': '#/components/schemas/' + schema, **CONTRACT}).validate(result)

    def loadings(self, **kwargs): return details.factor_loadings(self.catalog, FACTOR['source_id'], **kwargs)

    def publish(self, release):
        with sqlite3.connect(self.path) as c: c.execute('UPDATE reveal_ref_release SET release_id=?', (release,))

    def test_public_detail_and_fixed_ranges(self):
        response = self.client.get('/v1/factors/' + FACTOR['source_id'])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.validate(result, 'FactorDetail')
        self.assertEqual(result['genes']['total'], 4)
        self.assertEqual(result['genes']['max'], .9)
        self.assertEqual(result['gene_sets']['min'], .2)
        self.assertIn('top 50 of their library', result['gene_sets']['coverage'])
        self.assertEqual(result['provenance'], {'eaggl_import_id': RELEASE, 'factor_id': 'T2D::Factor1', 'factor_key': KEY, 'model': KPN_MODEL})
        self.assertEqual(result['trait_links'], TRAIT_LINKS)
        self.assertTrue(self.statements[0].startswith('SELECT release_id,published_at,manifest FROM reveal_ref_release'))  # the pin comes first

    def test_trait_links_are_the_factor_metadata_summary_and_optional(self):
        binding = self.catalog.bindings[FACTOR['source_id']]
        binding['cfde_payload']['trait_links'] = {**TRAIT_LINKS, 'extra': 1, 'top_linked': [{**TRAIT_LINKS['top_linked'][0], 'extra': 2}]}
        detail = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual(detail['trait_links'], {**TRAIT_LINKS, 'top_linked': TRAIT_LINKS['top_linked'][:1]})  # contract keys only
        self.validate(detail, 'FactorDetail')
        del binding['cfde_payload']['trait_links']  # a release without the optional links file
        detail = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertNotIn('trait_links', detail); self.validate(detail, 'FactorDetail')

    def test_pagination_ties_search_and_wildcards(self):
        first = self.loadings(limit=1)
        second = self.loadings(limit=1, offset=first['next_offset'])
        self.assertEqual(first['items'][0]['label'], 'GABRA5')
        self.assertEqual(second['items'][0]['label'], 'GABRB3')
        self.assertEqual(second['items'][0]['rank'], 2)
        searched = self.loadings(q='gabrg')
        self.assertEqual(searched['total'], 1)
        self.assertEqual(searched['items'][0]['rank'], 3)
        self.assertEqual(searched['summary']['total'], 4)
        self.assertEqual(searched['summary']['max'], .9)
        self.assertEqual(self.loadings(q='%_')['total'], 1)
        self.assertEqual(self.loadings(q="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.loadings(offset=500)['items'], [])
        self.assertIsNone(self.loadings(offset=500)['next_offset'])
        self.validate(searched, 'FactorLoadings')

    def test_gene_set_scores_are_distinct_and_search_includes_library(self):
        joint, marginal = [self.loadings(kind='gene_set', metric=metric) for metric in ('joint', 'marginal')]
        self.assertEqual(joint['items'][0]['id'], SET1)
        self.assertEqual(marginal['items'][0]['id'], SET2)
        self.assertEqual(marginal['summary']['max'], .7)
        self.assertEqual(joint['items'][0]['marginal_loading'], .1)
        self.assertEqual(joint['items'][1]['rank'], 1250)
        self.assertEqual(marginal['items'][0]['rank'], 1)
        self.assertEqual(self.loadings(kind='gene_set', q='brain')['items'][0]['rank'], 1250)
        self.assertEqual(self.loadings(kind='gene_set', q='go')['total'], 2)
        self.validate(joint, 'FactorLoadings')

    def test_library_filter_keeps_per_library_ranks_and_the_whole_factor_summary(self):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?,?,?,?)', (SET3, COLLECTION, 'Insulin signalling', 'Reactome', 40, 30, None, '{}'))
            c.execute('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?,?,?,?)', (KEY, SET3, 'Reactome', .9, .3, '0.9', '0.3', 1, 1, 1))
        everything = self.loadings(kind='gene_set')
        self.assertEqual([(item['id'], item['library'], item['rank']) for item in everything['items']], [(SET3, 'Reactome', 1), (SET1, 'GO', 1), (SET2, 'GO', 1250)])
        go = self.loadings(kind='gene_set', library='GO', limit=1)
        self.assertEqual(([item['id'] for item in go['items']], go['total'], go['next_offset']), ([SET1], 2, 1))
        self.assertEqual(go['summary'], everything['summary'])  # ranges stay over every library
        self.assertEqual(self.loadings(kind='gene_set', library='GO', offset=1)['items'][0]['id'], SET2)
        self.assertEqual(self.loadings(kind='gene_set', library='GO', offset=5)['total'], 2)
        self.assertEqual(self.loadings(kind='gene_set', library='Reactome', q='insulin')['total'], 1)
        self.assertEqual(self.loadings(kind='gene_set', library='KEGG')['total'], 0)
        self.assertEqual(self.loadings(kind='gene_set', library='')['total'], 3)  # empty is no filter
        self.assertEqual(self.loadings(library='GO')['total'], 4)  # genes have no library: ignored, like metric
        self.validate(go, 'FactorLoadings')
        with self.assertRaises(Problem) as caught: self.loadings(kind='gene_set', library='x' * 65)
        self.assertEqual(caught.exception.status, 422)
        over_http = self.client.get('/v1/factor-loadings', params={'source_id': FACTOR['source_id'], 'kind': 'gene_set', 'library': 'GO'})
        self.assertEqual(over_http.status_code, 200, over_http.text)
        self.assertEqual([item['id'] for item in over_http.json()['items']], [SET1, SET2])  # the route passes library through

    def test_gene_set_summary_reads_the_projections_alone_and_ties_order_by_the_projection_key(self):
        with sqlite3.connect(self.path) as c:  # SET1's joint loading (0.4): the tie orders by gene set id
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?,?,?,?)', (SET3, COLLECTION, 'Brain function', 'GO', 40, 30, None, '{}'))
            c.execute('INSERT INTO reveal_ref_projections VALUES(?,?,?,?,?,?,?,?,?,?)', (KEY, SET3, 'GO', .4, .3, '0.4', '0.3', 2, 3, 0))
        detail = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual((detail['gene_sets']['total'], detail['gene_sets']['min'], detail['gene_sets']['max']), (3, .2, .4))
        summary = next(sql for sql in self.statements if 'MIN(p.joint_loading)' in sql)
        self.assertEqual(summary, 'SELECT COUNT(*),MIN(p.joint_loading),MAX(p.joint_loading) FROM reveal_ref_projections p WHERE p.factor_key=%s')
        page = self.loadings(kind='gene_set')
        self.assertEqual([item['id'] for item in page['items']], [SET1, SET3, SET2])
        self.assertTrue(self.statements[-1].endswith(' ORDER BY p.joint_loading DESC,p.gene_set_id LIMIT %s OFFSET %s'))
        self.assertEqual(self.loadings(kind='gene_set', metric='marginal')['summary']['max'], .7)
        self.assertIn('MAX(p.marginal_loading) FROM reveal_ref_projections p WHERE', self.statements[-2])
        alphabetical = self.loadings(kind='gene_set', sort='alphabetical')  # equal names: by gene set id
        self.assertEqual([item['id'] for item in alphabetical['items']], [SET2, SET3, SET1])
        self.assertTrue(self.statements[-1].endswith(' ORDER BY LOWER(s.gene_set_name),p.gene_set_id LIMIT %s OFFSET %s'))

    def test_alphabetical_gene_order_is_global_case_insensitive_and_preserves_loading_rank(self):
        with sqlite3.connect(self.path) as c:
            for symbol, loading in [('aMixed', .005), ('AMixed', .001)]:
                c.execute('INSERT INTO reveal_ref_factor_genes VALUES(?,?,?)', (KEY, symbol, loading))
        pages=[self.loadings(sort='alphabetical',limit=2,offset=offset) for offset in (0,2,4)]
        rows=[row for page in pages for row in page['items']]
        self.assertEqual([row['label'] for row in rows], ['AMixed','aMixed','GABRA5','GABRB3','GABRG3','LITERAL%_GENE'])
        self.assertEqual([row['rank'] for row in rows], [6,5,1,2,3,4])
        self.assertEqual(pages[0]['next_offset'],2)
        self.assertIsNone(pages[-1]['next_offset'])
        self.assertTrue(all(page['sort']=='alphabetical' for page in pages))
        matched=self.loadings(sort='alphabetical',q='mixed')
        self.assertEqual([row['rank'] for row in matched['items']],[6,5])
        self.assertEqual(self.loadings()['sort'],'loading')
        self.assertEqual(self.loadings()['items'][0]['label'],'GABRA5')

    def test_alphabetical_gene_sets_page_before_metric_and_keep_original_projection_rank(self):
        first=self.loadings(kind='gene_set',sort='alphabetical',limit=1)
        second=self.loadings(kind='gene_set',sort='alphabetical',limit=1,offset=first['next_offset'])
        self.assertEqual(first['items'][0]['label'],'Brain function')
        self.assertEqual(first['items'][0]['rank'],1250)
        self.assertEqual(second['items'][0]['label'],'Synaptic transmission')
        marginal=self.loadings(kind='gene_set',sort='alphabetical',metric='marginal',limit=1)
        self.assertEqual(marginal['items'][0]['id'],first['items'][0]['id'])
        self.assertEqual(marginal['items'][0]['rank'],1)
        route=self.client.get('/v1/factor-loadings',params={'source_id':FACTOR['source_id'],'kind':'gene_set','sort':'alphabetical','limit':1})
        self.assertEqual(route.status_code,200,route.text)
        self.assertEqual(route.json()['items'][0]['id'],SET2)

    def test_warm_pages_read_only_what_changes_with_the_request(self):
        self.annotations()
        cold = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual(len(self.statements), 4)  # release pin, gnomAD pin, gene and gene-set summaries
        self.assertFalse(any('ROW_NUMBER' in sql for sql in self.statements))  # the summary never ranks the factor
        del self.statements[:]
        self.assertEqual(details.factor_detail(self.catalog, FACTOR['source_id']), cold)
        self.assertEqual((self.statements, len(self.opened)), ([], 1))  # warm detail: no statement and no lease
        page = self.loadings(gnomad_import_id='a'*64)
        self.assertEqual(len(self.statements), 2)  # the lease's release pin and the page itself
        del self.statements[:]
        searched = self.loadings(q='gabr', gnomad_import_id='a'*64)
        self.assertEqual((len(self.statements), searched['total'], [item['rank'] for item in searched['items']]), (2, 3, [1, 2, 3]))
        self.assertEqual(searched['summary'], page['summary'])
        self.assertEqual(self.loadings(q='gabr', limit=1, offset=1)['total'], 3)
        self.assertEqual((self.loadings(q='gabr', offset=10)['total'], self.loadings(q='absent')['total']), (3, 0))
        self.clock[0] += details.PIN_TTL_SECONDS; del self.statements[:]
        details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual(len(self.statements), 2)  # past the TTL only the gnomAD pin is re-read, on a pinned lease

    def test_reads_are_pinned_to_the_catalog_release_and_cached_per_release(self):
        cold = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.publish(NEXT_RELEASE)  # published, but the catalog has not loaded it yet
        self.assertEqual(details.factor_detail(self.catalog, FACTOR['source_id']), cold)  # a fact of the catalog's release, no read
        for read in (lambda: self.loadings(), lambda: details.gene_set_detail(self.catalog, SET1)):
            with self.assertRaises(Problem) as caught: read()
            self.assertEqual((caught.exception.status, caught.exception.code), (409, 'SOURCE_REVISION_CHANGED'))
        self.assertTrue(all(connection.closed for connection in self.opened))  # a refused lease is still returned
        with sqlite3.connect(self.path) as c: c.execute('UPDATE reveal_ref_factor_genes SET loading=loading/2')
        self.catalog.reference_generation_id = NEXT_RELEASE  # the catalog swapped releases
        detail = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual((detail['generation_id'], detail['provenance']['eaggl_import_id'], detail['genes']['max']), (NEXT_RELEASE, NEXT_RELEASE, .45))
        with sqlite3.connect(self.path) as c: c.execute('DELETE FROM reveal_ref_release')
        details.clear_caches()
        with self.assertRaises(Problem) as caught: details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual((caught.exception.status, caught.exception.code), (503, 'SOURCE_NOT_READY'))  # failures are never cached
        self.publish_row(NEXT_RELEASE)
        self.assertEqual(details.factor_detail(self.catalog, FACTOR['source_id'])['genes']['max'], .45)

    def publish_row(self, release):
        with sqlite3.connect(self.path) as c: c.execute('INSERT INTO reveal_ref_release VALUES(?,?,?)', (release, '2026-10-11T00:00:00Z', '{}'))

    def test_exact_generation_and_revision_are_checked_before_database(self):
        with patch.object(details, 'reference_mysql_connection', side_effect=AssertionError('No unpinned query')):
            for options in ({'generation_id': '0' * 64}, {'source_revision': '0' * 64}):
                for method in (details.factor_detail, details.factor_loadings):
                    with self.assertRaises(Problem) as caught: method(self.catalog, FACTOR['source_id'], **options)
                    self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')
            with self.assertRaises(Problem): details.factor_detail(self.catalog, 'factor:unknown')

    def test_exact_gene_set_its_own_lineage_and_portal_links(self):
        response = self.client.get('/v1/catalog/gene-sets/' + SET1, params={'generation_id': RELEASE})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.validate(result, 'CatalogGeneSet')
        self.assertEqual(result['object'], self.node)
        # Only this gene set's lineage: its generating activity, not the collection's other one; empty groups are left out.
        self.assertEqual(result['provenance'], {'prefixes': self.provenance['prefixes'], 'activities': self.provenance['activities'][:1]})
        self.assertEqual(result['collection']['payload_sha256'], 'f' * 64)
        self.assertEqual(result['name'], 'Synaptic transmission')
        self.assertEqual(result['limitations'], [])
        self.assertNotIn('dapper_gene_set', result['metadata'])
        portal = 'https://translator.broadinstitute.org/artifact_provenance'
        self.assertEqual(result['external_links'], [
            {'label': 'Gene-set provenance on the Translator portal', 'kind': 'portal_gene_set', 'href': portal + '/gene_set/details/id=dapper%3AGeneSet.' + 'a' * 32},
            {'label': 'Collection on the Translator portal', 'kind': 'portal_collection', 'href': portal + '/gene_set/list/id=dapper%3AGeneSetCollection.' + 'p' * 32}])
        with patch.dict('os.environ', {'REVEAL_PROVENANCE_PORTAL_URL': 'https://portal.example.org/ap/'}):
            self.assertEqual(details.gene_set_detail(self.catalog, SET1)['external_links'][0]['href'],
                             'https://portal.example.org/ap/gene_set/details/id=dapper%3AGeneSet.' + 'a' * 32)

    def test_a_glygen_hz2_like_gene_set_serves_a_small_closure_reaching_its_curated_dataset(self):
        """6,620 cached API responses used by one extractor, each linked by the curated map to the GlyGen release."""
        extract, glygen = ref('Activity', 'glygen'), ref('Dataset', 'glygen')
        gwu, consortium, sheet = ref('Organization', 'gwu'), ref('Organization', 'glygenc'), ref('C2M2File', 'sheet')
        responses = [f'dapper:File.{index:0>32}' for index in range(6620)]
        producer = {'files': [{'id': node, 'filename': f'G{index:05d}AB.json'} for index, node in enumerate(responses)],
                    'c2m2_files': [{'id': sheet, 'filename': 'glycan_masterlist.csv'}], 'activities': [{'id': extract}],
                    'used_edges': [{'subject': extract, 'predicate': 'prov:used', 'object': node, 'edge_role': 'data_input'} for node in responses]}
        note = 'Patch: the producer names the cached API responses, not the GlyGen release.'
        curated = {'source': 'lap/config/source_datasets.yaml', 'source_sha256': '0' * 64, 'notes': [{'dataset': glygen, 'note': note}],
                   'groups': {'organizations': [{'id': gwu, 'name': 'George Washington University'}, {'id': consortium, 'name': 'GlyGen Consortium'}],
                              'datasets': [{'id': glygen, 'name': 'GlyGen data release v1.12.1', 'publisher': gwu, 'was_attributed_to': [consortium]}],
                              'was_derived_from_edges': [{'subject': node, 'predicate': 'prov:wasDerivedFrom', 'object': glygen} for node in responses]}}
        node = {'id': GLYGEN_SET, 'name': 'Glycan binding', 'members': ['HGNC.SYMBOL:MGAT1'], 'was_generated_by': extract}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO reveal_ref_collections VALUES(?,?,?,?,?,?)', (GLYGEN_COLLECTION, 'GlyGen__HZ2', 'GlyGen', 2529, GLYGEN_COLLECTION, canonical(
                {'collection': {'id': GLYGEN_COLLECTION}, 'document_sha256': '1' * 64, 'provenance': producer, 'curated_provenance': curated})))
            c.execute('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?,?,?,?)', (GLYGEN_SET, GLYGEN_COLLECTION, 'Glycan binding', 'GlyGen', 1, 1, None,
                                                                                 json.dumps({'dapper_gene_set': node})))
        result = details.gene_set_detail(self.catalog, GLYGEN_SET); self.validate(result, 'CatalogGeneSet')
        provenance = result['provenance']
        self.assertEqual(([item['id'] for item in provenance['files']], [item['id'] for item in provenance['datasets']]), ([responses[0]], [glygen]))
        self.assertEqual(sorted(item['id'] for item in provenance['organizations']), sorted([gwu, consortium]))
        self.assertEqual((len(provenance['used_edges']), len(provenance['was_derived_from_edges'])), (1, 1))
        self.assertLess(len(canonical(result)), 8192)
        self.assertEqual(result['limitations'][0], f'6,620 inputs of {extract} share one upstream lineage; {responses[0]} represents them.')
        self.assertIn('GlyGen data release v1.12.1', result['limitations'][1]); self.assertIn('curated source-dataset map', result['limitations'][1])
        self.assertEqual(result['limitations'][2:], [note])

    def test_gene_set_lineage_is_parsed_once_per_release_collection_and_document(self):
        payloads = lambda: [sql for sql in self.statements if sql == details.PAYLOAD_SQL]
        first = details.gene_set_detail(self.catalog, SET1)
        self.assertEqual((len(payloads()), first['collection']['payload_sha256']), (1, 'f' * 64))
        self.assertIn("JSON_EXTRACT(c.payload,'$.document_sha256')", next(sql for sql in self.statements if 'JOIN reveal_ref_collections' in sql))
        first['provenance']['activities'][0]['description'] = 'changed'; first['collection']['object']['name'] = 'changed'
        second = details.gene_set_detail(self.catalog, SET2)  # another gene set of the same collection document
        self.assertEqual(len(payloads()), 1)
        self.assertEqual(second['collection']['object'], {'id': COLLECTION, 'name': 'GO BP'})  # callers get copies
        self.assertEqual(details.gene_set_detail(self.catalog, SET1)['provenance']['activities'][0]['description'], 'Imported source processing')
        self.assertEqual(len(payloads()), 1)
        self.publish(NEXT_RELEASE); self.catalog.reference_generation_id = NEXT_RELEASE  # never served from another release's entry
        details.gene_set_detail(self.catalog, SET1)
        self.assertEqual(len(payloads()), 2)
        with sqlite3.connect(self.path) as c:  # another document of the collection
            c.execute('UPDATE reveal_ref_collections SET payload=? WHERE collection_id=?', (json.dumps(
                {'collection': {'id': COLLECTION, 'name': 'GO BP v2'}, 'document_sha256': 'e' * 64, 'provenance': {}}), COLLECTION))
        changed = details.gene_set_detail(self.catalog, SET1)
        self.assertEqual((changed['collection']['payload_sha256'], changed['collection']['object']['name'], changed['provenance']), ('e' * 64, 'GO BP v2', {}))
        self.assertEqual(len(payloads()), 3)

    def test_collection_facts_are_keyed_by_release_and_bounded(self):
        reads = []
        def read(sha='a' * 64, size=0):
            def payload():
                reads.append(sha)
                return json.dumps({'collection': {'id': COLLECTION}, 'document_sha256': sha, 'provenance': {'files': [{'id': ref('File', 'x'), 'pad': 'x' * size}]}})
            return payload
        facts = details.collection_facts(RELEASE, COLLECTION, 'a' * 64, read())
        self.assertIs(details.collection_facts(RELEASE, COLLECTION, 'a' * 64, read()), facts)
        self.assertEqual((reads, [item['id'] for item in facts['lineage']['files']]), (['a' * 64], [ref('File', 'x')]))
        details.collection_facts(NEXT_RELEASE, COLLECTION, 'a' * 64, read())  # the key always holds the release id
        details.collection_facts(None, COLLECTION, 'a' * 64, read()); details.collection_facts(None, COLLECTION, 'a' * 64, read())  # never kept
        details.collection_facts(RELEASE, COLLECTION, 'b' * 64, read()); details.collection_facts(RELEASE, COLLECTION, 'b' * 64, read())  # sha differs
        self.assertEqual(len(reads), 6)
        self.assertEqual(set(details._lineages), {(RELEASE, COLLECTION, 'a' * 64), (NEXT_RELEASE, COLLECTION, 'a' * 64)})
        details.collection_facts(RELEASE, COLLECTION, 'a' * 64, read())  # a hit: now the most recently used
        with patch.object(details, 'LINEAGE_ENTRIES', 2):
            details.collection_facts(RELEASE, GLYGEN_COLLECTION, 'c' * 64, read('c' * 64))  # the least recently used goes first
        self.assertEqual(set(details._lineages), {(RELEASE, COLLECTION, 'a' * 64), (RELEASE, GLYGEN_COLLECTION, 'c' * 64)})
        with patch.object(details, 'LINEAGE_BYTES', 4096):
            details.collection_facts(RELEASE, PORTAL_COLLECTION, 'd' * 64, read('d' * 64, 2500))
            self.assertEqual(len(details._lineages), 3)
            details.collection_facts(NEXT_RELEASE, PORTAL_COLLECTION, 'd' * 64, read('d' * 64, 2500))  # past the bytes: older entries go
            self.assertEqual(set(details._lineages), {(NEXT_RELEASE, PORTAL_COLLECTION, 'd' * 64)})
            details.collection_facts(RELEASE, GLYGEN_COLLECTION, 'e' * 64, read('e' * 64, 5000))  # larger than the bound: served, not kept
            self.assertEqual(set(details._lineages), {(NEXT_RELEASE, PORTAL_COLLECTION, 'd' * 64)})
        self.assertEqual(len(reads), 10)
        details.clear_caches()
        self.assertEqual(details._lineages, {})
        self.assertEqual(details.extracted('"' + 'f' * 64 + '"'), 'f' * 64)  # MySQL's JSON text
        self.assertEqual((details.extracted('1' * 64), details.extracted(None)), ('1' * 64, None))  # SQLite's bare string

    def test_unavailable_exact_object_never_fabricates_members_or_provenance(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reveal_ref_gene_sets SET metadata=? WHERE gene_set_id=?', ('{}', SET1))
        result = details.gene_set_detail(self.catalog, SET1)
        self.assertIsNone(result['object'])
        self.assertTrue(result['limitations'])
        self.assertEqual(result['provenance'], {'prefixes': self.provenance['prefixes']})  # no generating activity is guessed
        self.assertEqual(self.client.get('/v1/catalog/gene-sets/not-a-gene-set').status_code, 404)
        del self.opened[:]
        for identity in ('dapper:GeneSet.TNFα' + 'a' * 28, 'dapper:GeneSet.' + '٠' * 32):  # non-ASCII: no lease, never compared (MySQL 1267)
            self.assertEqual(self.client.get('/v1/catalog/gene-sets/' + identity).status_code, 404)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.client.get('/v1/catalog/gene-sets/' + SET3).status_code, 404)  # not referenced by this release
        self.assertEqual(self.client.get('/v1/catalog/gene-sets/' + SET1, params={'generation_id': '0' * 64}).status_code, 409)

    def test_gene_set_object_identity_mismatch_fails_closed(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reveal_ref_gene_sets SET metadata=? WHERE gene_set_id=?', (json.dumps({'dapper_gene_set': {'id': SET2}}), SET1))
        with self.assertRaises(Problem) as caught: details.gene_set_detail(self.catalog, SET1)
        self.assertEqual(caught.exception.status, 503)

    def test_invalid_queries_are_bounded(self):
        for query in ({'limit': 501}, {'limit': 0}, {'offset': -1}, {'kind': 'trait'}, {'metric': 'bad'}, {'sort': 'bad'}, {'q': 'x' * 201},
                      {'kind': 'gene_set', 'sort': 'gnomad_pli'}, {'kind': 'gene_set', 'gnomad_import_id': 'none'}, {'gnomad_import_id': 'bad'},
                      {'kind': 'gene_set', 'library': 'x' * 65}):
            response = self.client.get('/v1/factor-loadings', params={'source_id': FACTOR['source_id'], **query})
            self.assertEqual(response.status_code, 422, response.text)

    def annotations(self):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO gnomad_constraint_imports VALUES(?,?,?,?,?,?)', ('a'*64, '4.1', 'b'*64, None, 'ensembl-mane-canonical-v1', 'ready'))
            c.execute('INSERT INTO gnomad_constraint_active VALUES(?,?)', (application_prefix(), 'a'*64))
            for symbol, pli, loeuf, mis_z, flags in [('GABRA5', .9, .3, 5., []), ('GABRB3', .9, .4, 6., ['outlier']), ('GABRG3', None, .2, -1., [])]:
                c.execute('INSERT INTO gnomad_gene_constraints VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          ('a'*64, symbol, 'ENSG00000166206', 'ENST00000000001', 'selected', 'mane_select', pli, loeuf, mis_z, .1, json.dumps(flags)))

    def test_constraint_sort_is_global_null_last_stable_and_preserves_factor_values(self):
        self.annotations()
        expected = {'gnomad_pli': ['GABRA5', 'GABRB3', 'GABRG3', 'LITERAL%_GENE'],
                    'gnomad_loeuf': ['GABRG3', 'GABRA5', 'GABRB3', 'LITERAL%_GENE'],
                    'gnomad_mis_z': ['GABRB3', 'GABRA5', 'GABRG3', 'LITERAL%_GENE']}
        baseline = self.loadings()
        original = {item['id']: (item['rank'], item['loading']) for item in baseline['items']}
        for sort, labels in expected.items():
            with self.subTest(sort=sort):
                first = self.loadings(sort=sort, limit=2, gnomad_import_id='a'*64)
                second = self.loadings(sort=sort, limit=2, offset=first['next_offset'], gnomad_import_id='a'*64)
                rows = first['items'] + second['items']
                self.assertEqual([item['label'] for item in rows], labels)
                self.assertEqual({item['id']: (item['rank'], item['loading']) for item in rows}, original)
                self.assertEqual(first['summary'], baseline['summary'])
                self.assertIsNone(second['next_offset'])
                self.validate(first, 'FactorLoadings')
        self.assertEqual(self.loadings(sort='gnomad_mis_z')['items'][0]['gnomad']['flags'], ['outlier'])
        missing = self.loadings(q='LITERAL', sort='gnomad_pli')
        self.assertIsNone(missing['items'][0]['gnomad'])
        matched = self.loadings(q='GABRG3', sort='gnomad_pli')['items'][0]
        self.assertEqual(matched['rank'], 3)
        self.assertIsNone(matched['gnomad']['pli'])
        self.assertEqual(matched['gnomad']['loeuf'], .2)
        response = self.client.get('/v1/factor-loadings', params={'source_id': FACTOR['source_id'], 'sort': 'gnomad_loeuf', 'gnomad_import_id': 'a'*64})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['label'], 'GABRG3')

    def test_annotation_pin_rejects_changes_and_is_environment_scoped(self):
        self.assertIsNone(self.loadings(gnomad_import_id='none')['gnomad'])
        self.annotations()
        self.assertIsNone(details.factor_detail(self.catalog, FACTOR['source_id'])['gnomad'])  # the pin is re-read every 5 s
        self.assertEqual(self.loadings(gnomad_import_id='a'*64)['gnomad']['import_id'], 'a'*64)  # a newer pin is re-read, never a 409
        self.clock[0] += details.PIN_TTL_SECONDS
        detail = details.factor_detail(self.catalog, FACTOR['source_id'])
        self.assertEqual(detail['gnomad']['import_id'], 'a'*64)
        self.validate(detail, 'FactorDetail')
        for pin in ('none', 'c'*64):
            with self.assertRaises(Problem) as caught: self.loadings(gnomad_import_id=pin)
            self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')
        with sqlite3.connect(self.path) as c:  # the other environment's release tables (application_sql prefixes them)
            for table in ('release', 'factor_genes'): c.execute(f'CREATE VIEW reveal_unannotated_ref_{table} AS SELECT * FROM reveal_ref_{table}')
        with patch.dict('os.environ', {'REVEAL_APPLICATION_TABLE_PREFIX': 'reveal_unannotated'}):
            self.assertIsNone(self.loadings()['gnomad'])
            self.assertTrue(all(item['gnomad'] is None for item in self.loadings()['items']))
            self.assertTrue(any('reveal_unannotated_ref_factor_genes' in sql for sql in self.statements))
            with self.assertRaises(Problem) as caught: self.loadings(sort='gnomad_pli')
            self.assertEqual(caught.exception.code, 'SOURCE_NOT_READY')

    def test_unresolved_annotation_is_retained_as_unknown_never_zero(self):
        self.annotations()
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO gnomad_gene_constraints VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      ('a'*64, 'LITERAL%_GENE', None, None, 'ambiguous_gene', 'multiple_ensembl_gene_ids', None, None, None, None, '[]'))
        item = self.loadings(sort='gnomad_loeuf')['items'][-1]
        self.assertEqual(item['gnomad']['status'], 'ambiguous_gene')
        self.assertIsNone(item['gnomad']['selection_method'])
        self.assertIsNone(item['gnomad']['loeuf'])
        self.validate(self.loadings(), 'FactorLoadings')

    def test_missing_optional_migration_is_tolerated_but_other_database_errors_are_not(self):
        from pymysql.err import ProgrammingError, OperationalError
        with patch.object(details, '_rows', side_effect=ProgrammingError(1146, "Table 'db.gnomad_constraint_active' doesn't exist")):
            self.assertIsNone(details._gnomad(None))
        for error in (OperationalError(2006, 'Server gone away'), ProgrammingError(1146, "Table 'db.reveal_ref_factor_genes' doesn't exist")):
            self.clock[0] += details.PIN_TTL_SECONDS  # failures are never cached; the absent table is, for the TTL
            with patch.object(details, '_rows', side_effect=error), self.assertRaises(type(error)):
                details._gnomad(None)


if __name__ == '__main__':
    unittest.main()
