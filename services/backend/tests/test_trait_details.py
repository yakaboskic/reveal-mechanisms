"""Public trait, gene-set beta, factor-trait link and Mechanism-id routes read one pinned release and match the contract."""
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
from reveal_backend import trait_details as traits
from reveal_backend.auth import Problem
from reveal_backend.reference_generation import public_id
from reveal_backend.runtime_config import ROOT

CONTRACT = json.loads((ROOT / 'api/openapi.json').read_text())
RELEASE, NEXT = 'd' * 64, 'e' * 64
T2D, RARE, HEIGHT, UNKNOWN = 'KPN.TRAIT:0000398', 'KPN.TRAIT:0000500', 'KPN.TRAIT:0001360', 'KPN.TRAIT:0009999'
F1, F2, R1 = public_id(T2D, 'Factor1'), public_id(T2D, 'Factor2'), public_id(RARE, 'Factor1')
S1, S2, S3 = ['dapper:GeneSet.' + c * 32 for c in 'abc']
COLLECTION = 'dapper:GeneSetCollection.' + 'c' * 32
CURRENT, OLD = 'dapper:Mechanism.' + '1' * 32, 'dapper:Mechanism.' + '2' * 32
GET_MECHANISM_OBJECT = CONTRACT['paths']['/v1/mechanism-objects/{dapper_id}']['get']
ARCHIVED = GET_MECHANISM_OBJECT['responses']['410']['content']['application/problem+json']['examples']['reference_generation_superseded']['value']['archived_reference_factor']
SUMMARY = {'n_phenotypes': 6698, 'n_linked': 3, 'n_linked_kpn_traits': 2, 'n_linked_atlas_traits': 1, 'own_trait_q': 1e-06}
ARTIFACTS = [{'ancestry_code': 'EU', 'ancestry_name': 'European', 'artifact_id': 'bl-t2d-eu'},
             {'ancestry_code': 'Mixed', 'ancestry_name': 'Mixed ancestry', 'artifact_id': 'bl t2d/mixed'}]
SCHEMA = '''
CREATE TABLE reveal_ref_release(release_id TEXT, published_at TEXT, manifest TEXT);
CREATE TABLE reveal_ref_traits(kpn_trait_id TEXT PRIMARY KEY, legacy_phenotype_id TEXT, phenotype_name TEXT, gwas_source_category TEXT,
  trait_group TEXT, trait_type TEXT, description TEXT, metadata TEXT);
CREATE TABLE reveal_ref_phenotypes(phenotype TEXT PRIMARY KEY, kpn_trait_id TEXT, phenotype_name TEXT, display_name TEXT, legacy_phenotype_id TEXT,
  category TEXT, is_atlas INTEGER, metadata TEXT);
CREATE TABLE reveal_ref_factors(factor_key TEXT PRIMARY KEY, public_id TEXT, kpn_trait_id TEXT, factor_number INTEGER, label TEXT, metadata TEXT);
CREATE TABLE reveal_ref_gene_sets(gene_set_id TEXT PRIMARY KEY, collection_id TEXT, gene_set_name TEXT, library TEXT, n_genes INTEGER);
CREATE TABLE reveal_ref_trait_gene_sets(kpn_trait_id TEXT, gene_set_id TEXT, library TEXT, beta_uncorrected REAL, beta REAL, avg_postp REAL,
  library_rank INTEGER, PRIMARY KEY(kpn_trait_id, gene_set_id));
CREATE TABLE reveal_ref_factor_trait_links(factor_key TEXT, phenotype TEXT, phenotype_kpn_trait_id TEXT, is_own_trait INTEGER, is_atlas_trait INTEGER,
  nnls_loading REAL, phewas_beta REAL, phewas_p_onesided REAL, phewas_q REAL, PRIMARY KEY(factor_key, phenotype));
'''


class Cursor:
    def __init__(self, connection, statements): self.cursor, self.lease, self.statements = connection.connection.cursor(), connection, statements
    def __enter__(self): return self
    def __exit__(self, *exc): self.cursor.close()
    def execute(self, sql, args=()):
        self.statements.append(sql); self.lease.statements.append(sql); self.cursor.execute(sql.replace('%s', '?'), args)
    def fetchall(self): return self.cursor.fetchall()


class Connection:
    def __init__(self, path, statements, opened):
        self.connection, self.shared, self.statements, self.closed = sqlite3.connect(path), statements, [], False
        opened.append(self)
    def cursor(self): return Cursor(self, self.shared)
    def close(self): self.connection.close(); self.closed = True


def bottom_line(portal=None, artifacts=(), gwas=None, reason=None):
    return {'bottom_line': {'portal_trait_id': portal, 'artifacts': list(artifacts), 'gwas_artifact': gwas, 'gwas_reason': reason}}


class TraitDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'reference.sqlite')
        with sqlite3.connect(self.path) as c:
            c.executescript(SCHEMA)
            c.execute('INSERT INTO reveal_ref_release VALUES(?,?,?)', (RELEASE, '2026-10-10 00:00:00', json.dumps({'release_id': RELEASE, 'absent_files': []})))
            c.executemany('INSERT INTO reveal_ref_traits VALUES(?,?,?,?,?,?,?,?)', [
                (T2D, 'T2D', 'Type 2 diabetes', 'KPN', 'metabolic', 'phenotype', 'Diabetes mellitus type 2',
                 json.dumps(bottom_line('T2D', ARTIFACTS, ARTIFACTS[1]))),
                (RARE, 'RARE_LIST', 'Rare disease list', 'rare_v2', None, None, None, json.dumps(bottom_line(reason='not_a_gwas_trait')))])
            c.executemany('INSERT INTO reveal_ref_phenotypes VALUES(?,?,?,?,?,?,?,?)', [
                ('T2D', T2D, 'Type 2 diabetes', 'Type 2 diabetes', 'T2D', 'KPN', 1, json.dumps(bottom_line('T2D', ARTIFACTS, ARTIFACTS[1]))),
                ('gcat_trait_height', HEIGHT, 'Height', 'Height', 'HEIGHT', 'gcat', 0, json.dumps(bottom_line(reason='not_on_portal'))),
                ('gcat_trait_noid', None, None, 'noid', None, None, 0, json.dumps(bottom_line(reason='not_on_portal')))])
            c.executemany('INSERT INTO reveal_ref_factors VALUES(?,?,?,?,?,?)', [
                (T2D + '::Factor1', F1, T2D, 1, 'Insulin secretion', json.dumps({'trait_links': {**SUMMARY, 'top_linked': []}})),
                (T2D + '::Factor2', F2, T2D, 2, 'Adiposity', json.dumps({})),
                (RARE + '::Factor1', R1, RARE, 1, 'Ciliopathy', json.dumps({}))])
            c.executemany('INSERT INTO reveal_ref_gene_sets VALUES(?,?,?,?,?)', [
                (S1, COLLECTION, 'Beta cell', 'GTEx', 40), (S2, COLLECTION, 'Islet', 'GTEx', 30), (S3, COLLECTION, 'Exercise', 'MoTrPAC', 20)])
            c.executemany('INSERT INTO reveal_ref_trait_gene_sets VALUES(?,?,?,?,?,?,?)', [
                (T2D, S1, 'GTEx', .5, .4, .3, 1), (T2D, S2, 'GTEx', .5, .1, .95, 2), (T2D, S3, 'MoTrPAC', 0., 0., .05, 1),
                (RARE, S1, 'GTEx', .2, .2, .5, 1)])
            c.executemany('INSERT INTO reveal_ref_factor_trait_links VALUES(?,?,?,?,?,?,?,?,?)', [
                (T2D + '::Factor1', 'T2D', T2D, 1, 1, .3, .5, 1e-9, 1e-6),
                (T2D + '::Factor1', 'gcat_trait_height', HEIGHT, 0, 0, .5, .2, 1e-5, 1e-3),
                (T2D + '::Factor1', 'gcat_trait_noid', None, 0, 0, .1, .9, 1e-4, .01),
                (T2D + '::Factor2', 'gcat_trait_height', HEIGHT, 0, 0, .2, .3, 1e-6, 1e-4),
                (RARE + '::Factor1', 'gcat_trait_height', HEIGHT, 0, 0, .4, .1, 1e-5, 1e-3),
                (RARE + '::Factor1', 'T2D', T2D, 0, 1, .05, .05, 1e-3, .02)])
        self.statements, self.opened = [], []
        traits.clear_caches(); self.addCleanup(traits.clear_caches)
        db = patch.object(traits, 'reference_mysql_connection', lambda: Connection(self.path, self.statements, self.opened))
        db.start(); self.addCleanup(db.stop)
        self.archived = []
        self.catalog = SimpleNamespace(load=lambda: None, lock=threading.Lock(), release_id=RELEASE,
            factors={F1: {'source_id': F1, 'object': {'id': CURRENT}}}, archived_for_mechanism=lambda identity: self.archived.append(identity) or (ARCHIVED if identity == OLD else None))
        catalog = patch.object(api, 'catalog', self.catalog); catalog.start(); self.addCleanup(catalog.stop)
        self.client = TestClient(api.app)

    def validate(self, result, schema):
        Draft202012Validator({'$ref': '#/components/schemas/' + schema, **CONTRACT}).validate(result)

    def get(self, path, status=200, schema=None, **params):
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, status, response.text)
        result = response.json()
        if schema or status >= 400: self.validate(result, schema or 'Problem')
        return result

    def betas(self, status=200, **params): return self.get('/v1/trait-gene-sets', status, 'TraitGeneSets' if status == 200 else None, **params)
    def links(self, status=200, **params): return self.get('/v1/factor-trait-links', status, 'FactorTraitLinks' if status == 200 else None, **params)

    def test_atlas_trait_detail_and_portal_links(self):
        result = self.get('/v1/traits/' + T2D, schema='TraitDetail')
        self.assertEqual({key: result[key] for key in ('phenotype', 'name', 'category', 'is_atlas', 'legacy_phenotype_id', 'trait_group', 'release_id')},
                         {'phenotype': 'T2D', 'name': 'Type 2 diabetes', 'category': 'KPN', 'is_atlas': True, 'legacy_phenotype_id': 'T2D',
                          'trait_group': 'metabolic', 'release_id': RELEASE})
        self.assertEqual(result['kpn_url'], 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000398/')
        portal = 'https://translator.broadinstitute.org/artifact_provenance'
        self.assertEqual(result['gwas'], {'status': 'available', 'portal_trait_id': 'T2D', 'gwas_artifact_id': 'bl t2d/mixed',
            'portal_href': portal + '/bottom_line/T2D', 'artifacts': [{**ARTIFACTS[0], 'href': portal + '/artifact/bl-t2d-eu'},
                                                                      {**ARTIFACTS[1], 'href': portal + '/artifact/bl%20t2d%2Fmixed'}]})
        self.assertEqual(result['factors'], [{'source_id': F1, 'label': 'Insulin secretion', 'factor_number': 1},
                                             {'source_id': F2, 'label': 'Adiposity', 'factor_number': 2}])
        self.assertEqual(result['gene_set_libraries'], [{'library': 'GTEx', 'n_gene_sets': 2, 'n_nonzero': 2},
                                                        {'library': 'MoTrPAC', 'n_gene_sets': 1, 'n_nonzero': 0}])
        self.assertEqual(result['linked_factors'], {'n_factors': 1})  # the rare trait's factor; T2D's own factor is not counted
        with patch.dict('os.environ', {'REVEAL_PROVENANCE_PORTAL_URL': 'https://portal.example.org/'}):  # links are built per request
            self.assertEqual(self.get('/v1/traits/' + T2D)['gwas']['portal_href'], 'https://portal.example.org/bottom_line/T2D')

    def test_non_atlas_phenotype_and_rare_trait_without_a_phenotype_row(self):
        height = self.get('/v1/traits/' + HEIGHT, schema='TraitDetail')
        self.assertEqual({key: height[key] for key in ('phenotype', 'name', 'category', 'is_atlas', 'legacy_phenotype_id', 'trait_group', 'factors',
                                                         'gene_set_libraries', 'linked_factors')},
                         {'phenotype': 'gcat_trait_height', 'name': 'Height', 'category': 'gcat', 'is_atlas': False, 'legacy_phenotype_id': 'HEIGHT',
                          'trait_group': None, 'factors': [], 'gene_set_libraries': [], 'linked_factors': {'n_factors': 3}})
        self.assertEqual(height['gwas'], {'status': 'not_on_portal', 'portal_trait_id': None, 'gwas_artifact_id': None, 'portal_href': None, 'artifacts': []})
        rare = self.get('/v1/traits/' + RARE, schema='TraitDetail')
        self.assertEqual((rare['phenotype'], rare['is_atlas'], rare['gwas']['status']), ('RARE_LIST', True, 'not_a_gwas_trait'))

    def test_unknown_or_malformed_trait_is_404_and_misses_are_not_cached(self):
        for identity in (UNKNOWN, 'T2D', 'KPN.TRAIT:398'):
            self.assertEqual(self.get('/v1/traits/' + identity, 404)['code'], 'NOT_FOUND')
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO reveal_ref_phenotypes VALUES(?,?,?,?,?,?,?,?)', ('late', UNKNOWN, None, 'late', None, None, 0, '{}'))
        self.assertEqual(self.get('/v1/traits/' + UNKNOWN)['phenotype'], 'late')

    def test_every_route_needs_a_published_release(self):
        with sqlite3.connect(self.path) as c: c.execute('DELETE FROM reveal_ref_release')
        for path, params in [('/v1/traits/' + T2D, {}), ('/v1/trait-gene-sets', {'trait_id': T2D}), ('/v1/factor-trait-links', {'source_id': F1})]:
            self.assertEqual(self.get(path, 503, **params)['code'], 'SOURCE_NOT_READY')
        with patch.object(traits, 'current_release', side_effect=ValueError('malformed')):
            self.assertEqual(self.get('/v1/traits/' + T2D, 503)['code'], 'SOURCE_NOT_READY')
        self.assertTrue(self.opened and all(connection.closed for connection in self.opened))

    def test_release_is_read_first_and_keys_every_cached_fact(self):
        first = self.get('/v1/traits/' + T2D)
        del self.statements[:]
        self.assertEqual(self.get('/v1/traits/' + T2D), first)
        self.assertEqual(len(self.statements), 1)  # a warm trait reads only its release
        self.betas(trait_id=T2D); del self.statements[:]
        self.betas(trait_id=T2D, limit=1, offset=1)
        self.assertEqual(len(self.statements), 2)  # the release and the page; the total is a fact of the release
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reveal_ref_release SET release_id=?,manifest=?', (NEXT, json.dumps({'release_id': NEXT})))
            c.execute("UPDATE reveal_ref_traits SET phenotype_name='Renamed' WHERE kpn_trait_id=?", (T2D,))
            c.execute('DELETE FROM reveal_ref_trait_gene_sets WHERE gene_set_id=?', (S3,))
        renamed = self.get('/v1/traits/' + T2D)
        self.assertEqual((renamed['name'], renamed['release_id']), ('Renamed', NEXT))
        self.assertEqual((self.betas(trait_id=T2D)['total'], self.betas(trait_id=T2D)['release_id']), (2, NEXT))
        self.links(source_id=F1); self.links(trait_id=HEIGHT)
        # one lease per request, closed, whose first statement reads the release
        self.assertTrue(all(lease.closed and 'reveal_ref_release' in lease.statements[0] for lease in self.opened))

    def test_trait_gene_sets_sort_page_and_filter(self):
        order = lambda result: [item['gene_set_id'] for item in result['items']]
        default = self.betas(trait_id=T2D)
        self.assertEqual((order(default), default['total'], default['next_offset'], default['sort']), ([S1, S2, S3], 3, None, 'beta_uncorrected'))
        self.assertEqual(default['items'][0], {'kpn_trait_id': T2D, 'trait_name': 'Type 2 diabetes', 'gene_set_id': S1, 'gene_set_name': 'Beta cell',
            'library': 'GTEx', 'collection_id': COLLECTION, 'n_genes': 40, 'beta_uncorrected': .5, 'beta': .4, 'avg_postp': .3, 'library_rank': 1})
        self.assertIn('absent pair is not a zero', default['coverage'])
        self.assertEqual(order(self.betas(trait_id=T2D, sort='beta')), [S1, S2, S3])
        self.assertEqual(order(self.betas(trait_id=T2D, sort='avg_postp')), [S2, S1, S3])
        pages = [self.betas(trait_id=T2D, limit=1, offset=offset) for offset in (0, 1, 2)]
        self.assertEqual(([page['items'][0]['gene_set_id'] for page in pages], [page['next_offset'] for page in pages]), ([S1, S2, S3], [1, 2, None]))
        past = self.betas(trait_id=T2D, offset=10)
        self.assertEqual((past['items'], past['total'], past['next_offset']), ([], 3, None))
        library = self.betas(trait_id=T2D, library='MoTrPAC')
        self.assertEqual((order(library), library['library'], library['items'][0]['beta_uncorrected']), ([S3], 'MoTrPAC', 0.))  # a kept zero
        reverse = self.betas(gene_set_id=S1)
        self.assertEqual(([item['kpn_trait_id'] for item in reverse['items']], reverse['trait_id'], reverse['gene_set_id']), ([T2D, RARE], None, S1))
        self.assertEqual(self.betas(trait_id=UNKNOWN)['total'], 0)

    def test_factor_trait_links_of_a_factor(self):
        result = self.links(source_id=F1)
        self.assertEqual([item['phenotype'] for item in result['items']], ['T2D', 'gcat_trait_height', 'gcat_trait_noid'])
        self.assertEqual((result['summary'], result['total'], result['scope'], result['trait_id']), (SUMMARY, 3, 'all', None))
        own, height, noid = result['items']
        self.assertEqual(own, {'source_id': F1, 'factor_label': 'Insulin secretion', 'fitted_kpn_trait_id': T2D, 'fitted_trait_name': 'Type 2 diabetes',
            'phenotype': 'T2D', 'phenotype_kpn_trait_id': T2D, 'phenotype_name': 'Type 2 diabetes', 'is_own_trait': True, 'is_atlas_trait': True,
            'nnls_loading': .3, 'phewas_beta': .5, 'phewas_p_onesided': 1e-9, 'phewas_q': 1e-6})
        self.assertEqual((noid['phenotype_kpn_trait_id'], noid['phenotype_name'], height['phenotype_name']), (None, 'noid', 'Height'))
        self.assertIn('not a negative', result['coverage'])
        order = lambda **params: [item['phenotype'] for item in self.links(source_id=F1, **params)['items']]
        self.assertEqual(order(sort='phewas_beta'), ['gcat_trait_noid', 'T2D', 'gcat_trait_height'])
        self.assertEqual(order(sort='nnls_loading'), ['gcat_trait_height', 'T2D', 'gcat_trait_noid'])
        self.assertEqual(order(scope='atlas'), ['T2D'])
        self.assertEqual(self.links(source_id=F1, scope='atlas')['total'], 1)
        page = self.links(source_id=F1, limit=2)
        self.assertEqual((len(page['items']), page['next_offset']), (2, 2))
        self.assertIsNone(self.links(source_id=F2)['summary'])  # a factor without a stored link summary
        self.assertEqual(self.links(404, source_id='factor:kpn:0000398:eaggl-capped-v1:Factor9')['code'], 'NOT_FOUND')

    def test_factor_trait_links_of_a_trait(self):
        result = self.links(trait_id=HEIGHT)
        # q ascending; the equal q of two factors is ordered by factor
        self.assertEqual([item['source_id'] for item in result['items']], [F2, F1, R1])
        self.assertEqual((result['summary'], result['source_id'], result['total']), (None, None, 3))
        self.assertEqual(self.links(trait_id=HEIGHT, scope='atlas')['total'], 0)  # height is not an atlas trait
        t2d = self.links(trait_id=T2D)
        self.assertEqual([(item['source_id'], item['is_own_trait']) for item in t2d['items']], [(F1, True), (R1, False)])
        self.assertEqual(self.links(trait_id=UNKNOWN)['items'], [])

    def test_absent_optional_files_say_so(self):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reveal_ref_release SET manifest=?', (json.dumps({'release_id': RELEASE, 'absent_files': ['trait_gene_sets.tsv.gz']}),))
        self.assertIn('gene-set betas', self.betas(404, trait_id=T2D)['detail'])
        self.assertEqual(self.links(source_id=F1)['total'], 3)
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reveal_ref_release SET manifest=?', (json.dumps({'release_id': RELEASE, 'absent_files': ['factor_trait_links.tsv.gz']}),))
        self.assertIn('factor-trait links', self.links(404, trait_id=T2D)['detail'])

    def test_invalid_queries_are_422(self):
        for params in ({}, {'trait_id': T2D, 'gene_set_id': S1}, {'trait_id': 'T2D'}, {'gene_set_id': 'gene-set'}, {'trait_id': T2D, 'sort': 'p'},
                       {'trait_id': T2D, 'limit': 0}, {'trait_id': T2D, 'limit': 501}, {'trait_id': T2D, 'limit': 'x'}, {'trait_id': T2D, 'offset': -1},
                       {'trait_id': T2D, 'library': ''}, {'trait_id': T2D, 'library': 'x' * 65}):
            with self.subTest(params=params): self.assertEqual(self.betas(422, **params)['code'], 'INVALID_QUERY')
        for params in ({}, {'source_id': F1, 'trait_id': T2D}, {'trait_id': 'T2D'}, {'source_id': ''}, {'source_id': F1, 'scope': 'some'},
                       {'source_id': F1, 'sort': 'beta'}, {'source_id': F1, 'limit': 501}, {'source_id': F1, 'offset': -1}):
            with self.subTest(params=params): self.assertEqual(self.links(422, **params)['code'], 'INVALID_QUERY')
        with patch.object(traits, 'reference_mysql_connection', side_effect=AssertionError('No query before validation')):
            for call in (lambda: traits.trait_gene_sets(trait_id=T2D, limit=0), lambda: traits.factor_trait_links(source_id='x' * 256)):
                with self.assertRaises(Problem) as caught: call()
                self.assertEqual(caught.exception.status, 422)

    def test_mechanism_object_current_superseded_and_unknown(self):
        current = self.get('/v1/mechanism-objects/' + CURRENT, schema='MechanismObject')
        self.assertEqual(current, {'dapper_id': CURRENT, 'status': 'current', 'source_id': F1, 'release_id': RELEASE})
        gone = self.get('/v1/mechanism-objects/' + OLD, 410)
        self.assertEqual((gone['code'], gone['archived_reference_factor']), ('REFERENCE_GENERATION_SUPERSEDED', ARCHIVED))
        self.assertEqual(self.get('/v1/mechanism-objects/dapper:Mechanism.' + '3' * 32, 404)['code'], 'NOT_FOUND')
        del self.archived[:]
        self.assertEqual(self.get('/v1/mechanism-objects/not-a-mechanism', 404)['code'], 'NOT_FOUND')
        self.assertEqual(self.archived, [])  # malformed ids never reach the archive index
        self.assertEqual(self.opened, [])  # resolved from the catalog, which serves the same release

    def test_mechanism_objects_route_is_not_shadowed_by_mechanisms(self):
        paths = [route.path for route in api.app.routes]
        self.assertLess(paths.index('/v1/mechanism-objects/{dapper_id:path}'), paths.index('/v1/mechanisms/{source_id:path}'))
        scope = {'type': 'http', 'method': 'GET', 'path': '/v1/mechanism-objects/' + CURRENT, 'root_path': ''}
        matched = next(route for route in api.app.routes if route.matches(scope)[0].name == 'FULL')
        self.assertEqual(matched.endpoint.__name__, 'get_mechanism_object')
        mechanism = {'type': 'http', 'method': 'GET', 'path': '/v1/mechanisms/' + F1, 'root_path': ''}
        self.assertEqual(next(route for route in api.app.routes if route.matches(mechanism)[0].name == 'FULL').endpoint.__name__, 'get_mechanism')
        for path in ('/v1/traits/{kpn_trait_id}', '/v1/trait-gene-sets', '/v1/factor-trait-links', '/v1/mechanism-objects/{dapper_id}'):
            self.assertIn('get', CONTRACT['paths'][path])

    def test_contract_examples_are_backend_shaped(self):
        self.assertEqual(GET_MECHANISM_OBJECT['operationId'], 'getMechanismObject')
        examples = lambda path: [example['value'] for example in CONTRACT['paths'][path]['get']['responses']['200']['content']['application/json']['examples'].values()]
        for value in examples('/v1/trait-gene-sets'): self.assertEqual(value['coverage'], traits.BETA_COVERAGE)
        for value in examples('/v1/factor-trait-links'): self.assertEqual(value['coverage'], traits.LINK_COVERAGE)
        parameters = {item['name']: item for item in CONTRACT['paths']['/v1/trait-gene-sets']['get']['parameters']}
        self.assertEqual(set(parameters), {'trait_id', 'gene_set_id', 'library', 'sort', 'limit', 'offset'})
        self.assertEqual(parameters['sort']['schema']['enum'], list(traits.BETA_SORTS))
        parameters = {item['name']: item for item in CONTRACT['paths']['/v1/factor-trait-links']['get']['parameters']}
        self.assertEqual(set(parameters), {'source_id', 'trait_id', 'scope', 'sort', 'limit', 'offset'})
        self.assertEqual(parameters['sort']['schema']['enum'], list(traits.LINK_SORTS))
