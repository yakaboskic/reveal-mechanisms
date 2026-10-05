"""Public factor browsing uses exact scientific import identity and bounded reads."""
from copy import deepcopy
import hashlib
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
from reveal_backend.runtime_config import ROOT

CONTRACT = json.loads((ROOT / 'api/openapi.json').read_text())
DETAIL = CONTRACT['paths']['/v1/factors/{source_id}']['get']['responses']['200']['content']['application/json']['examples']['factor']['value']
FACTOR = DETAIL['factor']
GENERATION = DETAIL['generation_id']
IMPORT = 'e' * 64
COLLECTION = 'dapper:GeneSetCollection.' + 'c' * 32
SET1, SET2 = ['dapper:GeneSet.' + c * 32 for c in ('a', 'b')]


class Cursor:
    def __init__(self, connection): self.cursor = connection.cursor()
    def __enter__(self): return self
    def __exit__(self, *exc): self.cursor.close()
    def execute(self, sql, args=()): self.cursor.execute(sql.replace('%s', '?'), args)
    def fetchall(self): return self.cursor.fetchall()


class Connection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.create_function('CONCAT', -1, lambda *values: ''.join(str(v) for v in values))
    def cursor(self): return Cursor(self.connection)
    def close(self): self.connection.close()


class FactorDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'reference.sqlite')
        self.catalog = SimpleNamespace(load=lambda: None, lock=threading.Lock(), reference_generation_id=GENERATION,
            factors={FACTOR['source_id']: deepcopy(FACTOR)}, model=KPN_MODEL, geneset_import=GENERATION,
            bindings={FACTOR['source_id']: {'eaggl_import_id': IMPORT, 'eaggl_factor_id': 'T2D::Factor1',
                'factor_key': 'KPN.TRAIT:0000398::Factor1', 'model': KPN_MODEL}})
        with sqlite3.connect(self.path) as c:
            c.executescript('''
CREATE TABLE eaggl_factors(import_id TEXT,factor_index INTEGER,factor_id_sha256 TEXT,factor_id TEXT);
CREATE TABLE eaggl_genes(import_id TEXT,gene_index INTEGER,symbol TEXT);
CREATE TABLE eaggl_gene_loadings(import_id TEXT,factor_index INTEGER,gene_index INTEGER,loading REAL);
CREATE TABLE cfde_gene_sets(generation_id TEXT,gene_set_id TEXT,collection_id TEXT,gene_set_name TEXT,library TEXT,n_genes INTEGER,n_genes_in_eaggl_universe INTEGER,metadata TEXT);
CREATE TABLE factor_gene_set_projections(generation_id TEXT,scope TEXT,factor_key TEXT,gene_set_id TEXT,joint_loading REAL,marginal_loading REAL,joint_rank INTEGER,marginal_rank INTEGER);
CREATE TABLE cfde_gene_set_collections(generation_id TEXT,collection_id TEXT,cfde_label TEXT,library TEXT,n_sets INTEGER,payload TEXT);
CREATE TABLE eaggl_cfde_gene_set_links(run_id TEXT,factor_index INTEGER,gene_set_import_id TEXT,resolved_alias_sha256 TEXT,source_key TEXT,node_id TEXT,gene_set_rank INTEGER);
CREATE TABLE cfde_gene_set_aliases(import_id TEXT,node_id_sha256 TEXT,dapper_id TEXT,provenance TEXT,source_key TEXT);
CREATE TABLE dapper_objects(id TEXT,payload TEXT);
''')
            c.execute('INSERT INTO eaggl_factors VALUES(?,?,?,?)', (IMPORT, 17, hashlib.sha256(b'T2D::Factor1').hexdigest(), 'T2D::Factor1'))
            for index, (symbol, weight) in enumerate([('GABRB3', .9), ('GABRA5', .9), ('GABRG3', .4), ('LITERAL%_GENE', .01)]):
                c.execute('INSERT INTO eaggl_genes VALUES(?,?,?)', (IMPORT, index, symbol))
                c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)', (IMPORT, 17, index, weight))
            self.node = {'id': SET1, 'name': 'Synaptic transmission', 'members': ['NCBIGene:2562'], 'was_generated_by': ['dapper:Activity.' + 'x' * 32]}
            self.provenance = {'prefixes': {'NCBIGene': 'https://www.ncbi.nlm.nih.gov/gene/'},
                'activities': [{'id': 'dapper:Activity.' + 'x' * 32, 'description': 'Imported source processing'}],
                'datasets': [], 'organizations': [], 'files': []}
            payload = {'collection': {'id': COLLECTION, 'name': 'GO BP'}, 'document_sha256': 'f' * 64, 'provenance': self.provenance}
            c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?,?,?,?)', (GENERATION, COLLECTION, 'GO_BP', 'GO', 2, json.dumps(payload)))
            for identity, name, joint, marginal, jr, mr in [(SET1, 'Synaptic transmission', .4, .1, 1, 2), (SET2, 'Brain function', .2, .7, 1250, 1)]:
                c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)', (GENERATION, identity, COLLECTION, name, 'GO', 100, 80, json.dumps({'dapper_gene_set': self.node if identity == SET1 else {'id': identity, 'name': name}, 'cfde_snapshot': '2026-09-28'})))
                c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?)', (GENERATION, 'per_trait', 'KPN.TRAIT:0000398::Factor1', identity, joint, marginal, jr, mr))
            # Same gene-set identity in another generation must never leak through.
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)', ('old', SET1, COLLECTION, 'WRONG GENERATION', 'BAD', 9, 9, '{}'))
        self.db_patch = patch.object(details, 'mysql_connection', lambda: Connection(self.path))
        self.db_patch.start(); self.addCleanup(self.db_patch.stop)
        self.cat_patch = patch.object(api, 'catalog', self.catalog)
        self.cat_patch.start(); self.addCleanup(self.cat_patch.stop)
        self.client = TestClient(api.app)

    def validate(self, result, schema):
        Draft202012Validator({'$ref': '#/components/schemas/' + schema, **CONTRACT}).validate(result)

    def loadings(self, **kwargs): return details.factor_loadings(self.catalog, FACTOR['source_id'], **kwargs)

    def test_public_detail_and_fixed_ranges(self):
        response = self.client.get('/v1/factors/' + FACTOR['source_id'])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.validate(result, 'FactorDetail')
        self.assertEqual(result['genes']['total'], 4)
        self.assertEqual(result['genes']['max'], .9)
        self.assertEqual(result['gene_sets']['min'], .2)
        self.assertIn('top 50', result['gene_sets']['coverage'])

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

    def test_alphabetical_gene_order_is_global_case_insensitive_and_preserves_loading_rank(self):
        with sqlite3.connect(self.path) as c:
            for index, symbol, loading in [(98, 'aMixed', .005), (99, 'AMixed', .001)]:
                c.execute('INSERT INTO eaggl_genes VALUES(?,?,?)', (IMPORT, index, symbol))
                c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)', (IMPORT, 17, index, loading))
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

    def test_exact_generation_and_revision_are_checked_before_database(self):
        with patch.object(details, 'mysql_connection', side_effect=AssertionError('No unpinned query')):
            for options in ({'generation_id': '0' * 64}, {'source_revision': '0' * 64}):
                for method in (details.factor_detail, details.factor_loadings):
                    with self.assertRaises(Problem) as caught: method(self.catalog, FACTOR['source_id'], **options)
                    self.assertEqual(caught.exception.code, 'SOURCE_REVISION_CHANGED')
            with self.assertRaises(Problem): details.factor_detail(self.catalog, 'factor:unknown')

    def test_exact_gene_set_and_collection_provenance(self):
        response = self.client.get('/v1/catalog/gene-sets/' + SET1, params={'generation_id': GENERATION})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json(); self.validate(result, 'CatalogGeneSet')
        self.assertEqual(result['object'], self.node)
        self.assertEqual(result['provenance'], self.provenance)
        self.assertEqual(result['collection']['payload_sha256'], 'f' * 64)
        self.assertEqual(result['name'], 'Synaptic transmission')
        self.assertEqual(result['limitations'], [])
        self.assertNotIn('dapper_gene_set', result['metadata'])

    def test_unavailable_exact_object_never_fabricates_members_or_provenance(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=? WHERE gene_set_id=?', ('{}', SET1))
        result = details.gene_set_detail(self.catalog, SET1)
        self.assertIsNone(result['object'])
        self.assertTrue(result['limitations'])
        self.assertEqual(self.client.get('/v1/catalog/gene-sets/not-a-gene-set').status_code, 404)
        self.assertEqual(self.client.get('/v1/catalog/gene-sets/' + SET1, params={'generation_id': '0' * 64}).status_code, 409)

    def test_gene_set_object_identity_mismatch_fails_closed(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_sets SET metadata=? WHERE gene_set_id=?', (json.dumps({'dapper_gene_set': {'id': SET2}}), SET1))
        with self.assertRaises(Problem) as caught: details.gene_set_detail(self.catalog, SET1)
        self.assertEqual(caught.exception.status, 503)

    def test_invalid_queries_are_bounded(self):
        for query in ({'limit': 501}, {'limit': 0}, {'offset': -1}, {'kind': 'trait'}, {'metric': 'bad'}, {'sort': 'bad'}, {'q': 'x' * 201}):
            response = self.client.get('/v1/factor-loadings', params={'source_id': FACTOR['source_id'], **query})
            self.assertEqual(response.status_code, 422, response.text)

    def test_legacy_rank_is_not_a_numeric_loading(self):
        self.catalog.bindings[FACTOR['source_id']].update(model='cfde-inc-v2', mapping_run_id='mapping')
        self.catalog.model = 'cfde-inc-v2'; self.catalog.geneset_import = 'legacy-import'
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO eaggl_cfde_gene_set_links VALUES(?,?,?,?,?,?,?)', ('mapping', 17, 'legacy-import', 'alias', 'legacy_set', 'gene_set:legacy', 3))
            c.execute('INSERT INTO eaggl_cfde_gene_set_links VALUES(?,?,?,?,?,?,?)', ('mapping', 17, 'legacy-import', None, 'Alpha legacy', 'gene_set:alpha', 8))
            c.execute('INSERT INTO cfde_gene_set_aliases VALUES(?,?,?,?,?)', ('legacy-import', 'alias', SET1, json.dumps({'source': 'legacy'}), 'legacy_set'))
            c.execute('INSERT INTO dapper_objects VALUES(?,?)', (SET1, json.dumps({key: value for key, value in self.node.items() if key != 'members'})))
        result = self.loadings(kind='gene_set')
        self.assertEqual(result['items'][0]['rank'], 3)
        self.assertIsNone(result['items'][0]['loading'])
        self.assertFalse(result['summary']['available'])
        self.assertIsNone(result['summary']['max'])
        self.validate(result, 'FactorLoadings')
        alphabetical = self.loadings(kind='gene_set', sort='alphabetical', limit=1)
        self.assertEqual(alphabetical['items'][0]['label'], 'Alpha legacy')
        self.assertEqual(alphabetical['items'][0]['rank'], 8)
        self.assertIsNone(alphabetical['items'][0]['loading'])
        gene_set = details.gene_set_detail(self.catalog, SET1)
        self.assertEqual(gene_set['provenance'], {'source': 'legacy'})
        self.assertIsNone(gene_set['gene_count'])
        self.validate(gene_set, 'CatalogGeneSet')
