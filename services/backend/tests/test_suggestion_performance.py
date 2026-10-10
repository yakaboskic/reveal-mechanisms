"""Query reuse must preserve scientific retrieval and keep the API responsive."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from reveal_backend import app as api
from reveal_backend import catalog as catalog_module
from reveal_backend.auth import Problem
from reveal_backend.catalog import Catalog
from reveal_backend.eaggl_bundle import text_hash
from reveal_backend.eaggl_embeddings import FactorSearchIndex, QueryVectorCache, vector_hash
from reveal_backend.vector_retrieval import UpstashFactorIndex

FACTOR_VECTORS = [('a', [1, 0]), ('b', [0, 1]), ('c', [.8, .6]), ('d', [0, -1]), ('alias', [-1, 0])]
RELEASE = text_hash('test release')


def make_index():
    factors, rows = [], []
    for identity, vector in [('a', [1, 0]), ('b', [0, 1]), ('c', [.8, .6]), ('d', [0, -1]), ('alias', [-1, 0])]:
        checksum = text_hash(identity)
        blob = np.asarray(vector, dtype='<f4').tobytes()
        factors.append({'factor_id': identity, 'label': identity, 'trait': 'trait', 'input_sha256': checksum})
        rows.append((checksum, identity, blob, vector_hash(blob)))
    run = {'run_id': 'run-1', 'dimensions': 2, 'config': {'model': 'model', 'model_revision': 'revision-1',
        'provider': 'test', 'service_url': 'https://example.invalid', 'import_id': 'import-1', 'template': 'template-1'}}
    return FactorSearchIndex(factors, run, rows)


def embedding(texts, **kwargs):
    values = {'alpha': [1, 0], 'beta': [0, 1], 'gamma': [-1, 0]}
    return np.asarray([values[text] for text in texts], dtype=np.float64)


class VectorProvider:
    """Upstash Vector stand-in over the environment's fixed namespaces; scores are (1 + cosine) / 2."""
    def __init__(self, namespaces): self.rows, self.fetches = namespaces, []
    def fetch(self, ids, namespace, **kwargs):
        self.fetches.append((namespace, list(ids)))
        return [deepcopy(self.rows.get(namespace, {}).get(identity)) for identity in ids]
    def query_many(self, queries, namespace):
        result = []
        for query in queries:
            q = np.asarray(query['vector'], dtype=np.float64); q /= np.linalg.norm(q)
            rows = [dict(row, score=float((1 + np.asarray(row['vector'], dtype=np.float64) / np.linalg.norm(row['vector']) @ q) / 2))
                    for row in self.rows.get(namespace, {}).values()]
            result.append(sorted(rows, key=lambda row: (-row['score'], row['id']))[:query['top_k']])
        return result


def make_release_index(contexts=None):
    """The release index over `test-factors` and `test-contexts` (context vectors keyed by their text's sha256)."""
    namespaces = {'test-factors': {identity: {'id': identity, 'vector': vector, 'metadata': {'kind': 'factor'}} for identity, vector in FACTOR_VECTORS},
                  'test-contexts': {text_hash(text): {'id': text_hash(text), 'vector': vector, 'metadata': {'kind': 'context'}}
                                    for text, vector in (contexts or {}).items()}}
    return UpstashFactorIndex([identity for identity, _ in FACTOR_VECTORS], dimensions=2, release_id=RELEASE, environment_name='test',
                              client=VectorProvider(namespaces))


class QueryCacheTests(unittest.TestCase):
    def test_imported_label_vectors_are_reused_before_runtime_cache(self):
        index = make_index()
        with patch('reveal_backend.eaggl_embeddings.get_embeddings', side_effect=embedding) as embedder:
            known = index.query_vectors(['c', 'a', 'c'], embedder=embedder)
            expected = index.matrix[[index.by_id[value] for value in ['c', 'a', 'c']]]
            np.testing.assert_array_equal(known, expected)
            embedder.assert_not_called()
            known[0] = [-99, -99]
            mixed = index.query_vectors(['c', 'alpha', 'a', 'alpha'], embedder=embedder)
            np.testing.assert_array_equal(mixed, np.stack([expected[0], [1, 0], [1, 0], [1, 0]]))
            self.assertEqual(embedder.call_args.args[0], ['alpha'])
            index.query_vectors(['alpha', 'c'], embedder=embedder)
            self.assertEqual(embedder.call_count, 1)

    def test_duplicate_and_overlapping_queries_preserve_order_and_cannot_mutate_cache(self):
        index = make_index()
        with patch('reveal_backend.eaggl_embeddings.get_embeddings', side_effect=embedding) as embedder:
            first = index.query_vectors(['beta', 'alpha', 'beta'], embedder=embedder)
            np.testing.assert_array_equal(first, [[0, 1], [1, 0], [0, 1]])
            self.assertEqual(embedder.call_args.args[0], ['beta', 'alpha'])
            first[0] = [-999, -999]
            second = index.query_vectors(['gamma', 'beta'], embedder=embedder)
            np.testing.assert_array_equal(second, [[-1, 0], [0, 1]])
            self.assertEqual(embedder.call_args.args[0], ['gamma'])
            self.assertEqual(embedder.call_count, 2)
            index.search(query='alpha', embedder=embedder)
            self.assertEqual(embedder.call_count, 2)

    def test_overlapping_concurrent_misses_do_not_duplicate_work_or_deadlock(self):
        index = make_index()
        first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def slow(texts, **kwargs):
            calls.append(texts)
            if texts == ['alpha', 'beta']:
                first_entered.set()
                self.assertTrue(release.wait(2))
            else:
                self.assertEqual(texts, ['gamma'])
                second_entered.set()
            return embedding(texts)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(index.query_vectors, ['alpha', 'beta'], embedder=slow)
            self.assertTrue(first_entered.wait(2))
            second = pool.submit(index.query_vectors, ['beta', 'gamma'], embedder=slow)
            self.assertTrue(second_entered.wait(2))
            release.set()
            np.testing.assert_array_equal(first.result(timeout=2), [[1, 0], [0, 1]])
            np.testing.assert_array_equal(second.result(timeout=2), [[0, 1], [-1, 0]])
        self.assertEqual(calls, [['alpha', 'beta'], ['gamma']])

    def test_failed_batch_reaches_waiters_without_poisoning_retry(self):
        index = make_index()
        first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()

        def failing(texts, **kwargs):
            if texts == ['alpha', 'beta']:
                first_entered.set()
                self.assertTrue(release.wait(2))
                raise TimeoutError('temporary service failure')
            second_entered.set()
            return embedding(texts)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(index.query_vectors, ['alpha', 'beta'], embedder=failing)
            self.assertTrue(first_entered.wait(2))
            second = pool.submit(index.query_vectors, ['beta', 'gamma'], embedder=failing)
            self.assertTrue(second_entered.wait(2))
            release.set()
            for future in (first, second):
                with self.assertRaises(TimeoutError):
                    future.result(timeout=2)
        calls = []

        def retry(texts, **kwargs):
            calls.append(texts)
            return embedding(texts)

        np.testing.assert_array_equal(index.query_vectors(['alpha', 'beta', 'gamma'], embedder=retry), [[1, 0], [0, 1], [-1, 0]])
        self.assertEqual(calls, [['alpha', 'beta']])

    def test_ttl_lru_and_complete_configuration_invalidate(self):
        index = make_index()
        index.query_cache = QueryVectorCache(max_entries=2, ttl_seconds=10)
        calls = []

        def embedder(texts, **kwargs):
            calls.append(texts)
            return embedding(texts)

        with patch('reveal_backend.eaggl_embeddings.time.monotonic', return_value=0):
            index.query_vectors(['alpha', 'beta'], embedder=embedder)
            index.query_vectors(['alpha'], embedder=embedder)
            index.query_vectors(['gamma'], embedder=embedder)
            index.query_vectors(['alpha'], embedder=embedder)
            index.query_vectors(['beta'], embedder=embedder)
        self.assertEqual(calls, [['alpha', 'beta'], ['gamma'], ['beta']])
        with patch('reveal_backend.eaggl_embeddings.time.monotonic', return_value=11):
            index.query_vectors(['beta'], embedder=embedder)
            for field in ['model_revision', 'model', 'provider', 'service_url', 'import_id', 'template']:
                index.run['config'][field] += '-changed'
                index.query_vectors(['beta'], embedder=embedder)
            index.run['run_id'] = 'run-2'
            index.query_vectors(['beta'], embedder=embedder)
        self.assertEqual(len(calls), 11)
        self.assertLessEqual(len(index.query_cache.values), 2)

    def test_bad_vectors_are_never_cached_and_float64_precision_is_preserved(self):
        index = make_index()
        for raw in [[[0, 0]], [[float('nan'), 1]], [['1', '2']], [[1, 2, 3]], [[1, 0], [0, 1]]]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                index.query_vectors(['alpha'], embedder=lambda *args, **kwargs: raw)
            self.assertFalse(index.query_cache.values)
            self.assertFalse(index.query_cache.pending)
        raw = np.array([[1, 1 + 1e-10]], dtype=np.float64)
        expected = raw / np.linalg.norm(raw, axis=1, keepdims=True)
        actual = index.query_vectors(['alpha'], embedder=lambda *args, **kwargs: raw)
        np.testing.assert_array_equal(actual, expected)
        self.assertNotEqual(actual[0, 0], actual[0, 1])


class SuggestionRankingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.catalog.loaded = True
        self.catalog.index = make_release_index()
        self.catalog.factor_legacy = {
            legacy: {'source_id': native, 'source_revision': 'exact-source-' + native}
            for legacy, native in [('a', 'native:Z'), ('b', 'native:Y'), ('c', 'native:A'), ('d', 'native:X'), ('alias', 'native:Y')]}
        self.catalog.factors = {record['source_id']: record for record in self.catalog.factor_legacy.values()}

    def test_cached_ranking_keeps_context_ties_native_dedup_exclusions_and_revisions(self):
        contexts = [('context-alpha', 'alpha'), ('context-beta', 'beta'), ('context-beta-again', 'beta')]
        with patch.object(catalog_module, 'get_embeddings', side_effect=embedding) as embedder:
            original = self.catalog.suggest_factors(contexts, 'semantic', 5, ())
            replay = self.catalog.suggest_factors(contexts, 'semantic', 5, ())
            self.assertEqual(original, replay)
            self.assertEqual([item['record']['source_id'] for item in original], ['native:Y', 'native:Z', 'native:A', 'native:X'])
            self.assertEqual(original[0]['contexts'], ['context-beta', 'context-beta-again'])
            self.assertEqual(original[1]['contexts'], ['context-alpha'])
            self.assertEqual(original[0]['ranking']['value'], 1)
            self.assertEqual(original[0]['record']['source_revision'], 'exact-source-native:Y')
            limited = self.catalog.suggest_factors(contexts, 'semantic', 2, {'native:Y'})
            self.assertEqual([item['record']['source_id'] for item in limited], ['native:Z', 'native:A'])
            self.assertEqual([item['ranking']['rank'] for item in limited], [1, 2])
            self.assertEqual(embedder.call_count, 1)
            self.assertEqual(embedder.call_args.args[0], ['alpha', 'beta'])
            self.assertEqual(self.catalog.suggest_factors(contexts, 'semantic', 0, ()), [])
            self.assertEqual(embedder.call_count, 1)

    def test_invalid_service_response_keeps_actionable_error_code(self):
        with patch.object(catalog_module, 'get_embeddings', return_value=[[0, 0]]):
            with self.assertRaises(Problem) as failure:
                self.catalog.suggest_factors([('context', 'alpha')], 'semantic', 5, ())
        self.assertEqual(failure.exception.code, 'EMBEDDING_UNAVAILABLE')


class SuggestionResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_preparation_and_audit_do_not_block_event_loop(self):
        root = Path(__file__).resolve().parents[3]
        body = json.loads((root / 'api/examples/suggestMechanisms.dismech_context.json').read_text())['request']['body']
        gap = next(iter(json.loads((root / 'api/examples/getKnowledgeGap.request.json').read_text())['responses']['200']['examples'].values()))
        loop_thread = threading.get_ident()
        stages, rows = [], []

        def delay(stage):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            stages.append(stage)
            time.sleep(.035)

        class Request:
            async def json(self): return body

        class Source:
            mechanisms = {}
            embedding_run, mapping_run = 'embedding', 'mapping'
            def selected(self, reference): delay('source'); return gap
            def suggest_factors(self, *args, **kwargs): return []
            def context_embedding_provenance(self, *args): return {'dismech_embedding_run_id': 'context-run'}
            def provenance(self, *args): return {}

        class Repository:
            def append(self, *record): delay('append'); rows.append(record)   # committed before the response

        ticks = 0
        with patch.object(api, 'catalog', Source()), patch.object(api, 'repo', Repository()):
            task = asyncio.create_task(api.suggest(Request()))
            while not task.done():
                ticks += 1
                await asyncio.sleep(.005)
            result = await task
        self.assertGreaterEqual(ticks, 6)
        self.assertEqual(stages, ['source', 'append'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ('suggestion', result['suggestion_id'], 'catalog'))
        self.assertEqual(rows[0][3]['mapping_run_id'], 'mapping')


if __name__ == '__main__':
    unittest.main()
