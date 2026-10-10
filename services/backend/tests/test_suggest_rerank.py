"""Jev ordering of automatic suggestions: request content, ranking, fallback, cache and audit."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from reveal_backend import app as api
from reveal_backend import gap_rerank
from reveal_backend.dismech_import import canonical
from reveal_backend.jev_batch import JevCallError, validate_questions

ROOT = Path(__file__).resolve().parents[3]
BODY = json.loads((ROOT / 'api/examples/suggestMechanisms.dismech_context.json').read_text())['request']['body'] | {'subquery': ''}
ON = {'REVEAL_SUGGEST_RERANK': 'jev', 'TYPESAFE_API_KEY': 'isolated-test-key'}
OFF = {'REVEAL_SUGGEST_RERANK': 'off', 'TYPESAFE_API_KEY': 'isolated-test-key'}


def native(number): return f'factor:kpn:0000001:eaggl-capped-v1:Factor{number}'


def record(number, label):
    return {'source_id': native(number), 'source_revision': 'a' * 64, 'object': {'name': f'CAD mechanism Factor{number}'},
            'cfde_anchor': {'label': label, 'subtitle': f'Coronary artery disease (Factor{number})'},
            'kpn_trait': {'name': 'Coronary artery disease'}}


def answer(score):
    probabilities = {str(level): 0. for level in range(5)}; probabilities[str(round(score))] = 1.
    return {'type': 'score', 'score': score, 'confidence': .9, 'probabilities': probabilities}


class Jev:
    """A SystemOne stand-in that rates each factor by its label: {label: (relevance, addresses)}."""
    def __init__(self, ratings, *, model=gap_rerank.MODEL, error=None, delay=0):
        self.ratings, self.model, self.error, self.delay, self.bodies = ratings, model, error, delay, []

    def __call__(self, content, *, api_key, deadline, max_response_bytes):
        assert api_key == 'isolated-test-key'
        body = json.loads(content); self.bodies.append(body)
        if self.delay: time.sleep(self.delay)
        if self.error: raise JevCallError(self.error)
        answers = {}
        for key, question in body['questions'].items():
            label = re.search(r'EAGGL factor "([^"]*)"', question['instructions']).group(1)
            answers[key] = answer(self.ratings[label][0 if key.startswith('relevance:') else 1])
        return {'model': self.model, 'answers': answers, 'usage': {'input_tokens': 100, 'output_tokens': 10}}

    def sent(self, bodies=None):
        return sorted(re.search(r'"([^"]*)"', question['instructions']).group(1) for body in (self.bodies if bodies is None else bodies)
                      for key, question in body['questions'].items() if key.startswith('relevance:'))


class Catalog:
    """Exact cosine and disease-identity stand-ins with the build_suggestions surface."""
    embedding_run, mapping_run, dismech_import = 'embedding', 'mapping', 'dismech-import'

    def __init__(self, cosine, disease=()):
        self.cosine, self.disease = cosine, list(disease)
        labels = {number: label for number, label, _ in cosine} | {number: label for number, label in self.disease}
        self.records = {native(number): record(number, label) for number, label in labels.items()}
        self.factors = self.records; self.bindings = {source_id: {'binding': source_id} for source_id in self.records}
        self.mechanisms = {'dismech:mechanism': {'source_id': 'dismech:mechanism',
            'object': {'name': 'Endothelial dysfunction', 'description': 'Impaired nitric oxide signalling in the vessel wall.'}}}
        self.gap = {'object': {'id': BODY['source_gap']['id'], 'text': 'Does endothelial dysfunction drive plaque formation?',
                               'gap_description': 'Causal direction between endothelial injury and plaque is unresolved.'},
                    'source': {'disease_label': 'Coronary Artery Disease'},
                    'attachments': [{'target': {'source_id': 'dismech:mechanism'}}]}
        self.suggest_calls, self.disease_calls = [], []

    def selected(self, reference): return self.gap
    def validate_composer(self, composer): pass

    def disease_factors(self, gap, remaining, exclude):
        self.disease_calls.append(remaining)
        if remaining <= 0: return []
        rows = sorted(native(number) for number, _ in self.disease if native(number) not in exclude)[:remaining]
        return [{'record': self.records[source_id], 'ranking': {'value': 1, 'metric': 'eligible_disease_identity', 'rank': rank},
                 'contexts': [gap['object']['id']], 'reason': 'Pinned trait mapping matches the selected disease.',
                 'retrieval': {'strategy': 'disease_identity'}} for rank, source_id in enumerate(rows, 1)]

    def suggest_factors(self, contexts, mode, remaining, exclude, *, precomputed=False, index=None):
        self.suggest_calls.append(remaining)
        rows = [(native(number), value) for number, _, value in self.cosine if native(number) not in exclude][:remaining]
        identity = contexts[0][0]
        return [{'record': self.records[source_id], 'ranking': {'value': value, 'metric': 'cosine_similarity', 'rank': rank},
                 'contexts': [identity], 'context_similarities': {identity: value}, 'retrieval': {'strategy': 'exact'}}
                for rank, (source_id, value) in enumerate(rows, 1)]

    def context_embedding_provenance(self, contexts, **kwargs): return {'dismech_embedding_run_id': 'context-run'}
    def provenance(self, *args, **kwargs): return {}


class Repository:
    def __init__(self): self.rows = {}
    def append(self, kind, identity, owner, data): self.rows[(kind, identity)] = deepcopy(data)


# Cosine favours generic labels; Jev should promote the specific ones.
COSINE = [(1, 'coronary artery disease risk', .91), (2, 'blood pressure regulation', .88), (3, 'lipoprotein metabolism', .85),
          (4, 'endothelial nitric oxide signalling', .80), (5, 'immune cell trafficking', .78), (6, 'vascular smooth muscle contraction', .75),
          (7, 'hepatic lipid synthesis', .70), (8, 'platelet activation', .65)]
RATINGS = {'coronary artery disease risk': (1, 1), 'blood pressure regulation': (2, 1), 'lipoprotein metabolism': (2, 2),
           'endothelial nitric oxide signalling': (4, 4), 'immune cell trafficking': (3, 2), 'vascular smooth muscle contraction': (3, 3),
           'hepatic lipid synthesis': (1, 0), 'platelet activation': (2, 3), 'arterial wall remodelling': (4, 3), 'disease locus Factor20': (0, 0)}
DISEASE = [(20, 'disease locus Factor20'), (9, 'arterial wall remodelling')]


class RequestContentTests(unittest.TestCase):
    def test_jev_sees_only_gap_mechanisms_and_factor_label_and_trait(self):
        catalog = Catalog(COSINE)
        state = gap_rerank.state(catalog.gap, list(catalog.mechanisms.values()))
        self.assertEqual(state['knowledge_gap'], {'question': catalog.gap['object']['text'], 'disease': 'Coronary Artery Disease',
                                                  'rationale': catalog.gap['object']['gap_description']})
        self.assertEqual(state['dismech_mechanisms'], [catalog.mechanisms['dismech:mechanism']['object']])
        factors = [(source_id, gap_rerank.factor_view(row)) for source_id, row in catalog.records.items()]
        self.assertEqual(factors[0][1], {'label': 'coronary artery disease risk', 'trait': 'Coronary artery disease'})
        [(body, refs)] = gap_rerank.request_bodies(state, factors)
        validate_questions(body['questions'])
        self.assertEqual((body['model'], len(body['questions']), len(refs)), (gap_rerank.MODEL, 16, 8))
        self.assertEqual(set(refs.values()), set(catalog.records))
        encoded = json.dumps(body)
        for absent in ('factor:kpn', 'Factor1', 'loading', 'genes', 'source_revision'):
            self.assertNotIn(absent, encoded)

    def test_legacy_trait_comes_from_the_anchor_subtitle(self):
        self.assertEqual(gap_rerank.factor_view({'cfde_anchor': {'label': 'insulin secretion', 'subtitle': 'T2D (Factor3)'}}),
                         {'label': 'insulin secretion', 'trait': 'T2D'})

    def test_a_rationale_equal_to_the_question_is_not_repeated(self):
        gap = {'object': {'text': 'Same text', 'gap_description': 'Same text', 'scope': 'Asthma'}, 'source': {}}
        self.assertEqual(gap_rerank.state(gap, [])['knowledge_gap'], {'question': 'Same text', 'disease': 'Asthma'})

    def test_chunks_cover_every_factor_and_halve_to_fit_the_byte_cap(self):
        state = gap_rerank.state(Catalog(COSINE).gap, [])
        factors = [(f'f{number}', {'label': f'label {number}', 'trait': 'trait'}) for number in range(120)]
        bodies = gap_rerank.request_bodies(state, factors)
        self.assertEqual([len(refs) for _, refs in bodies], [50, 50, 20])
        self.assertEqual([factor for _, refs in bodies for factor in refs.values()], [factor for factor, _ in factors])
        single = len(canonical(gap_rerank.request_bodies(state, factors[:1])[0][0]).encode())
        small = gap_rerank.request_bodies(state, factors[:8], max_bytes=single * 3)
        self.assertGreater(len(small), 1)
        self.assertTrue(all(len(canonical(body).encode()) <= single * 3 for body, _ in small))
        self.assertEqual([factor for _, refs in small for factor in refs.values()], [factor for factor, _ in factors[:8]])
        with self.assertRaises(gap_rerank.Skip) as skipped:
            gap_rerank.request_bodies(state, factors[:1], max_bytes=single - 1)
        self.assertEqual(skipped.exception.reason, 'too_large')


class RankingTests(unittest.TestCase):
    def entries(self, catalog):
        return gap_rerank.pool(catalog.disease_factors(catalog.gap, 50, set()), catalog.suggest_factors([('m', '')], 'semantic', 100, set()))

    def scores(self, entries):
        return {entry['item']['record']['source_id']: {'relevance': answer(RATINGS[entry['item']['record']['cfde_anchor']['label']][0]),
                'addresses': answer(RATINGS[entry['item']['record']['cfde_anchor']['label']][1])} for entry in entries}

    def test_pool_deduplicates_a_factor_found_by_cosine_and_disease_identity(self):
        catalog = Catalog(COSINE, [(4, 'endothelial nitric oxide signalling'), (9, 'arterial wall remodelling')])
        entries = self.entries(catalog)
        self.assertEqual(len(entries), 9)
        merged = next(entry for entry in entries if entry['item']['record']['source_id'] == native(4))
        self.assertEqual((merged['cosine'], merged['cosine_rank'], bool(merged['disease'])), (.80, 4, True))
        only = entries[-1]
        self.assertEqual((only['item']['record']['source_id'], only['cosine'], only['cosine_rank']), (native(9), None, None))

    def test_jev_value_orders_the_pool_and_disease_identity_is_not_pinned(self):
        catalog = Catalog(COSINE, DISEASE); entries = self.entries(catalog)
        items = gap_rerank.rank(entries, self.scores(entries), 5)
        self.assertEqual([item['record']['source_id'] for item in items], [native(4), native(9), native(6), native(5), native(8)])
        top = items[0]
        self.assertEqual(top['ranking'], {'value': 1., 'metric': 'jev_gap_relevance', 'rank': 1})
        self.assertEqual((top['cosine_rank'], top['context_similarities'], top['jev']['relevance']['score']), (4, {'m': .80}, 4))
        self.assertIn('relevance 4.0/4', top['reason']); self.assertIn('not biological support', top['reason'])
        self.assertIn('no cosine retrieval', items[1]['reason']); self.assertIn('matches the selected disease', items[1]['reason'])
        self.assertNotIn(native(20), [item['record']['source_id'] for item in items])

    def test_ties_prefer_higher_cosine_then_native_id(self):
        catalog = Catalog([(1, 'a', .5), (2, 'b', .7), (3, 'c', .7)], [(4, 'd')]); entries = self.entries(catalog)
        tied = {entry['item']['record']['source_id']: {'relevance': answer(2), 'addresses': answer(2)} for entry in entries}
        self.assertEqual([item['record']['source_id'] for item in gap_rerank.rank(entries, tied, 4)],
                         [native(2), native(3), native(1), native(4)])

    def test_fallback_is_disease_identity_first_then_cosine(self):
        catalog = Catalog(COSINE, DISEASE + [(1, 'coronary artery disease risk')]); entries = self.entries(catalog)
        items = gap_rerank.fallback(entries, catalog.disease_factors(catalog.gap, 50, set()), 5)
        self.assertEqual([item['record']['source_id'] for item in items], [native(1), native(20), native(9), native(2), native(3)])

    def test_pool_rows_are_compact_and_record_unscored_factors(self):
        catalog = Catalog(COSINE[:2], DISEASE[:1]); entries = self.entries(catalog)
        scores = self.scores(entries[:1])
        self.assertEqual(gap_rerank.pool_rows(entries, scores),
                         [[native(1), .91, False, 1, 1], [native(2), .88, False, None, None], [native(20), None, True, None, None]])


class ScorePoolTests(unittest.TestCase):
    def setUp(self):
        self.cache = gap_rerank.ScoreCache()
        self.state = gap_rerank.state(Catalog(COSINE).gap, [])
        self.factors = [(native(number), {'label': label, 'trait': 'Coronary artery disease'}) for number, label, _ in COSINE]

    def score(self, jev, factors=None, timeout=5):
        with patch.object(gap_rerank, 'post_systemone', jev), patch.object(gap_rerank, 'FACTORS_PER_REQUEST', 3):
            return gap_rerank.score_pool(self.state, factors or self.factors, key='isolated-test-key',
                                         deadline=time.monotonic() + timeout, cache=self.cache)

    def test_concurrent_chunks_return_every_factor_with_usage_and_cache_reuse(self):
        jev = Jev(RATINGS)
        scores, meta = self.score(jev)
        self.assertEqual(set(scores), {factor for factor, _ in self.factors})
        self.assertEqual(scores[native(4)]['relevance'], {'score': 4, 'confidence': .9, 'probabilities': answer(4)['probabilities']})
        self.assertEqual([row['factor_count'] for row in meta['requests']], [3, 3, 2])
        self.assertEqual((meta['cached_factor_count'], meta['usage']['input_tokens']), (0, 300))
        again, meta = self.score(jev)
        self.assertEqual((again, meta['cached_factor_count'], meta['requests']), (scores, 8, []))
        # A dismissal admits one new factor; only it reaches Jev.
        sent = len(jev.bodies)
        self.score(jev, self.factors[1:] + [(native(9), {'label': 'arterial wall remodelling', 'trait': 'Coronary artery disease'})])
        self.assertEqual(jev.sent(jev.bodies[sent:]), ['arterial wall remodelling'])

    def test_provider_failures_and_invalid_answers_become_public_skip_codes(self):
        for kind in ('timeout', 'too_large', 'unavailable', 'invalid'):
            with self.subTest(kind=kind), self.assertRaises(gap_rerank.Skip) as skipped:
                self.score(Jev(RATINGS, error=kind))
            self.assertEqual(skipped.exception.reason, kind)
        cases = {'wrong_model': lambda value: value.update(model='unrequested-model'),
                 'missing_answer': lambda value: value['answers'].popitem(),
                 'wrong_type': lambda value: value['answers'][next(iter(value['answers']))].update(type='noul'),
                 'score_out_of_range': lambda value: value['answers'][next(iter(value['answers']))].update(score=7)}
        for name, damage in cases.items():
            def post(content, damage=damage, **kwargs):
                value = Jev(RATINGS)(content, **kwargs); damage(value); return value
            with self.subTest(name=name), self.assertRaises(gap_rerank.Skip) as skipped:
                self.score(post)
            self.assertEqual(skipped.exception.reason, 'invalid')
        self.assertFalse(self.cache.values)

    def test_a_slow_provider_times_out_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(gap_rerank.Skip) as skipped:
            self.score(Jev(RATINGS, delay=.5), timeout=.05)
        self.assertEqual(skipped.exception.reason, 'timeout')
        self.assertLess(time.monotonic() - started, .4)


class BuildSuggestionsTests(unittest.TestCase):
    def setUp(self):
        gap_rerank.CACHE.values.clear()
        self.repo = Repository()

    def build(self, catalog, environment, body=BODY, jev=None):
        with patch.dict(os.environ, environment), patch.object(api, 'catalog', catalog), patch.object(api, 'repo', self.repo), \
                patch.object(gap_rerank, 'post_systemone', jev or Jev(RATINGS)):
            result = api.build_suggestions(deepcopy(body))
        return result, self.repo.rows[('suggestion', result['suggestion_id'])]

    def test_flag_off_keeps_todays_response_and_audit_row(self):
        jev = Jev(RATINGS)
        catalog = Catalog(COSINE, DISEASE); result, audit = self.build(catalog, OFF, jev=jev)
        self.assertNotIn('rerank', result); self.assertNotIn('rerank', audit); self.assertNotIn('rerank_pool', audit)
        self.assertEqual([item['factor']['source_id'] for item in result['automatic_anchors']],
                         [native(20), native(9), native(1), native(2), native(3)])
        self.assertEqual((catalog.disease_calls, catalog.suggest_calls, jev.bodies), ([5], [3], []))

    def test_applied_rerank_orders_by_jev_and_records_the_scored_pool(self):
        catalog = Catalog(COSINE, DISEASE); jev = Jev(RATINGS)
        result, audit = self.build(catalog, ON, jev=jev)
        self.assertEqual((catalog.disease_calls, catalog.suggest_calls), ([gap_rerank.DISEASE_POOL_SIZE], [gap_rerank.POOL_SIZE]))
        self.assertEqual([item['factor']['source_id'] for item in result['automatic_anchors']],
                         [native(4), native(9), native(6), native(5), native(8)])
        self.assertEqual([item['ranking']['rank'] for item in result['automatic_anchors']], [1, 2, 3, 4, 5])
        for item in result['automatic_anchors']: api.validate(item['ranking'], 'Rank')
        rerank_schema = api.CONTRACT['components']['schemas']['Suggestions']['properties']['rerank']
        self.assertEqual(list(Draft202012Validator(rerank_schema).iter_errors(result['rerank'])), [])
        self.assertEqual(result['rerank'], {'model': gap_rerank.MODEL, 'rubric_version': gap_rerank.RUBRIC_VERSION,
                                            'status': 'applied', 'pool_size': 10})
        self.assertEqual(len(result['limitations']), 2)
        self.assertEqual(jev.sent(), sorted(RATINGS))
        self.assertEqual(audit['rerank']['status'], 'applied')
        self.assertEqual(audit['rerank']['usage'], {'input_tokens': 100, 'output_tokens': 10})
        self.assertEqual(len(audit['rerank']['state_sha256']), 64)
        self.assertEqual(len(audit['rerank_pool']), 10)
        self.assertIn([native(4), .80, False, 4, 4], audit['rerank_pool'])
        hit = audit['hits'][native(4)]
        self.assertEqual((hit['ranking']['metric'], hit['cosine_rank'], hit['context_similarities']),
                         ('jev_gap_relevance', 4, {'dismech:mechanism': .80}))
        self.assertEqual(hit['jev']['addresses']['score'], 4)

    def test_draft_bindings_freeze_the_rerank_summary_but_not_the_pool(self):
        catalog = Catalog(COSINE, DISEASE); result, audit = self.build(catalog, ON)
        class Transaction:
            def get(self, kind, identity): return {'data': audit} if kind == 'suggestion' else None
        factor = result['automatic_anchors'][0]['factor']
        composer = {'source_gap': BODY['source_gap'], 'eaggl_anchors': [{'reference': {'source': 'eaggl', 'source_id': factor['source_id'],
            'source_revision': factor['source_revision'], 'dapper_id': 'dapper:Mechanism.' + 'x' * 32}, 'origin': 'automatic',
            'suggestion_id': result['suggestion_id']}]}
        with patch.object(api, 'catalog', catalog):
            frozen = api.freeze_draft_bindings(Transaction(), 'draft', 'owner', composer, new=True)
        retrieval = frozen['selections'][factor['source_id']]['retrieval']
        self.assertEqual(retrieval['rerank']['status'], 'applied'); self.assertNotIn('rerank_pool', retrieval)
        self.assertEqual(retrieval['hit']['jev'], audit['hits'][factor['source_id']]['jev'])

    def test_provider_failure_falls_back_to_exactly_the_flag_off_anchors(self):
        expected, _ = self.build(Catalog(COSINE, DISEASE), OFF)
        for kind in ('timeout', 'unavailable', 'invalid', 'too_large'):
            with self.subTest(kind=kind):
                result, audit = self.build(Catalog(COSINE, DISEASE), ON, jev=Jev(RATINGS, error=kind))
                self.assertEqual(result['automatic_anchors'], expected['automatic_anchors'])
                self.assertEqual((result['rerank']['status'], result['rerank']['reason']), ('fallback', kind))
                self.assertEqual(len(result['limitations']), 1)
                self.assertEqual((audit['rerank']['reason'], len(audit['rerank_pool'])), (kind, 10))
                self.assertTrue(all(row[3] is None for row in audit['rerank_pool']))

    def test_unexpected_errors_never_fail_the_suggestion(self):
        def broken(*args, **kwargs): raise RuntimeError('bug')
        with self.assertLogs('reveal', 'ERROR'):
            result, _ = self.build(Catalog(COSINE, DISEASE), ON, jev=broken)
        self.assertEqual((result['rerank']['status'], result['rerank']['reason']), ('fallback', 'internal_error'))
        self.assertEqual(len(result['automatic_anchors']), 5)

    def test_missing_key_keeps_todays_retrieval_and_reports_not_configured(self):
        expected, _ = self.build(Catalog(COSINE, DISEASE), OFF)
        catalog = Catalog(COSINE, DISEASE); jev = Jev(RATINGS)
        result, audit = self.build(catalog, ON | {'TYPESAFE_API_KEY': ''}, jev=jev)
        self.assertEqual(result['automatic_anchors'], expected['automatic_anchors'])
        self.assertEqual((result['rerank']['status'], result['rerank']['reason']), ('fallback', 'not_configured'))
        self.assertEqual((catalog.suggest_calls, jev.bodies), ([3], []))
        self.assertNotIn('rerank_pool', audit)

    def test_subquery_hybrid_and_full_manual_selection_are_not_reranked(self):
        anchor = {'source': 'eaggl', 'source_revision': 'a' * 64, 'dapper_id': 'dapper:Mechanism.' + 'x' * 32}
        cases = {'subquery': BODY | {'subquery': 'nitric oxide'}, 'hybrid_mode': BODY | {'mode': 'hybrid'},
                 'no_open_slots': BODY | {'manual_eaggl_anchors': [dict(anchor, source_id=native(30 + i)) for i in range(5)]}}
        for reason, body in cases.items():
            with self.subTest(reason=reason):
                jev = Jev(RATINGS); result, audit = self.build(Catalog(COSINE, DISEASE), ON, body=body, jev=jev)
                self.assertEqual(result['rerank'], {'status': 'not_applicable', 'reason': reason})
                self.assertEqual((audit['rerank'], jev.bodies), (result['rerank'], []))

    def test_a_dismissal_rescores_only_the_newly_admitted_factor(self):
        jev = Jev(RATINGS)
        with patch.object(gap_rerank, 'POOL_SIZE', 6):
            self.build(Catalog(COSINE, DISEASE), ON, jev=jev)
            sent = len(jev.bodies)
            second, audit = self.build(Catalog(COSINE, DISEASE), ON, body=BODY | {'dismissed_source_ids': [native(4)]}, jev=jev)
        self.assertEqual(len(jev.sent(jev.bodies[:sent])), 8)
        self.assertEqual(jev.sent(jev.bodies[sent:]), ['hepatic lipid synthesis'])
        self.assertEqual(audit['rerank']['cached_factor_count'], 7)
        self.assertNotIn(native(4), [item['factor']['source_id'] for item in second['automatic_anchors']])


if __name__ == '__main__':
    unittest.main()
