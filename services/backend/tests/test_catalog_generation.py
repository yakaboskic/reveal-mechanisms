"""Catalog reference generations (docs/reference-reload.md §4, §8): legacy and KPN loads,
the TTL refresh that switches generations without a restart, and archived factor lookups.

The shared scientific database is an SQLite stand-in with pymysql-style cursors; the
application records are a SQLite Repository; Vector and DAPPER are in-process fakes.
"""
from copy import deepcopy
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
from reveal_backend.catalog import GENERATION_TTL_SECONDS, Catalog
from reveal_backend.dismech_embeddings import TEMPLATES, context_input
from reveal_backend.eaggl_embeddings import FactorSearchIndex, text_hash, vector_hash
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.reference_generation import (KPN_KIND, KPN_MODEL, LEGACY_KIND, LEGACY_MODEL, archive_id, factor_key,
    legacy_generation_id, mechanism_node, public_id, write_active)
from reveal_backend.repository import Repository, canonical, digest
from reveal_backend.vector_retrieval import POLICY_VERSION, UpstashFactorIndex, embedding_space, metadata, vector_checksum

DISMECH_IMPORT, CONTEXT_RUN = digest('dismech-import'), digest('context-run')
EAGGL_IMPORT, EMBEDDING_RUN = digest('eaggl-import'), digest('eaggl-embedding-run')
MAPPING_RUN, GENE_SET_IMPORT = digest('mapping-run'), digest('gene-set-import')
LEGACY = legacy_generation_id(MAPPING_RUN)
KPN1, KPN2 = digest('kpn-generation-1'), digest('kpn-generation-2')
PINS = ('REVEAL_REFERENCE_GENERATION_ID', 'REVEAL_MAPPING_RUN_ID', 'REVEAL_EMBEDDING_RUN_ID', 'REVEAL_DISMECH_IMPORT_ID',
        'REVEAL_DISMECH_EMBEDDING_RUN_ID')

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
CONTEXTS = [(context_input(MECHANISM['id'], 'mechanism', FILES[MECHANISM['source_file']], MECHANISM['description']), [1., 0.]),
            (context_input(OPEN_GAP['id'], 'knowledge_gap', FILES[OPEN_GAP['source_file']], OPEN_GAP['raw']['prompt']), [0., 1.])]

# EAGGL factor id, label, vector; the legacy mapping run links factors 0 and 2 to CFDE portal factors.
EAGGL = [('T2D::Factor1', 'insulin secretion', [1., 0.]), ('T2D::Factor2', 'beta cell stress', [.6, .8]),
         ('BMI::Factor1', 'adipocyte lipid storage', [0., 1.])]
LEGACY_LINKS = {0: 'factor:portal:T2D:cfde-inc-v2:Factor1', 2: 'factor:portal:BMI:cfde-inc-v2:Factor1'}
TRAITS = {'KPN.TRAIT:0000398': ('T2D', 'Type 2 diabetes', 'metabolic', 'disease'),
          'KPN.TRAIT:0000012': ('BMI', 'Body mass index', 'anthropometric', 'quantitative')}
KPN_FACTORS = [('KPN.TRAIT:0000398', 'Factor1', 'T2D::Factor1'), ('KPN.TRAIT:0000398', 'Factor2', 'T2D::Factor2'),
               ('KPN.TRAIT:0000012', 'Factor1', 'BMI::Factor1')]
RUN = {'run_id': EMBEDDING_RUN, 'dimensions': 2, 'calibration': [],
       'config': {'model': 'm', 'provider': 'p', 'service_url': 'https://embed.invalid', 'import_id': EAGGL_IMPORT, 'template': 't'}}
CONTEXT_CONFIG = {'templates': TEMPLATES}

SCHEMA = '''
CREATE TABLE dismech_imports(import_id TEXT, source_commit TEXT, source_files TEXT, status TEXT);
CREATE TABLE dismech_documents(import_id TEXT, source_file TEXT, source_sha256 TEXT);
CREATE TABLE dismech_discussions(import_id TEXT, is_gap INTEGER, payload TEXT);
CREATE TABLE dismech_mechanisms(import_id TEXT, id_sha256 TEXT, payload TEXT);
CREATE TABLE dismech_gap_attachments(import_id TEXT, target_mechanism_sha256 TEXT, attachment_index INTEGER, payload TEXT);
CREATE TABLE eaggl_cfde_link_runs(run_id TEXT, eaggl_import_id TEXT, gene_set_import_id TEXT, status TEXT);
CREATE TABLE eaggl_embedding_runs(run_id TEXT, import_id TEXT, status TEXT);
CREATE TABLE eaggl_factors(import_id TEXT, factor_index INTEGER, factor_id TEXT, label TEXT);
CREATE TABLE eaggl_cfde_factor_links(run_id TEXT, eaggl_import_id TEXT, factor_index INTEGER, cfde_node_id TEXT, payload TEXT);
CREATE TABLE reference_generations(generation_id TEXT PRIMARY KEY, kind TEXT, model TEXT, status TEXT, eaggl_import_id TEXT,
  eaggl_embedding_run_id TEXT, dismech_import_id TEXT, legacy_mapping_run_id TEXT, legacy_gene_set_import_id TEXT, manifest TEXT,
  cold_export_ref TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE kpn_traits(generation_id TEXT, kpn_trait_id TEXT, legacy_phenotype_id TEXT, phenotype_name TEXT, trait_group TEXT,
  trait_type TEXT, metadata TEXT, PRIMARY KEY(generation_id, kpn_trait_id));
CREATE TABLE reference_factors(generation_id TEXT, factor_key TEXT, public_id TEXT, eaggl_factor_id TEXT, kpn_trait_id TEXT,
  factor_number INTEGER, label TEXT, eaggl_import_id TEXT, source_revision TEXT, metadata TEXT, PRIMARY KEY(generation_id, factor_key));
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
    """SQLite stand-in for the shared scientific database behind runtime_config.mysql_connection."""
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
    def __init__(self): self.rows = {}
    def upsert(self, vectors, namespace): self.rows.setdefault(namespace, {}).update({row['id']: deepcopy(row) for row in vectors})
    def fetch(self, ids, namespace, **kwargs): return [deepcopy(self.rows.get(namespace, {}).get(identity)) for identity in ids]
    def query_many(self, queries, namespace):
        result = []
        for query in queries:
            q = np.asarray(query['vector']) / np.linalg.norm(query['vector'])
            rows = [dict(row, score=float((1 + (np.asarray(row['vector']) / np.linalg.norm(row['vector'])) @ q) / 2))
                    for row in self.rows.get(namespace, {}).values()]
            result.append(sorted(rows, key=lambda row: (-row['score'], row['id']))[:query['top_k']])
        return result
    def info(self):
        return SimpleNamespace(dimension=2, similarity_function='COSINE', namespaces={
            name: SimpleNamespace(vector_count=len(rows), pending_vector_count=0) for name, rows in self.rows.items()})


class Vectors:
    """The environment's Vector registry: immutable snapshots plus the active pointer."""
    def __init__(self): self.snapshots, self.active, self.provider = {}, None, Provider()
    def registry(self, repo=None):
        vectors = self
        class Registry:
            scope = 'local'
            def active_identity_in(self, tx): return vectors.active
            def active_identity(self, *, required=True):
                if required and not vectors.active: raise catalog_module.VectorUnavailable('No verified active Vector snapshot')
                return vectors.active
            def get(self, identity): return deepcopy(vectors.snapshots[identity])
            def serving(self, identity, *, summary=False, context_ids=None):
                state = self.get(identity)
                state['_serving_counts'] = {kind: len(state[kind]) for kind in ('factors', 'contexts', 'gene_sets', 'collections') if kind in state}
                state['_serving_verified'] = True
                if context_ids is not None: state['contexts'] = [row for row in state['contexts'] if row['binding']['source_id'] in context_ids]
                for kind in ('factors', 'contexts', 'gene_sets', 'collections') if summary else ('gene_sets', 'collections'):
                    state.pop(kind, None)
                return state
        return Registry()
    def index_class(self):
        provider = self.provider
        class Index(UpstashFactorIndex):
            def __init__(self, snapshot, *, client=None): super().__init__(snapshot, client=client or provider)
        return Index
    def add(self, name, factors, *, context_run=CONTEXT_RUN, **fields):
        identity = digest(['snapshot', name])
        snapshot = {'snapshot_id': identity, 'environment': 'local', 'status': 'complete', 'run': RUN, 'dismech_import': DISMECH_IMPORT,
            'context_run_id': context_run, 'context_config': CONTEXT_CONFIG, 'embedding_space': embedding_space(RUN, CONTEXT_CONFIG),
            'factor_namespace': 'local-eaggl-factor-' + identity[:24], 'context_namespace': 'local-dismech-context-' + identity[:24],
            'policy_version': POLICY_VERSION, 'export_ref': {}, 'factors': [], 'contexts': [], **fields}
        corpora = [('factors', 'factor_namespace', factors), ('contexts', 'context_namespace',
                    [({**binding, 'import_id': DISMECH_IMPORT, 'embedding_run': context_run}, vector) for binding, vector in CONTEXTS])]
        generation = fields.get('reference_generation_id')
        if generation:
            # A reference-generation snapshot (docs §6) also carries CFDE gene sets and collections,
            # which the catalog never queries but the readiness check counts.
            collection = 'dapper:GeneSetCollection.' + digest([generation, 'collection'])[:32]
            snapshot.update(id_scheme='source-id-v1', gene_set_embedding_space=digest(['gene-set-space', generation]),
                gene_set_namespace='local-cfde-geneset-' + identity[:24], collection_namespace='local-cfde-collection-' + identity[:24],
                gene_sets=[], collections=[])
            corpora += [('gene_sets', 'gene_set_namespace', [({'source_kind': 'cfde_gene_set', 'source_id': 'dapper:GeneSet.' + digest([generation, 'set'])[:32],
                            'name': 'insulin signalling', 'collection_id': collection, 'library': 'GO', 'n_genes': 12,
                            'input_sha256': digest('insulin signalling'), 'generation_id': generation}, [1., 0.])]),
                        ('collections', 'collection_namespace', [({'source_kind': 'cfde_collection', 'source_id': collection, 'label': 'GO_BP',
                            'library': 'GO', 'n_sets': 1, 'input_sha256': digest('GO_BP'), 'generation_id': generation}, [0., 1.])])]
        for kind, namespace, rows in corpora:
            for binding, vector in rows:
                row = {'id': binding['factor_id' if kind == 'factors' else 'source_id'], 'binding': binding,
                       'original_vector_sha256': digest(vector), 'roundtrip_sha256': vector_checksum(vector), 'batch': kind + ':0'}
                snapshot[kind].append(row)
                self.provider.upsert([{'id': row['id'], 'vector': vector, 'metadata': metadata(snapshot, row) if kind in ('factors', 'contexts') else binding}],
                                     namespace=snapshot[namespace])
        self.snapshots[identity] = snapshot
        return identity


class CountingRepository:
    """The prefix's application records, counting reference_active reads."""
    def __init__(self, repo): self.repo, self.reads, self.fail = repo, 0, None
    def read_transaction(self, **kwargs):
        self.reads += 1
        if self.fail: raise self.fail
        return self.repo.read_transaction(**kwargs)
    def single_read(self):
        self.reads += 1
        if self.fail: raise self.fail
        return self.repo.single_read()


class Clock:
    def __init__(self): self.now = 1000.
    def __call__(self): return self.now


def legacy_raw(label): return {'label': label, 'top_genes': 'INS;GCK', 'top_gene_sets': 'insulin signalling'}


def kpn_metadata(generation, trait, label):
    return {'label': label, 'gene_set_score': 1.5, 'kpn': {'phenotype_name': TRAITS[trait][1]}, 'lap': {'generation': generation}}


def revision(generation, key): return digest(['revision', generation, key])


class CatalogGenerationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / 'science.sqlite'))
        self.db.run(SCHEMA)
        self.seed_sources()
        self.insert_generation(LEGACY, LEGACY_KIND, LEGACY_MODEL, legacy_mapping_run_id=MAPPING_RUN, legacy_gene_set_import_id=GENE_SET_IMPORT)
        self.insert_generation(KPN1, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        repo = Repository(str(Path(directory.name) / 'records.sqlite')); repo.migrate()
        self.repo, self.clock, self.vectors, self.runtimes = CountingRepository(repo), Clock(), Vectors(), []
        self.legacy_snapshot = self.vectors.add('legacy', self.legacy_bindings(), mapping_run=MAPPING_RUN, geneset_import=GENE_SET_IMPORT)
        self.kpn1_snapshot = self.kpn_snapshot(KPN1, KPN_FACTORS)
        self.vectors.active = self.legacy_snapshot
        def runtime(directory):
            self.runtimes.append(Runtime()); return self.runtimes[-1]
        environment = patch.dict(os.environ, {'REVEAL_RETRIEVAL_BACKEND': 'upstash'})
        environment.start(); self.addCleanup(environment.stop)
        for name in PINS: os.environ.pop(name, None)
        vector_client = patch('reveal_backend.vector_retrieval.client_from_environment', return_value=self.vectors.provider)
        vector_client.start(); self.addCleanup(vector_client.stop)
        for target, value in [('mysql_connection', self.db), ('reference_mysql_connection', self.db), ('Repository', lambda: self.repo), ('DapperRuntime', runtime),
                              ('VectorRegistry', self.vectors.registry), ('UpstashFactorIndex', self.vectors.index_class()),
                              ('monotonic', self.clock)]:
            context = patch.object(catalog_module, target, value); context.start(); self.addCleanup(context.stop)

    # Fixture data -------------------------------------------------------------------------

    def seed_sources(self):
        files = {'source': [{'path': path, 'sha256': checksum} for path, checksum in FILES.items()], 'gaps': []}
        self.db.run('INSERT INTO dismech_imports VALUES (?,?,?,?)', (DISMECH_IMPORT, 'c' * 40, json.dumps(files), 'complete'))
        self.db.run('INSERT INTO dismech_documents VALUES (?,?,?)', *[(DISMECH_IMPORT, path, checksum) for path, checksum in FILES.items()])
        self.db.run('INSERT INTO dismech_discussions VALUES (?,?,?)', *[(DISMECH_IMPORT, 1, json.dumps(row)) for row in (LINKED_GAP, OPEN_GAP)])
        self.db.run('INSERT INTO dismech_mechanisms VALUES (?,?,?)', (DISMECH_IMPORT, digest(MECHANISM['id']), json.dumps(MECHANISM)))
        self.db.run('INSERT INTO dismech_gap_attachments VALUES (?,?,?,?)', (DISMECH_IMPORT, digest(MECHANISM['id']), 0, json.dumps(ATTACHMENT)))
        self.db.run('INSERT INTO eaggl_cfde_link_runs VALUES (?,?,?,?)', (MAPPING_RUN, EAGGL_IMPORT, GENE_SET_IMPORT, 'complete'))
        self.db.run('INSERT INTO eaggl_embedding_runs VALUES (?,?,?)', (EMBEDDING_RUN, EAGGL_IMPORT, 'complete'))
        self.db.run('INSERT INTO eaggl_factors VALUES (?,?,?,?)', *[(EAGGL_IMPORT, index, factor, label) for index, (factor, label, _) in enumerate(EAGGL)])
        self.db.run('INSERT INTO eaggl_cfde_factor_links VALUES (?,?,?,?,?)', *[
            (MAPPING_RUN, EAGGL_IMPORT, index, native, json.dumps({'raw': legacy_raw(EAGGL[index][1]), 'score': 1}))
            for index, native in LEGACY_LINKS.items()])

    def insert_generation(self, generation, kind, model, *, status='complete', factors=(), labels=None, **columns):
        values = {'eaggl_import_id': EAGGL_IMPORT, 'eaggl_embedding_run_id': EMBEDDING_RUN, 'dismech_import_id': DISMECH_IMPORT, **columns}
        self.db.run('INSERT INTO reference_generations(generation_id,kind,model,status,eaggl_import_id,eaggl_embedding_run_id,dismech_import_id,'
                    'legacy_mapping_run_id,legacy_gene_set_import_id,manifest) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (generation, kind, model, status, values['eaggl_import_id'], values['eaggl_embedding_run_id'], values['dismech_import_id'],
                     values.get('legacy_mapping_run_id'), values.get('legacy_gene_set_import_id'), canonical({'kind': kind, 'generation': generation})))
        if not factors: return
        self.db.run('INSERT INTO kpn_traits VALUES (?,?,?,?,?,?,?)', *[(generation, trait, *TRAITS[trait], '{}') for trait in sorted({row[0] for row in factors})])
        self.db.run('INSERT INTO reference_factors VALUES (?,?,?,?,?,?,?,?,?,?)', *[
            (generation, factor_key(trait, factor), public_id(trait, factor), eaggl, trait, int(factor[6:]), self.label(eaggl, labels),
             EAGGL_IMPORT, revision(generation, factor_key(trait, factor)), json.dumps(kpn_metadata(generation, trait, self.label(eaggl, labels))))
            for trait, factor, eaggl in factors])

    def label(self, eaggl, labels=None):
        return (labels or {}).get(eaggl) or next(label for factor, label, _ in EAGGL if factor == eaggl)

    def vector(self, eaggl): return next(vector for factor, _, vector in EAGGL if factor == eaggl)

    def legacy_bindings(self):
        rows = []
        for index, native in LEGACY_LINKS.items():
            factor, label, vector = EAGGL[index]
            rows.append(({'factor_id': factor, 'label': label, 'trait': factor.split('::')[0], 'input_sha256': text_hash(label), 'input_text': label,
                          'source_kind': 'eaggl_factor', 'native_id': native, 'source_revision': sha256(canonical_json(legacy_raw(label))),
                          'import_id': EAGGL_IMPORT, 'mapping_run': MAPPING_RUN, 'embedding_run': EMBEDDING_RUN, 'template': 't'}, vector))
        return rows

    def kpn_snapshot(self, generation, factors, labels=None, name=None, context_run=CONTEXT_RUN):
        rows = []
        for trait, factor, eaggl in factors:
            key, label = factor_key(trait, factor), self.label(eaggl, labels)
            rows.append(({'factor_id': key, 'label': label, 'trait': eaggl.split('::')[0], 'input_sha256': text_hash(label), 'input_text': label,
                          'source_kind': 'eaggl_factor', 'native_id': public_id(trait, factor), 'source_revision': revision(generation, key),
                          'import_id': EAGGL_IMPORT, 'mapping_run': generation, 'embedding_run': EMBEDDING_RUN, 'template': 't',
                          'reference_generation_id': generation, 'kpn_trait_id': trait, 'eaggl_factor_id': eaggl}, self.vector(eaggl)))
        return self.vectors.add(name or generation, rows, context_run=context_run, mapping_run=generation, geneset_import=generation,
                                reference_generation_id=generation)

    def activate(self, generation, model, snapshot=None, *, previous=None):
        with self.repo.repo.transaction() as tx: write_active(tx, generation, model, expected_previous=previous)
        if snapshot: self.vectors.active = snapshot

    def load(self):
        catalog = Catalog(); catalog.load()
        return catalog

    def queried(self, table): return any(table in statement for statement in self.db.statements)

    def test_readiness_verifies_pins_and_counts_without_loading_catalog_or_vectors(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        self.db.statements.clear()
        catalog = Catalog()
        with patch('reveal_backend.vector_retrieval.client_from_environment', return_value=self.vectors.provider), \
             patch.object(catalog, 'load', side_effect=AssertionError('readiness must not preload')):
            result = catalog.readiness()
            calls = len(self.db.statements)
            self.assertEqual(catalog.readiness(), result)
            self.clock.now += GENERATION_TTL_SECONDS + 1
            with patch.object(self.vectors.provider, 'info', side_effect=AssertionError('unchanged pins need no Vector call')):
                self.assertEqual(catalog.readiness(), result)
        self.assertEqual(result, {'dismech_import': DISMECH_IMPORT, 'gaps': 2, 'mapping_run': KPN1,
            'mapped_factors': 3, 'embedding_run': EMBEDDING_RUN})
        self.assertEqual(len(self.db.statements), calls)
        self.assertFalse(catalog.loaded)
        self.assertEqual(self.runtimes, [])
        self.assertFalse(any('payload' in sql or 'f.factor_key' in sql for sql in self.db.statements))

    def test_readiness_database_cost_per_cache_regime_stays_within_budget(self):
        # (application record reads, direct connects, direct statements); lower these with each readiness fix.
        budget = {'cold': (1, 1, 5), 'within pointer TTL': (0, 0, 0), 'pointer recheck': (1, 0, 0), 'verification': (1, 1, 5)}
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = Catalog(); seen = {}
        for regime, advance in (('cold', 0), ('within pointer TTL', 0), ('pointer recheck', GENERATION_TTL_SECONDS + 1),
                                ('verification', catalog_module.READINESS_VERIFICATION_TTL_SECONDS + 1)):
            self.clock.now += advance; before = (self.repo.reads, self.db.opened, len(self.db.statements))
            catalog.readiness()
            seen[regime] = (self.repo.reads - before[0], self.db.opened - before[1], len(self.db.statements) - before[2])
        for regime, limits in budget.items():
            with self.subTest(regime=regime):
                self.assertTrue(all(cost <= limit for cost, limit in zip(seen[regime], limits)), (regime, seen[regime]))

    def test_plain_browse_and_selected_bindings_do_not_load_semantic_corpora(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        original = self.vectors.registry
        calls = []
        def registry(*args):
            result = original(*args); serving = result.serving
            def project(identity, **kwargs):
                calls.append(kwargs)
                return serving(identity, **kwargs)
            result.serving = project
            return result
        with patch.object(catalog_module, 'VectorRegistry', registry):
            catalog = self.load()
            catalog.search_gaps('insulin')
            catalog.search_factors('insulin', 'lexical')
            catalog.selected(catalog.by_source[LINKED_GAP['id']]['source'] | {'id': LINKED_GAP['id']})
            self.assertIsNone(catalog.index)
            self.assertTrue(all(call.get('summary') for call in calls))
            catalog.suggest_factors([(MECHANISM['id'], MECHANISM['description'])], 'semantic', 2, (), precomputed=True)
            self.assertIsNotNone(catalog.index)
            self.assertEqual(sum(not call.get('summary', False) for call in calls), 1)

    def test_lazy_index_checks_exact_source_revisions_before_semantic_use(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        snapshot = self.vectors.snapshots[self.kpn1_snapshot]
        for kind, key in (('factors', 'source_revision'), ('contexts', 'input_text')):
            binding = snapshot[kind][0]['binding']; original = binding[key]
            try:
                binding[key] = 'changed'
                catalog = self.load()
                self.assertIsNone(catalog.index)
                with self.assertRaises(Problem) as error: catalog.retrieval_index()
                self.assertEqual(error.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')
                self.assertIsNone(catalog.index)
            finally: binding[key] = original

    def test_catalog_registry_uses_explicit_repository_and_wraps_missing_snapshot(self):
        original = self.vectors.registry
        with patch.object(catalog_module, 'VectorRegistry', side_effect=original) as registry:
            catalog = Catalog(self.repo); catalog.load(); catalog.retrieval_index()
        self.assertTrue(all(call.args == (self.repo,) for call in registry.call_args_list))
        with patch.object(catalog_module, 'VectorRegistry', side_effect=catalog_module.VectorUnavailable('missing')):
            with self.assertRaises(Problem) as error: Catalog(self.repo).load()
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(error.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')

    def test_generation_cutover_cannot_publish_old_lazy_index(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        second = self.kpn_snapshot(KPN2, KPN_FACTORS)
        entered, release = threading.Event(), threading.Event()
        original = self.vectors.registry; failures = []
        def registry(*args):
            result = original(*args); serving = result.serving
            def project(identity, **kwargs):
                if not kwargs.get('summary'):
                    entered.set()
                    if not release.wait(5): raise AssertionError('cutover test did not release')
                return serving(identity, **kwargs)
            result.serving = project
            return result
        def initialize():
            try: catalog.retrieval_index()
            except Exception as error: failures.append(error)
        with patch.object(catalog_module, 'VectorRegistry', registry):
            worker = threading.Thread(target=initialize); worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.activate(KPN2, KPN_MODEL, second, previous=KPN1)
                self.clock.now += GENERATION_TTL_SECONDS + 1
                self.assertTrue(catalog.refresh_if_changed())
            finally: release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], Problem)
        self.assertEqual(catalog.reference_generation_id, KPN2)
        self.assertIsNone(catalog.index)
        self.assertEqual(catalog.vector_indexes, {})
        self.assertEqual(catalog.retrieval_index().snapshot['reference_generation_id'], KPN2)

    def serving_calls(self, gate=None):
        """Count full (non-summary) serving reads; gate holds them until released."""
        original, calls = self.vectors.registry, []
        def registry(*args):
            result = original(*args); serving = result.serving
            def project(identity, **kwargs):
                if not kwargs.get('summary'):
                    calls.append(identity)
                    if gate: gate[0].set(); gate[1].wait(5)
                    if len(gate or ()) > 2: raise gate[2]
                return serving(identity, **kwargs)
            result.serving = project
            return result
        return calls, patch.object(catalog_module, 'VectorRegistry', registry)

    def test_a_loaded_catalog_serves_semantic_requests_without_pointer_reads(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); reads = self.repo.reads
        calls, registry = self.serving_calls()
        gap = catalog.by_source[OPEN_GAP['id']]['object']; contexts = [(gap['id'], gap['text'])]
        with registry:
            index = catalog.retrieval_index()
            for _ in range(3):
                catalog.suggest_factors(contexts, 'semantic', 1, (), precomputed=True, index=index)
                catalog.context_embedding_provenance(contexts, index=index)
                catalog.provenance('insulin', 'semantic', True, index=index)
                catalog.search_factors('insulin', 'hybrid', 2, query_vector=np.array([1., 0.]))
        self.assertIs(catalog.retrieval_index(), index)
        self.assertEqual((self.repo.reads - reads, calls), (0, [self.kpn1_snapshot]))  # one cold build, no pointer reads

    def test_vector_only_activation_is_adopted_by_the_poller_not_by_requests(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); first = catalog.retrieval_index()
        other = self.kpn_snapshot(KPN1, KPN_FACTORS, name='kpn1-reindexed')
        self.vectors.active = other
        self.assertIs(catalog.retrieval_index(), first)  # requests keep the pinned snapshot until the poller sees the change
        self.clock.now += GENERATION_TTL_SECONDS - 1
        self.assertFalse(catalog.refresh_if_changed())
        self.clock.now += 1
        self.assertTrue(catalog.refresh_if_changed())
        self.assertEqual((catalog.active_generation, catalog.vector_snapshot_id), (KPN1, other))
        self.assertEqual(catalog.retrieval_index().snapshot['snapshot_id'], other)

    def test_readiness_monitor_reads_replace_the_pollers_own(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); reads = self.repo.reads
        self.clock.now += 1; catalog.observe_pointers(KPN1, self.kpn1_snapshot)
        self.assertFalse(catalog.refresh_if_changed())
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        second = self.kpn_snapshot(KPN2, KPN_FACTORS); self.activate(KPN2, KPN_MODEL, second, previous=KPN1)
        self.clock.now += GENERATION_TTL_SECONDS; catalog.observe_pointers(KPN2, second)
        self.assertTrue(catalog.refresh_if_changed())
        self.assertEqual((catalog.active_generation, catalog.vector_snapshot_id, self.repo.reads - reads), (KPN2, second, 0))
        self.clock.now += GENERATION_TTL_SECONDS + 1  # observations stopped: after two TTLs the poller reads itself
        self.assertFalse(catalog.refresh_if_changed()); self.assertEqual(self.repo.reads - reads, 0)
        self.clock.now += GENERATION_TTL_SECONDS
        self.assertFalse(catalog.refresh_if_changed()); self.assertEqual(self.repo.reads - reads, 1)

    def test_cold_semantic_index_is_built_once_for_concurrent_requests(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); entered, release = threading.Event(), threading.Event()
        calls, registry = self.serving_calls((entered, release)); results = []
        with registry:
            workers = [threading.Thread(target=lambda: results.append(catalog.retrieval_index())) for _ in range(4)]
            for worker in workers: worker.start()
            self.assertTrue(entered.wait(5)); time.sleep(.05); release.set()
            for worker in workers: worker.join(5)
        self.assertEqual((len(results), len({id(index) for index in results}), calls), (4, 1, [self.kpn1_snapshot]))

    def test_a_failed_cold_build_fails_every_waiter_without_replaying_it(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); entered, release = threading.Event(), threading.Event(); failures = []
        calls, registry = self.serving_calls((entered, release, RuntimeError('serving read lost')))
        def request():
            try: catalog.retrieval_index()
            except Exception as error: failures.append(error)
        with registry:
            workers = [threading.Thread(target=request) for _ in range(3)]
            for worker in workers: worker.start()
            self.assertTrue(entered.wait(5)); time.sleep(.05); release.set()
            for worker in workers: worker.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(type(error).__name__ for error in failures), ['Problem', 'Problem', 'RuntimeError'])
        self.assertTrue(all(error.code == 'SEMANTIC_SEARCH_UNAVAILABLE' for error in failures if isinstance(error, Problem)))
        self.assertEqual((catalog.vector_indexes, catalog.vector_builds), ({}, {}))
        self.assertEqual(catalog.retrieval_index().snapshot['snapshot_id'], self.kpn1_snapshot)  # the next request builds again

    def test_a_suggestion_resolves_one_index_for_hits_context_inputs_and_provenance(self):
        from reveal_backend import app as api
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load(); gap = catalog.by_source[OPEN_GAP['id']]
        seen, resolved = [], []
        class Recording:
            def __getattr__(self, name): return getattr(catalog, name)
            def retrieval_index(self): resolved.append(catalog.retrieval_index()); return resolved[-1]
            def suggest_factors(self, *args, index=None, **kwargs): seen.append(index); return catalog.suggest_factors(*args, index=index, **kwargs)
            def context_embedding_provenance(self, contexts, *, index=None): seen.append(index); return catalog.context_embedding_provenance(contexts, index=index)
            def provenance(self, *args, index=None): seen.append(index); return catalog.provenance(*args, index=index)
        body = {'manual_eaggl_anchors': [], 'dismissed_source_ids': [], 'subquery': '', 'mode': 'semantic', 'model': KPN_MODEL,
                'source_gap': {'id': gap['object']['id'], **{key: gap['source'][key] for key in ('source_id', 'source_revision')}}}
        with patch.object(api, 'catalog', Recording()), patch.object(api, 'repo', self.repo.repo):
            result = api.build_suggestions(body)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(seen), 3); self.assertTrue(all(index is resolved[0] for index in seen))
        self.assertEqual(result['search']['corpus_snapshot'], KPN1)

    def test_readiness_fails_closed_on_generation_snapshot_or_provider_mismatch(self):
        self.activate(KPN1, KPN_MODEL)
        with patch('reveal_backend.vector_retrieval.client_from_environment', return_value=self.vectors.provider):
            with self.assertRaises(Problem) as error: Catalog().readiness()
            self.assertEqual(error.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')
            self.vectors.active = self.kpn1_snapshot
            self.vectors.provider.rows[self.vectors.snapshots[self.kpn1_snapshot]['gene_set_namespace']].clear()
            with self.assertRaises(Problem) as error: Catalog().readiness()
            self.assertEqual(error.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')

    def test_readiness_refreshes_pins_after_bounded_cache_lifetime(self):
        catalog = Catalog()
        with patch('reveal_backend.vector_retrieval.client_from_environment', return_value=self.vectors.provider):
            self.assertEqual(catalog.readiness()['mapping_run'], MAPPING_RUN)
            self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
            self.clock.now += GENERATION_TTL_SECONDS + 1
            self.assertEqual(catalog.readiness()['mapping_run'], KPN1)
        self.assertFalse(catalog.loaded)

    def test_readiness_rechecks_provider_with_unchanged_pins(self):
        catalog = Catalog()
        with patch('reveal_backend.vector_retrieval.client_from_environment', return_value=self.vectors.provider):
            catalog.readiness()
            self.clock.now += catalog_module.READINESS_VERIFICATION_TTL_SECONDS + 1
            with patch.object(self.vectors.provider, 'info', side_effect=TimeoutError('provider unavailable')):
                with self.assertRaises(Problem) as error: catalog.readiness()
            self.assertEqual(error.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')

    # Legacy mode --------------------------------------------------------------------------

    def test_legacy_mode_without_active_record_serves_the_mapping_run_unchanged(self):
        catalog = self.load()
        self.assertIsNone(catalog.active_generation)
        self.assertIsNone(catalog.generation_record)
        self.assertEqual((catalog.model, catalog.reference_generation_id, catalog.mapping_run), (LEGACY_MODEL, LEGACY, MAPPING_RUN))
        self.assertEqual((catalog.eaggl_import, catalog.geneset_import, catalog.embedding_run), (EAGGL_IMPORT, GENE_SET_IMPORT, EMBEDDING_RUN))
        self.assertFalse(self.queried('reference_generations') or self.queried('reference_factors') or self.queried('kpn_traits'))
        self.assertEqual(set(catalog.factors), set(LEGACY_LINKS.values()))
        self.assertEqual(set(catalog.factor_legacy), {'T2D::Factor1', 'BMI::Factor1'})
        runtime, native, raw = self.runtimes[0], LEGACY_LINKS[0], legacy_raw('insulin secretion')
        node = {'name': 'T2D mechanism Factor1', 'description': f'EAGGL mechanism {native}. Source label: insulin secretion.'}
        node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
        self.assertEqual(catalog.factors[native], {'source': 'eaggl', 'source_id': native, 'source_revision': sha256(canonical_json(raw)),
            'object_class': 'Mechanism', 'object': node, 'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': 'insulin secretion',
            'subtitle': 'T2D (Factor1)'}, 'model': 'cfde-inc-v2', 'catalog_file': runtime.file('cfde-factor.json', canonical_json(raw), 'application/json')})
        self.assertEqual(catalog.bindings[native], {'eaggl_factor_id': 'T2D::Factor1', 'eaggl_import_id': EAGGL_IMPORT, 'embedding_run_id': EMBEDDING_RUN,
            'mapping_run_id': MAPPING_RUN, 'gene_set_import_id': GENE_SET_IMPORT, 'cfde_node_id': native, 'cfde_payload': raw})
        self.assertEqual(len(catalog.gaps), 2); self.assertEqual(list(catalog.mechanisms), [MECHANISM['id']])
        self.assertEqual(catalog.provenance('q', 'semantic', True)['corpus_snapshot'], MAPPING_RUN)
        self.assertEqual(catalog.provenance('q', 'lexical')['corpus_snapshot'], DISMECH_IMPORT)
        # Lexical search text is unchanged for legacy records.
        self.assertEqual([row['record']['source_id'] for row in catalog.search_factors('portal:bmi', 'lexical')], [LEGACY_LINKS[2]])
        self.assertFalse(catalog.binding_superseded({'reference_generation_id': KPN1}))
        self.assertFalse(catalog.source_superseded(public_id('KPN.TRAIT:0000398', 'Factor1')))

    def test_legacy_mode_keeps_the_vector_snapshot_pinned_by_mapping_run(self):
        self.vectors.active = self.kpn1_snapshot
        with self.assertRaises(Problem) as failure: self.load()
        self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SEMANTIC_SEARCH_UNAVAILABLE'))

    # KPN mode -----------------------------------------------------------------------------

    def test_kpn_generation_serves_every_factor_with_docs_records_and_bindings(self):
        os.environ['REVEAL_MAPPING_RUN_ID'] = MAPPING_RUN  # The deployed legacy pin does not constrain KPN mode.
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        self.assertEqual((catalog.active_generation, catalog.reference_generation_id, catalog.model), (KPN1, KPN1, KPN_MODEL))
        self.assertEqual((catalog.mapping_run, catalog.geneset_import, catalog.eaggl_import, catalog.embedding_run),
                         (KPN1, KPN1, EAGGL_IMPORT, EMBEDDING_RUN))
        self.assertEqual(catalog.generation_record['kind'], KPN_KIND)
        self.assertFalse(self.queried('eaggl_cfde_link_runs') or self.queried('eaggl_cfde_factor_links'))
        self.assertEqual(set(catalog.factors), {public_id(trait, factor) for trait, factor, _ in KPN_FACTORS})
        self.assertEqual(set(catalog.factor_legacy), {factor_key(trait, factor) for trait, factor, _ in KPN_FACTORS})
        trait, runtime = 'KPN.TRAIT:0000398', self.runtimes[0]
        native, key, meta = public_id(trait, 'Factor2'), factor_key(trait, 'Factor2'), kpn_metadata(KPN1, trait, 'beta cell stress')
        node = mechanism_node(native, 'Type 2 diabetes', trait, 'Factor2', 'beta cell stress')
        node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
        self.assertEqual(catalog.factors[native], {'source': 'eaggl', 'source_id': native, 'source_revision': revision(KPN1, key),
            'object_class': 'Mechanism', 'object': node,
            'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': 'beta cell stress', 'subtitle': 'Type 2 diabetes (Factor2)'},
            'model': KPN_MODEL, 'reference_generation_id': KPN1,
            'kpn_trait': {'id': trait, 'name': 'Type 2 diabetes', 'legacy_phenotype_id': 'T2D', 'trait_group': 'metabolic', 'trait_type': 'disease',
                'ontology_mappings': [], 'mapping_interpretations': [], 'mapping_policy_version': 'reveal.trait-identity-eligibility/2'},
            'catalog_file': runtime.file('cfde-factor.json', canonical_json(meta), 'application/json')})
        self.assertIs(catalog.factor_legacy[key], catalog.factors[native])
        self.assertEqual(catalog.bindings[native], {'eaggl_factor_id': 'T2D::Factor2', 'factor_key': key, 'kpn_trait_id': trait,
            'eaggl_import_id': EAGGL_IMPORT, 'embedding_run_id': EMBEDDING_RUN, 'mapping_run_id': KPN1, 'gene_set_import_id': KPN1,
            'reference_generation_id': KPN1, 'model': KPN_MODEL, 'cfde_node_id': native, 'cfde_payload': meta})
        self.assertEqual(catalog.provenance('q', 'hybrid', True)['corpus_snapshot'], KPN1)
        self.assertEqual(catalog.provenance('q', 'lexical')['corpus_snapshot'], DISMECH_IMPORT)
        self.assertEqual(len(catalog.gaps), 2)

    def test_kpn_lexical_and_fuzzy_search_match_label_public_id_and_trait(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        search = lambda query, mode='lexical': [row['record']['source_id'] for row in catalog.search_factors(query, mode)]
        t2d = [public_id('KPN.TRAIT:0000398', 'Factor1'), public_id('KPN.TRAIT:0000398', 'Factor2')]
        self.assertEqual(search('diabetes'), t2d)
        self.assertEqual(search('T2D'), t2d)
        self.assertEqual(search('0000012'), [public_id('KPN.TRAIT:0000012', 'Factor1')])
        self.assertEqual(search('adipocyte'), [public_id('KPN.TRAIT:0000012', 'Factor1')])
        self.assertEqual(search('diabetis', 'fuzzy'), t2d)

    def test_kpn_semantic_retrieval_and_suggestions_resolve_factor_key_aliases(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        rows = catalog.search_factors('insulin', 'semantic', 2, query_vector=np.array([1., 0.]))
        self.assertEqual([row['record']['source_id'] for row in rows],
                         [public_id('KPN.TRAIT:0000398', 'Factor1'), public_id('KPN.TRAIT:0000398', 'Factor2')])
        self.assertEqual(rows[0]['retrieval']['mapping_run_id'], KPN1)
        self.assertEqual(rows[0]['retrieval']['aliases'][0]['id'], factor_key('KPN.TRAIT:0000398', 'Factor1'))
        gap = catalog.by_source[OPEN_GAP['id']]['object']  # Unlinked gaps suggest from their imported question vector.
        suggested = catalog.suggest_factors([(gap['id'], gap['text'])], 'semantic', 1, (), precomputed=True)
        self.assertEqual([row['record']['source_id'] for row in suggested], [public_id('KPN.TRAIT:0000012', 'Factor1')])

    def test_kpn_vector_snapshot_must_carry_the_active_generation_and_every_factor(self):
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        self.activate(KPN1, KPN_MODEL, self.legacy_snapshot)
        for snapshot in (self.legacy_snapshot, self.kpn_snapshot(KPN2, KPN_FACTORS), self.kpn_snapshot(KPN1, KPN_FACTORS[:2], name='partial')):
            with self.subTest(snapshot=snapshot):
                self.vectors.active = snapshot
                with self.assertRaises(Problem) as failure: self.load()
                self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SEMANTIC_SEARCH_UNAVAILABLE'))

    def test_legacy_retrieval_backend_rekeys_eaggl_ids_to_factor_keys(self):
        os.environ['REVEAL_RETRIEVAL_BACKEND'] = 'legacy'
        self.activate(KPN1, KPN_MODEL)
        factors, rows, extra = [], [], EAGGL + [('HEIGHT::Factor1', 'growth plate', [-1., 0.])]
        for factor, label, vector in extra:
            blob = np.asarray(vector, dtype='<f4').tobytes()
            factors.append({'factor_id': factor, 'label': label, 'trait': factor.split('::')[0], 'input_sha256': text_hash(label)})
            rows.append((text_hash(label), label, blob, vector_hash(blob)))
        exact = FactorSearchIndex(factors, RUN, rows)
        def contexts(connection, dismech_import, run, inputs, run_id=None):
            self.assertEqual((dismech_import, run['run_id']), (DISMECH_IMPORT, EMBEDDING_RUN))
            return {'run_id': CONTEXT_RUN, 'config': CONTEXT_CONFIG, 'bindings': {row['source_id']: row for row in inputs},
                    'vectors': {binding['source_id']: np.asarray(vector) for binding, vector in CONTEXTS}}
        with patch.object(catalog_module, 'database_search_index', return_value=exact) as search_index, \
             patch.object(catalog_module, 'load_context_vectors', side_effect=contexts):
            catalog = self.load()
        search_index.assert_called_once()
        self.assertEqual(search_index.call_args.args[1:], (EAGGL_IMPORT, EMBEDDING_RUN))
        self.assertEqual([row['factor_id'] for row in catalog.index.factors],
                         [factor_key(trait, factor) for trait, factor, _ in KPN_FACTORS] + ['HEIGHT::Factor1'])
        self.assertEqual(exact.factors[0]['factor_id'], 'T2D::Factor1')  # The loaded index is re-keyed on a copy.
        rows = catalog.search_factors('insulin', 'semantic', 3, query_vector=np.array([1., 0.]))
        self.assertEqual([row['record']['source_id'] for row in rows],
                         [public_id('KPN.TRAIT:0000398', 'Factor1'), public_id('KPN.TRAIT:0000398', 'Factor2'), public_id('KPN.TRAIT:0000012', 'Factor1')])
        suggested = catalog.suggest_factors([(MECHANISM['id'], MECHANISM['description'])], 'hybrid', 2, (), precomputed=True)
        self.assertEqual(suggested[0]['record']['source_id'], public_id('KPN.TRAIT:0000398', 'Factor1'))
        with patch.object(catalog_module, 'database_search_index', return_value=FactorSearchIndex(factors[1:], RUN, rows_of(factors[1:], extra))), \
             patch.object(catalog_module, 'load_context_vectors', side_effect=contexts):
            with self.assertRaises(Problem) as failure: self.load()
        self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SOURCE_NOT_READY'))

    def test_inconsistent_factor_identities_and_unservable_generations_are_503(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        self.db.run("UPDATE reference_factors SET public_id='factor:kpn:0000012:eaggl-capped-v1:Factor9' WHERE factor_key='KPN.TRAIT:0000398::Factor1'")
        with self.assertRaises(Problem) as failure: self.load()
        self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SOURCE_NOT_READY'))
        self.db.run(f"UPDATE reference_factors SET public_id='{public_id('KPN.TRAIT:0000398', 'Factor1')}' WHERE factor_key='KPN.TRAIT:0000398::Factor1'")
        self.load()
        for status in ('loading', 'failed', 'retired'):
            with self.subTest(status=status):
                self.db.run(f"UPDATE reference_generations SET status='{status}' WHERE generation_id='{KPN1}'")
                with self.assertRaises(Problem) as failure: self.load()
                self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SOURCE_NOT_READY'))
        # Another prefix cutting over marks the shared row superseded; this prefix still serves it.
        self.db.run(f"UPDATE reference_generations SET status='superseded' WHERE generation_id='{KPN1}'")
        self.assertEqual(self.load().active_generation, KPN1)
        os.environ['REVEAL_REFERENCE_GENERATION_ID'] = digest('unknown')
        with self.assertRaises(Problem) as failure: self.load()
        self.assertEqual(failure.exception.code, 'SOURCE_NOT_READY')

    def test_guards_see_superseded_bindings_and_sources_only_outside_legacy_mode(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        current = public_id('KPN.TRAIT:0000398', 'Factor1')
        self.assertFalse(catalog.binding_superseded(catalog.bindings[current]))
        self.assertTrue(catalog.binding_superseded({'mapping_run_id': MAPPING_RUN, 'eaggl_factor_id': 'T2D::Factor1'}))
        self.assertTrue(catalog.binding_superseded({'reference_generation_id': KPN2}))
        self.assertTrue(catalog.binding_superseded({'eaggl_factor_id': 'T2D::Factor1'}))
        self.assertTrue(catalog.source_superseded(LEGACY_LINKS[0]))
        self.assertTrue(catalog.source_superseded(public_id('KPN.TRAIT:0000398', 'Factor7')))
        self.assertFalse(catalog.source_superseded(current))
        self.assertFalse(catalog.source_superseded(MECHANISM['id']))
        stale = {'source_gap': None, 'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': LEGACY_LINKS[0],
                 'source_revision': sha256(canonical_json(legacy_raw('insulin secretion'))), 'dapper_id': 'dapper:Mechanism.' + '0' * 32}}]}
        with self.assertRaises(Problem) as failure: catalog.validate_composer(stale)
        self.assertEqual((failure.exception.status, failure.exception.code), (409, 'SOURCE_REVISION_CHANGED'))
        record = catalog.factors[current]
        fresh = {'source_gap': None, 'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': current,
                 'source_revision': record['source_revision'], 'dapper_id': record['object']['id']}}]}
        self.assertIsNone(catalog.validate_composer(fresh))

    # Active generation resolution ---------------------------------------------------------

    def test_environment_override_pins_the_generation(self):
        os.environ['REVEAL_REFERENCE_GENERATION_ID'] = KPN1
        self.vectors.active = self.kpn1_snapshot
        catalog = self.load()
        self.assertEqual((catalog.active_generation, catalog.model), (KPN1, KPN_MODEL))
        self.assertEqual(self.repo.reads, 0)
        # An active legacy generation (a rollback) serves the legacy data under its own generation id.
        os.environ['REVEAL_REFERENCE_GENERATION_ID'] = LEGACY
        self.vectors.active = self.legacy_snapshot
        catalog = self.load()
        self.assertEqual((catalog.active_generation, catalog.reference_generation_id, catalog.model), (LEGACY, LEGACY, LEGACY_MODEL))
        self.assertEqual(set(catalog.factors), set(LEGACY_LINKS.values()))
        self.assertFalse(catalog.binding_superseded(catalog.bindings[LEGACY_LINKS[0]]))
        self.assertTrue(catalog.binding_superseded({'reference_generation_id': KPN1}))
        os.environ['REVEAL_REFERENCE_GENERATION_ID'] = 'not-a-generation'
        with self.assertRaises(Problem) as failure: self.load()
        self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SOURCE_NOT_READY'))

    def test_ttl_refresh_switches_generations_without_a_restart(self):
        catalog = self.load()
        self.assertEqual((catalog.active_generation, self.repo.reads), (None, 1))
        legacy_factors = catalog.factors
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        self.clock.now += GENERATION_TTL_SECONDS - 1
        self.assertFalse(catalog.refresh_if_changed())
        self.assertEqual((catalog.active_generation, self.repo.reads), (None, 1))  # No record read inside the TTL.
        self.clock.now += 2
        catalog.load(); catalog.gap(LINKED_GAP['id'])
        self.assertEqual((catalog.active_generation, self.repo.reads), (None, 1))  # Requests never check: the poller does.
        self.assertTrue(catalog.refresh_if_changed())
        self.assertEqual((catalog.active_generation, catalog.model, self.repo.reads), (KPN1, KPN_MODEL, 2))
        self.assertEqual(set(catalog.factors), {public_id(trait, factor) for trait, factor, _ in KPN_FACTORS})
        self.assertEqual(set(legacy_factors), set(LEGACY_LINKS.values()))  # Readers of the old state kept a consistent view.
        self.assertEqual(len(self.runtimes), 1)  # The DAPPER runtime is process-wide.
        self.assertTrue(catalog.loaded)
        self.assertFalse(catalog.refresh_if_changed())
        # A second reload: KPN1 -> KPN2 with relabelled factors and its own snapshot.
        labels = {'T2D::Factor1': 'insulin secretion (refit)'}
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS, labels=labels)
        self.activate(KPN2, KPN_MODEL, self.kpn_snapshot(KPN2, KPN_FACTORS, labels), previous=KPN1)
        self.clock.now += GENERATION_TTL_SECONDS
        self.assertTrue(catalog.refresh_if_changed())
        native = public_id('KPN.TRAIT:0000398', 'Factor1')
        self.assertEqual((catalog.active_generation, catalog.factors[native]['reference_generation_id']), (KPN2, KPN2))
        self.assertEqual(catalog.factors[native]['cfde_anchor']['label'], 'insulin secretion (refit)')
        self.assertEqual(catalog.bindings[native]['mapping_run_id'], KPN2)
        self.assertEqual(catalog.retrieval_index().snapshot['reference_generation_id'], KPN2)

    def test_requests_keep_serving_the_loaded_generation_while_a_refresh_loads(self):
        catalog = self.load()
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        entered, release = threading.Event(), threading.Event()
        self.db.block = ('reference_factors', entered, release)
        self.clock.now += GENERATION_TTL_SECONDS
        refresher = threading.Thread(target=catalog.refresh_if_changed); refresher.start()
        try:
            self.assertTrue(entered.wait(10))
            self.clock.now += GENERATION_TTL_SECONDS  # Even past the TTL, other requests never wait for the reload.
            catalog.load(); catalog.search_factors('insulin', 'lexical')
            self.assertIsNone(catalog.active_generation)
            self.assertEqual(set(catalog.factors), set(LEGACY_LINKS.values()))
        finally:
            release.set(); refresher.join(10)
        self.assertFalse(refresher.is_alive())
        self.assertEqual((catalog.active_generation, catalog.loaded), (KPN1, True))
        self.assertEqual(set(catalog.factors), {public_id(trait, factor) for trait, factor, _ in KPN_FACTORS})

    def test_failed_checks_keep_serving_and_failed_reloads_fail_closed(self):
        catalog = self.load()
        self.repo.fail = RuntimeError('records unavailable')
        self.clock.now += GENERATION_TTL_SECONDS
        with self.assertLogs(catalog_module.LOGGER, 'WARNING'): self.assertFalse(catalog.refresh_if_changed())
        self.assertTrue(catalog.loaded); self.assertIsNone(catalog.active_generation)
        self.repo.fail = None
        self.activate(KPN1, KPN_MODEL)  # Vector still serves the legacy snapshot: the new generation cannot load.
        self.clock.now += GENERATION_TTL_SECONDS
        with self.assertRaises(Problem) as failure: catalog.refresh_if_changed()
        self.assertEqual(failure.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')
        self.assertFalse(catalog.loaded)
        with self.assertRaises(Problem): catalog.load()  # Requests reload inline, fail-closed.
        self.vectors.active = self.kpn1_snapshot
        catalog.load()
        self.assertEqual((catalog.active_generation, catalog.model, catalog.loaded), (KPN1, KPN_MODEL, True))
        self.assertEqual(catalog.retrieval_index().snapshot['snapshot_id'], self.kpn1_snapshot)
        # Initial loads fail closed when the records cannot be read.
        self.repo.fail = RuntimeError('records unavailable')
        with self.assertRaises(Problem) as failure: Catalog().load()
        self.assertEqual((failure.exception.status, failure.exception.code), (503, 'SOURCE_NOT_READY'))

    def test_a_loaded_catalog_does_no_io_in_load_even_past_the_ttl(self):
        # Callers such as draft saves and job submits run catalog.load() inside a pooled write
        # transaction: once loaded, load() must never read the records or the scientific tables.
        catalog = self.load()
        reads, opened, statements = self.repo.reads, self.db.opened, len(self.db.statements)
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        for _ in range(3):
            self.clock.now += GENERATION_TTL_SECONDS * 2
            catalog.load(); catalog.selected(catalog.by_source[LINKED_GAP['id']]['source'] | {'id': LINKED_GAP['id']})
            catalog.search_factors('insulin', 'lexical')
        self.assertEqual((self.repo.reads, self.db.opened, len(self.db.statements)), (reads, opened, statements))
        self.assertIsNone(catalog.active_generation)
        self.assertIsNone(catalog.poller)  # Catalog() without poll_seconds starts no thread.

    def test_background_poller_switches_generations_off_the_request_path(self):
        catalog = Catalog(poll_seconds=0.01); self.addCleanup(catalog.stop_poller, 10)
        catalog.load()
        thread = catalog.poller[1]
        self.assertTrue(thread.is_alive())
        self.assertEqual(thread.name, 'reference-generation-poll')
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        self.clock.now += GENERATION_TTL_SECONDS
        deadline = time.monotonic() + 10
        while catalog.active_generation != KPN1 and time.monotonic() < deadline: time.sleep(0.01)
        self.assertEqual((catalog.active_generation, catalog.model, catalog.loaded), (KPN1, KPN_MODEL, True))
        catalog.load()
        self.assertIs(catalog.poller[1], thread)  # One poller per process, kept across reloads.
        catalog.stop_poller(10)
        self.assertFalse(thread.is_alive())

    def test_a_failed_refresh_recovers_when_the_new_generation_binds_another_context_run(self):
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        context_run = digest('context-run-2')
        self.activate(KPN2, KPN_MODEL, self.kpn_snapshot(KPN2, KPN_FACTORS, context_run=context_run), previous=KPN1)
        info, failures = self.vectors.provider.info, [RuntimeError('Upstash unavailable')]
        def flaky():
            if failures: raise failures.pop()
            return info()
        self.vectors.provider.info = flaky
        self.clock.now += GENERATION_TTL_SECONDS
        with self.assertRaises(Exception): catalog.refresh_if_changed()
        self.assertFalse(catalog.loaded)
        self.assertEqual(catalog.dismech_embeddings['run_id'], CONTEXT_RUN)  # The failed reload left the old state untouched.
        catalog.load()  # Never compared against the previous generation's DisMech context run.
        self.assertEqual((catalog.active_generation, catalog.loaded), (KPN2, True))
        self.assertEqual((catalog.dismech_embeddings['run_id'], catalog.retrieval_index().snapshot['context_run_id']), (context_run, context_run))

    def test_startup_warmup_loads_off_the_request_path_and_fails_closed(self):
        self.activate(KPN1, KPN_MODEL)  # Vector still serves the legacy snapshot: the warmup cannot load
        catalog = Catalog()
        with self.assertLogs(catalog_module.LOGGER, 'WARNING'): self.assertFalse(catalog.warm())
        self.assertEqual((catalog.loaded, catalog.warmed.is_set()), (False, False))
        with self.assertRaises(Problem): catalog.load()  # the first request still loads inline and fails closed
        self.vectors.active = self.kpn1_snapshot
        with patch.object(Catalog, 'dismech_catalog', autospec=True) as dismech:
            catalog.start_warmup(); thread = catalog.warmup[1]; catalog.start_warmup()
            thread.join(10)
            self.assertIs(catalog.warmup[1], thread)  # one warmup per process
        dismech.assert_called_once_with(catalog)  # the first full mechanism search is warm too
        self.assertEqual((catalog.loaded, catalog.warmed.is_set(), catalog.active_generation), (True, True, KPN1))
        self.insert_generation(KPN2, KPN_KIND, KPN_MODEL, factors=KPN_FACTORS)
        self.activate(KPN2, KPN_MODEL, self.kpn_snapshot(KPN2, KPN_FACTORS), previous=KPN1)
        self.clock.now += GENERATION_TTL_SECONDS
        self.assertTrue(catalog.refresh_if_changed())
        self.assertTrue(catalog.warmed.is_set()); self.assertIs(catalog.warmup[1], thread)  # process state survives a reload

    def test_kpn_trait_metadata_is_read_once_per_trait(self):
        mappings = [{'target_id': 'MONDO:0005148', 'predicate': 'exact'}]
        self.db.run(f"UPDATE kpn_traits SET metadata='{json.dumps({'ontology_mappings': mappings})}' WHERE kpn_trait_id='KPN.TRAIT:0000398'")
        self.activate(KPN1, KPN_MODEL, self.kpn1_snapshot)
        catalog = self.load()
        factor_statement = next(sql for sql in self.db.statements if 'FROM reference_factors f' in sql)
        self.assertNotIn('t.metadata', factor_statement)
        self.assertEqual(sum('FROM kpn_traits' in sql for sql in self.db.statements), 1)  # one read per generation, 711 rows in KPN
        t2d = [catalog.factors[public_id('KPN.TRAIT:0000398', factor)]['kpn_trait'] for factor in ('Factor1', 'Factor2')]
        self.assertEqual([trait['ontology_mappings'] for trait in t2d], [mappings, mappings])
        self.assertIsNot(t2d[0]['ontology_mappings'], t2d[1]['ontology_mappings'])  # each factor owns its copy
        self.assertEqual(catalog.factors[public_id('KPN.TRAIT:0000012', 'Factor1')]['kpn_trait']['ontology_mappings'], [])

    def test_a_failed_cold_load_leaves_no_partial_state(self):
        self.activate(KPN1, KPN_MODEL)  # Vector still serves the legacy snapshot.
        catalog = Catalog()
        with self.assertRaises(Problem): catalog.load()
        self.assertEqual((catalog.loaded, catalog.active_generation, catalog.reference_generation_id), (False, None, None))
        self.assertFalse(hasattr(catalog, 'factors'))
        self.vectors.active = self.kpn1_snapshot
        catalog.load()
        self.assertEqual((catalog.active_generation, catalog.loaded), (KPN1, True))

    # Lookups ------------------------------------------------------------------------------

    def test_archived_reference_factor_lookups(self):
        legacy_source, kpn_source = LEGACY_LINKS[0], public_id('KPN.TRAIT:0000398', 'Factor1')
        snapshots = [(LEGACY, legacy_source, '2026-09-30T10:00:00Z'), (KPN1, kpn_source, '2026-10-01T10:00:00Z'),
                     (KPN2, kpn_source, '2026-11-01T10:00:00Z')]
        self.db.run('INSERT INTO archived_reference_factors VALUES (?,?,?,?,?)', *[
            (archive_id(generation, source), generation, source, json.dumps({'format': 'reveal.archived-reference-factor/1',
             'generation_id': generation, 'source_id': source, 'label': 'insulin secretion', 'top_genes': [{'symbol': 'INS', 'loading': 0.5}]}), at)
            for generation, source, at in snapshots])
        catalog = Catalog()
        identity = archive_id(LEGACY, legacy_source)
        found = catalog.archived_reference_factor(identity)
        self.assertEqual(found, {'format': 'reveal.archived-reference-factor/1', 'generation_id': LEGACY, 'source_id': legacy_source,
            'label': 'insulin secretion', 'top_genes': [{'symbol': 'INS', 'loading': 0.5}], 'archive_id': identity,
            'captured_at': '2026-09-30T10:00:00Z'})
        opened = self.db.opened
        found['label'] = 'changed by a caller'
        self.assertEqual(catalog.archived_reference_factor(identity)['label'], 'insulin secretion')
        self.assertEqual(self.db.opened, opened)  # Immutable rows are served from the process cache.
        self.assertIsNone(catalog.archived_reference_factor(digest('unknown')))
        self.assertIsNone(catalog.archived_reference_factor("x' OR '1'='1"))
        self.assertEqual(catalog.archived_for_source(kpn_source)['generation_id'], KPN2)  # Latest capture.
        self.assertEqual(catalog.archived_for_source(kpn_source, KPN1)['archive_id'], archive_id(KPN1, kpn_source))
        self.assertIsNone(catalog.archived_for_source(kpn_source, LEGACY))
        self.assertIsNone(catalog.archived_for_source('factor:portal:X:cfde-inc-v2:Factor1'))
        self.assertIsNone(catalog.archived_for_source(''))
        self.assertFalse(self.queried('dismech_'))  # Lookups never load the catalog.
        self.db.run('DROP TABLE archived_reference_factors')  # Migration 008 not applied yet.
        self.assertIsNone(Catalog().archived_reference_factor(identity))
        self.assertIsNone(catalog.archived_for_source(kpn_source))

    def test_archived_reference_factor_misses_are_cached_and_legacy_mode_rarely_connects(self):
        catalog, unknown = Catalog(), digest('unknown')
        self.db.run("INSERT INTO archived_reference_factors VALUES ('%s','%s','x','{}','2026-09-30T10:00:00Z')" % (digest('other'), LEGACY))
        self.assertIsNone(catalog.archived_reference_factor(unknown))
        opened = self.db.opened
        for _ in range(5): self.assertIsNone(catalog.archived_reference_factor(unknown))
        self.assertEqual(self.db.opened, opened)  # A repeated miss is served from the cache within the TTL.
        self.db.run("INSERT INTO archived_reference_factors VALUES ('%s','%s','y','{\"label\": \"late\"}','2026-09-30T11:00:00Z')" % (unknown, LEGACY))
        self.clock.now += GENERATION_TTL_SECONDS
        self.assertEqual(catalog.archived_reference_factor(unknown)['label'], 'late')  # A later capture is seen after the TTL.
        # Legacy mode: an empty (or missing) table costs at most one connection per TTL, whatever the ids.
        self.db.run('DELETE FROM archived_reference_factors')
        catalog = Catalog()
        self.assertIsNone(catalog.archived_reference_factor(digest('first')))
        opened = self.db.opened
        for index in range(20): self.assertIsNone(catalog.archived_reference_factor(digest(['random', index])))
        self.assertIsNone(catalog.archived_for_source(LEGACY_LINKS[0]))
        self.assertEqual(self.db.opened, opened)
        self.clock.now += GENERATION_TTL_SECONDS
        self.db.run('DROP TABLE archived_reference_factors')
        self.assertIsNone(catalog.archived_reference_factor(digest('second')))
        self.assertEqual(self.db.opened, opened + 1)
        for index in range(20): self.assertIsNone(catalog.archived_reference_factor(digest(['again', index])))
        self.assertEqual(self.db.opened, opened + 1)

    def test_generation_lookup_is_cached_for_the_ttl(self):
        catalog = Catalog()
        generation = catalog.generation(KPN1)
        self.assertEqual((generation['generation_id'], generation['kind'], generation['model'], generation['status']),
                         (KPN1, KPN_KIND, KPN_MODEL, 'complete'))
        self.assertEqual(generation['manifest'], {'kind': KPN_KIND, 'generation': KPN1})
        self.assertEqual(catalog.generation(LEGACY)['legacy_mapping_run_id'], MAPPING_RUN)
        self.assertIsNone(catalog.generation('legacy'))
        self.assertIsNone(catalog.generation(KPN2))
        opened = self.db.opened
        self.db.run(f"UPDATE reference_generations SET status='superseded' WHERE generation_id='{KPN1}'")
        self.assertEqual(catalog.generation(KPN1)['status'], 'complete')
        self.assertEqual(self.db.opened, opened)
        self.clock.now += GENERATION_TTL_SECONDS
        self.assertEqual(catalog.generation(KPN1)['status'], 'superseded')
        self.db.run('DROP TABLE reference_generations')
        self.assertIsNone(Catalog().generation(KPN1))


def rows_of(factors, eaggl):
    vectors = {factor: vector for factor, _, vector in eaggl}
    result = []
    for row in factors:
        blob = np.asarray(vectors[row['factor_id']], dtype='<f4').tobytes()
        result.append((row['input_sha256'], row['label'], blob, vector_hash(blob)))
    return result


if __name__ == '__main__':
    unittest.main()
