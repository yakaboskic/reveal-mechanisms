"""Real SQL against retained-generation fixtures; no external scientific fetches."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import tarfile
import unittest
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,urlparse

from reveal_backend.auth import Problem
from reveal_backend.evidence_package import DapperRuntime, EvidenceBuildError, canonical_json, decode, sha256
from reveal_backend.research_data import ReferenceQueryService, SmallModelBioIndex
from reveal_backend.research_seed import prepare_research_seed, validate_seed_shape
from reveal_backend.repository import digest
from reveal_backend.runtime_config import ROOT, CURRENT_DAPPER_SNAPSHOT

GEN='a'*64
OTHER='b'*64
IMP='c'*64
FACTOR='factor:kpn:0000398:eaggl-capped-v1:Factor1'
KEY='KPN.TRAIT:0000398::Factor1'
SET='dapper:GeneSet.'+'d'*32


class Cursor:
    def __init__(self,c,queries): self.cursor=c.cursor(); self.queries=queries
    def __enter__(self): return self
    def __exit__(self,*args): self.cursor.close()
    def execute(self,sql,args=()):
        self.queries.append((sql,tuple(args)))
        self.cursor.execute(sql.replace('%s','?'),args)
    def fetchall(self): return self.cursor.fetchall()


class Connection:
    def __init__(self,path,queries):
        self.c=sqlite3.connect(path); self.queries=queries
        self.c.create_function('SHA2',2,lambda value,bits: hashlib.sha256(value.encode()).hexdigest() if value is not None else None)
    def cursor(self): return Cursor(self.c,self.queries)
    def close(self): self.c.close()
    def rollback(self): self.c.rollback()


class ReferenceQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'refs.sqlite'); self.queries=[]
        self.service=ReferenceQueryService(lambda:Connection(self.path,self.queries),cursor_secret=b'test-durable-key')
        with sqlite3.connect(self.path) as c:
            c.executescript('''
CREATE TABLE reference_generations(generation_id TEXT,kind TEXT,model TEXT,status TEXT,eaggl_import_id TEXT,legacy_mapping_run_id TEXT,legacy_gene_set_import_id TEXT,manifest TEXT);
CREATE TABLE reference_factors(generation_id TEXT,factor_key TEXT,public_id TEXT,eaggl_factor_id TEXT,kpn_trait_id TEXT,label TEXT,source_revision TEXT,metadata TEXT);
CREATE TABLE eaggl_factors(import_id TEXT,factor_index INTEGER,factor_id TEXT,trait TEXT,label TEXT,input_sha256 TEXT,metadata TEXT,factor_id_sha256 TEXT);
CREATE TABLE eaggl_genes(import_id TEXT,gene_index INTEGER,symbol TEXT);
CREATE TABLE eaggl_gene_loadings(import_id TEXT,factor_index INTEGER,gene_index INTEGER,loading REAL);
CREATE TABLE cfde_gene_sets(generation_id TEXT,gene_set_id TEXT,collection_id TEXT,gene_set_name TEXT,library TEXT,n_genes INTEGER,n_genes_in_eaggl_universe INTEGER,metadata TEXT);
CREATE TABLE cfde_gene_set_collections(generation_id TEXT,collection_id TEXT,payload TEXT);
CREATE TABLE factor_gene_set_projections(generation_id TEXT,scope TEXT,factor_key TEXT,gene_set_id TEXT,joint_loading REAL,marginal_loading REAL,joint_loading_text TEXT,marginal_loading_text TEXT,joint_rank INTEGER,marginal_rank INTEGER,is_joint_top_factor INTEGER);
CREATE TABLE kpn_traits(generation_id TEXT,kpn_trait_id TEXT,legacy_phenotype_id TEXT,phenotype_name TEXT,trait_group TEXT,trait_type TEXT,description TEXT,metadata TEXT,gwas_source_category TEXT);
CREATE TABLE eaggl_graph_nodes(import_id TEXT,node_index INTEGER,node_id TEXT,factor_index INTEGER,payload TEXT);
CREATE TABLE eaggl_graph_edges(import_id TEXT,parent_index INTEGER,child_index INTEGER);
CREATE TABLE eaggl_cfde_factor_links(run_id TEXT,factor_index INTEGER,cfde_node_id TEXT,payload TEXT);
CREATE TABLE eaggl_cfde_gene_set_links(run_id TEXT,factor_index INTEGER,node_id TEXT,source_key TEXT,gene_set_rank INTEGER);
CREATE TABLE cfde_gene_set_aliases(import_id TEXT,dapper_id TEXT,source_key TEXT,node_id TEXT,node_id_sha256 TEXT,provenance TEXT);
CREATE TABLE dapper_objects(id TEXT,payload TEXT);
''')
            for generation,status in [(GEN,'superseded'),(OTHER,'complete')]:
                c.execute('INSERT INTO reference_generations VALUES(?,?,?,?,?,?,?,?)',
                    (generation,'kpn-eaggl-capped','eaggl-capped-v1',status,IMP,None,None,'{"source_release":"fixture"}'))
                c.execute('INSERT INTO reference_factors VALUES(?,?,?,?,?,?,?,?)',
                    (generation,KEY,FACTOR,'T2D::Factor1','KPN.TRAIT:0000398','retained' if generation==GEN else 'WRONG ACTIVE','e'*64,'{}'))
            c.execute('INSERT INTO eaggl_factors VALUES(?,?,?,?,?,?,?,?)',(IMP,1,'T2D::Factor1','T2D','label','e'*64,'{}',hashlib.sha256(b'T2D::Factor1').hexdigest()))
            for i,(symbol,loading) in enumerate([('GENE_A',.8),('GENE_B',.8),('literal%_',.1)]):
                c.execute('INSERT INTO eaggl_genes VALUES(?,?,?)',(IMP,i,symbol))
                c.execute('INSERT INTO eaggl_gene_loadings VALUES(?,?,?,?)',(IMP,1,i,loading))
            node={'id':SET,'name':'stored set','members':['HGNC.SYMBOL:GENE_A','HGNC.SYMBOL:GENE_B']}
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',(GEN,SET,'collection','stored set','GO',2,2,json.dumps({'dapper_gene_set':node})))
            c.execute('INSERT INTO cfde_gene_set_collections VALUES(?,?,?)',(GEN,'collection',json.dumps({'provenance':{'prefixes':{'HGNC.SYMBOL':'https://identifiers.org/hgnc.symbol:'}}})))
            c.execute('INSERT INTO factor_gene_set_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)',(GEN,'per_trait',KEY,SET,.4,.2,'0.4','0.2',1,1250,1))
            c.execute('INSERT INTO kpn_traits VALUES(?,?,?,?,?,?,?,?,?)',(GEN,'KPN.TRAIT:0000398','T2D','type 2 diabetes','kpn','phenotype','description','{}','KPN'))
            c.execute('INSERT INTO eaggl_graph_nodes VALUES(?,?,?,?,?)',(IMP,1,'T2D::Factor1',1,'{"kind":"factor"}'))
            c.execute('INSERT INTO eaggl_graph_nodes VALUES(?,?,?,?,?)',(IMP,2,'child',None,'{"kind":"factor_set"}'))
            c.execute('INSERT INTO eaggl_graph_edges VALUES(?,?,?)',(IMP,1,2))

    def query(self,op,args=None,gen=GEN): return self.service.query(op,args or {},generation_id=gen)

    def test_retained_generation_is_read_without_active_catalog(self):
        value=self.query('get_factor',{'factor_id':FACTOR})
        self.assertEqual(value.result['items'][0]['label'],'retained')
        self.assertEqual(value.source['generation_id'],GEN)
        self.assertEqual(value.raw,self.query('get_factor',{'factor_id':FACTOR}).raw)
        self.assertTrue(all('reference_active' not in sql for sql,_ in self.queries))

    def test_missing_retired_or_cross_generation_is_not_substituted(self):
        with self.assertRaises(Problem): self.query('get_factor',{'factor_id':FACTOR},'f'*64)
        with sqlite3.connect(self.path) as c: c.execute('UPDATE reference_generations SET status=? WHERE generation_id=?',('retired',GEN))
        with self.assertRaises(Problem): self.query('get_factor',{'factor_id':FACTOR})

    def test_loading_cursor_ties_and_filter_binding(self):
        first=self.query('get_factor_loadings',{'factor_id':FACTOR,'limit':1})
        self.assertEqual(first.result['items'][0]['symbol'],'GENE_A')
        self.assertEqual(first.result['status'],'partial')
        cursor=first.result['next_cursor']
        second=self.query('get_factor_loadings',{'factor_id':FACTOR,'limit':1,'cursor':cursor})
        self.assertEqual(second.result['items'][0]['symbol'],'GENE_B')
        for args,gen in [({'factor_id':FACTOR,'limit':1,'cursor':cursor,'q':'GENE'},GEN),
                         ({'factor_id':FACTOR,'limit':1,'cursor':cursor},OTHER),
                         ({'factor_id':FACTOR,'limit':1,'cursor':cursor+'x'},GEN)]:
            with self.assertRaises(Problem): self.query('get_factor_loadings',args,gen)
        literal=self.query('get_factor_loadings',{'factor_id':FACTOR,'q':'%_'})
        self.assertEqual([x['symbol'] for x in literal.result['items']],['literal%_'])

    def test_definition_members_and_projection_are_distinct(self):
        definition=self.query('get_gene_set',{'gene_set_id':SET})
        self.assertEqual(definition.result['items'][0]['metadata']['dapper_gene_set']['id'],SET)
        first=self.query('get_gene_set_members',{'gene_set_id':SET,'limit':1})
        self.assertEqual(first.result['total_rows'],2)
        self.assertEqual(first.result['items'][0]['source_pointer'],'/members/0')
        projection=self.query('get_factor_loadings',{'factor_id':FACTOR,'kind':'gene_set','metric':'marginal'})
        self.assertEqual(projection.result['items'][0]['marginal_rank'],1250)
        self.assertIn('top 50',projection.result['import_coverage'])
        self.assertNotIn('beta',projection.result['items'][0])
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE factor_gene_set_projections SET joint_loading_text=?',('0.40000000000000000001',))
        reverse=self.query('get_gene_set_factors',{'gene_set_id':SET})
        self.assertEqual(reverse.result['items'][0]['joint_loading_text'],'0.40000000000000000001')

    def test_essential_imported_read_families(self):
        cases=[('search_factors',{}),('search_genes',{'q':'GENE'}),('resolve_gene',{'gene':'GENE_A'}),
            ('get_gene_factors',{'gene':'GENE_A'}),('search_gene_sets',{}),('get_gene_set_factors',{'gene_set_id':SET}),
            ('search_traits',{}),('get_trait',{'trait_id':'KPN.TRAIT:0000398'}),
            ('get_connections',{'factor_id':FACTOR}),('get_imported_graph',{'node_id':'T2D::Factor1'})]
        for op,args in cases:
            with self.subTest(op=op): self.assertTrue(self.query(op,args).result['items'])
        self.assertEqual(self.query('get_imported_graph',{'node_id':'absent'}).result['status'],'not_stored')
        self.assertEqual(self.query('resolve_gene',{'gene':'absent'}).result['status'],'empty')
        with self.assertRaises(Problem): self.query('query_sql',{'sql':'SELECT 1'})
        with self.assertRaises(Problem): self.query('search_genes',{'model':'small'})

    def test_gene_gene_sets_match_exact_stored_membership_with_optional_factor_projection(self):
        other,third='dapper:GeneSet.'+'e'*32,'dapper:GeneSet.'+'f'*32
        with sqlite3.connect(self.path) as c:
            # LIKE wildcards in a symbol stay literal; a longer symbol ending in the query never matches.
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',(GEN,other,'collection','wildcard set','GO',2,2,
                json.dumps({'dapper_gene_set':{'id':other,'members':['HGNC.SYMBOL:GENEXA','HGNC.SYMBOL:PGENE_A']}})))
            c.execute('INSERT INTO cfde_gene_sets VALUES(?,?,?,?,?,?,?,?)',(GEN,third,'collection','larger set','GO',3,3,
                json.dumps({'dapper_gene_set':{'id':third,'members':['HGNC.SYMBOL:X','HGNC.SYMBOL:gene_a','HGNC.SYMBOL:Y']}})))
        found=self.query('get_gene_gene_sets',{'gene':'GENE_A'})
        self.assertEqual([(x['gene_set_id'],x['member'],x['source_pointer'],x['n_genes']) for x in found.result['items']],
                         [(SET,'HGNC.SYMBOL:GENE_A','/members/0',2),(third,'HGNC.SYMBOL:gene_a','/members/1',3)])
        self.assertEqual((found.result['status'],found.result['gene']),('complete','GENE_A'))
        self.assertIn('get_gene_set',found.result['import_coverage'])
        self.assertEqual(found.source['tables'],['reference_generations','cfde_gene_sets'])
        self.assertEqual(self.query('get_gene_gene_sets',{'gene':'HGNC.SYMBOL:gene_b'}).result['items'][0]['source_pointer'],'/members/1')
        self.assertEqual(self.query('get_gene_gene_sets',{'gene':'GENE%'}).result['status'],'empty')
        first=self.query('get_gene_gene_sets',{'gene':'GENE_A','limit':1})
        self.assertEqual((first.result['status'],[x['gene_set_id'] for x in first.result['items']]),('partial',[SET]))
        second=self.query('get_gene_gene_sets',{'gene':'GENE_A','limit':1,'cursor':first.result['next_cursor']})
        self.assertEqual([x['gene_set_id'] for x in second.result['items']],[third])
        # A factor restricts to its retained projections and carries their exact loadings.
        scoped=self.query('get_gene_gene_sets',{'gene':'gene_a','factor_id':FACTOR})
        self.assertEqual(len(scoped.result['items']),1)
        row=scoped.result['items'][0]
        self.assertEqual((row['gene_set_id'],row['joint_loading'],row['marginal_loading'],row['joint_loading_text'],row['marginal_rank'],row['source_pointer']),
                         (SET,.4,.2,'0.4',1250,'/members/0'))
        self.assertEqual(scoped.result['factor'],{'factor_id':FACTOR,'factor_key':KEY,'label':'retained'})
        self.assertIn('factor_gene_set_projections',scoped.source['tables'])
        self.assertIn('top 50',scoped.result['import_coverage'])
        self.assertEqual(self.query('get_gene_gene_sets',{'gene':'GENE_A','factor_id':'absent'}).result['status'],'not_stored')
        for bad in ('urn:x:GENE_A','GENE"A'):
            with self.assertRaises(Problem): self.query('get_gene_gene_sets',{'gene':bad})
        context=scoped.materialize(SeedTests.runtime())
        self.assertTrue(context['eligible_source_ids'])
        self.assertNotIn('gene_sets',context['dapper_context'])  # membership rows never mint a GeneSet
        semantics=next(item for item in self.service.catalog(GEN)['operations'] if item['name']=='get_gene_gene_sets')['semantics']
        self.assertEqual(semantics['factor_id'],scoped.result['import_coverage'].split(' Read the trusted GeneSet with get_gene_set before citing membership. ')[1])

    def test_phenotype_trait_resolves_kpn_or_bioindex_identifiers(self):
        expected={'kpn_trait_id':'KPN.TRAIT:0000398','legacy_phenotype_id':'T2D','phenotype_name':'type 2 diabetes','gwas_source_category':'KPN'}
        self.assertEqual(self.service.phenotype_trait(GEN,'KPN.TRAIT:0000398'),expected)
        self.assertEqual(self.service.phenotype_trait(GEN,'T2D'),expected)
        self.assertIsNone(self.service.phenotype_trait(GEN,'BMI'))
        with self.assertRaises(Problem): self.service.phenotype_trait('f'*64,'T2D')

    def test_kpn_factor_lookup_uses_unique_keys_and_matches_the_exact_text_join(self):
        from reveal_backend import research_data
        connection = Connection(self.path, self.queries)
        for identity in (FACTOR, KEY, 'T2D::Factor1', 'absent'):
            with self.subTest(identity=identity):
                indexed = self.service._kpn_factors(connection, GEN, IMP, [identity])
                text = self.service._rows(connection, research_data.KPN_FACTOR_TEXT_JOIN, [IMP, GEN, identity, identity, identity],
                                          research_data.KPN_FACTOR_COLUMNS)
                self.assertEqual(indexed, text)
        self.assertNotIn(' OR ', self.queries[0][0])
        # An import keyed another way (e.g. a canonical-JSON digest) is still found, through the exact text join.
        with sqlite3.connect(self.path) as c: c.execute('UPDATE eaggl_factors SET factor_id_sha256=?', (digest('T2D::Factor1'),))
        self.assertEqual([row['factor_index'] for row in self.service._kpn_factors(connection, GEN, IMP, [FACTOR])], [1])
        self.assertEqual(self.query('get_factor', {'factor_id': FACTOR}).result['items'][0]['factor_index'], 1)
        connection.close()

    def test_gene_factors_join_from_the_gene_with_or_without_the_symbol_index(self):
        capture=self.query('get_gene_factors',{'gene':'GENE_A'})
        statement=next(sql for sql,_ in reversed(self.queries) if 'FROM eaggl_genes g JOIN' in sql)
        self.assertTrue(statement.startswith('SELECT /*+ JOIN_ORDER(g, l, f) */ '))
        self.assertEqual(capture.result['items'],[{'factor_id':'T2D::Factor1','trait':'T2D','label':'label','loading':.8}])
        self.assertEqual(self.query('get_gene_factors',{'gene':'urn:reveal:eaggl-gene:'+IMP+':0'}).result['status'],'empty')
        # Migration 010 only adds the (import_id, symbol prefix) index online; the reader never depends on it.
        sql='\n'.join(line for line in (ROOT/'schema/migrations/010_eaggl_gene_symbol_index.sql').read_text().splitlines() if not line.startswith('--'))
        self.assertEqual(sql.strip(),'ALTER TABLE eaggl_genes ADD INDEX eaggl_genes_symbol (import_id, symbol(64)), ALGORITHM=INPLACE, LOCK=NONE;')

    def test_gene_crosswalk_requires_taxon_and_preserves_source_scope(self):
        from reveal_backend.gene_identity import MAPPING_REVISION
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE eaggl_genes SET symbol=? WHERE gene_index=0', ('ERCC2',))
        unknown = self.query('resolve_gene', {'gene': 'ERCC2'}).result['items'][0]
        self.assertIsNone(unknown['organism'])
        self.assertEqual(unknown['external_identity_mapping'], 'needs_taxon')
        resolved = self.query('resolve_gene', {'gene': 'ERCC2', 'taxon': 'NCBITaxon:9606',
            'mapping_revision': MAPPING_REVISION}).result['items'][0]
        self.assertEqual(resolved['identity_mapping']['status'], 'mapped')
        self.assertEqual(resolved['source_local_id'], 'urn:reveal:eaggl-gene:'+IMP+':0')
        self.assertEqual(resolved['identity_mapping']['source_local']['source_import'], IMP)
        self.assertTrue(resolved['identity_mapping']['verified_identity'])
        with self.assertRaises(Problem):
            self.query('resolve_gene', {'gene': 'ERCC2', 'taxon': 'NCBITaxon:9606', 'mapping_revision': '0'*64})

    def test_materialized_scientific_file_hash_and_evidence_eligibility(self):
        dapper=SeedTests.runtime()
        capture=self.query('get_gene_factors',{'gene':'GENE_A'})
        context=capture.materialize(dapper)
        node=context['dapper_context']['files'][0]
        self.assertEqual(node['sha256'],sha256(capture.raw))
        self.assertEqual(context['eligible_source_ids'],[node['id']])
        self.assertEqual(context['cfde_source_ids'],[node['id']])
        empty=self.query('get_imported_graph',{'node_id':'absent'}).materialize(dapper)
        self.assertEqual(empty['eligible_source_ids'],[])

    def test_progressive_factor_materializes_catalog_identity_and_pinned_trait(self):
        from reveal_backend.reference_generation import mechanism_node
        capture = self.query('get_factor', {'factor_id': FACTOR})
        runtime = SeedTests.runtime()
        expected = mechanism_node(FACTOR, 'type 2 diabetes', 'KPN.TRAIT:0000398', 'Factor1', 'retained')
        expected['id'] = runtime.compute_id(expected, 'Mechanism', runtime.schema)
        context = capture.materialize(runtime)
        self.assertEqual(context['dapper_context']['mechanisms'], [expected])
        self.assertEqual(context['object_resolution'][0]['dapper_id'], expected['id'])
        self.assertEqual(capture.result['items'][0]['trait_metadata']['phenotype_name'], 'type 2 diabetes')
        self.assertIn('kpn_traits', capture.source['tables'])
        self.assertEqual(context, self.query('get_factor', {'factor_id': FACTOR}).materialize(runtime))
        capture.result['items'][0]['trait_metadata']['phenotype_name'] = 'edited'
        with self.assertRaisesRegex(EvidenceBuildError, 'retained bytes'):
            capture.materialize(runtime)

    def test_versioned_mechanisms_separate_fits_and_preserve_historical_generation(self):
        from reveal_backend.reference_generation import mechanism_node
        runtime = SeedTests.runtime()
        legacy = self.query('get_factor', {'factor_id': FACTOR}).materialize(runtime)['dapper_context']['mechanisms'][0]
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reference_generations SET manifest=? WHERE generation_id=?', ('{"mechanism_identity_version":2}', GEN))
        fitted = self.query('get_factor', {'factor_id': FACTOR}).materialize(runtime)['dapper_context']['mechanisms'][0]
        self.assertNotEqual(legacy['id'], fitted['id'])
        for import_id in (IMP, '1'*64):
            exact = mechanism_node(FACTOR, 'type 2 diabetes', 'KPN.TRAIT:0000398', 'Factor1', 'retained',
                identity_version=2, eaggl_import_id=import_id)
            exact['id'] = runtime.compute_id(exact, 'Mechanism', runtime.schema)
            self.assertEqual(exact['id'] == fitted['id'], import_id == IMP)
        with sqlite3.connect(self.path) as c:
            c.execute('DELETE FROM kpn_traits WHERE generation_id=?', (GEN,))
        missing = self.query('get_factor', {'factor_id': FACTOR}).materialize(runtime)
        self.assertNotIn('mechanisms', missing['dapper_context'])
        self.assertEqual(missing['object_resolution'][0]['status'], 'unavailable')

    def test_legacy_import_reads_only_pinned_mapping_and_reports_rank_not_loading(self):
        legacy='1'*64; mapping='2'*64; sets='3'*64; native='factor:portal:T2D:cfde-inc-v2:Factor1'
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO reference_generations VALUES(?,?,?,?,?,?,?,?)',(legacy,'legacy-cfde-inc-v2','cfde-inc-v2','superseded',IMP,mapping,sets,'{}'))
            c.execute('INSERT INTO eaggl_cfde_factor_links VALUES(?,?,?,?)',(mapping,1,native,'{"match_method":"trait-factor-number"}'))
            c.execute('INSERT INTO eaggl_cfde_gene_set_links VALUES(?,?,?,?,?)',(mapping,1,'gene_set:source','source',1))
            c.execute('INSERT INTO cfde_gene_set_aliases VALUES(?,?,?,?,?,?)',(sets,SET,'source','gene_set:source','f'*64,'{}'))
            c.execute('INSERT INTO dapper_objects VALUES(?,?)',(SET,json.dumps({'id':SET,'name':'legacy set','members':['HGNC.SYMBOL:GENE_A']})))
        self.assertEqual(self.query('get_factor',{'factor_id':native},legacy).result['items'][0]['public_id'],native)
        ranked=self.query('get_factor_loadings',{'factor_id':native,'kind':'gene_set'},legacy)
        self.assertEqual(ranked.result['items'][0]['rank'],1)
        self.assertNotIn('loading',ranked.result['items'][0])
        self.assertIn('does not store numeric',ranked.result['import_coverage'])
        self.assertEqual(self.query('get_gene_set_members',{'gene_set_id':SET},legacy).result['items'][0]['member'],'HGNC.SYMBOL:GENE_A')
        self.assertEqual(self.query('get_gene_set_factors',{'gene_set_id':SET},legacy).result['status'],'not_stored')
        self.assertEqual(self.query('get_gene_gene_sets',{'gene':'GENE_A'},legacy).result['status'],'not_stored')
        catalog=self.service.catalog(legacy)
        self.assertEqual(catalog['generation']['model'],'cfde-inc-v2')
        self.assertEqual(catalog['generation']['legacy_mapping_run_id'],mapping)
        self.assertNotIn('manifest',catalog['generation'])
        availability={operation['name']:operation['availability'] for operation in catalog['operations']}
        self.assertEqual(availability['get_factor_loadings'],'supported')
        self.assertEqual(availability['get_gene_set_factors'],'not_stored')
        self.assertEqual(availability['get_gene_gene_sets'],'not_stored')
        self.assertEqual(availability['get_trait'],'not_stored')
        self.assertEqual(catalog['coverage']['gene_set_projections'],'not_stored')

    def test_catalog_reports_exact_retained_generation_and_truthful_model_coverage(self):
        catalog=self.service.catalog(GEN)
        self.assertEqual(catalog['generation']['generation_id'],GEN)
        self.assertEqual(catalog['generation']['status'],'superseded')
        self.assertEqual(catalog['generation']['model'],'eaggl-capped-v1')
        self.assertNotIn('manifest',catalog['generation'])
        self.assertEqual(catalog['generation']['generation_manifest_sha256'],sha256(canonical_json({'source_release':'fixture'})))
        self.assertEqual({item['availability'] for item in catalog['operations']},{'supported'})
        self.assertIn('top 50',catalog['coverage']['gene_set_projections'])
        with self.assertRaises(Problem): self.service.catalog('9'*64)

    def test_materialization_validates_exact_gene_set_and_never_invents_missing_dependencies(self):
        dapper=SeedTests.runtime()
        node={'name':'exact source set','member_type':'gene','members':['https://identifiers.org/hgnc.symbol:GENE_A']}
        node['id']=dapper.compute_id(node,'GeneSet',dapper.schema)
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_sets SET gene_set_id=?,metadata=? WHERE generation_id=?',
                      (node['id'],json.dumps({'dapper_gene_set':node}),GEN))
        result=self.query('get_gene_set',{'gene_set_id':node['id']}).materialize(dapper)
        self.assertEqual(result['dapper_context']['gene_sets'],[node])
        self.assertEqual(result['object_resolution'][0]['status'],'ready')
        node['was_generated_by']=['dapper:Activity.'+'a'*32]
        node['id']=dapper.compute_id(node,'GeneSet',dapper.schema)
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE cfde_gene_sets SET gene_set_id=?,metadata=? WHERE generation_id=?',
                      (node['id'],json.dumps({'dapper_gene_set':node}),GEN))
        missing=self.query('get_gene_set',{'gene_set_id':node['id']}).materialize(dapper)
        self.assertNotIn('gene_sets',missing['dapper_context'])
        self.assertEqual(missing['object_resolution'][0]['status'],'unavailable')

    def test_byte_budget_does_not_truncate_a_source_object_and_unknown_model_is_explicit(self):
        self.service.max_capture_bytes=20_300
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE reference_factors SET metadata=? WHERE generation_id=?',(json.dumps({'large':'x'*4000}),GEN))
        result=self.query('get_factor',{'factor_id':FACTOR})
        self.assertEqual(result.result['status'],'unavailable')
        self.assertEqual(result.result['items'],[])
        with sqlite3.connect(self.path) as c: c.execute('UPDATE reference_generations SET model=? WHERE generation_id=?',('unknown',GEN))
        result=self.query('search_factors')
        self.assertEqual(result.result['status'],'not_supported_for_model')


class SmallModelTests(unittest.TestCase):
    TRAIT={'kpn_trait_id':'KPN.TRAIT:0000398','legacy_phenotype_id':'T2D','phenotype_name':'type 2 diabetes','gwas_source_category':'KPN'}

    def adapter(self,pages,**kwargs):
        self.calls=[]; self.slept=[]
        def fetch(url,max_bytes,timeout):
            self.calls.append((url,max_bytes,timeout))
            value=pages.pop(0)
            if isinstance(value,Exception): raise value
            return value if isinstance(value,bytes) else canonical_json(value)
        kwargs.setdefault('sleep',self.slept.append)
        return SmallModelBioIndex(fetch=fetch,verified=True,clock=lambda:'2026-10-05T00:00:00Z',**kwargs)

    def traits(self,generation,phenotype):
        self.assertEqual(generation,GEN)
        return dict(self.TRAIT) if phenotype in ('T2D','KPN.TRAIT:0000398') else None

    def gene_page(self,gene,group='portal',phenotypes=('BMI','T2D'),**kwargs):
        return {'index':'pigean-gene','q':[group,gene,'2','small'],'continuation':None,'progress':{'bytes_read':10,'bytes_total':10},
                'data':[{'phenotype':p,'gene':gene.upper(),'trait_group':group,'gene_set_size':'small','sigma':2,'combined':1.5,'log_bf':.5,'prior':1.0}
                        for p in phenotypes],**kwargs}

    def page(self,**kwargs):
        return {'index':'pigean-gene-phenotype','data':[{'gene':'INS','phenotype':'T2D','gene_set_size':'small','sigma':2,'combined':2.3}],
                'continuation':None,'progress':{'bytes_read':10,'bytes_total':10},**kwargs}

    def test_fixed_three_key_query_and_captured_pages(self):
        adapter=self.adapter([self.page(continuation='opaque'),self.page()])
        capture=adapter.query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(parse_qs(urlparse(self.calls[0][0]).query)['q'],['T2D,2,small'])
        self.assertEqual(urlparse(self.calls[1][0]).path,'/api/bio/cont')
        self.assertEqual(capture.result['status'],'complete')
        self.assertEqual(len(capture.extra_files),2)
        self.assertIsNone(capture.source['generation_id'])
        materialized=capture.materialize(SeedTests.runtime())
        self.assertTrue(materialized['eligible_source_ids']); self.assertEqual(materialized['cfde_source_ids'],[])

    def test_gene_set_query_is_the_only_other_allowed_index(self):
        adapter=self.adapter([self.page(index='pigean-gene-set-phenotype',data=[{'gene_set':'test','gene_set_size':'small','sigma':2,'beta':.5}])])
        value=adapter.query('get_pigean_gene_set_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(value.result['status'],'complete')
        self.assertIn('/pigean-gene-set-phenotype?',self.calls[0][0])
        for op,args in [('get_factor',{'phenotype_id':'T2D'}),('get_pigean_gene_phenotype',{'phenotype_id':'T2D','model':'cfde'}),
                        ('get_pigean_gene_phenotype',{'phenotype_id':'T2D,2,large'})]:
            with self.assertRaises(Problem): adapter.query(op,args)

    def test_false_completeness_budgets_mismatch_and_outage(self):
        partial=self.adapter([self.page(progress={'bytes_read':10,'bytes_total':20})]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(partial.result['status'],'partial')
        bounded=self.adapter([self.page(continuation='next')],max_pages=1).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(bounded.result['status'],'partial')
        mismatch=self.adapter([self.page(data=[{'gene_set_size':'large'}])]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(mismatch.result['status'],'source_unavailable'); self.assertEqual(mismatch.result['items'],[])
        denied=self.adapter([HTTPError('https://bioindex.hugeamp.org',403,'Forbidden',None,None)]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(denied.result['status'],'source_unavailable'); self.assertEqual(len(self.calls),1)
        too_big=self.adapter([b' '*101],max_bytes=100).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(too_big.result['status'],'source_unavailable')

    def test_limit_is_forwarded_and_a_capped_result_stays_partial(self):
        adapter=self.adapter([self.page()])
        default=adapter.query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(parse_qs(urlparse(self.calls[0][0]).query)['limit'],['100'])
        self.assertEqual((default.result['limit'],default.source['bounds']['rows']),(100,100))
        capped=self.adapter([self.page(index='pigean-gene-set-phenotype',progress={'bytes_read':10,'bytes_total':6_500_000})]).query('get_pigean_gene_set_phenotype',
            {'phenotype_id':'T2D','limit':2})
        self.assertEqual(parse_qs(urlparse(self.calls[0][0]).query)['limit'],['2'])
        self.assertEqual((capped.result['status'],capped.result['truncated']),('partial',True))
        over=self.adapter([self.page(data=[dict(self.page()['data'][0]) for _ in range(3)])]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D','limit':2})
        self.assertEqual((over.result['returned_rows'],over.result['status']),(2,'partial'))
        for bad in ({'phenotype_id':'T2D','limit':0},{'phenotype_id':'T2D','limit':501},{'phenotype_id':'T2D','limit':True},
                    {'phenotype_id':'T2D','genes':['INS']} ,{}):
            with self.subTest(bad=bad),self.assertRaises(Problem):
                self.adapter([]).query('get_pigean_gene_set_phenotype' if 'genes' in bad else 'get_pigean_gene_phenotype',bad)

    def test_per_gene_mode_reads_one_phenotype_row_per_gene_in_its_trait_group(self):
        adapter=self.adapter([self.gene_page('INS'),self.gene_page('gck',phenotypes=('BMI',))],traits=self.traits)
        capture=adapter.query('get_pigean_gene_phenotype',{'phenotype_id':'KPN.TRAIT:0000398','genes':['HGNC.SYMBOL:INS','gck']},generation_id=GEN)
        self.assertEqual([parse_qs(urlparse(url).query) for url,_,_ in self.calls],[{'q':['portal,INS,2,small']},{'q':['portal,gck,2,small']}])
        self.assertTrue(all('/pigean-gene?' in url for url,_,_ in self.calls))
        self.assertEqual([(row['gene'],row['phenotype']) for row in capture.result['items']],[('INS','T2D')])
        self.assertEqual(capture.result['queries'],[{'gene':'INS','trait_group':'portal','status':'complete','returned_rows':1},
                                                    {'gene':'gck','trait_group':'portal','status':'complete','returned_rows':0}])
        self.assertEqual(capture.result['status'],'complete')
        self.assertIn('not a biological negative',capture.result['absence_note'])
        self.assertEqual((capture.source['index'],capture.source['phenotype'],capture.source['genes'],capture.source['trait_groups']),
                         ('pigean-gene','T2D',['INS','gck'],['portal']))
        self.assertEqual(capture.source['trait'],{**self.TRAIT,'trait_group':'portal','reference_generation_id':GEN})
        self.assertIsNone(capture.source['generation_id'])
        self.assertEqual(sorted(capture.extra_files),['page-0.json','page-1.json'])
        self.assertTrue(capture.materialize(SeedTests.runtime())['eligible_source_ids'])
        # Without a phenotype, each gene is read in every mapped trait group under the row limit.
        alone=self.adapter([self.gene_page('INS'),self.gene_page('INS','rare_v2',phenotypes=('Rare_diabetes',))]).query(
            'get_pigean_gene_phenotype',{'genes':['INS'],'limit':5})
        self.assertEqual([parse_qs(urlparse(url).query) for url,_,_ in self.calls],
                         [{'q':['portal,INS,2,small'],'limit':['5']},{'q':['rare_v2,INS,2,small'],'limit':['5']}])
        self.assertEqual([row['phenotype'] for row in alone.result['items']],['BMI','T2D','Rare_diabetes'])
        self.assertIsNone(alone.source['phenotype'])
        # A per-KPN-trait phenotype resolves to its BioIndex identifier in phenotype mode as well.
        resolved=self.adapter([self.page()],traits=self.traits).query('get_pigean_gene_phenotype',{'phenotype_id':'KPN.TRAIT:0000398'},generation_id=GEN)
        self.assertEqual(parse_qs(urlparse(self.calls[0][0]).query)['q'],['T2D,2,small'])
        self.assertEqual(resolved.source['trait']['kpn_trait_id'],'KPN.TRAIT:0000398')
        plain=self.adapter([self.page(data=[{'phenotype':'BMI','gene_set_size':'small','sigma':2}])],traits=self.traits).query(
            'get_pigean_gene_phenotype',{'phenotype_id':'BMI'},generation_id=GEN)
        self.assertEqual((plain.result['status'],plain.source.get('trait')),('complete',None))

    def test_per_gene_mode_refuses_unknown_trait_groups_and_scope_escapes(self):
        for arguments,traits in (({'phenotype_id':'BMI','genes':['INS']},self.traits),({'phenotype_id':'KPN.TRAIT:0000001'},self.traits),
                                 ({'phenotype_id':'T2D','genes':['INS']},None),
                                 ({'phenotype_id':'T2D','genes':['INS']},lambda g,p:{**self.TRAIT,'gwas_source_category':'gcat'})):
            with self.subTest(arguments=arguments),self.assertRaises(Problem) as error:
                self.adapter([]).query('get_pigean_gene_phenotype',arguments,generation_id=GEN) if traits is None else \
                    self.adapter([],traits=traits).query('get_pigean_gene_phenotype',arguments,generation_id=GEN)
            self.assertEqual(error.exception.code,'INVALID_QUERY'); self.assertEqual(self.calls,[])
        for name,page in (('gene',{**self.gene_page('GCK'),'data':self.gene_page('INS')['data']}),
                          ('trait group',{**self.gene_page('GCK'),'data':self.gene_page('GCK','rare_v2')['data']}),
                          ('query keys',self.gene_page('GCK',q=['portal','GCK','2','large']))):
            with self.subTest(escape=name):
                escaped=self.adapter([self.gene_page('INS',phenotypes=('T2D',)),page],traits=self.traits).query(
                    'get_pigean_gene_phenotype',{'phenotype_id':'T2D','genes':['INS','GCK']},generation_id=GEN)
                self.assertEqual((escaped.result['status'],escaped.result['items']),('source_unavailable',[]))
                self.assertEqual({entry['status'] for entry in escaped.result['queries']},{'unavailable'})

    def test_transient_failures_retry_within_bounds_and_outages_are_never_absence(self):
        retry_after=lambda value: HTTPError('https://bioindex.hugeamp.org',429,'Too Many',{'Retry-After':value},None)
        unavailable=lambda: HTTPError('https://bioindex.hugeamp.org',503,'Unavailable',None,None)
        recovered=self.adapter([unavailable(),retry_after('1'),self.page()]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual((recovered.result['status'],len(self.calls),self.slept),('complete',3,[.5,1.0]))
        timed=self.adapter([TimeoutError('timed out'),URLError(TimeoutError('timed out')),self.page()]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual((timed.result['status'],len(self.calls)),('complete',3))
        self.assertTrue(all(timeout<=10 for _,_,timeout in self.calls))
        down=self.adapter([unavailable(),unavailable(),unavailable()]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual((down.result['status'],down.result['items'],len(self.calls)),('source_unavailable',[],3))
        self.assertIn('HTTP 503 after 3 attempts',down.result['reason']); self.assertIn('not evidence of absence',down.result['reason'])
        self.assertEqual(down.materialize(SeedTests.runtime())['eligible_source_ids'],[])
        for failure in (retry_after('30'),retry_after('Wed, 21 Oct 2015 07:28:00 GMT'),URLError('Name or service not known')):
            with self.subTest(failure=failure):
                final=self.adapter([failure]).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
                self.assertEqual((final.result['status'],len(self.calls),self.slept),('source_unavailable',1,[]))
        # An outage after earlier genes keeps their rows, marks the gene unavailable and the rest not queried.
        partial=self.adapter([self.gene_page('INS'),unavailable(),unavailable(),unavailable()],traits=self.traits).query(
            'get_pigean_gene_phenotype',{'phenotype_id':'T2D','genes':['INS','GCK','PDX1']},generation_id=GEN)
        self.assertEqual((partial.result['status'],partial.result['truncated'],len(partial.result['items'])),('partial',True,1))
        self.assertEqual([(entry['gene'],entry['status']) for entry in partial.result['queries']],
                         [('INS','complete'),('GCK','unavailable'),('PDX1','not_queried')])
        self.assertIn('outage',partial.result['reason'])
        budget=self.adapter([self.gene_page('INS'),self.gene_page('GCK')],traits=self.traits,max_bytes=len(canonical_json(self.gene_page('INS')))+5).query(
            'get_pigean_gene_phenotype',{'phenotype_id':'T2D','genes':['INS','GCK']},generation_id=GEN)
        self.assertEqual((budget.result['status'],[entry['status'] for entry in budget.result['queries']]),('partial',['complete','unavailable']))
        self.assertIn('not observed',budget.result['reason'])

    def test_live_fetch_names_its_agent_and_never_follows_redirects(self):
        from unittest.mock import patch
        from reveal_backend import research_data
        seen=[]
        class Opener:
            def __init__(self,body): self.body=body
            def open(self,request,timeout): seen.append((request,timeout)); return io.BytesIO(self.body)
        with patch.object(research_data,'build_opener',return_value=Opener(b'{"data":[]}')) as build:
            self.assertEqual(SmallModelBioIndex._fetch(SmallModelBioIndex.HOST+'/api/bio/indexes',100,10),b'{"data":[]}')
        request,timeout=seen[0]
        # The host's CDN refuses urllib's default Python-urllib agent with HTTP 403.
        self.assertEqual((request.get_header('User-agent'),request.get_header('Accept'),timeout),
                         (SmallModelBioIndex.USER_AGENT,'application/json',10))
        self.assertNotIn('urllib',SmallModelBioIndex.USER_AGENT.lower())
        self.assertIsInstance(build.call_args.args[0],research_data._NoRedirect)
        with patch.object(research_data,'build_opener',return_value=Opener(b' '*101)),self.assertRaises(ValueError):
            SmallModelBioIndex._fetch(SmallModelBioIndex.HOST+'/api/bio/indexes',100,10)

    def test_verify_uses_each_index_key_count_and_the_mapped_trait_groups(self):
        catalog={'data':[{'index':index,'query':{'keys':keys}} for index,keys in SmallModelBioIndex.SIGNATURES.items()]}
        small={'keys':[['large'],['small']]}; groups={'keys':[['portal'],['rare_v2'],['hpo']]}
        adapter=self.adapter([catalog,small,small,small,groups]); adapter.verified=False
        adapter.verify()
        self.assertTrue(adapter.verified)
        self.assertEqual([urlparse(url).path for url,_,_ in self.calls],['/api/bio/indexes','/api/bio/keys/pigean-gene-phenotype/3',
            '/api/bio/keys/pigean-gene-set-phenotype/3','/api/bio/keys/pigean-gene/4','/api/bio/keys/pigean-gene/4'])
        self.assertEqual(parse_qs(urlparse(self.calls[-1][0]).query),{'columns':['trait_group']})
        for pages in ([{'data':catalog['data'][:2]}],[catalog,small,small,small,{'keys':[['portal']]}],[catalog,{'keys':[['large']]}]):
            with self.subTest(pages=len(pages)),self.assertRaises(Problem):
                self.adapter(pages).verify()

    def test_unverified_deployment_performs_no_scientific_request(self):
        def reject(*args): raise AssertionError('unexpected source call')
        result=SmallModelBioIndex(fetch=reject).query('get_pigean_gene_phenotype',{'phenotype_id':'T2D'})
        self.assertEqual(result.result['status'],'source_unavailable')

    def test_capabilities_match_configured_execution_gate_without_requests(self):
        from unittest.mock import patch
        from reveal_backend.research_data import capability_catalog
        for value in ('false', 'true'):
            with patch('reveal_backend.runtime_config.setting', return_value=value):
                descriptor = SmallModelBioIndex.descriptor()
                self.assertEqual(descriptor['deployment_verified'], value == 'true')
                self.assertEqual(descriptor['availability'] == 'supported', value == 'true')
                self.assertEqual(capability_catalog()['small_model_phenotype'], descriptor)
                self.assertEqual(set(descriptor['operations']), set(SmallModelBioIndex.INDEXES))
                self.assertEqual((descriptor['model'], descriptor['sigma']), ('small', 2))


class SeedTests(unittest.TestCase):
    @staticmethod
    def runtime(): return DapperRuntime(CURRENT_DAPPER_SNAPSHOT)

    def inputs(self):
        dapper=self.runtime()
        gap={'text':'Which mechanism explains this observation?','gap_description':'An unanswered question','gap_kind':'KNOWLEDGE_GAP','scope':'test disease'}
        gap['id']=dapper.compute_id(gap,'KnowledgeGap',dapper.schema)
        mechanism={'name':'Frozen factor','description':'Exact imported source label'}
        mechanism['id']=dapper.compute_id(mechanism,'Mechanism',dapper.schema)
        frozen={'id':'request-test','question_id':gap['id'],'document':{'knowledge_gaps':[gap],'mechanisms':[mechanism]},
            'composer':{'source_gap':{'source_id':'dismech:gap','source_revision':'f'*64},'eaggl_anchors':[{'reference':{'source_id':FACTOR},'origin':'manual'}],
                        'selected_kgs':[],'dismissed_source_ids':[]},'linked_dismech_context':[]}
        binding={'source_gap':{'object':gap,'attachments':[],'source_detail':{'raw':{'prompt':gap['text']}}},'dismech_import_id':'f'*64,
            'anchors':[{'cfde_node_id':FACTOR,'reference_generation_id':GEN,'eaggl_import_id':IMP,'model':'eaggl-capped-v1'}]}
        return dapper,frozen,binding

    def test_seed_has_no_eager_collection_and_writes_existing_empty_directory(self):
        dapper,frozen,binding=self.inputs()
        with tempfile.TemporaryDirectory() as path:
            built=prepare_research_seed(frozen,binding,dapper=dapper,project_root=ROOT,output=path)
            package=built.package
            self.assertEqual(package['retrieval_mode'],'progressive')
            self.assertFalse(package['readiness']['input_capture_complete'])
            self.assertEqual(package['pigean']['candidates'],{})
            self.assertEqual(package['pigean']['graph']['edges'],[])
            self.assertEqual(package['eligible_source_ids'],[])
            self.assertEqual(package['dapper_context']['knowledge_gaps'],frozen['document']['knowledge_gaps'])
            self.assertLess(len(built.files['evidence-package.json']),100_000)
            self.assertEqual(Path(path,'evidence-package.json').read_bytes(),built.files['evidence-package.json'])
            self.assertEqual(built.files,prepare_research_seed(frozen,binding,dapper=dapper,project_root=ROOT).files)
            validate_seed_shape(package,path)

    def test_seed_validator_rejects_false_completeness_and_mutated_sources(self):
        dapper,frozen,binding=self.inputs()
        built=prepare_research_seed(frozen,binding,dapper=dapper,project_root=ROOT)
        for mutate in (
            lambda value:value['readiness'].update(input_capture_complete=True),
            lambda value:value.update(reference_generation_id='active'),
            lambda value:value['source_artifacts']['frozen-gap'].update(path='../outside'),
            lambda value:value['source_artifacts']['frozen-gap'].update(sha256='0'*64),
            lambda value:value['authoring_kit'].update(kit_sha256='0'*64),
        ):
            with self.subTest(mutation=mutate):
                changed=deepcopy(built.package); mutate(changed)
                with self.assertRaises(EvidenceBuildError): validate_seed_shape(changed)
        with tempfile.TemporaryDirectory() as path:
            built.write(Path(path)/'package')
            source=Path(path)/'package'/built.package['source_artifacts']['frozen-gap']['path']
            source.write_bytes(b'changed')
            with self.assertRaises(EvidenceBuildError): validate_seed_shape(built.package,Path(path)/'package')

    def test_seed_dispatch_and_hosted_bundle_keep_exact_source_files(self):
        from reveal_backend.agent_execution import ExecutionRequest
        from reveal_backend.box_adapter import make_bundle
        from reveal_backend.evidence_budget import fit_input_budget
        from reveal_backend.evidence_files import INDEX_PATH, build_evidence_files
        from reveal_backend.dispatch_view import FILE_INPUT_FILENAME
        dapper,frozen,binding=self.inputs()
        with tempfile.TemporaryDirectory() as path:
            directory=Path(path)/'package'
            built=prepare_research_seed(frozen,binding,dapper=dapper,project_root=ROOT,output=directory)
            # The orchestration service adds only a scoped routing context, not
            # the credential. This fixture exercises the same hosted boundary.
            package=deepcopy(built.package)
            package['research_context']={'mcp_url':'https://reveal.example/mcp','local_work_id':'fixture-work','research_request_id':frozen['id']}
            target=directory/'evidence-package.json'; target.write_bytes(canonical_json(package))
            selected,value,measurement=fit_input_budget(target,'box',1)
            self.assertEqual(fit_input_budget(target,'box',1),(selected,value,measurement))
            request=ExecutionRequest('fixture-job',1,'research',selected,Path(path)/'output',selected_graphs=())
            with tarfile.open(fileobj=io.BytesIO(make_bundle(ROOT,request)),mode='r:gz') as archive:
                self.assertEqual(archive.extractfile('input/evidence-package.json').read(),target.read_bytes())
                self.assertIn('input/'+FILE_INPUT_FILENAME,archive.getnames())
                for artifact in package['source_artifacts'].values():
                    self.assertEqual(archive.extractfile('input/'+artifact['path']).read(),built.files[artifact['path']])
                sources={key:archive.extractfile('input/'+artifact['path']).read() for key,artifact in package['source_artifacts'].items()}
                reader=build_evidence_files(target.read_bytes(),source_bytes=sources)
                self.assertIn(INDEX_PATH,reader)
                self.assertEqual(set(reader), {INDEX_PATH})
                self.assertEqual(len(decode(reader[INDEX_PATH])['source_inventory']), len(sources))
            self.assertFalse(measurement['reduced'])

    def test_frozen_upload_bytes_and_extraction_are_preserved(self):
        dapper,frozen,binding=self.inputs(); original=b'Observed experiment text'
        extraction=canonical_json({'format':'reveal.upload-text/1','original_sha256':sha256(original),'segments':[{'locator':'line:1','text':original.decode()}]})
        frozen['user_inputs']={'research_direction':'Investigate','context':None,'hypotheses':None,'uploads':[{
            'id':'upload','filename':'paper.txt','media_type':'text/plain','sha256':sha256(original),
            'storage':{'sha256':sha256(original)},'extraction':{'storage':{'sha256':sha256(extraction)}}}]}
        blobs={sha256(original):original,sha256(extraction):extraction}
        built=prepare_research_seed(frozen,binding,dapper=dapper,project_root=ROOT,read_upload=lambda ref:blobs[ref['sha256']])
        upload=built.package['user_inputs']['uploads'][0]
        self.assertNotIn('content',upload)
        self.assertNotIn('storage',upload)
        self.assertNotIn('storage',upload['extraction'])
        source=built.package['source_artifacts'][upload['extraction_artifact_id']]
        self.assertEqual(decode(built.files[source['path']])['segments'][0]['text'],original.decode())
        self.assertEqual(built.package['eligible_source_ids'],[upload['extraction_file_id']])
        self.assertEqual(built.package['cfde_source_ids'],[])


if __name__=='__main__': unittest.main()
