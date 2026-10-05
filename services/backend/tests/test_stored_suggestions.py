"""Automatic source-context retrieval reads the stored `<env>-contexts` vectors; only misses reach the model service."""
import unittest
from unittest.mock import patch

import numpy as np

import test_suggestion_performance as fixtures
import test_dismech_embeddings as import_fixtures
from reveal_backend import catalog as catalog_module
from reveal_backend.auth import Problem
from reveal_backend.catalog import Catalog
from reveal_backend.dismech_embeddings import TEMPLATES, context_input
from reveal_backend import dismech_embeddings


def without_retrieval(rows):
    """Suggestions without their retrieval provenance, which names how each context vector was obtained."""
    return [{key: value for key, value in row.items() if key != 'retrieval'} for row in rows]


class StoredSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.catalog.loaded = True
        self.catalog.release_id = fixtures.RELEASE
        self.catalog.index = fixtures.make_release_index({text: vector for text, vector in zip(('alpha', 'beta', 'gamma'), fixtures.embedding(['alpha', 'beta', 'gamma']).tolist())})
        self.provider = self.catalog.index.client
        self.catalog.dismech_import = 'dismech-import'
        self.catalog.factor_legacy = {legacy: {'source_id': native, 'source_revision': 'original-revision',
            'cfde_anchor': {'label': label}} for legacy, native, label in [
                ('a', 'native:Z', 'alpha'), ('b', 'native:Y', 'beta'), ('c', 'native:A', 'alpha beta'),
                ('d', 'native:X', 'gamma'), ('alias', 'native:Y', 'beta')]}
        self.catalog.factors = {row['source_id']: row for row in self.catalog.factor_legacy.values()}
        self.catalog.mechanisms = {identity: {'source_id': identity, 'source_revision': revision,
            'object': {'description': text}} for identity, revision, text in [
                ('source:alpha', 'a' * 64, 'alpha'), ('source:beta', 'b' * 64, 'beta')]}
        self.catalog.gaps = {'dapper:gap': {'object': {'id': 'dapper:gap', 'text': 'gamma'},
            'source': {'source_id': 'source:gap', 'source_revision': 'c' * 64}}}

    def test_linked_semantic_and_hybrid_match_dynamic_ranking_without_any_http(self):
        contexts = [('source:alpha', 'alpha'), ('source:beta', 'beta')]
        for mode in ('semantic', 'hybrid'):
            with self.subTest(mode=mode):
                with patch.object(self.catalog.index, 'query_vectors', side_effect=lambda texts, **kwargs: fixtures.embedding(texts)), \
                     patch.object(catalog_module, 'get_embeddings', side_effect=fixtures.embedding):
                    before = self.catalog.suggest_factors(contexts, mode, 5, ())
                with patch.object(self.catalog.index, 'query_vectors', side_effect=AssertionError('Automatic context used runtime embedding')), \
                     patch.object(catalog_module, 'get_embeddings', side_effect=AssertionError('Automatic context used HTTP')):
                    after = self.catalog.suggest_factors(contexts, mode, 5, (), precomputed=True)
                    excluded = self.catalog.suggest_factors(contexts, mode, 2, {'native:Y'}, precomputed=True)
                self.assertEqual(without_retrieval(before), without_retrieval(after))
                self.assertEqual(len({row['record']['source_id'] for row in after}), len(after))
                self.assertNotIn('native:Y', {row['record']['source_id'] for row in excluded})
                self.assertEqual(len(excluded), 2)
                self.assertEqual([row['ranking']['rank'] for row in excluded], [1, 2])

    def test_unlinked_gap_fallback_uses_the_stored_exact_question_vector(self):
        with patch.object(self.catalog.index, 'query_vectors', side_effect=AssertionError('Fallback must not embed')), \
             patch.object(catalog_module, 'get_embeddings', side_effect=AssertionError('Fallback must not call HTTP')):
            for mode in ('semantic', 'hybrid'):
                result = self.catalog.suggest_factors([('dapper:gap', 'gamma')], mode, 5, (), precomputed=True)
                self.assertTrue(result)
                self.assertEqual(result[0]['contexts'], ['dapper:gap'])
        provenance = self.catalog.context_embedding_provenance([('dapper:gap', 'gamma')])
        self.assertEqual(provenance['dismech_embedding_run_id'], fixtures.RELEASE)
        self.assertEqual(provenance['context_embedding_inputs'][0], {
            key: value for key, value in context_input('source:gap', 'knowledge_gap', 'c' * 64, 'gamma').items() if key != 'input_text'})
        self.assertEqual(provenance['context_embedding_inputs'][0]['template'], 'dismech-gap-text-v1')

    def test_pairwise_cosines_survive_maximum_selection_and_hybrid_ranking(self):
        contexts = [('source:alpha', 'alpha'), ('source:beta', 'beta')]
        with patch.object(catalog_module, 'get_embeddings', side_effect=AssertionError('No model call expected')):
            for mode in ('semantic', 'hybrid'):
                with self.subTest(mode=mode):
                    hits = {item['record']['source_id']: item for item in
                            self.catalog.suggest_factors(contexts, mode, 5, (), precomputed=True)}
                    self.assertEqual(hits['native:Z']['context_similarities'], {'source:alpha': 1., 'source:beta': 0.})
                    # Deduplicated native aliases retain the best cosine for
                    # each context, including zero and negative measurements.
                    self.assertEqual(hits['native:Y']['context_similarities'], {'source:alpha': 0., 'source:beta': 1.})
                    self.assertEqual(hits['native:X']['context_similarities'], {'source:alpha': 0., 'source:beta': -1.})
                    if mode == 'hybrid':
                        self.assertEqual(hits['native:Z']['ranking']['metric'], 'reciprocal_rank_fusion')
                        self.assertNotEqual(hits['native:Z']['ranking']['value'], 1.)

    def test_lexical_suggestions_do_not_acquire_semantic_scores(self):
        hits = self.catalog.suggest_factors([('source:alpha', 'alpha')], 'lexical', 5, (), precomputed=True)
        self.assertTrue(hits)
        self.assertTrue(all('context_similarities' not in hit for hit in hits))

    def test_a_context_without_a_stored_vector_is_embedded_live_once(self):
        self.provider.rows['test-contexts'].pop(fixtures.text_hash('beta'))
        contexts = [('source:alpha', 'alpha'), ('source:beta', 'beta')]
        with patch.object(catalog_module, 'get_embeddings', side_effect=fixtures.embedding) as embedder:
            first = self.catalog.suggest_factors(contexts, 'semantic', 5, (), precomputed=True)
            second = self.catalog.suggest_factors(contexts, 'semantic', 5, (), precomputed=True)
        self.assertEqual(first, second)
        self.assertEqual(embedder.call_count, 1)
        self.assertEqual(embedder.call_args.args[0], ['beta'])
        self.assertEqual(len([namespace for namespace, _ in self.provider.fetches if namespace == 'test-contexts']), 1)  # Cached in process.
        with self.assertRaises(Problem) as failure:
            self.catalog.suggest_factors([('source:unknown', 'alpha')], 'semantic', 5, (), precomputed=True)
        self.assertEqual(failure.exception.code, 'DISMECH_EMBEDDINGS_NOT_READY')

    def test_free_text_is_embedded_live_once_and_cached(self):
        with patch.object(catalog_module, 'get_embeddings', return_value=np.asarray([[1., 1.]])) as embedder:
            first = self.catalog.suggest_factors([('mechanism_subquery', 'novel text')], 'semantic', 5, ())
            second = self.catalog.suggest_factors([('mechanism_subquery', 'novel text')], 'semantic', 5, ())
            self.assertEqual(first, second)
            self.assertEqual(embedder.call_count, 1)
            self.assertEqual(embedder.call_args.args[0], ['novel text'])
            self.catalog.search_factors('beta', 'semantic')
            self.assertEqual(embedder.call_count, 2)
            self.assertEqual(embedder.call_args.args[0], ['beta'])

    def test_frozen_context_provenance_has_native_revision_and_hash_for_each_context(self):
        result = self.catalog.context_embedding_provenance([('source:beta', 'beta'), ('source:alpha', 'alpha')])
        self.assertEqual(result['dismech_import_id'], 'dismech-import')
        self.assertEqual(result['context_embedding_templates'], TEMPLATES)
        self.assertEqual([row['source_id'] for row in result['context_embedding_inputs']], ['source:beta', 'source:alpha'])
        self.assertEqual([row['source_revision'] for row in result['context_embedding_inputs']], ['b' * 64, 'a' * 64])
        self.assertTrue(all(row['input_sha256'] and 'input_text' not in row for row in result['context_embedding_inputs']))


class BoundedEmbeddingTests(import_fixtures.CaptureFixture):
    def test_length_buckets_can_pause_after_durable_group_and_resume_same_run(self):
        manifest = self.prepare()
        seen = []

        def observed(texts, **kwargs):
            seen.extend(text for text in texts if text not in import_fixtures.CALIBRATION_TEXTS)
            return import_fixtures.embed(texts)

        partial = dismech_embeddings.embed_capture(self.output, batch_size=1, max_workers=1, max_batches=1, embedder=observed)
        self.assertEqual(partial['status'], 'embedding')
        self.assertEqual(partial['embedded_now'], 1)
        self.assertEqual(partial['remaining_vectors'], 1)
        self.assertEqual(seen, [min([import_fixtures.MECHANISM_TEXT, import_fixtures.GAP_TEXT], key=len)])
        with self.assertRaises(ValueError):
            dismech_embeddings.read_local_vectors(self.output)
        resumed = dismech_embeddings.embed_capture(self.output, batch_size=1, max_workers=1, max_batches=1, embedder=observed)
        self.assertEqual(resumed['status'], 'complete')
        self.assertEqual(resumed['run_id'], manifest['run_id'])
        self.assertEqual(resumed['embedded_now'], 1)
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(len(dismech_embeddings.read_local_vectors(self.output)[2]), 2)


if __name__ == '__main__':
    unittest.main()
