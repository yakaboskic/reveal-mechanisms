"""Catalog over the flat reference release tables: loads, the TTL refresh that swaps releases without a
restart, Vector readiness and query embedding, and archived factor lookups.

One SQLite file stands in for the shared database: the scientific tables behind
runtime_config.mysql_connection (pymysql-style cursors) and the application records of a SQLite
Repository, which the poller reads. Vector and DAPPER are in-process fakes.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from reveal_backend import catalog as catalog_module
from reveal_backend.auth import Problem
from reveal_backend.catalog import RELEASE_TTL_SECONDS, Catalog
from reveal_backend.dismech_embeddings import TEMPLATES
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.reference_generation import KPN_MODEL, archive_id, factor_key, mechanism_node, public_id
from reveal_backend.repository import Repository, canonical, digest
from reveal_backend.vector_retrieval import UpstashFactorIndex

DISMECH_IMPORT = digest('dismech-import')
EAGGL_IMPORT = digest('eaggl-import')
R1, R2 = digest('release-1'), digest('release-2')
LEGACY = digest('legacy-generation')
EMBEDDING = {'EMBEDDING_MODEL': 'm', 'EMBEDDING_PROVIDER': 'p', 'EMBEDDING_SERVICE_URL': 'https://embed.invalid'}

FILES = {'kb/modules/B.yaml': 'b' * 64, 'kb/disorders/A.yaml': 'a' * 64}
MECHANISM = {'id': 'dismech:modules/B#/pathophysiology/0', 'name': 'Insulin resistance', 'description': 'Insulin signalling fails in muscle',
             'source_file': 'kb/modules/B.yaml', 'document_name': 'B'}
LINKED_GAP = {'id': 'dismech:disorders/A#discussion:gap', 'kind': 'KNOWLEDGE_GAP', 'status': 'OPEN', 'document_name': 'A',
              'source_file': 'kb/disorders/A.yaml', 'source_pointer': '/discussions/0',
              'raw': {'prompt': 'Does insulin resistance drive beta cell failure?', 'rationale': 'Open question.'}}
OPEN_GAP = {'id': 'dismech:disorders/A#discussion:open', 'kind': 'KNOWLEDGE_GAP', 'status': 'OPEN', 'document_name': 'A',
            'source_file': 'kb/disorders/A.yaml', 'source_pointer': '/discussions/1', 'raw': {'prompt': 'Which adipose pathways matter?'}}
ATTACHMENT = {'gap_id': LINKED_GAP['id'], 'attachment_index': 0, 'source_reference': 'B.yaml::pathophysiology#Insulin resistance',
              'resolution': 'resolved', 'target_id': MECHANISM['id'], 'target_kind': 'pathophysiology'}
# The context text rule: a mechanism's description (else name); an unattached gap's prompt.
CONTEXTS = {MECHANISM['description']: [1., 0.], OPEN_GAP['raw']['prompt']: [0., 1.]}

# EAGGL factor id, label, vector.
EAGGL = [('T2D::Factor1', 'insulin secretion', [1., 0.]), ('T2D::Factor2', 'beta cell stress', [.6, .8]),
         ('BMI::Factor1', 'adipocyte lipid storage', [0., 1.])]
TRAITS = {'KPN.TRAIT:0000398': ('T2D', 'Type 2 diabetes', 'metabolic', 'disease'),
          'KPN.TRAIT:0000012': ('BMI', 'Body mass index', 'anthropometric', 'quantitative')}
KPN_FACTORS = [('KPN.TRAIT:0000398', 'Factor1', 'T2D::Factor1'), ('KPN.TRAIT:0000398', 'Factor2', 'T2D::Factor2'),
               ('KPN.TRAIT:0000012', 'Factor1', 'BMI::Factor1')]
T2D1, T2D2, BMI1 = (public_id(trait, factor) for trait, factor, _ in KPN_FACTORS)
LEGACY_ID = 'factor:portal:T2D:cfde-inc-v2:Factor1'

SCHEMA = '''
CREATE TABLE dismech_imports(import_id TEXT, source_commit TEXT, source_files TEXT, status TEXT);
CREATE TABLE dismech_documents(import_id TEXT, source_file TEXT, source_sha256 TEXT);
CREATE TABLE dismech_discussions(import_id TEXT, is_gap INTEGER, payload TEXT);
CREATE TABLE dismech_mechanisms(import_id TEXT, id_sha256 TEXT, payload TEXT);
CREATE TABLE dismech_gap_attachments(import_id TEXT, target_mechanism_sha256 TEXT, attachment_index INTEGER, payload TEXT);
CREATE TABLE reveal_ref_release(release_id TEXT PRIMARY KEY, published_at TEXT, manifest TEXT);
CREATE TABLE reveal_ref_traits(kpn_trait_id TEXT PRIMARY KEY, legacy_phenotype_id TEXT, phenotype_name TEXT, gwas_source_category TEXT,
  trait_group TEXT, legacy_trait_group TEXT, trait_type TEXT, description TEXT, is_dichotomous INTEGER, is_complex INTEGER, n_factors INTEGER, metadata TEXT);
CREATE TABLE reveal_ref_factors(factor_key TEXT PRIMARY KEY, public_id TEXT, eaggl_factor_id TEXT, kpn_trait_id TEXT, factor_number INTEGER,
  label TEXT, input_sha256 TEXT, source_revision TEXT, metadata TEXT);
CREATE TABLE archived_reference_factors(archive_id TEXT PRIMARY KEY, generation_id TEXT, source_id TEXT, snapshot TEXT, captured_at TEXT);
'''


class MissingTable(Exception):
    """pymysql reports an absent table as ProgrammingError(1146, message)."""


class Cursor:
    def __init__(self, database, connection): self.database, self.cursor, self.rowcount = database, connection.cursor(), 0
    def __enter__(self): return self
    def __exit__(self, *exc): self.cursor.close()
    def execute(self, sql, params=()):
        self.database.statements.append(sql)
        if self.database.block and self.database.block[0] in sql:
            _, entered, release = self.database.block
            entered.set(); release.wait(10)
        try: self.cursor.execute(sql.replace('%s', '?'), tuple(params))
        except sqlite3.OperationalError as error:
            if 'no such table' in str(error): raise MissingTable(1146, str(error)) from error
            raise
        self.rowcount = self.cursor.rowcount
    def fetchall(self): return self.cursor.fetchall()
    def fetchone(self): return self.cursor.fetchone()


class Connection:
    def __init__(self, database): self.database, self.connection = database, sqlite3.connect(database.path)
    def cursor(self): return Cursor(self.database, self.connection)
    def commit(self): self.connection.commit()
    def close(self): self.connection.close()


class Database:
    """SQLite stand-in for the shared database behind runtime_config.mysql_connection."""
    def __init__(self, path): self.path, self.opened, self.statements, self.block = path, 0, [], None
    def __call__(self):
        self.opened += 1
        return Connection(self)
    def run(self, sql, *rows):
        with sqlite3.connect(self.path) as connection:
            if rows: connection.executemany(sql, rows)
            else: connection.executescript(sql)


class Runtime:
    """DAPPER stand-in: content-addressed ids without the schema snapshot."""
    schema = 'test-schema'
    def compute_id(self, node, kind, schema): return f'dapper:{kind}.' + digest([kind, node])[:32]
    def resolver(self, prefixes): raise AssertionError('Fixture gaps name no disease term')
    def file(self, name, data, mime):
        node = {'filename': name, 'mime_type': mime, 'sha256': sha256(data), 'size_in_bytes': len(data)}
        node['id'] = self.compute_id(node, 'File', self.schema)
        return node


class Provider:
    """Upstash Vector stand-in (fetch/query/info) for the real UpstashFactorIndex."""
    def __init__(self): self.rows, self.dimension, self.fetches = {}, 2, []
    def upsert(self, vectors, namespace): self.rows.setdefault(namespace, {}).update({row['id']: deepcopy(row) for row in vectors})
    def fetch(self, ids, namespace, **kwargs):
        self.fetches.append((namespace, list(ids)))
        return [deepcopy(self.rows.get(namespace, {}).get(identity)) for identity in ids]
    def query_many(self, queries, namespace):
        result = []
        for query in queries:
            q = np.asarray(query['vector']) / np.linalg.norm(query['vector'])
            rows = [dict(row, score=float((1 + (np.asarray(row['vector']) / np.linalg.norm(row['vector'])) @ q) / 2))
                    for row in self.rows.get(namespace, {}).values()]
            result.append(sorted(rows, key=lambda row: (-row['score'], row['id']))[:query['top_k']])
        return result
    def info(self):
        return SimpleNamespace(dimension=self.dimension, similarity_function='COSINE', namespaces={
            name: SimpleNamespace(vector_count=len(rows), pending_vector_count=0) for name, rows in self.rows.items()})


class CountingRepository:
    """The prefix's application repository, counting the poller's release reads."""
    def __init__(self, repo): self.repo, self.reads, self.fail = repo, 0, None
    def read_transaction(self, **kwargs):
        self.reads += 1
        if self.fail: raise self.fail
        return self.repo.read_transaction(**kwargs)


class Clock:
    def __init__(self): self.now = 1000.
    def __call__(self): return self.now


def kpn_metadata(release, trait, label):
    return {'label': label, 'gene_set_score': 1.5, 'kpn': {'phenotype_name': TRAITS[trait][1]}, 'lap': {'release': release}}


def revision(release, key): return digest(['revision', release, key])


def manifest(**embedding):
    return {'format': 'reveal.reference-release/1', 'eaggl_import_id': EAGGL_IMPORT,
            'embedding': {'model': 'm', 'model_revision': 'r', 'provider': 'p', 'dimensions': 2, **embedding}}


class CatalogReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'shared.sqlite')
        self.db = Database(self.path)
        self.db.run(SCHEMA)
        self.seed_sources()
        repo = Repository(self.path, table_prefix='reveal'); repo.migrate()
        self.repo, self.clock, self.provider, self.runtimes = CountingRepository(repo), Clock(), Provider(), []
        self.provider.upsert([{'id': hashlib.sha256(text.encode()).hexdigest(), 'vector': vector, 'metadata': {'kind': 'context'}}
                              for text, vector in CONTEXTS.items()], namespace='local-contexts')
        self.publish(R1)
        def runtime(directory):
            self.runtimes.append(Runtime()); return self.runtimes[-1]
        provider = self.provider
        class Index(UpstashFactorIndex):
            def __init__(self, factor_ids, **kwargs): super().__init__(factor_ids, **{**kwargs, 'client': kwargs.get('client') or provider})
        environment = patch.dict(os.environ, {'REVEAL_VECTOR_ENVIRONMENT': 'local', **EMBEDDING})
        environment.start(); self.addCleanup(environment.stop)
        for name in ('REVEAL_APPLICATION_TABLE_PREFIX', 'REVEAL_DISMECH_IMPORT_ID'): os.environ.pop(name, None)
        for target, value in [('mysql_connection', self.db), ('Repository', lambda: self.repo), ('DapperRuntime', runtime),
                              ('UpstashFactorIndex', Index), ('monotonic', self.clock)]:
            context = patch.object(catalog_module, target, value); context.start(); self.addCleanup(context.stop)

    # Fixture data -------------------------------------------------------------------------

    def seed_sources(self):
        files = {'source': [{'path': path, 'sha256': checksum} for path, checksum in FILES.items()], 'gaps': []}
        self.db.run('INSERT INTO dismech_imports VALUES (?,?,?,?)', (DISMECH_IMPORT, 'c' * 40, json.dumps(files), 'complete'))
        self.db.run('INSERT INTO dismech_documents VALUES (?,?,?)', *[(DISMECH_IMPORT, path, checksum) for path, checksum in FILES.items()])
        self.db.run('INSERT INTO dismech_discussions VALUES (?,?,?)', *[(DISMECH_IMPORT, 1, json.dumps(row)) for row in (LINKED_GAP, OPEN_GAP)])
        self.db.run('INSERT INTO dismech_mechanisms VALUES (?,?,?)', (DISMECH_IMPORT, digest(MECHANISM['id']), json.dumps(MECHANISM)))
        self.db.run('INSERT INTO dismech_gap_attachments VALUES (?,?,?,?)', (DISMECH_IMPORT, digest(MECHANISM['id']), 0, json.dumps(ATTACHMENT)))

    def label(self, eaggl, labels=None):
        return (labels or {}).get(eaggl) or next(label for factor, label, _ in EAGGL if factor == eaggl)

    def publish(self, release, factors=KPN_FACTORS, *, labels=None, release_manifest=None):
        """Replace the flat release tables and upsert the factor vectors, as the publisher does."""
        with sqlite3.connect(self.path) as db:
            for table in ('reveal_ref_release', 'reveal_ref_factors', 'reveal_ref_traits'): db.execute('DELETE FROM ' + table)
            db.execute('INSERT INTO reveal_ref_release VALUES (?,?,?)', (release, '2026-10-05 12:00:00', canonical(release_manifest or manifest())))
            db.executemany('INSERT INTO reveal_ref_traits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [
                (trait, legacy, name, 'KPN', group, None, kind, None, None, None, sum(row[0] == trait for row in factors), canonical({}))
                for trait, (legacy, name, group, kind) in TRAITS.items() if any(row[0] == trait for row in factors)])
            db.executemany('INSERT INTO reveal_ref_factors VALUES (?,?,?,?,?,?,?,?,?)', [
                (factor_key(trait, factor), public_id(trait, factor), eaggl, trait, int(factor[6:]), self.label(eaggl, labels),
                 digest(self.label(eaggl, labels)), revision(release, factor_key(trait, factor)), json.dumps(kpn_metadata(release, trait, self.label(eaggl, labels))))
                for trait, factor, eaggl in factors])
        self.provider.upsert([{'id': factor_key(trait, factor), 'vector': next(vector for name, _, vector in EAGGL if name == eaggl),
                               'metadata': {'kind': 'factor', 'public_id': public_id(trait, factor), 'vector_sha256': 'v-' + eaggl}}
                              for trait, factor, eaggl in factors], namespace='local-factors')

    def load(self):
        catalog = Catalog(); catalog.load()
        return catalog

    def queried(self, table): return any(table in statement for statement in self.db.statements)

    def assertProblem(self, status, code, function, *args):
        with self.assertRaises(Problem) as failure: function(*args)
        self.assertEqual((failure.exception.status, failure.exception.code), (status, code))
        return failure.exception

    # Loads ----------------------------------------------------------------------------------

    def test_release_serves_every_factor_with_records_and_bindings(self):
        catalog = self.load()
        self.assertEqual((catalog.release_id, catalog.reference_generation_id, catalog.model), (R1, R1, KPN_MODEL))
        self.assertEqual((catalog.mapping_run, catalog.geneset_import, catalog.embedding_run, catalog.eaggl_import), (R1, R1, R1, EAGGL_IMPORT))
        self.assertEqual(catalog.release, {'release_id': R1, 'published_at': '2026-10-05 12:00:00', 'manifest': manifest()})
        self.assertTrue(self.queried('FROM reveal_ref_release') and self.queried('reveal_ref_factors f JOIN reveal_ref_traits t'))
        for retired in ('reference_generations', 'kpn_traits', 'reference_factors', 'eaggl_cfde', 'eaggl_embedding_runs', 'dismech_embedding'):
            self.assertFalse(self.queried(retired), retired)
        self.assertEqual(set(catalog.factors), {T2D1, T2D2, BMI1})
        self.assertEqual(set(catalog.factor_legacy), {factor_key(trait, factor) for trait, factor, _ in KPN_FACTORS})
        trait, runtime = 'KPN.TRAIT:0000398', self.runtimes[0]
        key, meta = factor_key(trait, 'Factor2'), kpn_metadata(R1, trait, 'beta cell stress')
        node = mechanism_node(T2D2, 'Type 2 diabetes', trait, 'Factor2', 'beta cell stress')
        node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
        self.assertEqual(catalog.factors[T2D2], {'source': 'eaggl', 'source_id': T2D2, 'source_revision': revision(R1, key),
            'object_class': 'Mechanism', 'object': node,
            'cfde_anchor': {'node_id': T2D2, 'node_type': 'factor', 'label': 'beta cell stress', 'subtitle': 'Type 2 diabetes (Factor2)'},
            'model': KPN_MODEL, 'reference_generation_id': R1,
            'kpn_trait': {'id': trait, 'name': 'Type 2 diabetes', 'legacy_phenotype_id': 'T2D', 'trait_group': 'metabolic', 'trait_type': 'disease'},
            'catalog_file': runtime.file('cfde-factor.json', canonical_json(meta), 'application/json')})
        self.assertIs(catalog.factor_legacy[key], catalog.factors[T2D2])
        # Bindings keep their field names; every run field names the release.
        self.assertEqual(catalog.bindings[T2D2], {'eaggl_factor_id': 'T2D::Factor2', 'factor_key': key, 'kpn_trait_id': trait,
            'eaggl_import_id': EAGGL_IMPORT, 'embedding_run_id': R1, 'mapping_run_id': R1, 'gene_set_import_id': R1,
            'reference_generation_id': R1, 'model': KPN_MODEL, 'cfde_node_id': T2D2, 'cfde_payload': meta})
        self.assertEqual(catalog.provenance('q', 'hybrid', True), {'query': 'q', 'mode': 'hybrid', 'corpus_snapshot': R1, 'embedding_model': 'm',
            'embedding_revision': R1, 'template_version': 'eaggl-label-v1', 'score_aggregation': 'maximum_per_context'})
        self.assertEqual(catalog.provenance('q', 'lexical')['corpus_snapshot'], DISMECH_IMPORT)
        self.assertEqual((len(catalog.gaps), list(catalog.mechanisms)), (2, [MECHANISM['id']]))
        self.assertEqual((catalog.index.factor_namespace, catalog.index.context_namespace, catalog.index.dimensions), ('local-factors', 'local-contexts', 2))

    def test_a_manifest_without_an_eaggl_import_leaves_bindings_without_one(self):
        self.publish(R1, release_manifest={'embedding': {'dimensions': 2}})
        catalog = self.load()
        self.assertIsNone(catalog.bindings[T2D1]['eaggl_import_id'])
        self.assertEqual(catalog.bindings[T2D1]['mapping_run_id'], R1)

    def test_lexical_and_fuzzy_search_match_label_public_id_and_trait(self):
        catalog = self.load()
        search = lambda query, mode='lexical': [row['record']['source_id'] for row in catalog.search_factors(query, mode)]
        self.assertEqual(search('diabetes'), [T2D1, T2D2])
        self.assertEqual(search('T2D'), [T2D1, T2D2])
        self.assertEqual(search('0000012'), [BMI1])
        self.assertEqual(search('adipocyte'), [BMI1])
        self.assertEqual(search('diabetis', 'fuzzy'), [T2D1, T2D2])

    def test_semantic_retrieval_and_stored_context_suggestions(self):
        catalog = self.load()
        rows = catalog.search_factors('insulin', 'semantic', 2, query_vector=np.array([1., 0.]))
        self.assertEqual([row['record']['source_id'] for row in rows], [T2D1, T2D2])
        retrieval = rows[0]['retrieval']
        self.assertEqual((retrieval['release_id'], retrieval['mapping_run_id'], retrieval['embedding_run_id']), (R1, R1, R1))
        self.assertEqual(retrieval['aliases'], [{'id': factor_key('KPN.TRAIT:0000398', 'Factor1'), 'vector_sha256': 'v-T2D::Factor1'}])
        gap = catalog.by_source[OPEN_GAP['id']]['object']  # An unattached gap suggests from its prompt's stored vector.
        with patch.object(catalog_module, 'get_embeddings', side_effect=AssertionError('Stored contexts need no embedding call')):
            suggested = catalog.suggest_factors([(gap['id'], gap['text'])], 'semantic', 1, (), precomputed=True)
            linked = catalog.suggest_factors([(MECHANISM['id'], MECHANISM['description'])], 'hybrid', 2, (), precomputed=True)
        self.assertEqual([row['record']['source_id'] for row in suggested], [BMI1])
        self.assertEqual(suggested[0]['retrieval']['query_inputs'], [{'context_id': gap['id'], 'input_sha256': sha256(gap['text'].encode()),
            'source_id': OPEN_GAP['id'], 'source_kind': 'knowledge_gap', 'source_revision': FILES[OPEN_GAP['source_file']],
            'template': 'dismech-gap-text-v1'}])
        self.assertEqual(linked[0]['record']['source_id'], T2D1)
        self.assertEqual(linked[0]['context_similarities'], {MECHANISM['id']: 1.0})
        self.assertEqual(catalog.context_embedding_provenance([(MECHANISM['id'], MECHANISM['description'])]), {
            'dismech_embedding_run_id': R1, 'dismech_import_id': DISMECH_IMPORT, 'context_embedding_templates': TEMPLATES,
            'context_embedding_inputs': [{'source_id': MECHANISM['id'], 'source_kind': 'mechanism', 'source_revision': FILES[MECHANISM['source_file']],
                                          'template': 'dismech-description-v1', 'input_sha256': sha256(MECHANISM['description'].encode())}]})
        self.assertProblem(503, 'DISMECH_EMBEDDINGS_NOT_READY', catalog.context_embedding_provenance, [('unknown', 'text')])

    def test_context_misses_and_queries_embed_live_with_runtime_configuration(self):
        catalog = self.load()
        calls = []
        def embed(texts, **kwargs):
            calls.append((texts, kwargs)); return np.asarray([[0., 3.] for _ in texts])
        self.provider.rows['local-contexts'].clear()  # No stored vector for the prompt: embedded live.
        gap = catalog.by_source[OPEN_GAP['id']]['object']
        with patch.object(catalog_module, 'get_embeddings', side_effect=embed):
            for _ in range(2):
                suggested = catalog.suggest_factors([(gap['id'], gap['text'])], 'semantic', 1, (), precomputed=True)
                self.assertEqual([row['record']['source_id'] for row in suggested], [BMI1])
            rows = catalog.search_factors('adipose storage', 'semantic', 1)
        self.assertEqual([row['record']['source_id'] for row in rows], [BMI1])
        self.assertEqual(calls, [([gap['text']], {'model': 'm', 'provider': 'p', 'service_url': 'https://embed.invalid',
                                                  'max_workers': 1, 'max_retries': 2, 'timeout': 30}),
                                 (['adipose storage'], {'model': 'm', 'provider': 'p', 'service_url': 'https://embed.invalid',
                                                        'max_workers': 1, 'max_retries': 2, 'timeout': 30})])
        with patch.object(catalog_module, 'get_embeddings', return_value=[[0., 0., 1.]]):
            self.assertProblem(503, 'EMBEDDING_UNAVAILABLE', catalog.suggest_factors, [('mechanism_subquery', 'novel')], 'semantic', 1, ())

    def test_validate_composer_accepts_only_served_factors(self):
        catalog = self.load()
        stale = {'source_gap': None, 'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': LEGACY_ID,
                 'source_revision': 'f' * 64, 'dapper_id': 'dapper:Mechanism.' + '0' * 32}}]}
        self.assertProblem(409, 'SOURCE_REVISION_CHANGED', catalog.validate_composer, stale)
        record = catalog.factors[T2D1]
        fresh = {'source_gap': None, 'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': T2D1,
                 'source_revision': record['source_revision'], 'dapper_id': record['object']['id']}}]}
        self.assertIsNone(catalog.validate_composer(fresh))

    def test_missing_release_or_reference_tables_are_503_source_not_ready(self):
        self.db.run('DELETE FROM reveal_ref_release')
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)
        self.publish(R1)
        self.db.run('DROP TABLE reveal_ref_release')
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)
        self.db.run('CREATE TABLE reveal_ref_release(release_id TEXT PRIMARY KEY, published_at TEXT, manifest TEXT)')
        self.db.run('INSERT INTO reveal_ref_release VALUES (?,?,?)', ('not-a-release', '2026-10-05', canonical(manifest())))
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)
        self.publish(R1)
        self.db.run('DELETE FROM reveal_ref_factors')
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)
        self.publish(R1)
        self.db.run(f"UPDATE reveal_ref_factors SET public_id='{public_id('KPN.TRAIT:0000012', 'Factor9')}' WHERE factor_key='KPN.TRAIT:0000398::Factor1'")
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)
        self.publish(R1)
        self.db.run('DROP TABLE reveal_ref_factors')
        self.assertProblem(503, 'SOURCE_NOT_READY', self.load)

    def test_vector_readiness_and_release_embedding_dimensions_are_required(self):
        self.publish(R1, release_manifest={'embedding': {'model': 'm'}})
        self.assertProblem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', self.load)
        self.publish(R1, release_manifest=manifest(dimensions=3))
        self.assertIn('dimensions', self.assertProblem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', self.load).detail)
        self.publish(R1)
        factors = self.provider.rows.pop('local-factors')
        self.assertIn('empty', self.assertProblem(503, 'SEMANTIC_SEARCH_UNAVAILABLE', self.load).detail)
        # Readiness never counts vectors: a namespace with ids the release does not serve is ready.
        self.provider.rows['local-factors'] = {**factors, 'KPN.TRAIT:0000777::Factor1': {'id': 'KPN.TRAIT:0000777::Factor1', 'vector': [1., 0.]}}
        catalog = self.load()
        self.assertEqual([row['record']['source_id'] for row in catalog.search_factors('x', 'semantic', 3, query_vector=np.array([1., 0.]))],
                         [T2D1, T2D2, BMI1])

    def test_a_different_configured_embedding_model_is_logged(self):
        with patch.dict(os.environ, {'EMBEDDING_MODEL': 'another-model'}), self.assertLogs(catalog_module.LOGGER, 'WARNING'):
            self.load()

    # Release changes ------------------------------------------------------------------------

    def test_ttl_refresh_swaps_releases_without_a_restart(self):
        catalog = self.load()
        self.assertEqual((catalog.release_id, self.repo.reads), (R1, 0))
        first_factors = catalog.factors
        labels = {'T2D::Factor1': 'insulin secretion (refit)'}
        self.publish(R2, labels=labels)
        self.clock.now += RELEASE_TTL_SECONDS - 1
        self.assertFalse(catalog.refresh_if_changed())
        self.assertEqual((catalog.release_id, self.repo.reads), (R1, 0))  # No release read inside the TTL.
        self.clock.now += 2
        catalog.load(); catalog.gap(LINKED_GAP['id'])
        self.assertEqual((catalog.release_id, self.repo.reads), (R1, 0))  # Requests never check: the poller does.
        self.assertTrue(catalog.refresh_if_changed())
        self.assertEqual((catalog.release_id, self.repo.reads), (R2, 1))
        self.assertEqual((catalog.factors[T2D1]['reference_generation_id'], catalog.factors[T2D1]['cfde_anchor']['label']), (R2, 'insulin secretion (refit)'))
        self.assertEqual({catalog.bindings[T2D1][key] for key in ('embedding_run_id', 'mapping_run_id', 'gene_set_import_id', 'reference_generation_id')}, {R2})
        self.assertEqual(first_factors[T2D1]['reference_generation_id'], R1)  # Readers of the old state kept a consistent view.
        self.assertEqual(catalog.index.release_id, R2)
        self.assertEqual(len(self.runtimes), 1)  # The DAPPER runtime is process-wide.
        self.assertTrue(catalog.loaded)
        opened = self.db.opened
        self.clock.now += RELEASE_TTL_SECONDS
        self.assertFalse(catalog.refresh_if_changed())  # The same release is never reloaded.
        self.assertEqual((self.db.opened, self.repo.reads), (opened, 2))

    def test_a_release_that_drops_a_factor_stops_serving_it(self):
        catalog = self.load()
        self.publish(R2, KPN_FACTORS[:2])
        self.clock.now += RELEASE_TTL_SECONDS
        self.assertTrue(catalog.refresh_if_changed())
        self.assertEqual(set(catalog.factors), {T2D1, T2D2})
        # The namespace still holds the dropped factor's vector: it is skipped, never served.
        self.assertEqual([row['record']['source_id'] for row in catalog.search_factors('x', 'semantic', 3, query_vector=np.array([0., 1.]))],
                         [T2D2, T2D1])

    def test_requests_keep_serving_the_loaded_release_while_a_refresh_loads(self):
        catalog = self.load()
        self.publish(R2, labels={'T2D::Factor1': 'relabelled'})
        entered, release = threading.Event(), threading.Event()
        self.db.block = ('reveal_ref_factors', entered, release)
        self.clock.now += RELEASE_TTL_SECONDS
        refresher = threading.Thread(target=catalog.refresh_if_changed); refresher.start()
        try:
            self.assertTrue(entered.wait(10))
            self.clock.now += RELEASE_TTL_SECONDS  # Even past the TTL, other requests never wait for the reload.
            catalog.load(); catalog.search_factors('insulin', 'lexical')
            self.assertEqual(catalog.release_id, R1)
            self.assertEqual(catalog.factors[T2D1]['cfde_anchor']['label'], 'insulin secretion')
        finally:
            release.set(); refresher.join(10)
        self.assertFalse(refresher.is_alive())
        self.assertEqual((catalog.release_id, catalog.loaded, catalog.factors[T2D1]['cfde_anchor']['label']), (R2, True, 'relabelled'))

    def test_failed_checks_keep_serving_and_failed_reloads_fail_closed(self):
        catalog = self.load()
        self.repo.fail = RuntimeError('records unavailable')
        self.clock.now += RELEASE_TTL_SECONDS
        with self.assertLogs(catalog_module.LOGGER, 'WARNING'): self.assertFalse(catalog.refresh_if_changed())
        self.assertTrue(catalog.loaded); self.assertEqual(catalog.release_id, R1)
        self.repo.fail = None
        self.publish(R2, release_manifest=manifest(dimensions=3))  # The new release cannot load against this Vector index.
        self.clock.now += RELEASE_TTL_SECONDS
        with self.assertRaises(Problem) as failure: catalog.refresh_if_changed()
        self.assertEqual(failure.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')
        self.assertFalse(catalog.loaded)
        self.assertEqual(catalog.release_id, R1)  # The failed reload left the old state untouched.
        with self.assertRaises(Problem): catalog.load()  # Requests reload inline, fail-closed.
        self.publish(R2)
        catalog.load()
        self.assertEqual((catalog.release_id, catalog.loaded), (R2, True))

    def test_a_loaded_catalog_does_no_io_in_load_even_past_the_ttl(self):
        # Callers such as draft saves and job submits run catalog.load() inside a pooled write
        # transaction: once loaded, load() must never read the records or the scientific tables.
        catalog = self.load()
        reads, opened, statements = self.repo.reads, self.db.opened, len(self.db.statements)
        self.publish(R2)
        for _ in range(3):
            self.clock.now += RELEASE_TTL_SECONDS * 2
            catalog.load(); catalog.selected(catalog.by_source[LINKED_GAP['id']]['source'] | {'id': LINKED_GAP['id']})
            catalog.search_factors('insulin', 'lexical')
        self.assertEqual((self.repo.reads, self.db.opened, len(self.db.statements)), (reads, opened, statements))
        self.assertEqual(catalog.release_id, R1)
        self.assertIsNone(catalog.poller)  # Catalog() without poll_seconds starts no thread.

    def test_background_poller_switches_releases_off_the_request_path(self):
        catalog = Catalog(poll_seconds=0.01); self.addCleanup(catalog.stop_poller, 10)
        catalog.load()
        thread = catalog.poller[1]
        self.assertTrue(thread.is_alive())
        self.assertEqual(thread.name, 'reference-release-poll')
        self.publish(R2)
        self.clock.now += RELEASE_TTL_SECONDS
        deadline = time.monotonic() + 10
        while catalog.release_id != R2 and time.monotonic() < deadline: time.sleep(0.01)
        self.assertEqual((catalog.release_id, catalog.loaded), (R2, True))
        catalog.load()
        self.assertIs(catalog.poller[1], thread)  # One poller per process, kept across reloads.
        catalog.stop_poller(10)
        self.assertFalse(thread.is_alive())

    def test_a_failed_cold_load_leaves_no_partial_state(self):
        self.db.run('DELETE FROM reveal_ref_release')
        catalog = Catalog()
        with self.assertRaises(Problem): catalog.load()
        self.assertEqual((catalog.loaded, catalog.release_id, catalog.reference_generation_id), (False, None, None))
        self.assertFalse(hasattr(catalog, 'factors'))
        self.publish(R1)
        catalog.load()
        self.assertEqual((catalog.release_id, catalog.loaded), (R1, True))

    # Lookups ------------------------------------------------------------------------------

    def test_archived_reference_factor_lookups_need_no_release(self):
        snapshots = [(LEGACY, LEGACY_ID, '2026-09-30T10:00:00Z'), (R1, T2D1, '2026-10-01T10:00:00Z'), (R2, T2D1, '2026-11-01T10:00:00Z')]
        self.db.run('INSERT INTO archived_reference_factors VALUES (?,?,?,?,?)', *[
            (archive_id(generation, source), generation, source, json.dumps({'format': 'reveal.archived-reference-factor/1',
             'generation_id': generation, 'source_id': source, 'label': 'insulin secretion', 'top_genes': [{'symbol': 'INS', 'loading': 0.5}]}), at)
            for generation, source, at in snapshots])
        self.db.run('DROP TABLE reveal_ref_release')  # Lookups never read the release or load the catalog.
        catalog = Catalog()
        identity = archive_id(LEGACY, LEGACY_ID)
        found = catalog.archived_reference_factor(identity)
        self.assertEqual(found, {'format': 'reveal.archived-reference-factor/1', 'generation_id': LEGACY, 'source_id': LEGACY_ID,
            'label': 'insulin secretion', 'top_genes': [{'symbol': 'INS', 'loading': 0.5}], 'archive_id': identity,
            'captured_at': '2026-09-30T10:00:00Z'})
        opened = self.db.opened
        found['label'] = 'changed by a caller'
        self.assertEqual(catalog.archived_reference_factor(identity)['label'], 'insulin secretion')
        self.assertEqual(self.db.opened, opened)  # Immutable rows are served from the process cache.
        self.assertIsNone(catalog.archived_reference_factor(digest('unknown')))
        self.assertIsNone(catalog.archived_reference_factor("x' OR '1'='1"))
        self.assertEqual(catalog.archived_for_source(T2D1)['generation_id'], R2)  # Latest capture.
        self.assertEqual(catalog.archived_for_source(LEGACY_ID)['archive_id'], identity)
        self.assertIsNone(catalog.archived_for_source('factor:portal:X:cfde-inc-v2:Factor1'))
        self.assertIsNone(catalog.archived_for_source(''))
        self.assertFalse(self.queried('dismech_') or self.queried('reveal_ref_'))
        self.db.run('DROP TABLE archived_reference_factors')
        self.assertIsNone(Catalog().archived_reference_factor(identity))

    def test_archived_reference_factor_misses_are_cached_and_an_empty_table_rarely_connects(self):
        catalog, unknown = Catalog(), digest('unknown')
        self.db.run("INSERT INTO archived_reference_factors VALUES ('%s','%s','x','{}','2026-09-30T10:00:00Z')" % (digest('other'), LEGACY))
        self.assertIsNone(catalog.archived_reference_factor(unknown))
        opened = self.db.opened
        for _ in range(5): self.assertIsNone(catalog.archived_reference_factor(unknown))
        self.assertEqual(self.db.opened, opened)  # A repeated miss is served from the cache within the TTL.
        self.db.run("INSERT INTO archived_reference_factors VALUES ('%s','%s','y','{\"label\": \"late\"}','2026-09-30T11:00:00Z')" % (unknown, LEGACY))
        self.clock.now += RELEASE_TTL_SECONDS
        self.assertEqual(catalog.archived_reference_factor(unknown)['label'], 'late')  # A later capture is seen after the TTL.
        # An empty (or missing) table costs at most one connection per TTL, whatever the ids.
        self.db.run('DELETE FROM archived_reference_factors')
        catalog = Catalog()
        self.assertIsNone(catalog.archived_reference_factor(digest('first')))
        opened = self.db.opened
        for index in range(20): self.assertIsNone(catalog.archived_reference_factor(digest(['random', index])))
        self.assertIsNone(catalog.archived_for_source(LEGACY_ID))
        self.assertEqual(self.db.opened, opened)
        self.clock.now += RELEASE_TTL_SECONDS
        self.db.run('DROP TABLE archived_reference_factors')
        self.assertIsNone(catalog.archived_reference_factor(digest('second')))
        self.assertEqual(self.db.opened, opened + 1)
        for index in range(20): self.assertIsNone(catalog.archived_reference_factor(digest(['again', index])))
        self.assertEqual(self.db.opened, opened + 1)


if __name__ == '__main__':
    unittest.main()
