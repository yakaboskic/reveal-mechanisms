"""Captured literature is auxiliary, scoped and readable; discovery is not evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json
from reveal_backend.scientific_grounding import MODEL, ScientificReviewUnavailable, review_account, review_evidence, validate_review
from reveal_backend.scientific_review_reader import EvidenceReader


def envelope(scope='abstract'):
    value = {'provider': 'Europe PMC', 'scope': scope, 'paper_id': '1234', 'source': 'MED',
             'http_status': 200, 'retrieved_at': '2026-09-28T00:00:00Z', 'row_count': 1,
             'upstream_sha256': 'a' * 64, 'url': 'https://www.ebi.ac.uk/europepmc/webservices/rest/search',
             'data': {'paper': {'id': '1234', 'source': 'MED', 'title': 'Offline test paper',
                               'commentCorrectionList': {'commentCorrection': [{'type': 'RetractionIn', 'id': '5678'}]}},
                      'text': 'Observed association; causal direction was not measured.',
                      'text_kind': 'abstract_html' if scope == 'abstract' else 'oa_xml_text',
                      'offset': 100, 'next_offset': 155, 'total_characters': 1200,
                      'limitation': 'Only this exact excerpt was captured.'}}
    if scope == 'full_text':
        value.update(source='PMC', paper_id='PMC1234')
        value['data']['paper'].update(id='PMC1234', source='PMC')
    return {'isError': False, 'structuredContent': value, 'content': [{'type': 'text', 'text': json.dumps(value)}]}


def response(calls):
    result = Mock(status_code=200)
    result.json.return_value = {'stop_reason': 'tool_use', 'content': calls, 'usage': {'input_tokens': 1500, 'output_tokens': 100}}
    return result


class LiteratureReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = Ledger(self.root, 'offline-review', 1)
        self.package = {'selection': {'knowledge_gap_id': 'gap:1'}, 'external_evidence': {'selected_graphs': []},
                        'pigean': {'factor': {'loading': .3}}}
        self.document = {'claims': [{'id': 'claim:1'}]}
        self.text_pointer = '/paper_calls/0/response/structuredContent/data/text'

    def add_read(self, scope='abstract', status='completed'):
        result = envelope(scope)
        entry = self.ledger.start('read_paper', {'source': result['structuredContent']['source'],
            'id': result['structuredContent']['paper_id'], 'section': scope, 'offset': 100, 'limit': 55}, None)
        self.ledger.finish(entry, result, status)
        return result, entry

    def evidence(self):
        self.ledger.freeze()
        return review_evidence(self.package, self.root / 'manifest.json')

    def test_paper_reads_preserve_exact_scope_offsets_and_corrections_but_searches_are_excluded(self):
        discovery = self.ledger.start('search_papers', {'query': 'a proposed causal mechanism'}, None)
        self.ledger.finish(discovery, {'structuredContent': {'scope': 'discovery', 'data': {'records': [{'title': 'A title is not a finding'}]}}}, 'completed')
        abstract, _ = self.add_read()
        full_text, _ = self.add_read('full_text')
        evidence = self.evidence()
        self.assertEqual(evidence['graph_calls'], [])
        self.assertEqual([item['response'] for item in evidence['paper_calls']], [abstract, full_text])
        self.assertNotIn('A title is not a finding', json.dumps(evidence))
        reader = EvidenceReader(evidence)
        initial = reader.initial()
        self.assertEqual([header['metadata']['scope'] for header in initial['paper_headers']], ['abstract', 'full_text'])
        for header in initial['paper_headers']:
            self.assertEqual(header['excerpt']['offset'], 100)
            self.assertEqual(header['excerpt']['next_offset'], 155)
            self.assertEqual(header['excerpt']['total_characters'], 1200)
            self.assertIn('commentCorrectionList', header['excerpt']['paper'])
            self.assertNotIn('text', header['excerpt'])
        self.assertFalse(reader.was_read(self.text_pointer))
        self.assertTrue(reader.was_read('/paper_calls/0/response/structuredContent/scope'))
        self.assertEqual(reader.read(self.text_pointer)['value'], abstract['structuredContent']['data']['text'])
        self.assertTrue(reader.was_read(self.text_pointer))

    def test_changed_paper_capture_fails_integrity_before_provider_call(self):
        _, entry = self.add_read()
        self.ledger.freeze()
        (self.root / entry['response']['path']).write_bytes(b'{}')
        client = Mock()
        with self.assertRaises(ScientificReviewUnavailable):
            review_account(self.document, self.package, self.root / 'manifest.json', model=MODEL, api_key='offline', client=client)
        client.post.assert_not_called()

    def test_paper_source_must_be_actually_read_even_though_scope_headers_are_supplied(self):
        self.add_read(); evidence = self.evidence()
        verdict = {'verdict': 'supported', 'finding': 'The captured loading and excerpt support only a bounded association.',
                   'source_refs': ['/package/pigean/factor', self.text_pointer]}
        review = {'claims': [{**deepcopy(verdict), 'claim_id': 'claim:1'}], 'synthesis': deepcopy(verdict)}
        for include_paper in (False, True):
            reads = [{'type': 'tool_use', 'id': 'native', 'name': 'read_evidence', 'input': {'pointer': '/package/pigean/factor'}}]
            if include_paper: reads.append({'type': 'tool_use', 'id': 'paper', 'name': 'read_evidence', 'input': {'pointer': self.text_pointer}})
            client = Mock(); client.post.side_effect = [response(reads), response([{'type': 'tool_use', 'id': 'final', 'name': 'submit_review', 'input': review}])]
            with self.subTest(include_paper=include_paper):
                if not include_paper:
                    with self.assertRaisesRegex(ScientificReviewUnavailable, 'not read'):
                        review_account(self.document, self.package, self.root / 'manifest.json', model=MODEL, api_key='offline', client=client)
                else:
                    result = review_account(self.document, self.package, self.root / 'manifest.json', model=MODEL, api_key='offline', client=client)
                    self.assertTrue(result['accepted'])
                    self.assertEqual([read['pointer'] for read in result['reads']], ['/package/pigean/factor', self.text_pointer])
        invalid = deepcopy(review); invalid['claims'][0]['source_refs'] = ['/search_papers/0']
        with self.assertRaises(EvidenceBuildError): validate_review(invalid, self.document, evidence)

    def test_failed_and_empty_paper_reads_retain_diagnostic_status_not_invented_text(self):
        for status, result in [('empty', {'structuredContent': {'scope': 'abstract', 'row_count': 0, 'data': {'limitation': 'No exact paper record'}}}),
                               ('failed', {'isError': True, 'content': [{'type': 'text', 'text': 'Source unavailable'}]})]:
            entry = self.ledger.start('read_paper', {'source': 'MED', 'id': '1234'}, None)
            self.ledger.finish(entry, result, status)
        evidence = self.evidence(); reader = EvidenceReader(evidence)
        self.assertEqual([header['status'] for header in reader.initial()['paper_headers']], ['empty', 'failed'])
        with self.assertRaises(EvidenceBuildError): reader.read(self.text_pointer)
        self.assertFalse(reader.was_read(self.text_pointer))


if __name__ == '__main__':
    unittest.main()
