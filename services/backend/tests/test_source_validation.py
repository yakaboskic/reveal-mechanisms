"""Regression coverage for the shared source-fidelity gate."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.source_validation import exact_document, ledger_sources, observation_findings, validate_new_files


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

    def test_progressive_loading_captures_preserve_loading_semantics(self):
        for metric in ('loading','joint_loading','marginal_loading'):
            with self.subTest(metric=metric):
                self.sources={'file':{'result':{'items':[{'gene':'IL6',metric:0.1497}]}}}
                self.document['claim_scores'][0].update(metric=metric,score_kind='LOADING')
                self.document['evidence_items'][0].update(context='Captured /result/items/0.',snippet='')
                self.assertEqual(self.checks(),set())
                self.document['claim_scores'][0]['score_kind']='EFFECT_ESTIMATE'
                self.assertIn('source-metric-kind',self.checks())

    def test_metric_fidelity_uses_exact_decimal_values_before_float_rounding(self):
        source = b'{"data":[{"loading":0.10000000000000000000009}]}'
        authored = b'{"claims":[{"id":"claim","has_evidence":["evidence"],"has_score":["score"]}],"claim_scores":[{"id":"score","metric":"loading","value":0.10000000000000000000009,"score_kind":"LOADING"}],"evidence_items":[{"id":"evidence","was_derived_from":["file"],"context":"/data/0"}]}'
        def checks(raw):
            return {row['check'] for row in observation_findings(json.loads(raw), {'file':json.loads(source)},
                exact_observed={'file':exact_document(source)}, authored_exact=exact_document(raw))}
        self.assertEqual(checks(authored), set())
        rounded = authored.replace(b'0.10000000000000000000009', b'0.1')
        self.assertEqual(json.loads(authored), json.loads(rounded))
        self.assertEqual(checks(rounded), {'source-metric'})
        # Spelling can differ when the mathematical value is identical.
        source = b'{"data":[{"loading":0.10}]}'
        self.assertEqual(checks(rounded), set())

    def test_yaml_metric_tokens_preserve_precision_and_strings_remain_non_numeric(self):
        source = b'{"data":[{"loading":0.10000000000000000000009}]}'
        self.document['evidence_items'][0]['snippet'] = ''
        self.document['claim_scores'][0].update(metric='loading', value=0.1)
        authored = b'claim_scores:\n- id: score\n  value: 0.10000000000000000000009\n'
        def checks(raw):
            return {row['check'] for row in observation_findings(self.document, {'file':json.loads(source)},
                exact_observed={'file':exact_document(source)}, authored_exact=exact_document(raw, 'yaml'))}
        self.assertEqual(checks(authored), set())
        self.assertEqual(checks(authored.replace(b'0.10000000000000000000009', b'0.1')), {'source-metric'})
        self.assertEqual(checks(authored.replace(b'0.10000000000000000000009', b'"0.10000000000000000000009"')), {'source-metric'})
        self.assertEqual(checks(authored.replace(b'0.10000000000000000000009', b'true')), {'source-metric'})

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

    def test_shared_source_claim_dag_has_bounded_traversal_and_preserves_scores(self):
        from reveal_backend import source_validation
        document = {'claims': [{'id': 'base', 'has_evidence': ['original']}],
            'evidence_items': [{'id': 'original', 'was_derived_from': ['file'], 'context': '/data/0'}],
            'claim_scores': [{'id': 'score', 'metric': 'factor_value', 'value': 0.1497, 'score_kind': 'LOADING'}]}
        previous = ['base']
        for level in range(128):
            current = []
            for branch in range(2):
                claim, evidence = f'claim-{level}-{branch}', f'evidence-{level}-{branch}'
                document['claims'].append({'id': claim, 'has_evidence': [evidence]})
                document['evidence_items'].append({'id': evidence, 'source_claims': previous})
                current.append(claim)
            previous = current
        document['claims'][-1]['has_score'] = ['score']
        # The original implementation doubled visits at each shared layer.
        # Bound calls explicitly instead of relying on a machine-specific timer.
        limit = 16 * sum(len(rows) for rows in document.values())
        count = 0
        original = source_validation.references
        def bounded_references(*args):
            nonlocal count
            count += 1
            self.assertLess(count, limit, 'Source DAG traversal repeats shared branches')
            return original(*args)
        with patch.object(source_validation, 'references', side_effect=bounded_references):
            self.assertEqual(observation_findings(document, self.sources), [])
        document['claim_scores'][0]['value'] = 0.5795
        self.assertEqual({row['check'] for row in observation_findings(document, self.sources)}, {'source-metric'})
        # Cycles do not suppress reachable observations or recurse indefinitely.
        document['evidence_items'][0]['source_claims'] = previous
        document['claim_scores'][0]['value'] = 0.1497
        self.assertEqual(observation_findings(document, self.sources), [])


if __name__ == '__main__':
    unittest.main()
