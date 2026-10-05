"""Only a complete, source-checked reviewer verdict can reject science."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from reveal_backend import durable_review
from reveal_backend.box_mcp import Ledger
from reveal_backend.job_failures import review_failure
from reveal_backend.scientific_grounding import ScientificReviewUnavailable


class ReviewResponseFailureTests(unittest.TestCase):
    def initial(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'review-response-test', 1); ledger.freeze()
            state = durable_review.initial('analysis', {'claims': [{'id': 'claim:1'}]},
                {'selection': {'knowledge_gap_id': 'gap:1'},
                 'dismech': {'observation': 'An association was measured'},
                 'external_evidence': {'selected_graphs': []}},
                ledger_path=Path(temp)/'manifest.json', budget=1)
        state['session'].update(spent=.05, calls=[{'cost_usd': .05}])
        return state

    def verdict(self, verdict='supported'):
        item = {'verdict': verdict, 'finding': 'Observational association only.',
                'source_refs': ['/package/dismech/observation']}
        return {'claims': [{**item, 'claim_id': 'claim:1'}], 'synthesis': item}

    def response(self, state, review):
        state['response'] = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': 'verdict-1', 'name': 'submit_review', 'input': review}]}

    def unavailable(self, state):
        saved = deepcopy(state)
        with self.assertRaises(ScientificReviewUnavailable) as failure:
            durable_review.process_response(state)
        self.assertEqual(state, saved, 'The durable response must survive processing failure')
        self.assertEqual(failure.exception.audit['actual_cost_usd'], .05)
        self.assertEqual(review_failure(failure.exception)['code'], 'REVIEW_UNAVAILABLE')
        self.assertIn('no scientific verdict', review_failure(failure.exception)['message'])
        return failure.exception

    def test_invalid_schema_is_operational_and_does_not_echo_response(self):
        state = self.initial(); self.response(state, {'untrusted': 'private-provider-text'})
        error = self.unavailable(state)
        self.assertEqual(error.audit['response_error_type'], 'ValidationError')
        self.assertNotIn('private-provider-text', str(error))

    def test_incomplete_or_duplicate_claim_coverage_is_not_a_rejection(self):
        for claims in ([], ['claim:1', 'claim:1'], ['claim:other']):
            with self.subTest(claims=claims):
                state = self.initial(); review = self.verdict()
                review['claims'] = [{**review['claims'][0], 'claim_id': identity} for identity in claims]
                self.response(state, review); self.unavailable(state)

    def test_invalid_source_reference_is_not_a_rejection(self):
        state = self.initial(); review = self.verdict()
        review['synthesis']['source_refs'] = ['/package/dismech/absent']
        self.response(state, review); self.unavailable(state)

    def test_reader_protocol_failure_retains_cost_audit(self):
        state = self.initial(); state['response'] = {'stop_reason': 'max_tokens', 'content': []}
        self.unavailable(state)

    def test_invalid_paragraph_json_is_operational(self):
        state = self.initial(); state.update(kind='paragraph', segment_count=1)
        state['response'] = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{incomplete'}]}
        self.unavailable(state)

    def test_complete_supported_and_rejected_verdicts_remain_distinct(self):
        for verdict, accepted in (('supported', True), ('unsupported', False), ('overstated', False)):
            with self.subTest(verdict=verdict):
                state = self.initial(); self.response(state, self.verdict(verdict))
                result = durable_review.process_response(state)
                self.assertTrue(result['complete'])
                self.assertEqual(result['result']['accepted'], accepted)
                self.assertEqual(result['result']['actual_cost_usd'], .05)
