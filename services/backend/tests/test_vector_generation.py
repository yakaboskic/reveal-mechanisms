"""Reference-generation Vector snapshots (docs/reference-reload.md §6): export, 4-kind import/verify, bindings, namespace deletion."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from reveal_backend import vector_ingestion
from reveal_backend.eaggl_bundle import canonical as eaggl_canonical, text_hash
from reveal_backend.reference_generation import (GENERATION_COLUMNS, KPN_KIND, KPN_MODEL, ReferenceError, legacy_generation_id,
    read_active, write_active)
from reveal_backend.repository import Repository, canonical, digest
from reveal_backend.vector_ingestion import (VectorRegistry, build_export, build_generation_export, clone_export, context_vector_id,
    delete_namespace, delete_vector_bindings, import_batch, list_namespaces, read_export, record_vector_bindings, save_export,
    verify_snapshot)
from reveal_backend.vector_retrieval import (ID_SCHEME, POLICY_VERSION, UpstashFactorIndex, VectorUnavailable, embedding_space,
    metadata, retrieve_native)

DIMENSIONS = 4
PINS = ('REVEAL_MAPPING_RUN_ID', 'REVEAL_EMBEDDING_RUN_ID', 'REVEAL_DISMECH_IMPORT_ID', 'REVEAL_DISMECH_EMBEDDING_RUN_ID')
EAGGL_IMPORT, DISMECH_IMPORT, CONTEXT_RUN, SPACE = 'a' * 64, 'd' * 64, 'c' * 64, 'e' * 64
MAPPING_RUN, GENESET_IMPORT = '1' * 64, '2' * 64
GENERATION = '9' * 64
RUN_CONFIG = {'import_id': EAGGL_IMPORT, 'model': 'm', 'model_revision': 'r', 'service_url': 'https://embed.invalid', 'provider': 'p',
              'template': 'factor-label-v1', 'dtype': 'float32-le', 'normalization': 'none', 'metric': 'cosine'}
EAGGL_RUN = text_hash(eaggl_canonical(RUN_CONFIG))
CONTEXT_CONFIG = {'version': 'dismech-embeddings-v1', 'templates': {'mechanism': 'dismech-description-v1', 'knowledge_gap': 'dismech-gap-text-v1'}}
# (EAGGL factor id, trait, label, KPN trait id); labels may repeat across traits.
FACTORS = [('T2D::Factor1', 'T2D', 'insulin secretion', 'KPN.TRAIT:0000398'), ('T2D::Factor2', 'T2D', 'beta cell mass', 'KPN.TRAIT:0000398'),
           ('T2D::Factor10', 'T2D', 'glucose homeostasis', 'KPN.TRAIT:0000398'), ('BMI::Factor1', 'BMI', 'adipogenesis', 'KPN.TRAIT:0000012'),
           ('BMI::Factor2', 'BMI', 'insulin secretion', 'KPN.TRAIT:0000012')]
CONTEXTS = [('mechanism/A#1:x', 'mechanism', 'Pancreatic beta cells fail'), ('gap/2', 'knowledge_gap', 'Why does adipose tissue expand?')]
COLLECTIONS = [('dapper:GeneSetCollection.' + 'C' * 32, 'GO_BP', 'GO', 2), ('dapper:GeneSetCollection.' + 'D' * 32, 'KEGG', 'KEGG', 1)]
GENE_SETS = [('dapper:GeneSet.' + 'A' * 31 + '1', COLLECTIONS[0][0], 'insulin secretion pathway', 'GO', 12),
             ('dapper:GeneSet.' + 'A' * 31 + '2', COLLECTIONS[0][0], 'fat cell differentiation', 'GO', 30),
             ('dapper:GeneSet.' + 'B' * 31 + '3', COLLECTIONS[1][0], 'Insulin signaling', 'KEGG', 137)]


def vector(seed):
    values = np.random.default_rng(seed).normal(size=DIMENSIONS).astype('<f4')
    return values, hashlib.sha256(values.tobytes()).hexdigest()


def factor_key(row): return row[3] + '::' + row[0].split('::')[1]
def public(row): return 'factor:kpn:' + row[3][-7:] + ':' + KPN_MODEL + ':' + row[0].split('::')[1]


class Cursor:
    def __init__(self, db): self.db, self.rows, self.rowcount = db, [], -1
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=()):
        self.db.log.append(sql)
        for pattern, handler in self.db.handlers:
            if pattern in sql:
                self.rows = [tuple(row) for row in handler(*params)]; self.rowcount = len(self.rows); return
        raise AssertionError('Unexpected SQL: ' + sql)
    def executemany(self, sql, rows):
        assert sql.startswith('INSERT IGNORE INTO vector_bindings(')
        self.rowcount = 0
        for row in rows:
            key, unique = row[:3], (row[0], row[3], row[5], row[7])
            if key in self.db.bindings or unique in {(r[0], r[3], r[5], r[7]) for r in self.db.bindings.values()}: continue
            self.db.bindings[key] = tuple(row); self.rowcount += 1
    def fetchall(self): return list(self.rows)
    def fetchone(self): return self.rows[0] if self.rows else None


class Database:
    """A DB-API connection answering the read queries of the export with fixture rows."""
    def __init__(self, *, generation_status='complete'):
        self.log, self.bindings, self.commits = [], {}, 0
        self.labels = {}
        for index, (_, _, label, _) in enumerate(FACTORS):
            if label not in self.labels: self.labels[label] = (text_hash(label), *vector(index))
        self.names = sorted((sha, label, blob.tobytes(), checksum) for label, (sha, blob, checksum) in self.labels.items())
        self.eaggl = [(factor_id, label, trait, text_hash(label)) for factor_id, trait, label, _ in FACTORS]
        rows = [(factor_key(row), public(row), row[0], row[3], row[2], EAGGL_IMPORT, text_hash(row[2]), digest(['revision', row[0]])) for row in FACTORS]
        self.reference_factors = random.Random(3).sample(rows, len(rows))
        self.contexts = []
        for index, (source_id, kind, text) in enumerate(CONTEXTS):
            values, checksum = vector(100 + index)
            self.contexts.append(({'source_id': source_id, 'source_kind': kind, 'source_revision': 'rev' + str(index),
                                   'template': CONTEXT_CONFIG['templates'][kind], 'input_sha256': text_hash(text)}, text, values.tobytes(), checksum))
        self.reference_vectors = []
        for index, (source_id, _, _, _, _) in enumerate(GENE_SETS):
            values, checksum = vector(200 + index); text = 'gene set text ' + source_id
            self.reference_vectors.append(('cfde_gene_set', source_id, SPACE, text_hash(text), text, values.tobytes(), checksum))
        for index, (source_id, _, _, _) in enumerate(COLLECTIONS):
            values, checksum = vector(300 + index); text = 'collection text ' + source_id
            self.reference_vectors.append(('cfde_collection', source_id, SPACE, text_hash(text), text, values.tobytes(), checksum))
        self.reference_vectors.reverse()
        self.generation = dict(zip(GENERATION_COLUMNS, (GENERATION, KPN_KIND, KPN_MODEL, generation_status, EAGGL_IMPORT, EAGGL_RUN,
                                                         DISMECH_IMPORT, None, None, json.dumps({'kind': KPN_KIND}), None)))
        self.space = (DIMENSIONS, 'cosine')
        self.handlers = [
            ('FROM reference_generations WHERE generation_id=%s', lambda identity: [tuple(self.generation.values())] if identity == GENERATION else []),
            ('SELECT config,dimensions FROM eaggl_embedding_runs', lambda run: [(json.dumps(RUN_CONFIG), DIMENSIONS)] if run == EAGGL_RUN else []),
            ('SELECT config,dimensions,expected_rows FROM eaggl_embedding_runs', lambda run, _: [(json.dumps(RUN_CONFIG), DIMENSIONS, len(self.names))]),
            ('ORDER BY input_sha256 LIMIT 3', lambda run: self.names[:3]),
            ('SELECT input_sha256,input_text,vector,vector_sha256 FROM eaggl_name_embeddings WHERE run_id=%s', lambda run: self.names if run == EAGGL_RUN else []),
            ('SELECT status FROM eaggl_imports', lambda identity: [('complete',)] if identity == EAGGL_IMPORT else []),
            ('SELECT factor_id,trait,input_sha256 FROM eaggl_factors', lambda identity: [(r[0], r[2], r[3]) for r in self.eaggl]),
            ('SELECT factor_id,label,trait,input_sha256 FROM eaggl_factors', lambda identity: self.eaggl),
            ('FROM reference_factors WHERE generation_id=%s', lambda identity: self.reference_factors if identity == GENERATION else []),
            ("FROM dismech_embedding_runs WHERE dismech_import_id=%s AND eaggl_embedding_run_id=%s", lambda identity, run: [(CONTEXT_RUN,)] if run == EAGGL_RUN else []),
            ('FROM dismech_embedding_vectors WHERE run_id=%s', lambda run: [(row[0]['input_sha256'], row[1], row[2], row[3]) for row in self.contexts]),
            ('FROM cfde_gene_set_collections WHERE generation_id=%s', lambda identity: COLLECTIONS),
            ('FROM cfde_gene_sets WHERE generation_id=%s', lambda identity: GENE_SETS),
            ('FROM reference_vectors WHERE generation_id=%s', lambda identity: self.reference_vectors),
            ('FROM embedding_spaces WHERE space_id=%s', lambda identity: [self.space] if identity == SPACE else []),
            ('FROM eaggl_cfde_link_runs', lambda: [(MAPPING_RUN, EAGGL_IMPORT, GENESET_IMPORT)]),
            ('SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s', lambda identity: [(EAGGL_RUN,)]),
            ("SELECT import_id FROM dismech_imports", lambda: [(DISMECH_IMPORT,)]),
            ('FROM eaggl_cfde_factor_links l JOIN eaggl_factors f', lambda run: [
                (factor_id, f'factor:portal:{trait}:cfde-inc-v2:{factor_id.split("::")[1]}', json.dumps({'raw': {'label': label, 'factor': factor_id}}))
                for factor_id, trait, label, _ in FACTORS[:4]]),
            ('SELECT environment,namespace,vector_id', lambda environment, snapshot: [row for row in self.bindings.values() if row[0] == environment and row[3] == snapshot]),
            ('DELETE FROM vector_bindings', self.delete_bindings)]
    def delete_bindings(self, environment, namespace):
        keys = [key for key in self.bindings if key[:2] == (environment, namespace)]
        for key in keys: del self.bindings[key]
        return [()] * len(keys)
    def cursor(self): return Cursor(self)
    def commit(self): self.commits += 1


class Upstash:
    """In-memory Upstash Vector index with the SDK 0.8.0 surface the importer uses."""
    def __init__(self, dimension=DIMENSIONS): self.rows, self.dimension, self.deleted = {}, dimension, []
    def upsert(self, vectors, namespace):
        self.rows.setdefault(namespace, {}).update({row['id']: deepcopy(row) for row in vectors})
    def fetch(self, ids, namespace, **kwargs):
        return [deepcopy(self.rows.get(namespace, {}).get(identity)) for identity in ids]
    def range(self, cursor='', limit=1, namespace='', **kwargs):
        ids = sorted(self.rows.get(namespace, {})); start = int(cursor or 0)
        end = start + limit
        return SimpleNamespace(vectors=[deepcopy(self.rows[namespace][i]) for i in ids[start:end]], next_cursor=str(end) if end < len(ids) else '')
    def info(self):
        return SimpleNamespace(dimension=self.dimension, similarity_function='COSINE', namespaces={
            name: SimpleNamespace(vector_count=len(rows), pending_vector_count=0) for name, rows in self.rows.items()})
    def query_many(self, queries, namespace):
        result = []
        for query in queries:
            snapshot = query['filter'].split("'")[1]
            q = np.array(query['vector']); q /= np.linalg.norm(q)
            rows = []
            for row in self.rows.get(namespace, {}).values():
                if row['metadata']['snapshot_id'] != snapshot: continue
                v = np.array(row['vector']); v /= np.linalg.norm(v)
                rows.append(dict(row, score=float((1 + v @ q) / 2)))
            result.append(sorted(rows, key=lambda row: (-row['score'], row['id']))[:query['top_k']])
        return result
    def list_namespaces(self): return sorted(self.rows)
    def delete_namespace(self, namespace):
        if namespace not in self.rows: raise ValueError('No such namespace')
        del self.rows[namespace]; self.deleted.append(namespace)


def export(db=None, **kwargs):
    db = db or Database()
    with patch.object(vector_ingestion, '_database_inputs', return_value=[row[0] for row in db.contexts]), \
            patch.object(vector_ingestion, 'load_context_vectors', return_value={'run_id': CONTEXT_RUN, 'config': CONTEXT_CONFIG}):
        return build_generation_export(db, GENERATION, **kwargs)


class Environment(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.env = patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'local', 'REVEAL_ARTIFACT_STORE': 'filesystem'})
        self.env.start()
        for name in PINS: os.environ.pop(name, None)
    def tearDown(self): self.env.stop(); self.directory.cleanup()
    def registry(self, name='registry.sqlite3', environment_name=None):
        repo = Repository(Path(self.directory.name) / name); repo.migrate()
        return VectorRegistry(repo, environment_name=environment_name)
    def loaded(self, registry=None, **kwargs):
        registry = registry or self.registry()
        manifest, rows = export(**kwargs)
        manifest = save_export(manifest, rows, self.directory.name)
        registry.register(manifest)
        return registry, manifest


class ExportTests(Environment):
    def test_export_is_deterministic_with_source_ids_and_generation_namespaces(self):
        manifest, rows = export(batch_size=2)
        again = Database(); again.reference_factors.reverse()
        self.assertEqual(canonical(export(again, batch_size=2)), canonical((manifest, rows)))
        identity = manifest['snapshot_id']
        self.assertEqual(manifest['id_scheme'], ID_SCHEME)
        self.assertEqual((manifest['reference_generation_id'], manifest['mapping_run'], manifest['geneset_import']), (GENERATION,) * 3)
        self.assertEqual(manifest['gene_set_embedding_space'], SPACE)
        self.assertEqual(manifest['policy_version'], POLICY_VERSION)
        self.assertEqual(manifest['embedding_space'], embedding_space(manifest['run'], CONTEXT_CONFIG))
        self.assertEqual({key: manifest[key] for key in ('factor_namespace', 'context_namespace', 'gene_set_namespace', 'collection_namespace')}, {
            'factor_namespace': 'local-eaggl-factor-' + identity[:24], 'context_namespace': 'local-dismech-context-' + identity[:24],
            'gene_set_namespace': 'local-cfde-geneset-' + identity[:24], 'collection_namespace': 'local-cfde-collection-' + identity[:24]})
        self.assertEqual([row['id'] for row in manifest['factors']], ['KPN.TRAIT:0000012::Factor1', 'KPN.TRAIT:0000012::Factor2',
            'KPN.TRAIT:0000398::Factor1', 'KPN.TRAIT:0000398::Factor2', 'KPN.TRAIT:0000398::Factor10'])
        self.assertEqual([row['id'] for row in manifest['contexts']],
                         ['dismech:mechanism:' + hashlib.sha256(b'mechanism/A#1:x').hexdigest()[:32],
                          'dismech:knowledge_gap:' + hashlib.sha256(b'gap/2').hexdigest()[:32]])
        self.assertEqual(manifest['contexts'][1]['id'], context_vector_id('knowledge_gap', 'gap/2'))
        self.assertEqual([row['id'] for row in manifest['gene_sets']], sorted(row[0] for row in GENE_SETS))
        self.assertEqual([row['id'] for row in manifest['collections']], sorted(row[0] for row in COLLECTIONS))
        self.assertEqual(manifest['batches'], {'factors:0': [row['id'] for row in manifest['factors'][:2]],
            'factors:1': [row['id'] for row in manifest['factors'][2:4]], 'factors:2': [manifest['factors'][4]['id']],
            'contexts:0': [row['id'] for row in manifest['contexts']], 'gene_sets:0': [row['id'] for row in manifest['gene_sets'][:2]],
            'gene_sets:1': [manifest['gene_sets'][2]['id']], 'collections:0': [row['id'] for row in manifest['collections']]})
        self.assertEqual(len(rows), 5 + 2 + 3 + 2)
        self.assertTrue(all(len(probe['eligible_ids']) for probe in manifest['quality_probes']))

    def test_factor_and_gene_set_bindings_carry_reference_metadata(self):
        manifest, rows = export()
        factor = manifest['factors'][2]['binding']
        self.assertEqual(factor, {'factor_id': 'KPN.TRAIT:0000398::Factor1', 'label': 'insulin secretion', 'trait': 'T2D',
            'input_sha256': text_hash('insulin secretion'), 'input_text': 'insulin secretion', 'source_kind': 'eaggl_factor',
            'native_id': 'factor:kpn:0000398:eaggl-capped-v1:Factor1', 'source_revision': digest(['revision', 'T2D::Factor1']),
            'import_id': EAGGL_IMPORT, 'mapping_run': GENERATION, 'embedding_run': EAGGL_RUN, 'template': 'factor-label-v1',
            'reference_generation_id': GENERATION, 'kpn_trait_id': 'KPN.TRAIT:0000398', 'eaggl_factor_id': 'T2D::Factor1'})
        # Shared labels share one EAGGL label vector.
        by_id = {row['id']: row for row in rows}
        self.assertEqual(by_id['KPN.TRAIT:0000398::Factor1']['vector'], by_id['KPN.TRAIT:0000012::Factor2']['vector'])
        self.assertEqual(manifest['contexts'][0]['binding'], {**Database().contexts[0][0], 'input_text': CONTEXTS[0][2],
                                                              'import_id': DISMECH_IMPORT, 'embedding_run': CONTEXT_RUN})
        # Gene-set/collection bindings live in the export batches; the manifest keeps compact rows.
        gene_set = by_id[GENE_SETS[0][0]]
        self.assertEqual(gene_set['binding'], {'source_kind': 'cfde_gene_set', 'source_id': GENE_SETS[0][0], 'name': 'insulin secretion pathway',
            'collection_id': COLLECTIONS[0][0], 'library': 'GO', 'n_genes': 12, 'input_sha256': text_hash('gene set text ' + GENE_SETS[0][0]),
            'generation_id': GENERATION})
        self.assertEqual(by_id[COLLECTIONS[1][0]]['binding'], {'source_kind': 'cfde_collection', 'source_id': COLLECTIONS[1][0], 'label': 'KEGG',
            'library': 'KEGG', 'n_sets': 1, 'input_sha256': text_hash('collection text ' + COLLECTIONS[1][0]), 'generation_id': GENERATION})
        self.assertEqual(manifest['gene_sets'][0], {key: gene_set[key] for key in ('id', 'batch', 'original_vector_sha256', 'roundtrip_sha256')})
        self.assertTrue(all(set(row) == {'id', 'batch', 'original_vector_sha256', 'roundtrip_sha256'} for row in manifest['collections']))
        self.assertEqual(metadata(manifest, gene_set)['embedding_space'], SPACE)
        self.assertEqual(metadata(manifest, manifest['factors'][0])['embedding_space'], manifest['embedding_space'])

    def test_export_refuses_inconsistent_generation_data(self):
        cases = {
            'legacy generation': lambda db: db.generation.update(kind='legacy-cfde-inc-v2', model='cfde-inc-v2'),
            'loading generation': lambda db: db.generation.update(status='loading'),
            'other EAGGL import': lambda db: db.generation.update(eaggl_import_id='b' * 64),
            'public id mismatch': lambda db: db.reference_factors.__setitem__(0, (*db.reference_factors[0][:1], 'factor:kpn:0000001:eaggl-capped-v1:Factor1', *db.reference_factors[0][2:])),
            'unbound label': lambda db: db.reference_factors.__setitem__(0, (*db.reference_factors[0][:6], 'f' * 64, db.reference_factors[0][7])),
            'missing gene-set vector': lambda db: db.reference_vectors.pop(),
            'second space': lambda db: db.reference_vectors.__setitem__(0, (*db.reference_vectors[0][:2], 'f' * 64, *db.reference_vectors[0][3:])),
            'dimension mismatch': lambda db: setattr(db, 'space', (8, 'cosine')),
            'text hash mismatch': lambda db: db.reference_vectors.__setitem__(0, (*db.reference_vectors[0][:4], 'changed', *db.reference_vectors[0][5:])),
            'vector checksum': lambda db: db.reference_vectors.__setitem__(0, (*db.reference_vectors[0][:6], 'f' * 64)),
            'unknown vector': lambda db: db.reference_vectors.append(('cfde_gene_set', 'dapper:GeneSet.' + 'Z' * 32, SPACE, *db.reference_vectors[0][3:])),
        }
        for name, change in cases.items():
            with self.subTest(name):
                db = Database(); change(db)
                with self.assertRaises(ValueError): export(db)
        with self.assertRaises(ValueError): export(batch_size=201)

    def test_legacy_export_keeps_digest_ids_and_layout(self):
        db = Database()
        with patch.object(vector_ingestion, '_database_inputs', return_value=[row[0] for row in db.contexts]), \
                patch.object(vector_ingestion, 'load_context_vectors', return_value={'run_id': CONTEXT_RUN, 'config': CONTEXT_CONFIG}):
            manifest, rows = build_export(db)
        identity = manifest['snapshot_id']
        self.assertNotIn('id_scheme', manifest); self.assertNotIn('gene_sets', manifest)
        self.assertEqual((manifest['factor_namespace'], manifest['context_namespace']), ('local-f-' + identity[:48], 'local-c-' + identity[:48]))
        self.assertEqual([row['binding']['factor_id'] for row in manifest['factors']], [row[0] for row in FACTORS[:4]])
        for kind in ('factors', 'contexts'):
            self.assertTrue(all(row['id'] == digest([identity, kind, row['binding']]) for row in manifest[kind]))
        self.assertEqual(set(manifest['batches']), {'factors:0', 'contexts:0'})
        self.assertFalse(any('reference_' in sql or 'cfde_gene_set' in sql for sql in db.log))


class ImportTests(Environment):
    def test_four_kind_import_verify_complete_and_serve(self):
        registry, manifest = self.loaded(batch_size=2)
        provider, identity = Upstash(), manifest['snapshot_id']
        for key in manifest['batches']:
            if key != 'collections:0': import_batch(registry, identity, key, client=provider)
        with self.assertRaises(ValueError): verify_snapshot(registry, identity, client=provider)
        self.assertEqual(import_batch(registry, identity, 'collections:0', client=provider)['count'], 2)
        self.assertEqual({name: len(rows) for name, rows in provider.rows.items()}, {
            manifest['factor_namespace']: 5, manifest['context_namespace']: 2, manifest['gene_set_namespace']: 3, manifest['collection_namespace']: 2})
        stored = provider.rows[manifest['gene_set_namespace']][GENE_SETS[0][0]]
        exported = {row['id']: row for row in read_export(manifest, 'gene_sets:0')}
        self.assertEqual(stored['metadata'], metadata(manifest, exported[GENE_SETS[0][0]]))
        self.assertEqual(stored['metadata']['name'], 'insulin secretion pathway')
        with patch.object(vector_ingestion, 'read_export', wraps=read_export) as reader:
            report = verify_snapshot(registry, identity, client=provider)
        self.assertEqual([call.kwargs.get('kinds') for call in reader.call_args_list], [('factors', 'contexts')])
        self.assertTrue(report['passed'])
        self.assertEqual(report['record_count'], {'factors': 5, 'contexts': 2, 'gene_sets': 3, 'collections': 2})
        state = registry.get(identity)
        self.assertEqual(state['status'], 'complete')
        for kind in ('factors', 'contexts', 'gene_sets', 'collections'):
            self.assertTrue(all(row['roundtrip_sha256'] == state['verified_batches'][row['batch']][row['id']] for row in state[kind]))
        # reference_reload apply switches vector_active and reference_active in one transaction.
        with self.assertRaises(ReferenceError), registry.repo.transaction() as tx:
            registry.activate_in(tx, identity)
            write_active(tx, GENERATION, KPN_MODEL, expected_previous='8' * 64, vector_snapshot_id=identity)
        with self.assertRaises(VectorUnavailable): registry.active()
        with registry.repo.transaction() as tx:
            registry.activate_in(tx, identity)
            write_active(tx, GENERATION, KPN_MODEL, expected_previous=None, vector_snapshot_id=identity)
        with registry.repo.read_transaction() as tx: self.assertEqual(read_active(tx)['vector_snapshot_id'], identity)
        index = UpstashFactorIndex(registry.active(), client=provider)
        index.check()
        self.assertEqual(index.provenance()['reference_generation_id'], GENERATION)
        self.assertEqual(index.provenance()['id_scheme'], ID_SCHEME)
        records = {row['factor_id']: {'source_id': row['native_id']} for row in index.factors}
        hits = retrieve_native(index, records, index.fetch_vectors(['KPN.TRAIT:0000398::Factor2']), 1)
        self.assertEqual(hits[0]['record']['source_id'], 'factor:kpn:0000398:eaggl-capped-v1:Factor2')
        np.testing.assert_allclose(index.context_vectors(['gap/2'])[0] @ index.context_vectors(['gap/2'])[0], 1)

    def test_verify_inventories_gene_set_namespaces(self):
        registry, manifest = self.loaded()
        provider, identity = Upstash(), manifest['snapshot_id']
        for key in manifest['batches']: import_batch(registry, identity, key, client=provider)
        namespace = manifest['gene_set_namespace']
        extra = dict(provider.rows[namespace].pop(GENE_SETS[0][0]), id='dapper:GeneSet.' + 'Q' * 32)
        provider.rows[namespace][extra['id']] = extra
        with self.assertRaises(VectorUnavailable): verify_snapshot(registry, identity, client=provider)
        self.assertEqual(registry.get(identity)['status'], 'loading')

    def test_readback_is_verified_for_gene_sets(self):
        class Corrupt(Upstash):
            def fetch(self, ids, namespace, **kwargs):
                rows = super().fetch(ids, namespace, **kwargs)
                rows[0]['metadata']['embedding_space'] = 'f' * 64
                return rows
        registry, manifest = self.loaded()
        with self.assertRaises(VectorUnavailable): import_batch(registry, manifest['snapshot_id'], 'gene_sets:0', client=Corrupt())
        with self.assertRaises(ValueError): import_batch(registry, manifest['snapshot_id'], 'unknown:0', client=Upstash())
        self.assertFalse(registry.batch_done(manifest['snapshot_id'], 'gene_sets:0'))

    def test_complete_requires_every_corpus_kind(self):
        registry, manifest = self.loaded()
        provider, identity = Upstash(), manifest['snapshot_id']
        for key in manifest['batches']: import_batch(registry, identity, key, client=provider)
        state = registry.get(identity)
        with registry.repo.transaction() as tx:
            tx.put('vector_snapshot', identity, 'catalog', dict(state, collections=[], verified_batches={}))
        registry._snapshots.clear()
        with self.assertRaises(ValueError): registry.complete(identity, {'passed': True})


class WorkflowImportTests(Environment):
    """The durable (QStash Workflow) import path checks every namespace of a reference snapshot."""
    def imported(self, **kwargs):
        registry, manifest = self.loaded(**kwargs)
        provider = Upstash()
        for key in manifest['batches']: import_batch(registry, manifest['snapshot_id'], key, client=provider)
        return registry, manifest, provider

    def test_inventory_and_finalize_cover_gene_set_and_collection_namespaces(self):
        from reveal_backend.vector_retrieval import snapshot_kinds
        from reveal_backend.vector_workflow import finalize, inventory_page, query_probe
        registry, manifest, provider = self.imported(batch_size=2)
        identity, state = manifest['snapshot_id'], registry.get(manifest['snapshot_id'])
        self.assertEqual(snapshot_kinds(state), ['factors', 'contexts', 'gene_sets', 'collections'])
        for kind in ('factors', 'contexts'): self.assertTrue(inventory_page(registry, identity, kind, client=provider)['done'])
        for i in range(len(state['quality_probes'])): query_probe(registry, identity, i, client=provider)
        with self.assertRaises(ValueError): finalize(registry, identity, client=provider)  # gene sets not yet inventoried
        for kind in ('gene_sets', 'collections'): self.assertTrue(inventory_page(registry, identity, kind, client=provider)['done'])
        report = finalize(registry, identity, client=provider)
        self.assertEqual(report['record_count'], {'factors': 5, 'contexts': 2, 'gene_sets': 3, 'collections': 2})
        self.assertEqual(registry.get(identity)['status'], 'complete')

    def test_inventory_rejects_foreign_gene_set_metadata_and_unknown_kinds(self):
        from reveal_backend.vector_workflow import inventory_page
        registry, manifest, provider = self.imported()
        identity = manifest['snapshot_id']
        provider.rows[manifest['gene_set_namespace']][GENE_SETS[0][0]]['metadata']['embedding_space'] = 'f' * 64
        with self.assertRaises(VectorUnavailable): inventory_page(registry, identity, 'gene_sets', client=provider)
        with self.assertRaises(ValueError): inventory_page(registry, identity, 'unknown', client=provider)
        self.assertTrue(inventory_page(registry, identity, 'collections', client=provider)['done'])


class IndexTests(Environment):
    def complete(self, name='registry.sqlite3'):
        registry, manifest = self.loaded(self.registry(name))
        provider, identity = Upstash(), manifest['snapshot_id']
        for key in manifest['batches']: import_batch(registry, identity, key, client=provider)
        verify_snapshot(registry, identity, client=provider)
        return registry.get(identity), provider

    def test_check_covers_extra_namespaces(self):
        snapshot, provider = self.complete()
        UpstashFactorIndex(snapshot, client=provider).check()
        provider.rows[snapshot['collection_namespace']].popitem()
        with self.assertRaises(VectorUnavailable): UpstashFactorIndex(snapshot, client=provider).check()
        snapshot, provider = self.complete('second.sqlite3')
        pending = provider.info
        def info():
            result = pending(); result.namespaces[snapshot['gene_set_namespace']].pending_vector_count = 1
            return result
        provider.info = info
        with self.assertRaises(VectorUnavailable): UpstashFactorIndex(snapshot, client=provider).check()

    def test_reference_snapshot_shape_is_validated(self):
        snapshot, provider = self.complete()
        for change in ({'gene_set_namespace': ''}, {'collection_namespace': snapshot['gene_set_namespace']}, {'id_scheme': 'other'},
                       {'gene_set_embedding_space': None}, {'reference_generation_id': 'x'}, {'collections': []}):
            with self.subTest(change), self.assertRaises(VectorUnavailable): UpstashFactorIndex(dict(snapshot, **change), client=provider)
        partial = dict(snapshot); partial.pop('collections'); partial.pop('collection_namespace')
        with self.assertRaises(VectorUnavailable): UpstashFactorIndex(partial, client=provider)
        legacy = {key: item for key, item in snapshot.items() if key not in ('id_scheme', 'gene_sets', 'collections', 'gene_set_namespace', 'collection_namespace')}
        self.assertNotIn('reference_generation_id', UpstashFactorIndex(legacy, client=provider).provenance())

    def test_clone_keeps_source_ids_and_rebinds_namespaces(self):
        snapshot, provider = self.complete()
        store = {}
        class Store:
            def put(self, data, content_type):
                sha = hashlib.sha256(data).hexdigest(); store[sha] = data
                return {'store': 's3', 'key': sha, 'sha256': sha}
            def get(self, ref): return store[ref['sha256']]
            def validate(self, ref): pass
        with patch.object(vector_ingestion.artifact_store, 'store', return_value=Store()), patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'qa'}):
            clone = clone_export(snapshot)
            identity = clone['snapshot_id']
            self.assertEqual(clone['gene_set_namespace'], 'qa-cfde-geneset-' + identity[:24])
            self.assertEqual(clone['factor_namespace'], 'qa-eaggl-factor-' + identity[:24])
            for kind in ('factors', 'contexts', 'gene_sets', 'collections'):
                self.assertEqual([row['id'] for row in clone[kind]], [row['id'] for row in snapshot[kind]])
            self.assertEqual(clone['batches'], snapshot['batches'])
            registry = self.registry('qa.sqlite3', 'qa'); registry.register(clone)
            for key in clone['batches']: import_batch(registry, identity, key, client=provider)
            self.assertTrue(verify_snapshot(registry, identity, client=provider)['passed'])
            gene_sets = read_export(clone, 'gene_sets:0')
            self.assertEqual({row['id'] for row in gene_sets}, {row['id'] for row in clone['gene_sets']})


class BindingTests(Environment):
    def test_bindings_are_recorded_once_per_vector(self):
        registry, manifest = self.loaded()
        provider, identity = Upstash(), manifest['snapshot_id']
        for key in manifest['batches']: import_batch(registry, identity, key, client=provider)
        db = Database()
        with self.assertRaises(ValueError): record_vector_bindings(db, registry.get(identity))
        verify_snapshot(registry, identity, client=provider)
        state = registry.get(identity)
        self.assertEqual(record_vector_bindings(db, state), 12)
        self.assertEqual(record_vector_bindings(db, state), 0)
        rows = {(row[1], row[2]): row for row in db.bindings.values()}
        factor = rows[(state['factor_namespace'], 'KPN.TRAIT:0000398::Factor1')]
        self.assertEqual(factor, ('local', state['factor_namespace'], 'KPN.TRAIT:0000398::Factor1', identity, GENERATION, 'eaggl_factor',
            'KPN.TRAIT:0000398::Factor1', hashlib.sha256(b'KPN.TRAIT:0000398::Factor1').hexdigest(), state['factors'][2]['original_vector_sha256']))
        context = rows[(state['context_namespace'], context_vector_id('mechanism', 'mechanism/A#1:x'))]
        self.assertEqual(context[5:7], ('mechanism', 'mechanism/A#1:x'))
        self.assertEqual(rows[(state['collection_namespace'], COLLECTIONS[0][0])][5], 'cfde_collection')
        inventory = {(name, identity_row) for name, vectors in provider.rows.items() for identity_row in vectors}
        self.assertEqual(set(rows), inventory)
        conflict = Database()
        key = ('local', state['gene_set_namespace'], GENE_SETS[0][0])
        conflict.bindings[key] = (*key, 'f' * 64, GENERATION, 'cfde_gene_set', GENE_SETS[0][0], 'x' * 64, 'y' * 64)
        with self.assertRaises(ValueError): record_vector_bindings(conflict, state)
        self.assertEqual(delete_vector_bindings(db, state['gene_set_namespace'], environment='local'), 3)
        self.assertEqual(len(db.bindings), 9)

    def test_legacy_bindings_name_the_legacy_generation(self):
        manifest = {'environment': 'local', 'snapshot_id': 'f' * 64, 'mapping_run': MAPPING_RUN, 'status': 'complete',
                    'verification': {'passed': True}, 'factor_namespace': 'local-f-' + 'f' * 48, 'context_namespace': 'local-c-' + 'f' * 48,
                    'factors': [{'id': 'x' * 64, 'binding': {'factor_id': 'T2D::Factor1', 'source_kind': 'eaggl_factor'}, 'original_vector_sha256': 'a' * 64}],
                    'contexts': [{'id': 'y' * 64, 'binding': {'source_id': 'gap/2', 'source_kind': 'knowledge_gap'}, 'original_vector_sha256': 'b' * 64}]}
        db = Database()
        self.assertEqual(record_vector_bindings(db, manifest), 2)
        self.assertEqual({row[4] for row in db.bindings.values()}, {legacy_generation_id(MAPPING_RUN)})


class NamespaceTests(unittest.TestCase):
    def provider(self, *names):
        provider = Upstash()
        for name in names: provider.upsert([{'id': 'v', 'vector': [1., 0., 0., 0.], 'metadata': {}}], namespace=name)
        return provider

    def test_list_namespaces_counts_and_filters_by_environment(self):
        provider = self.provider('local-f-' + 'a' * 48, 'qa-eaggl-factor-' + 'b' * 24, 'localx-f-' + 'c' * 48)
        provider.rows['qa-eaggl-factor-' + 'b' * 24]['w'] = {'id': 'w'}
        self.assertEqual(list_namespaces(provider), {'local-f-' + 'a' * 48: 1, 'localx-f-' + 'c' * 48: 1, 'qa-eaggl-factor-' + 'b' * 24: 2})
        self.assertEqual(list(list_namespaces(provider, environment='local')), ['local-f-' + 'a' * 48])
        with self.assertRaises(ValueError): list_namespaces(provider, environment='Local')

    def test_delete_namespace_is_scoped_to_one_environment(self):
        allowed = ['local-f-' + 'a' * 48, 'local-c-' + 'a' * 48, 'local-eaggl-factor-' + 'b' * 24, 'local-dismech-context-' + 'b' * 24,
                   'local-cfde-geneset-' + 'b' * 24, 'local-cfde-collection-' + 'b' * 24]
        refused = ['', 'local', 'local-', 'qa-f-' + 'a' * 48, 'prod-eaggl-factor-' + 'b' * 24, 'local-f-' + 'a' * 47, 'local-f-' + 'A' * 48,
                   'local-eaggl-factor-' + 'b' * 25, 'local-eaggl-factor-' + 'b' * 23, 'localx-f-' + 'a' * 48, 'local-other-' + 'b' * 24,
                   'local-f-' + 'a' * 48 + '\n', ' local-f-' + 'a' * 48, 'local-qa-f-' + 'a' * 48, 'local-eaggl-factor-' + 'b' * 24 + '-x']
        provider = self.provider(*allowed, *[name for name in refused if name])
        for name in refused:
            with self.subTest(name), self.assertRaises(ValueError): delete_namespace(provider, name, environment='local')
        for environment in ('', 'Local', 'local.*', None):
            with self.assertRaises(ValueError): delete_namespace(provider, allowed[0], environment=environment)
        with self.assertRaises(ValueError): delete_namespace(provider, allowed[2], environment='local', protected=[allowed[2]])
        self.assertEqual(provider.deleted, [])
        for name in allowed: self.assertTrue(delete_namespace(provider, name, environment='local'))
        self.assertEqual(provider.deleted, allowed)
        self.assertFalse(delete_namespace(provider, allowed[0], environment='local'))
        with self.assertRaises(ValueError): delete_vector_bindings(Database(), 'qa-f-' + 'a' * 48, environment='local')


if __name__ == '__main__': unittest.main()
