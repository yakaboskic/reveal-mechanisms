"""No network: full evidence remains readable; budgets never imply acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.scientific_grounding import MODEL, ScientificReviewUnavailable, review_account, _ReviewSession
from reveal_backend.scientific_review_reader import EvidenceReader, MAX_READ_BYTES, child_path


def response(calls, incoming=2000, outgoing=100):
    value = Mock(status_code=200)
    value.json.return_value = {'stop_reason': 'tool_use', 'content': calls,
                              'usage': {'input_tokens': incoming, 'output_tokens': outgoing}}
    return value


def call(identity, name, value):
    return {'type': 'tool_use', 'id': identity, 'name': name, 'input': value}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.package = {'selection': {'knowledge_gap_id': 'gap:1', 'eaggl_mechanism_ids': ['f:1']},
                        'dismech': {'knowledge_gap': {'prompt': 'Does the association establish causality?'},
                                    'mechanisms': {'m:1': {'description': 'Relevant context.'}}},
                        'coverage': {'retained_nodes': 301, 'queries': {'gene': 'ok_limit_reached'}},
                        'external_evidence': {'selected_graphs': []},
                        'pigean': {'mechanisms': {'f:1': {'display_name': 'Observed factor', 'gene_loadings': {
                            'status': 'ok', 'items': {'gene:X': {'loading': 0.2}, 'gene:Y': {'loading': -0.1}}}}},
                                   'candidates': {f'gene:{i}': {'observation': 'source data ' * 200} for i in range(400)}}}
        self.document = {'claims': [{'id': 'c:1'}, {'id': 'c:2'}]}
        self.ref = '/package/pigean/mechanisms/f:1/gene_loadings/items'
        verdict = {'verdict': 'supported', 'finding': 'Association only; causal direction remains unknown.', 'source_refs': [self.ref]}
        self.review = {'claims': [{**deepcopy(verdict), 'claim_id': claim['id']} for claim in self.document['claims']],
                       'synthesis': {**deepcopy(verdict), 'source_refs': [self.ref, '/package/dismech/knowledge_gap']}}

    def run_review(self, responses, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), 'test', 1); ledger.freeze()
            client = Mock(); client.post.side_effect = responses
            result = review_account(self.document, self.package, Path(temp) / 'manifest.json',
                                    model=MODEL, api_key='offline-only', client=client, **kwargs)
            return result, client

    def test_large_evidence_complete_access_and_parallel_reads_within_default_budget(self):
        frozen = canonical_json(self.package)
        self.assertGreater(len(frozen), 800_000)
        result, client = self.run_review([
            response([call('r1', 'read_evidence', {'pointer': self.ref}),
                      call('r2', 'read_evidence', {'pointer': '/package/pigean/candidates/gene:399'})]),
            response([call('final', 'submit_review', self.review)])])
        self.assertTrue(result['accepted'])
        self.assertLess(result['actual_cost_usd'], .30)
        self.assertEqual(canonical_json(self.package), frozen)
        self.assertEqual(len(result['reads']), 2)
        messages = client.post.call_args_list[1].kwargs['json']['messages']
        self.assertEqual([item['tool_use_id'] for item in messages[-1]['content']], ['r1', 'r2'])
        self.assertLess(len(canonical_json(client.post.call_args_list[0].kwargs['json'])), 20_000)
        self.assertTrue(all('/count_tokens' not in item.args[0] for item in client.post.call_args_list))
        self.assertEqual(result['reads'][0]['source_sha256'], sha256(canonical_json({'gene:X': {'loading': .2}, 'gene:Y': {'loading': -.1}})))

    def test_unsupported_content_remains_rejected_and_no_sources_can_be_invented(self):
        unsupported = deepcopy(self.review); unsupported['claims'][0]['verdict'] = 'overstated'
        result, _ = self.run_review([response([call('r', 'read_evidence', {'pointer': self.ref})]),
                                     response([call('f', 'submit_review', unsupported)])])
        self.assertFalse(result['accepted'])
        for wrong in ('unread', 'missing_claim', 'invented_source', 'self_citation'):
            review = deepcopy(self.review)
            if wrong == 'missing_claim': review['claims'].pop()
            if wrong == 'invented_source': review['claims'][0]['source_refs'] = ['/package/missing']
            if wrong == 'self_citation': review['claims'][0]['source_refs'] = ['/proposed_document/claims/0']
            responses = [] if wrong == 'unread' else [response([call('r', 'read_evidence', {'pointer': self.ref})])]
            with self.subTest(wrong=wrong), self.assertRaises(ScientificReviewUnavailable):
                self.run_review([*responses, response([call('f', 'submit_review', review)])])

    def test_invalid_read_is_not_authorized_source_and_never_accesses_files(self):
        for path in ('file:///etc/passwd', '/package/../../secret', '/graph_calls/9'):
            with self.subTest(path=path), self.assertRaises(ScientificReviewUnavailable):
                self.run_review([response([call('r', 'read_evidence', {'pointer': path})]),
                                 response([call('f', 'submit_review', self.review)])])

    def test_all_large_collection_members_and_exact_unicode_are_retrievable(self):
        evidence = {'package': {'records': {f'x/{i}~': {'text': '\u03b2' * 50, 'score': i / 10} for i in range(301)},
                                'long': '\u03b2\U0001f9ec' * 5000}, 'graph_calls': []}
        reader = EvidenceReader(evidence)
        original = canonical_json(evidence)
        path = '/package/records'; offset = 0; recovered = {}
        while offset is not None:
            page = reader.read(path, offset)
            self.assertLessEqual(len(canonical_json(page)), MAX_READ_BYTES)
            self.assertFalse(reader.was_read(path))
            for entry in page['entries']:
                # Separate readers avoid the intentional per-review read limit.
                data = EvidenceReader(evidence).read(entry['pointer'])
                recovered[entry['key']] = data['value']
            offset = page['next_offset']
        self.assertEqual(recovered, evidence['package']['records'])
        reader = EvidenceReader(evidence); chunks = []; offset = 0
        while offset is not None:
            data = reader.read('/package/long', offset); chunks.append(data['text']); offset = data['next_offset']
        self.assertEqual(''.join(chunks), evidence['package']['long'])
        self.assertTrue(reader.was_read('/package/long'))
        self.assertEqual(canonical_json(evidence), original)

    def test_budget_and_turn_exhaustion_fail_without_a_verdict(self):
        with self.assertRaises(ScientificReviewUnavailable):
            self.run_review([], max_budget_usd=.001)
        with self.assertRaises(ScientificReviewUnavailable) as failure:
            self.run_review([response([call(str(i), 'read_evidence', {'pointer': self.ref})]) for i in range(8)])
        self.assertIn('turn limit', str(failure.exception))
        self.assertEqual(len(failure.exception.audit['calls']), 8)

    def test_final_allowed_call_can_decide_but_cannot_keep_reading(self):
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_TURNS': '2'}):
            result, client = self.run_review([
                response([call('r', 'read_evidence', {'pointer': self.ref})]),
                response([call('f', 'finish_review', {'review': self.review})])])
        self.assertTrue(result['accepted'])
        tools = client.post.call_args_list[-1].kwargs['json']['tools']
        self.assertIn('finish_review', [tool['name'] for tool in tools])
        from reveal_backend.scientific_grounding import FINISH_SCHEMA
        self.assertIs(next(tool['input_schema'] for tool in tools if tool['name']=='finish_review'),FINISH_SCHEMA)
        self.assertEqual(client.post.call_args_list[-1].kwargs['json']['tool_choice'], {'type': 'tool', 'name': 'finish_review'})
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_TURNS': '1'}), self.assertRaises(ScientificReviewUnavailable):
            self.run_review([response([call('f', 'finish_review', {'review': self.review})])])
        with patch.dict('os.environ', {'REVEAL_GROUNDING_MAX_TURNS': '1'}), self.assertRaises(ScientificReviewUnavailable):
            self.run_review([response([call('f', 'finish_review', {'unavailable_reason': 'More evidence must be inspected.'})])])

    def test_measured_prefix_reuse_requires_exact_unchanged_configuration_and_messages(self):
        client = Mock(); client.post.return_value = response([], incoming=1000)
        session = _ReviewSession(MODEL, 'offline', 1, client)
        payload = {'model': MODEL, 'system': 'x' * 30_000, 'max_tokens': 100,
                   'messages': [{'role': 'user', 'content': 'x' * 20_000}]}
        session.post(payload)
        payload['messages'].append({'role': 'assistant', 'content': 'next'})
        session.post(payload)
        self.assertLess(session.calls[1]['input_tokens_upper_bound'], 6000)
        payload['messages'][0]['content'] += 'changed'
        session.post(payload)
        self.assertGreater(session.calls[2]['input_tokens_upper_bound'], 50_000)

    def test_usage_and_tool_protocol_anomalies_never_accept(self):
        for mutation in ('cached_usage', 'missing_usage', 'excess_usage', 'unknown_tool', 'duplicate_id', 'mixed_final'):
            read = call('r', 'read_evidence', {'pointer': self.ref})
            item = response([read])
            body = item.json.return_value
            if mutation == 'cached_usage': body['usage']['cache_creation_input_tokens'] = 1000
            if mutation == 'missing_usage': body['usage'].pop('output_tokens')
            if mutation == 'excess_usage': body['usage']['input_tokens'] = 65_000
            if mutation == 'unknown_tool': body['content'][0]['name'] = 'shell'
            if mutation == 'duplicate_id': body['content'].append(deepcopy(read))
            if mutation == 'mixed_final': body['content'].append(call('final', 'submit_review', self.review))
            with self.subTest(mutation=mutation), self.assertRaises(ScientificReviewUnavailable):
                self.run_review([item])
