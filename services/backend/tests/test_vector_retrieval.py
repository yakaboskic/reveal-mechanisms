"""Provider-independent contract and failure tests for immutable ANN retrieval."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from reveal_backend.catalog import Catalog, check_vector_readiness
from reveal_backend.auth import Problem
from reveal_backend.repository import Repository, digest
from reveal_backend.vector_ingestion import VectorRegistry, import_batch, save_export, verify_snapshot
from reveal_backend.vector_retrieval import (POLICY_VERSION, UpstashFactorIndex, VectorUnavailable, check_snapshot_readiness,
    cosine_score, embedding_space, metadata, query_vector_provenance, retrieve_native, vector_checksum)


class Provider:
    def __init__(self): self.rows = {}; self.queries = []; self.fetches = []; self.fail = False; self.query_batches = []
    def upsert(self, vectors, namespace):
        self.rows.setdefault(namespace, {}).update({row['id']: deepcopy(row) for row in vectors})
    def fetch(self, ids, namespace, **kwargs):
        if self.fail: raise TimeoutError('provider unreachable')
        self.fetches.append((namespace, list(ids)))
        return [deepcopy(self.rows.get(namespace, {}).get(identity)) for identity in ids]
    def query_many(self, queries, namespace):
        self.query_batches.append(sum(query['top_k'] for query in queries))
        if self.fail: raise TimeoutError('provider unreachable')
        result = []
        for query in queries:
            self.queries.append((namespace, deepcopy(query)))
            q = np.array(query['vector']); q /= np.linalg.norm(q)
            rows = []
            for row in self.rows.get(namespace, {}).values():
                v = np.array(row['vector']); v /= np.linalg.norm(v)
                rows.append(dict(row, score=float((1+v @ q)/2)))
            result.append(sorted(rows, key=lambda row: (-row['score'], row['id']))[:query['top_k']])
        return result
    def info(self):
        return SimpleNamespace(dimension=2, similarity_function='COSINE', namespaces={
            ns: SimpleNamespace(vector_count=len(rows), pending_vector_count=0) for ns, rows in self.rows.items()})
    def range(self, namespace, **kwargs):
        return SimpleNamespace(vectors=list(self.rows[namespace].values()), next_cursor='0')


def fixture(*, many=False):
    run = {'run_id': 'r', 'config': {'model': 'm', 'provider': 'p', 'service_url': 'https://embed.invalid', 'import_id': 'i'},
           'dimensions': 2, 'calibration': []}
    config = {'templates': {'mechanism': 't'}}
    snapshot = {'snapshot_id': 'f' * 64, 'environment': 'local', 'status': 'complete', 'run': run,
        'context_run_id': 'c', 'context_config': config, 'embedding_space': embedding_space(run, config),
        'factor_namespace': 'local-f-snapshot', 'context_namespace': 'local-c-snapshot',
        'policy_version': POLICY_VERSION, 'factors': [], 'contexts': [], 'export_ref': {}, 'batches': {}, 'verified_batches': {}}
    vectors = [('a', 'one', [1., 0.]), ('b', 'two', [0., 1.]), ('alias', 'two', [-1., 0.]), ('c', 'three', [.8, .6])]
    if many: vectors = [(str(i), 'one', [1., 0.]) for i in range(45)] + [('other', 'two', [.6, .8])]
    client = Provider(); records = {}
    for alias, native, vector in vectors:
        sha = digest(alias)
        row = {'id': alias, 'binding': {'factor_id': alias, 'native_id': native, 'label': alias, 'trait': 't',
            'input_text': alias, 'input_sha256': sha, 'source_kind': 'eaggl_factor'},
            'original_vector_sha256': 'original-' + alias, 'roundtrip_sha256': vector_checksum(vector), 'batch': 'factors:0'}
        snapshot['factors'].append(row)
        records[alias] = {'source_id': native}
        client.upsert([{'id': alias, 'vector': vector, 'metadata': metadata(snapshot, row)}], namespace=snapshot['factor_namespace'])
    row = {'id': 'context', 'binding': {'source_id': 'context', 'source_kind': 'mechanism', 'source_revision': 'revision',
        'template': 't', 'input_sha256': 'sha', 'input_text': 'description'}, 'original_vector_sha256': 'original-context',
        'roundtrip_sha256': vector_checksum([1., 0.]), 'batch': 'contexts:0'}
    snapshot['contexts'].append(row)
    client.upsert([{'id': row['id'], 'vector': [1., 0.], 'metadata': metadata(snapshot, row)}], namespace=snapshot['context_namespace'])
    return snapshot, client, records


class RetrievalTests(unittest.TestCase):
    def test_registry_serving_projects_large_import_fields_and_preserves_exact_bindings(self):
        snapshot, provider, _ = fixture()
        snapshot.update(verification={'passed': True, 'large_audit': 'a'*100_000},
            verified_batches={'large_bookkeeping': 'b'*100_000}, batches={'large_inventory': 'c'*100_000})
        with TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / 'registry.sqlite3'); repo.migrate()
            with repo.transaction() as tx: tx.put('vector_snapshot', snapshot['snapshot_id'], 'catalog', snapshot)
            registry = VectorRegistry(repo, environment_name='local')
            with patch.object(registry, 'get', side_effect=AssertionError('must not read full snapshot')):
                serving = registry.serving(snapshot['snapshot_id'])
                summary = registry.serving(snapshot['snapshot_id'], summary=True)
                none = registry.serving(snapshot['snapshot_id'], context_ids=[])
                selected = registry.serving(snapshot['snapshot_id'], context_ids=['context'])
            self.assertEqual(serving['factors'], snapshot['factors'])
            self.assertEqual(serving['contexts'], snapshot['contexts'])
            self.assertEqual(selected['contexts'], snapshot['contexts'])
            self.assertEqual(none['contexts'], [])
            self.assertEqual(none['_serving_counts']['contexts'], 1)
            self.assertEqual(summary['_serving_counts'], {'factors': 4, 'contexts': 1})
            self.assertFalse({'verification', 'verified_batches', 'batches'} & serving.keys())
            self.assertFalse({'factors', 'contexts', 'gene_sets', 'collections'} & summary.keys())
            check_snapshot_readiness(summary, client=provider)
            UpstashFactorIndex(serving, client=provider).check()
            provider.rows[snapshot['context_namespace']].clear()
            with self.assertRaises(VectorUnavailable): check_snapshot_readiness(summary, client=provider)
            snapshot['verification']['passed'] = False
            with repo.transaction() as tx: tx.put('vector_snapshot', snapshot['snapshot_id'], 'catalog', snapshot)
            with self.assertRaises(VectorUnavailable): registry.serving(snapshot['snapshot_id'])

    def test_serving_summary_is_projected_again_only_when_the_snapshot_row_changes(self):
        from reveal_backend.repository import Transaction
        snapshot, _, _ = fixture()
        snapshot['verification'] = {'passed': True}
        with TemporaryDirectory() as directory:
            repo = Repository(Path(directory) / 'registry.sqlite3'); repo.migrate()
            with repo.transaction() as tx: tx.put('vector_snapshot', snapshot['snapshot_id'], 'catalog', snapshot)
            registry, statements = VectorRegistry(repo, environment_name='local'), []
            execute = Transaction.execute
            def recorded(tx, sql, params=()): statements.append(sql); return execute(tx, sql, params)
            with patch.object(Transaction, 'execute', recorded):
                first = registry.serving(snapshot['snapshot_id'], summary=True)
                projections = sum('JSON_OBJECT' in sql for sql in statements)
                first['run'] = 'changed by a caller'
                again = registry.serving(snapshot['snapshot_id'], summary=True)
                self.assertEqual(sum('JSON_OBJECT' in sql for sql in statements), projections)  # served from the version check
                self.assertEqual(again['run'], snapshot['run'])
                snapshot['run'] = dict(snapshot['run'], run_id='refit')
                with repo.transaction() as tx: tx.put('vector_snapshot', snapshot['snapshot_id'], 'catalog', snapshot)
                self.assertEqual(registry.serving(snapshot['snapshot_id'], summary=True)['run']['run_id'], 'refit')
                self.assertEqual(sum('JSON_OBJECT' in sql for sql in statements), projections + 1)
                snapshot['verification']['passed'] = False
                with repo.transaction() as tx: tx.put('vector_snapshot', snapshot['snapshot_id'], 'catalog', snapshot)
                with self.assertRaises(VectorUnavailable): registry.serving(snapshot['snapshot_id'], summary=True)

    def test_embedding_space_tolerates_mysql_json_float_presentation_only(self):
        snapshot, _, _ = fixture()
        run = deepcopy(snapshot['run'])
        value = float(np.float32(.1234567))
        run['calibration'] = [{'vector': [value, 1.], 'vector_sha256': 'original', 'input_sha256': 'input'}]
        original = embedding_space(run, snapshot['context_config'])
        run['calibration'][0]['vector'][0] = np.nextafter(value, float('inf')).item()
        self.assertEqual(original, embedding_space(run, snapshot['context_config']))
        run['calibration'][0]['vector'][0] = .2
        self.assertNotEqual(original, embedding_space(run, snapshot['context_config']))

    def test_batch_queries_respect_aggregate_provider_read_limit(self):
        snapshot, provider, _ = fixture()
        index = UpstashFactorIndex(snapshot, client=provider)
        rows = index.candidates([[1., 0.]] * 24, 100)
        self.assertEqual(len(rows), 24)
        self.assertEqual(provider.query_batches, [1000, 1000, 400])

    def test_catalog_readiness_provider_failure_is_explicit_503(self):
        snapshot, provider, _ = fixture()
        index = UpstashFactorIndex(snapshot, client=provider)
        with patch.object(provider, 'info', side_effect=TimeoutError('provider unavailable')):
            with self.assertRaises(Problem) as failure: check_vector_readiness(index)
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(failure.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')

    def test_score_conversion_rejects_invalid_provider_values(self):
        self.assertEqual([cosine_score(x) for x in (0, .5, 1)], [-1, 0, 1])
        for value in (True, None, float('nan'), 1.1):
            with self.assertRaises(VectorUnavailable): cosine_score(value)

    def test_all_aliases_and_nonwinning_context_scores_are_real(self):
        snapshot, client, records = fixture()
        index = UpstashFactorIndex(snapshot, client=client)
        rows = retrieve_native(index, records, np.array([[1., 0.], [0., 1.], [-1., 0.]]), 3)
        by_native = {row['record']['source_id']: row for row in rows}
        np.testing.assert_array_equal(by_native['two']['scores'], [0., 1., 1.])
        self.assertEqual(by_native['one']['retrieval']['score_conversion'], '2 * provider_score - 1')
        self.assertEqual(len(by_native['two']['retrieval']['aliases']), 2)
        self.assertTrue(all(ns == snapshot['factor_namespace'] for ns, _ in client.queries))
        self.assertFalse(hasattr(index, 'matrix'))

    def test_query_vectors_are_stored_once_as_exact_base64_bytes(self):
        import base64
        vectors = np.array([[.6, .8], [1 / 3, (8 / 9) ** .5]])
        value = query_vector_provenance(vectors)
        self.assertEqual(set(value), {'query_vector_checksums', 'query_vector_encoding', 'query_vectors_base64'})
        self.assertEqual(value['query_vector_checksums'], [vector_checksum(vector) for vector in vectors])
        decoded = [np.frombuffer(base64.b64decode(text), dtype='<f8') for text in value['query_vectors_base64']]
        np.testing.assert_array_equal(decoded, vectors)   # lossless: the float list was a second copy

    def test_hits_share_one_retrieval_record_built_once_and_search_can_skip_it(self):
        snapshot, client, records = fixture()
        index = UpstashFactorIndex(snapshot, client=client)
        vectors = np.array([[1., 0.], [0., 1.], [-1., 0.]])
        with patch('reveal_backend.vector_retrieval.query_vector_provenance', wraps=query_vector_provenance) as built:
            rows = retrieve_native(index, records, vectors, 3)
        self.assertEqual(built.call_count, 1)
        self.assertGreater(len(rows), 1)
        shared = [{key: value for key, value in row['retrieval'].items() if key != 'aliases'} for row in rows]
        self.assertTrue(all(digest(value) == digest(shared[0]) for value in shared))
        self.assertNotIn('query_vectors', shared[0])
        self.assertEqual(shared[0]['query_vectors_base64'], query_vector_provenance(vectors)['query_vectors_base64'])
        bare = retrieve_native(index, records, vectors, 3, provenance=False)
        self.assertEqual([(row['record'], row['value']) for row in bare], [(row['record'], row['value']) for row in rows])
        self.assertTrue(all('retrieval' not in row for row in bare))

    def test_duplicate_alias_underfill_expands_before_cutoff(self):
        snapshot, client, records = fixture(many=True)
        rows = retrieve_native(UpstashFactorIndex(snapshot, client=client), records, [[1., 0.]], 2)
        self.assertEqual([row['record']['source_id'] for row in rows], ['one', 'two'])
        self.assertEqual([query['top_k'] for _, query in client.queries], [32, 46])
        rows = retrieve_native(UpstashFactorIndex(snapshot, client=client), records, [[1., 0.]], 1, exclude=['one'])
        self.assertEqual(rows[0]['record']['source_id'], 'two')

    def test_fetch_validates_provenance_and_precision(self):
        snapshot, client, _ = fixture()
        index = UpstashFactorIndex(snapshot, client=client)
        record = client.rows[snapshot['factor_namespace']]['a']
        record['metadata']['source_kind'] = 'different'
        with self.assertRaises(VectorUnavailable): index.fetch_vectors(['a'])
        record['metadata'] = metadata(snapshot, snapshot['factors'][0])
        record['vector'] = [.999, .001]
        with self.assertRaises(VectorUnavailable): index.fetch_vectors(['a'])

    def test_provider_failure_never_falls_back_to_exact_or_empty_result(self):
        snapshot, client, records = fixture()
        index = UpstashFactorIndex(snapshot, client=client); client.fail = True
        with self.assertRaises(VectorUnavailable): retrieve_native(index, records, [[1., 0.]], 3)

    def test_context_vectors_are_lazy_and_source_bound(self):
        snapshot, client, _ = fixture()
        index = UpstashFactorIndex(snapshot, client=client)
        self.assertIn('context', index.contexts['vectors'])
        self.assertEqual(client.fetches, [])
        np.testing.assert_array_equal(index.contexts['vectors']['context'], [1, 0])
        self.assertEqual(client.fetches, [(snapshot['context_namespace'], ['context'])])

    def test_catalog_suggestions_and_hybrid_use_shared_provider(self):
        snapshot, client, records = fixture()
        index = UpstashFactorIndex(snapshot, client=client)
        catalog = Catalog(); catalog.loaded = True; catalog.index = index
        catalog.factor_legacy = records
        catalog.factors = {record['source_id']: {**record, 'cfde_anchor': {'label': 'query'}} for record in records.values()}
        with patch.object(catalog, 'runtime_query_vectors', return_value=np.array([[1., 0.], [0., 1.]])):
            rows = catalog.suggest_factors([('x', 'x'), ('y', 'y')], 'semantic', 3, [])
        self.assertTrue(all('context_similarities' in row for row in rows))
        self.assertTrue(client.queries)
        before = len(client.queries)
        hybrid = catalog.search_factors('query', 'hybrid', 3, query_vector=np.array([1., 0.]))
        self.assertGreater(len(client.queries), before)
        self.assertTrue(any('retrieval' in row for row in hybrid))
        bare = catalog.search_factors('query', 'hybrid', 3, query_vector=np.array([1., 0.]), provenance=False)
        self.assertEqual([(row['record'], row['ranking']) for row in bare], [(row['record'], row['ranking']) for row in hybrid])
        self.assertTrue(all(set(row) == {'record', 'ranking'} for row in bare))

    def test_snapshot_requires_explicit_namespace_and_compatible_space(self):
        snapshot, client, _ = fixture()
        for key, val in [('factor_namespace', ''), ('embedding_space', 'wrong'), ('status', 'loading')]:
            changed = dict(snapshot, **{key: val})
            with self.assertRaises(VectorUnavailable): UpstashFactorIndex(changed, client=client)


class ImportTests(unittest.TestCase):
    def test_partial_import_is_inactive_replays_batches_and_requires_quality_gate(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'local', 'REVEAL_ARTIFACT_STORE': 'filesystem'}):
            repo = Repository(Path(directory) / 'registry.sqlite3'); repo.migrate()
            registry = VectorRegistry(repo)
            manifest, populated, _ = fixture()
            manifest.update(status='loading', export_ref={}, verified_batches={})
            exported = []
            for kind, ns in [('factors', manifest['factor_namespace']), ('contexts', manifest['context_namespace'])]:
                manifest['batches'][kind + ':0'] = [row['id'] for row in manifest[kind]]
                for row in manifest[kind]:
                    exported.append({'kind': kind, **row, 'vector': populated.rows[ns][row['id']]['vector']})
            manifest = save_export(manifest, exported, directory)
            registry.register(manifest)
            provider = Provider()
            with self.assertRaises(ValueError): registry.activate(manifest['snapshot_id'])
            with self.assertRaises(VectorUnavailable): registry.active()
            import_batch(registry, manifest['snapshot_id'], 'factors:0', client=provider)
            fetches = len(provider.fetches)
            import_batch(registry, manifest['snapshot_id'], 'factors:0', client=provider)
            self.assertEqual(fetches, len(provider.fetches))
            with self.assertRaises(ValueError): verify_snapshot(registry, manifest['snapshot_id'], client=provider)
            import_batch(registry, manifest['snapshot_id'], 'contexts:0', client=provider)
            report = verify_snapshot(registry, manifest['snapshot_id'], client=provider)
            self.assertTrue(report['passed'])
            registry.activate(manifest['snapshot_id'])
            self.assertEqual(registry.active()['snapshot_id'], manifest['snapshot_id'])
            provider.rows[manifest['context_namespace']]['context']['vector'] = [0., 1.]
            with self.assertRaises(VectorUnavailable): UpstashFactorIndex(registry.active(), client=provider).context_vectors(['context'])

    def test_readback_mismatch_never_marks_batch_verified(self):
        class Corrupt(Provider):
            def fetch(self, *args, **kwargs):
                rows = super().fetch(*args, **kwargs)
                rows[0]['vector'] = [0, 1]
                return rows
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'local', 'REVEAL_ARTIFACT_STORE': 'filesystem'}):
            repo = Repository(Path(directory) / 'registry.sqlite3'); repo.migrate(); registry = VectorRegistry(repo)
            manifest, provider, _ = fixture()
            manifest.update(status='loading', export_ref={}, verified_batches={})
            manifest['batches'] = {'factors:0': [row['id'] for row in manifest['factors']]}
            rows = [{'kind': 'factors', **row, 'vector': provider.rows[manifest['factor_namespace']][row['id']]['vector']} for row in manifest['factors']]
            registry.register(save_export(manifest, rows, directory))
            with self.assertRaises(VectorUnavailable): import_batch(registry, manifest['snapshot_id'], 'factors:0', client=Corrupt())
            self.assertEqual(registry.get(manifest['snapshot_id'])['verified_batches'], {})


if __name__ == '__main__': unittest.main()
