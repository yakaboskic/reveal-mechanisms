"""Provider-independent contract and failure tests for ANN retrieval over the fixed reference namespaces."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from reveal_backend.catalog import Catalog, check_vector_readiness
from reveal_backend.auth import Problem
from reveal_backend.vector_retrieval import (UpstashFactorIndex, VectorUnavailable, cosine_score, environment, namespace,
    retrieve_native, text_sha256)

RELEASE = 'e' * 64


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


def fixture(*, many=False, stale=()):
    """Served factor ids (vector ids) mapped to native ids; `stale` vectors are in the namespace but not served."""
    vectors = [('a', 'one', [1., 0.]), ('b', 'two', [0., 1.]), ('alias', 'two', [-1., 0.]), ('c', 'three', [.8, .6])]
    if many: vectors = [(str(i), 'one', [1., 0.]) for i in range(45)] + [('other', 'two', [.6, .8])]
    client, records = Provider(), {}
    for identity, native, vector in vectors:
        records[identity] = {'source_id': native}
        client.upsert([{'id': identity, 'vector': vector, 'metadata': {'kind': 'factor', 'vector_sha256': 'sha-' + identity}}], namespace='local-factors')
    for identity, vector in stale:
        client.upsert([{'id': identity, 'vector': vector, 'metadata': {'kind': 'factor'}}], namespace='local-factors')
    client.upsert([{'id': text_sha256('description'), 'vector': [1., 0.], 'metadata': {'kind': 'context'}}], namespace='local-contexts')
    index = UpstashFactorIndex([identity for identity, _, _ in vectors], dimensions=2, release_id=RELEASE, environment_name='local', client=client)
    return index, client, records


class RetrievalTests(unittest.TestCase):
    def test_namespaces_are_fixed_per_vector_environment(self):
        self.assertEqual([namespace(kind, 'qa') for kind in ('factors', 'contexts')], ['qa-factors', 'qa-contexts'])
        with self.assertRaises(KeyError): namespace('gene_sets', 'qa')  # gene sets are not embedded
        with patch.dict('os.environ', {'REVEAL_VECTOR_ENVIRONMENT': 'prod'}):
            self.assertEqual(environment(), 'prod')
            index = UpstashFactorIndex(['a'], dimensions=2, release_id=RELEASE, client=Provider())
            self.assertEqual((index.factor_namespace, index.context_namespace), ('prod-factors', 'prod-contexts'))
        for invalid in ('Prod', 'qa;drop', '', 'x' * 30):
            with self.assertRaises(VectorUnavailable): namespace('factors', invalid or '0')
        with self.assertRaises(VectorUnavailable): UpstashFactorIndex(['a'], dimensions=None, release_id=RELEASE, environment_name='local', client=Provider())

    def test_batch_queries_respect_aggregate_provider_read_limit(self):
        index, provider, _ = fixture()
        rows = index.candidates([[1., 0.]] * 24, 100)
        self.assertEqual(len(rows), 24)
        self.assertEqual(provider.query_batches, [1000, 1000, 400])
        # No snapshot filter: the environment's namespace is the served corpus.
        self.assertTrue(all('filter' not in query and namespace == 'local-factors' for namespace, query in provider.queries))

    def test_catalog_readiness_provider_failure_is_explicit_503(self):
        index, provider, _ = fixture()
        with patch.object(provider, 'info', side_effect=TimeoutError('provider unavailable')):
            with self.assertRaises(Problem) as failure: check_vector_readiness(index)
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(failure.exception.code, 'SEMANTIC_SEARCH_UNAVAILABLE')

    def test_readiness_checks_dimension_metric_and_a_nonempty_factor_namespace_only(self):
        index, provider, _ = fixture(stale=[('KPN.TRAIT:0000009::Factor9', [0., -1.])])
        index.check()  # Vector counts need not equal the served factor count.
        for changes in ({'dimension': 3}, {'similarity_function': 'EUCLIDEAN'}):
            info = provider.info()
            with patch.object(provider, 'info', return_value=SimpleNamespace(**{**vars(info), **changes})):
                with self.assertRaises(VectorUnavailable): index.check()
        provider.rows.pop('local-factors')
        with self.assertRaisesRegex(VectorUnavailable, 'empty'): index.check()

    def test_score_conversion_rejects_invalid_provider_values(self):
        self.assertEqual([cosine_score(x) for x in (0, .5, 1)], [-1, 0, 1])
        for value in (True, None, float('nan'), 1.1):
            with self.assertRaises(VectorUnavailable): cosine_score(value)

    def test_all_aliases_and_nonwinning_context_scores_are_real(self):
        index, client, records = fixture()
        rows = retrieve_native(index, records, np.array([[1., 0.], [0., 1.], [-1., 0.]]), 3)
        by_native = {row['record']['source_id']: row for row in rows}
        np.testing.assert_array_equal(by_native['two']['scores'], [0., 1., 1.])
        retrieval = by_native['one']['retrieval']
        self.assertEqual((retrieval['score_conversion'], retrieval['release_id'], retrieval['mapping_run_id'], retrieval['embedding_run_id']),
                         ('2 * provider_score - 1', RELEASE, RELEASE, RELEASE))
        self.assertEqual((retrieval['factor_namespace'], retrieval['context_namespace']), ('local-factors', 'local-contexts'))
        self.assertEqual(by_native['two']['retrieval']['aliases'], [{'id': 'b', 'vector_sha256': 'sha-b'}, {'id': 'alias', 'vector_sha256': 'sha-alias'}])
        self.assertTrue(all(ns == 'local-factors' for ns, _ in client.queries))

    def test_ids_the_release_does_not_serve_are_skipped_not_raised(self):
        stale = [('KPN.TRAIT:%07d::Factor1' % number, [1., 0.]) for number in range(40)]
        index, client, records = fixture(stale=stale)
        rows = index.candidates([[1., 0.]], 32)
        self.assertEqual(rows[0].returned, 32)
        self.assertTrue(all(row['factor_id'] in records for row in rows[0]))
        result = retrieve_native(index, records, [[1., 0.]], 3)
        self.assertEqual([row['record']['source_id'] for row in result], ['one', 'three', 'two'])
        # Skipped ids took provider slots, so the depth grew past the served factor count to find every native.
        self.assertGreater(result[0]['retrieval']['candidate_depth'], len(index.factors))
        self.assertTrue(all(identity in records for _, ids in client.fetches for identity in ids))

    def test_duplicate_alias_underfill_expands_before_cutoff(self):
        index, client, records = fixture(many=True)
        rows = retrieve_native(index, records, [[1., 0.]], 2)
        self.assertEqual([row['record']['source_id'] for row in rows], ['one', 'two'])
        self.assertEqual([query['top_k'] for _, query in client.queries], [32, 46])
        rows = retrieve_native(fixture(many=True)[0], records, [[1., 0.]], 1, exclude=['one'])
        self.assertEqual(rows[0]['record']['source_id'], 'two')

    def test_fetch_skips_missing_vectors_and_rejects_invalid_values(self):
        index, client, _ = fixture()
        client.rows['local-factors'].pop('c')
        fetched = index.fetch_vectors(['a', 'c', 'not-served'])
        self.assertEqual(list(fetched), ['a'])
        self.assertEqual(client.fetches[-1], ('local-factors', ['a', 'c']))
        np.testing.assert_array_equal(fetched['a'], [1., 0.])
        client.rows['local-factors']['a']['vector'] = [1., 0., 0.]
        with self.assertRaises(VectorUnavailable): index.fetch_vectors(['a'])

    def test_provider_failure_never_falls_back_to_exact_or_empty_result(self):
        index, client, records = fixture()
        client.fail = True
        with self.assertRaises(VectorUnavailable): retrieve_native(index, records, [[1., 0.]], 3)
        with self.assertRaises(VectorUnavailable): index.fetch_vectors(['a'])

    def test_context_vectors_are_fetched_by_text_hash_and_misses_embedded_live(self):
        index, client, _ = fixture()
        calls = []
        def embed(texts, **kwargs):
            calls.append(texts); return np.asarray([[0., 2.] for _ in texts])
        vectors = index.context_vectors(['description', 'novel source text', 'description'], embedder=embed)
        np.testing.assert_array_equal(vectors, [[1., 0.], [0., 1.], [1., 0.]])
        self.assertEqual(client.fetches, [('local-contexts', [hashlib.sha256(b'description').hexdigest(),
                                                              hashlib.sha256(b'novel source text').hexdigest()])])
        self.assertEqual(calls, [['novel source text']])
        index.context_vectors(['novel source text', 'description'], embedder=embed)
        self.assertEqual((len(client.fetches), len(calls)), (1, 1))  # Cached in process.

    def test_catalog_suggestions_and_hybrid_use_shared_provider(self):
        index, client, records = fixture()
        catalog = Catalog(); catalog.loaded = True; catalog.index = index
        catalog.factor_legacy = records
        catalog.factors = {record['source_id']: {**record, 'cfde_anchor': {'label': 'query'}} for record in records.values()}
        with patch.object(catalog, 'runtime_query_vectors', return_value=np.array([[1., 0.], [0., 1.]])):
            rows = catalog.suggest_factors([('x', 'x'), ('y', 'y')], 'semantic', 3, [])
        self.assertTrue(all('context_similarities' in row for row in rows))
        self.assertTrue(client.queries)
        before = len(client.queries)
        catalog.search_factors('query', 'hybrid', 3, query_vector=np.array([1., 0.]))
        self.assertGreater(len(client.queries), before)
        with patch.object(catalog, 'runtime_query_vectors', return_value=np.array([[1., 0.]])):
            hybrid = catalog.suggest_factors([('x', 'query')], 'hybrid', 3, [])
        self.assertEqual(hybrid[0]['retrieval']['release_id'], RELEASE)
        self.assertTrue(all('context_similarities' in row for row in hybrid))


if __name__ == '__main__': unittest.main()
