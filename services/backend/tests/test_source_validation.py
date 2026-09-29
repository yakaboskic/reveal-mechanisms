"""Regression coverage for the shared source-fidelity gate."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.source_validation import ledger_sources, observation_findings, validate_new_files


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'factor': 'Factor3', 'factor_value': 0.1497, 'gene': 'IL6', 'phenotype': 'LymphoCount'}
        self.sources = {'file': {'data': [self.row, {'factor_value': 0.5795, 'gene': 'OTHER'}]}}
        self.document = {
            'claims': [{'id': 'claim', 'has_evidence': ['evidence'], 'has_score': ['score']}],
            'claim_scores': [{'id': 'score', 'metric': 'factor_value', 'value': 0.1497, 'score_kind': 'LOADING'}],
            'evidence_items': [{'id': 'evidence', 'was_derived_from': ['file'], 'context': 'Captured row /data/0.',
                                'snippet': json.dumps(self.row)}]}

    def checks(self):
        return {item['check'] for item in observation_findings(self.document, self.sources)}

    def test_sentence_period_and_backticks_resolve_exact_row(self):
        for context in ('Captured row /data/0.', 'Captured row `/data/0`.', 'Captured row (/data/0).'):
            with self.subTest(context=context):
                self.document['evidence_items'][0]['context'] = context
                self.assertEqual(self.checks(), set())

    def test_original_failure_mixes_derived_fields_into_raw_source_quote(self):
        self.document['evidence_items'][0]['snippet'] = 'result_key: "factor:portal:LymphoCount:cfde-inc-v2:Factor3:gene:IL6", factor_value: 0.1497, normalized_score: 0.5795, relation: "direct"'
        self.assertEqual(self.checks(), {'evidence-snippet'})

    def test_wrong_row_and_unresolved_pointer_never_search_whole_response(self):
        for context in ('Captured row /data/1.', 'Captured row /data/999.'):
            with self.subTest(context=context):
                self.document['evidence_items'][0]['context'] = context
                checks = self.checks()
                self.assertIn('evidence-snippet', checks)
                self.assertIn('source-metric', checks)
                self.assertEqual('source-locator' in checks, '999' in context)

    def test_wrong_metric_name_value_and_kind_are_rejected(self):
        for changes, check in (({'metric': 'normalized_score'}, 'source-metric'),
                               ({'value': 0.5795}, 'source-metric'),
                               ({'score_kind': 'PROBABILITY'}, 'source-metric-kind')):
            with self.subTest(changes=changes):
                original = deepcopy(self.document['claim_scores'][0])
                self.document['claim_scores'][0].update(changes)
                self.assertIn(check, self.checks())
                self.document['claim_scores'][0] = original

    def test_string_locator_supports_exact_literature_text_and_literal_dot_keys(self):
        self.document['claims'][0]['has_score'] = []
        self.sources['file'] = {'structuredContent': {'data': {'text': 'Exact captured abstract.', 'text.': 'Literal dot key.'}}}
        item = self.document['evidence_items'][0]
        item.update(context='Source /structuredContent/data/text.', snippet='Literal dot key.')
        self.assertEqual(self.checks(), set())
        item.update(context='Source `/structuredContent/data/text`.', snippet='Exact captured abstract.')
        self.assertEqual(self.checks(), set())
        item['snippet'] = 'Invented abstract.'
        self.assertEqual(self.checks(), {'evidence-snippet'})

    def test_live_and_final_ledgers_have_same_completed_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root, 'test', 1)
            complete = ledger.start('query_graph', {'graph': 'prokn'}, 'prokn')
            ledger.finish(complete, {'content': [{'text': 'captured row'}]}, 'completed')
            ledger.start('query_graph', {'graph': 'prokn'}, 'prokn')
            snapshot = root / 'lint-sources.json'
            snapshot.write_bytes(ledger.sanitized_bytes({'calls': ledger.entries})[0])
            live = ledger_sources(snapshot)
            self.assertFalse(ledger.frozen)
            ledger.freeze()
            self.assertEqual(live, ledger_sources(root / 'manifest.json'))
            source = complete['response']
            document = {'files': [{'id': 'external', 'sha256': source['sha256'], 'size_in_bytes': source['size_bytes']}]}
            validate_new_files(document, {}, live)
            document['files'][0]['size_in_bytes'] += 1
            with self.assertRaises(EvidenceBuildError):
                validate_new_files(document, {}, live)
            (root / source['path']).write_text('tampered')
            with self.assertRaisesRegex(EvidenceBuildError, 'checksum'):
                ledger_sources(snapshot)


if __name__ == '__main__':
    unittest.main()
