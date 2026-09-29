"""Payload fidelity and paid-request resume/retry boundaries; no live API calls."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from scipy.sparse import csr_matrix

from reveal_backend import demo_prioritization as demo
from reveal_backend import jev_batch as batch


def corpus_fixture():
    corpus = demo.LocalCorpus.__new__(demo.LocalCorpus)
    corpus.gaps = [{'id': f'gap:{i}', 'document_name': disease, 'kind': 'KNOWLEDGE_GAP',
        'status': 'RESOLVED' if i == 3 else 'OPEN', 'source_file': f'{i}.yaml',
        'raw': {'prompt': f'Why does process {i} cause this disease?', 'rationale': 'Unresolved mechanism.'}}
        for i, disease in enumerate(['Diabetes', 'Diabetes', 'Arthritis', 'Cancer'])]
    corpus.contexts = {row['id']: [{'source_id': row['id'], 'input_text': row['raw']['prompt']}]
                       for row in corpus.gaps}
    corpus.gap_vectors = {'gap:0': np.array([[1., 0.]]), 'gap:1': np.array([[.8, .6]]),
                          'gap:2': np.array([[0., 1.]]), 'gap:3': np.array([[-1., 0.]])}
    corpus.centroids = {key: value[0] for key, value in corpus.gap_vectors.items()}
    corpus.candidates = [{'index': i, 'factor_id': f'legacy:{i}', 'label': label, 'trait': trait,
        'metadata': {'top_gene_sets': 'metabolism,immune'}} for i, (label, trait) in
        enumerate([('Glucose regulation', 'Diabetes'), ('Immune activation', 'Arthritis')])]
    corpus.factor_matrix = np.array([[1., 0.], [0., 1.]])
    corpus.matches = {row['factor_id']: {'cfde_node_id': f'cfde:{row["index"]}',
        'payload': {'raw': {'label': row['label'] + ' current'}}} for row in corpus.candidates}
    corpus.genes = ['ZZZ', 'AAA', 'INS']
    corpus.loadings = csr_matrix([[.5, .5, .1], [0., .4, 0.]])
    corpus.gene_cache = {}
    corpus.factor_nodes = {row['factor_id']: {'parents': ['group']} for row in corpus.candidates}
    corpus.graph = {'group': {'kind': 'group', 'label': 'Shared pathway'}}
    corpus.attachments = defaultdict(list)
    corpus.provenance = {'gene_source': 'legacy, not current', 'fixture': True}
    corpus.source_files = {f'{i}.yaml': str(i) * 64 for i in range(4)}
    return corpus


def valid_response(body, *, score=3, confidence=.9, risk=.1, model='jev-test-pinned'):
    answers = {}
    for key, question in body['questions'].items():
        kind = question['type']
        if kind == 'score':
            probabilities = {str(i): float(i == score) for i in range(len(question['criteria']))}
            answers[key] = {'type': kind, 'score': score, 'confidence': confidence, 'probabilities': probabilities}
        elif kind == 'noul':
            answers[key] = {'type': kind, 'noul': risk if key == 'overclaim_risk' else .8}
        else:
            choice = 'none' if key == 'main_blocker' else next(iter(question['criteria']))
            answers[key] = {'type': kind, 'choice': choice, 'confidence': confidence,
                            'probabilities': {option: float(option == choice) for option in question['criteria']}}
    return {'model': model, 'answers': answers, 'usage': {'input_tokens': 123, 'output_tokens': 45}}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.corpus = corpus_fixture()

    def test_gene_weights_ties_coverage_and_graph_semantics(self):
        state = self.corpus.state(self.corpus.gaps[0], self.corpus.gaps, top_factors=2, top_genes=2, other_gaps=2)
        first = state['eaggl']['factors'][0]
        self.assertEqual(first['legacy_factor_id'], 'legacy:0')
        self.assertEqual(first['cfde_node_id'], 'cfde:0')
        self.assertEqual(first['genes'], [['AAA', .5], ['ZZZ', .5]])
        self.assertEqual(first['nonzero_genes'], 3)
        self.assertAlmostEqual(first['retained_loading_fraction'], 1 / 1.1, places=5)
        self.assertEqual(state['eaggl']['shared_hierarchy_groups'][0]['factors'], ['F01', 'F02'])
        self.assertEqual(state['eaggl']['top_gene_overlaps'][0][2], 1)
        self.assertIn('not probabilities', state['evaluation_policy'])

    def test_maximum_context_ranking_and_comparison_exclusion(self):
        self.corpus.gap_vectors['gap:0'] = np.array([[.8, .6], [0., 1.]])
        self.corpus.contexts['gap:0'].append({'source_id': 'context:second', 'input_text': 'Immune'})
        state = self.corpus.state(self.corpus.gaps[0], self.corpus.gaps, top_factors=2, other_gaps=2)
        self.assertEqual(state['eaggl']['factors'][0]['legacy_factor_id'], 'legacy:1')
        self.assertEqual(state['eaggl']['factors'][0]['matched_contexts'], ['C2'])
        self.assertNotIn('gap:0', {row['source_id'] for row in state['comparison_gaps']})
        self.assertEqual(self.corpus.comparison_gaps(self.corpus.gaps[0], self.corpus.gaps, 0), [])

    def test_budget_removes_comparisons_without_truncating_evidence(self):
        state = self.corpus.state(self.corpus.gaps[0], self.corpus.gaps, top_factors=2, other_gaps=2)
        original = deepcopy(state)
        full, size = demo.make_request(state)
        smaller, small_size = demo.make_request(state, max_bytes=size['request_bytes'] - 50)
        self.assertLess(len(smaller['state']['comparison_gaps']), len(full['state']['comparison_gaps']))
        self.assertEqual(smaller['state']['eaggl']['factors'], full['state']['eaggl']['factors'])
        self.assertEqual(state, original)
        with self.assertRaisesRegex(ValueError, 'reduce --top-factors'):
            demo.make_request(state, max_bytes=100)

    def test_oversized_payloads_are_explicitly_blocked_not_silently_cut(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'blocked'
            summary = demo.prepare(self.corpus, output, top_factors=2, max_bytes=100)
            self.assertEqual((summary['prepared_requests'], summary['blocked_requests']), (0, 3))
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(len(manifest['blocked']), 3)
            self.assertIn('reduce --top-factors', manifest['blocked'][0]['error'])
            self.assertEqual(batch.run(output, send=True)['selected'], 0)

    def test_numeric_json_uses_the_revised_estimate_and_smaller_default_budget(self):
        state = self.corpus.state(self.corpus.gaps[0], self.corpus.gaps)
        body, size = demo.make_request(state)
        self.assertEqual(state['eaggl']['requested_factors'], 20)
        self.assertEqual(state['eaggl']['requested_genes_per_factor'], 20)
        self.assertEqual(state['comparison_context']['requested_other_gaps'], 12)
        self.assertEqual(size['estimated_input_tokens'], (size['request_bytes'] + 1) // 2)
        state['focal_gap']['rationale'] = 'x' * 50_000
        with self.assertRaisesRegex(demo.PayloadTooLarge, '48,000'):
            demo.make_request(state)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'prepared'
        demo.prepare(corpus_fixture(), self.output, top_factors=2, top_genes=2, other_gaps=2)

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_dry_run_resume_and_no_duplicate_paid_requests(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=valid_response(calls[-1]))
        client = self.client(handler)
        summary = batch.run(self.output, client=client)
        self.assertEqual(summary['prepared'], 3)  # Resolved gap excluded.
        self.assertEqual(calls, [])
        first = batch.run(self.output, client=client, send=True, limit=2, workers=1)
        second = batch.run(self.output, client=client, send=True, workers=1)
        third = batch.run(self.output, send=True)  # Fully cached needs no API key.
        self.assertEqual((first['succeeded_now'], second['succeeded_now'], third['selected']), (2, 1, 0))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({body['state']['focal_gap']['source_id'] for body in calls}), 3)
        report = batch.report(self.output)
        self.assertEqual(report['completed'], 3)
        rows = json.loads((self.output / 'report.json').read_text())['ranked']
        self.assertEqual(rows[0]['demo_score_100'], 75.)
        self.assertEqual(rows[0]['prioritize_probability'], .8)
        self.assertEqual(report['usage_successful_responses']['input_tokens'], 369)
        self.assertEqual(report['shortlist_count'], 1)  # Identical anchor sets are deduplicated.

    def test_429_retry_after_and_auth_failure(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={'Retry-After': '2'}) if len(calls) == 1 else httpx.Response(200, json=valid_response(body))
        sleeps = []
        result = batch.send_request(self.client(handler), body, sleep=sleeps.append)
        self.assertTrue(result['ok']); self.assertEqual(sleeps, [2.]); self.assertEqual(result['attempts'], 2)
        result = batch.send_request(self.client(lambda _: httpx.Response(401)), body, sleep=sleeps.append)
        self.assertTrue(result['fatal_auth']); self.assertEqual(result['attempts'], 1)
        result = batch.send_request(self.client(lambda _: httpx.Response(429, headers={'Retry-After': '120'})), body, sleep=sleeps.append)
        self.assertEqual(result['retry_after_seconds'], 120)
        self.assertEqual(sleeps, [2.])

    def test_invalid_success_never_cached_and_then_can_resume(self):
        client = self.client(lambda _: httpx.Response(200, text='not JSON', headers={'content-type': 'application/json'}))
        result = batch.run(self.output, client=client, send=True, limit=1)
        self.assertEqual(result['failed_now'], 1)
        self.assertFalse((self.output / 'results').exists())
        self.assertEqual(batch.run(self.output)['pending'], 3)

    def test_changed_payload_rejected_before_any_network_request(self):
        manifest = json.loads((self.output / 'manifest.json').read_text())
        path = self.output / manifest['requests'][0]['path']
        body = json.loads(path.read_text()); body['model'] = 'changed'; demo.atomic_json(path, body)
        with self.assertRaisesRegex(ValueError, 'request changed'):
            batch.run(self.output, send=True, client=self.client(lambda _: self.fail('Must not send')))

    def test_low_confidence_and_overclaiming_require_review(self):
        client = self.client(lambda request: httpx.Response(200, json=valid_response(json.loads(request.content), confidence=.2, risk=.9)))
        batch.run(self.output, client=client, send=True, limit=1)
        summary = batch.report(self.output)
        self.assertEqual(summary['shortlist_count'], 0)
        self.assertEqual(summary['review_candidate_count'], 0)
        row = json.loads((self.output / 'report.json').read_text())['ranked'][0]
        self.assertIn('low_model_confidence', row['review_flags'])
        self.assertIn('overclaim_risk', row['review_flags'])

    def test_report_explains_zero_shortlist_and_surfaces_only_confidence_only_candidates(self):
        def handler(request):
            body = json.loads(request.content)
            response = valid_response(body)
            source_id = body['state']['focal_gap']['source_id']
            if source_id == 'gap:0': response['answers']['distinctiveness']['confidence'] = .2
            if source_id == 'gap:1': response['answers']['overclaim_risk']['noul'] = .8
            if source_id == 'gap:2': response['answers']['evidence_fit']['score'] = 1
            return httpx.Response(200, json=response)
        batch.run(self.output, client=self.client(handler), send=True, workers=1)
        saved = {p.name: p.read_bytes() for p in (self.output / 'results').glob('*.json')}
        with patch.object(batch.httpx, 'Client', side_effect=AssertionError('Reports must stay offline')):
            summary = batch.report(self.output)
        self.assertEqual((summary['completed'], summary['shortlist_count'], summary['review_candidate_count']), (3, 0, 1))
        diagnostics = summary['shortlist_diagnostics']
        self.assertEqual(diagnostics['exclusion_counts']['low_model_confidence'], 1)
        self.assertEqual(diagnostics['confidence_only_exclusions'], 1)
        self.assertEqual([step['remaining'] for step in diagnostics['gate_funnel']], [2, 2, 2, 1, 0])
        self.assertEqual(diagnostics['main_blocker_counts'], {'none': 3})
        report = json.loads((self.output / 'report.json').read_text())
        candidate = report['review_candidates'][0]
        self.assertEqual(candidate['source_id'], 'gap:0')
        self.assertEqual(candidate['review_flags'], ['low_model_confidence'])
        self.assertEqual(candidate['rubric_confidences']['distinctiveness'], .2)
        self.assertIn('Why does process 0', (self.output / 'review-candidates.md').read_text())
        self.assertEqual(saved, {p.name: p.read_bytes() for p in (self.output / 'results').glob('*.json')})

    def test_review_candidates_obey_diversity_and_confidence_threshold(self):
        client = self.client(lambda request: httpx.Response(200, json=valid_response(json.loads(request.content), confidence=.2)))
        batch.run(self.output, client=client, send=True, workers=1)
        summary = batch.report(self.output)
        self.assertEqual(summary['shortlist_count'], 0)
        self.assertEqual(summary['shortlist_diagnostics']['confidence_only_exclusions'], 3)
        self.assertEqual(summary['review_candidate_count'], 1)  # Same top-five anchors.
        updated = batch.report(self.output, min_confidence=0)
        self.assertEqual((updated['shortlist_count'], updated['review_candidate_count']), (1, 0))
        report = json.loads((self.output / 'report.json').read_text())
        self.assertEqual(report['shortlist'][0]['minimum_rubric_confidence'], .2)

    def test_nonfinite_missing_answers_and_unknown_choices_rejected(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        for mutation in ('nonfinite', 'missing', 'unknown'):
            response = valid_response(body)
            if mutation == 'nonfinite': response['answers']['prioritize']['noul'] = float('nan')
            if mutation == 'missing': del response['answers']['answerability']
            if mutation == 'unknown': response['answers']['best_anchor']['choice'] = 'F99'
            with self.assertRaises(ValueError): batch.validate_response(response, body['questions'])

    def test_custom_rubric_preserves_answers_without_inventing_standard_scores(self):
        output = Path(self.temp.name) / 'custom'
        demo.prepare(corpus_fixture(), output, limit=1,
                     questions={'interesting': {'type': 'noul', 'instructions': 'Is the focal gap interesting?'}})
        client = self.client(lambda request: httpx.Response(200, json=valid_response(json.loads(request.content))))
        batch.run(output, send=True, client=client)
        summary = batch.report(output)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['shortlist_count'], 0)
        self.assertFalse((output / 'ranking.csv').exists())
        self.assertIn('interesting', json.loads((output / 'answers.json').read_text())[0]['response']['answers'])

    def test_timeout_retries_are_bounded(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('simulated')
        result = batch.send_request(self.client(handler), body, retries=2, sleep=lambda _: None)
        self.assertFalse(result['ok'])
        self.assertEqual((len(calls), result['attempts']), (3, 3))

    def test_http_error_body_request_id_and_secret_redaction_are_persisted(self):
        secret = 'test-provider-key-that-must-not-be-logged'
        client = self.client(lambda _: httpx.Response(400, json={
            'error': 'State exceeds the context window', 'api_key': 'another-sensitive-value',
            'detail': f'An echoed credential: {secret}', 'authorization': 'Bearer yet-another-secret'},
            headers={'x-request-id': 'provider-request-123', 'set-cookie': 'private-cookie'}))
        client.headers['authorization'] = f'Bearer {secret}'
        result = batch.run(self.output, client=client, send=True, limit=1, retries=0)
        log = (self.output / 'run.log').read_text()
        error = json.loads(next((self.output / 'errors').glob('*.json')).read_text())
        self.assertEqual(error['http_status'], 400)
        self.assertIn('State exceeds the context window', error['response_body'])
        self.assertEqual(error['response_headers']['x-request-id'], 'provider-request-123')
        self.assertIn('attempt_failed', log)
        self.assertIn('provider-request-123', log)
        self.assertIn('run_finished', log)
        self.assertIn(result['run_id'], log)
        self.assertTrue(Path(result['log_file']).exists())
        for text in (log, json.dumps(error), (self.output / 'events.jsonl').read_text()):
            for forbidden in (secret, 'another-sensitive-value', 'yet-another-secret', 'private-cookie'):
                self.assertNotIn(forbidden, text)
            self.assertIn('[REDACTED]', text)

    def test_run_log_appends_and_preserves_retry_history(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1: return httpx.Response(429, text='Slow down', headers={'retry-after': '0'})
            return httpx.Response(200, json=valid_response(json.loads(request.content)))
        first = batch.run(self.output, client=self.client(handler), send=True, limit=1, retries=1)
        before = (self.output / 'run.log').read_text()
        second = batch.run(self.output)
        after = (self.output / 'run.log').read_text()
        self.assertTrue(after.startswith(before))
        self.assertIn('retry_scheduled', before)
        self.assertIn('Slow down', before)
        self.assertIn('attempt_succeeded', before)
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(after.count('run_start'), 2)

    def test_plaintext_error_body_is_bounded_and_validation_reason_is_logged(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        result = batch.send_request(self.client(lambda _: httpx.Response(502, text='x' * 20_000)), body, retries=0)
        self.assertEqual(len(result['response_body'].encode()), batch.ERROR_BODY_LIMIT)
        self.assertTrue(result['response_body_truncated'])
        client = self.client(lambda _: httpx.Response(200, json={'model': 'jev-test', 'answers': {}}))
        batch.run(self.output, client=client, send=True, limit=1)
        self.assertIn('Missing question answers', (self.output / 'run.log').read_text())

    def test_preflight_failure_is_logged(self):
        with self.assertRaisesRegex(ValueError, 'Set TYPESAFE_API_KEY'):
            batch.run(self.output, send=True)
        log = (self.output / 'run.log').read_text()
        self.assertIn('run_aborted', log)
        self.assertIn('Set TYPESAFE_API_KEY', log)

    def test_context_rejection_is_actionable_and_never_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(400, json={'detail': {'error_type': 'max_tokens_exceeded'}})
        result = batch.run(self.output, client=self.client(handler), send=True, limit=1, retries=2)
        self.assertEqual(len(calls), 1)
        error = json.loads(next((self.output / 'errors').glob('*.json')).read_text())
        self.assertEqual(error['provider_error_type'], 'max_tokens_exceeded')
        self.assertIn('Reprepare', error['action_required'])
        self.assertEqual(result['failed_now'], 1)
        self.assertIn('max_tokens_exceeded', (self.output / 'run.log').read_text())

    def test_rounded_probability_sums_are_accepted_unchanged_and_logged(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        for key in ('best_anchor', 'demo_clarity'):
            for remainder in (.49, .51):
                with self.subTest(question=key, total=.5 + remainder):
                    response = valid_response(body)
                    probabilities = response['answers'][key]['probabilities']
                    options = list(probabilities)
                    probabilities.update({option: 0. for option in options})
                    probabilities[options[0]], probabilities[options[1]] = .5, remainder
                    original = deepcopy(response)
                    events = []
                    result = batch.send_request(self.client(lambda _: httpx.Response(200, json=response)), body,
                        log=lambda event, **fields: events.append((event, fields)))
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['response'], original)
                    self.assertEqual(result['validation_warnings'][0]['question'], key)
                    self.assertAlmostEqual(result['validation_warnings'][0]['probability_sum'], .5 + remainder)
                    self.assertEqual(events[-1][1]['validation_warnings'], result['validation_warnings'])

    def test_probability_tolerance_does_not_accept_malformed_distributions(self):
        _, requests = batch.read_preparation(self.output); _, body = next(requests)
        for mutation in ('too_low', 'too_high', 'zero', 'missing', 'extra', 'negative', 'nonfinite', 'boolean'):
            with self.subTest(mutation=mutation):
                response = valid_response(body)
                p = response['answers']['demo_clarity']['probabilities']
                if mutation == 'too_low': p['3'] = .98
                if mutation == 'too_high': p['0'] = .02
                if mutation == 'zero': p['3'] = 0.
                if mutation == 'missing': del p['0']
                if mutation == 'extra': p['unknown'] = 0.
                if mutation == 'negative': p['0'], p['1'] = -.01, .01
                if mutation == 'nonfinite': p['0'] = float('nan')
                if mutation == 'boolean': p['3'] = True
                with self.assertRaisesRegex(ValueError, 'Invalid probability distribution'):
                    batch.validate_response(response, body['questions'])

    def saved_validation_error(self, entry, body):
        response = valid_response(body)
        response['answers']['demo_clarity']['probabilities'] = {'0': 0., '1': .01, '2': .11, '3': .81, '4': .06}
        return {'ok': False, 'error': 'Invalid successful API response', 'http_status': 200,
                'validation_error': 'ValueError: Invalid probability distribution for demo_clarity',
                'response_body': json.dumps(response), 'response_body_truncated': False,
                'request_sha256': entry['request_sha256'], 'source_id': entry['source_id'],
                'requested_model': body['model'], 'attempts': 1, 'completed_at': '2026-09-29T12:00:00+00:00'}

    def test_recovery_preserves_saved_response_and_prevents_duplicate_paid_requests(self):
        _, requests = batch.read_preparation(self.output); entry, body = next(requests)
        error = self.saved_validation_error(entry, body)
        path = self.output / 'errors' / (entry['request_sha256'] + '.json')
        batch.atomic_json(path, error)
        original_error = path.read_bytes()
        with patch.object(batch.httpx, 'Client', side_effect=AssertionError('Recovery must stay offline')):
            first = batch.recover(self.output)
            second = batch.recover(self.output)
        self.assertEqual((first['recovered_now'], first['http_attempts'], first['pending_after']), (1, 0, 2))
        self.assertEqual((second['recovered_now'], second['cached']), (0, 1))
        self.assertEqual(path.read_bytes(), original_error)
        result = batch.read_result(self.output, entry, body)
        self.assertEqual(result['response'], json.loads(error['response_body']))
        self.assertEqual(result['completed_at'], error['completed_at'])
        self.assertEqual(result['recovered_from'], 'errors/' + path.name)
        self.assertIn('response_recovered', (self.output / 'run.log').read_text())
        self.assertEqual(batch.report(self.output)['completed'], 1)
        self.assertEqual(batch.run(self.output)['cached'], 1)
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=valid_response(calls[-1]))
        batch.run(self.output, send=True, client=self.client(handler), workers=1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(entry['source_id'], [item['state']['focal_gap']['source_id'] for item in calls])

    def test_recovery_rejects_incomplete_invalid_or_mismatched_saved_responses(self):
        _, requests = batch.read_preparation(self.output); entry, body = next(requests)
        path = self.output / 'errors' / (entry['request_sha256'] + '.json')
        for mutation in ('truncated', 'bad_json', 'bad_distribution', 'http_error', 'wrong_hash', 'wrong_source', 'wrong_model'):
            with self.subTest(mutation=mutation):
                error = self.saved_validation_error(entry, body)
                if mutation == 'truncated': error['response_body_truncated'] = True
                if mutation == 'bad_json': error['response_body'] = 'not JSON'
                if mutation == 'bad_distribution':
                    response = json.loads(error['response_body'])
                    response['answers']['demo_clarity']['probabilities']['3'] = .5
                    error['response_body'] = json.dumps(response)
                if mutation == 'http_error': error['http_status'] = 400
                if mutation == 'wrong_hash': error['request_sha256'] = '0' * 64
                if mutation == 'wrong_source': error['source_id'] = 'another-gap'
                if mutation == 'wrong_model': error['requested_model'] = 'another-model'
                batch.atomic_json(path, error)
                with patch.object(batch.httpx, 'Client', side_effect=AssertionError('Must stay offline')):
                    result = batch.recover(self.output)
                self.assertEqual((result['recovered_now'], result['unrecoverable_saved_errors']), (0, 1))
                self.assertIsNone(batch.read_result(self.output, entry, body))


if __name__ == '__main__':
    unittest.main()
