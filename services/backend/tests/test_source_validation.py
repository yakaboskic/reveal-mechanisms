"""Regression coverage for the shared source-fidelity gate."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.source_validation import bind_sources, exact_document, ledger_sources, observation_findings, source_findings, validate_new_files


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

    def test_pigean_gene_phenotype_scores_are_score_kinds(self):
        row = {'gene': 'INS', 'phenotype': 'T2D', 'combined': 12.3, 'log_bf': 5.11, 'prior': 7.23}
        self.sources = {'file': {'result': {'items': [row]}}}
        self.document['evidence_items'][0].update(context='Captured /result/items/0.', snippet='')
        for metric in ('combined', 'log_bf', 'prior'):
            with self.subTest(metric=metric):
                self.document['claim_scores'][0].update(metric=metric, value=row[metric], score_kind='SCORE')
                self.assertEqual(self.checks(), set())
                for kind in ('LOADING', 'EFFECT_ESTIMATE', 'PROBABILITY'):
                    self.document['claim_scores'][0]['score_kind'] = kind
                    self.assertIn('source-metric-kind', self.checks())

    def test_release_beta_and_factor_trait_metrics_keep_their_kinds(self):
        rows = {'beta_uncorrected': ('EFFECT_ESTIMATE', 0.0213), 'beta': ('EFFECT_ESTIMATE', 0.0102), 'avg_postp': ('POSTERIOR_PROBABILITY', 0.734),
                'phewas_beta': ('EFFECT_ESTIMATE', 0.0412), 'nnls_loading': ('LOADING', 0.218), 'phewas_q': ('SCORE', 0.0031),
                'phewas_p_onesided': ('SCORE', 1.2e-05)}
        self.sources = {'file': {'result': {'items': [{metric: value for metric, (_, value) in rows.items()}]}}}
        self.document['evidence_items'][0].update(context='Captured /result/items/0.', snippet='')
        for metric, (kind, value) in rows.items():
            with self.subTest(metric=metric):
                self.document['claim_scores'][0].update(metric=metric, value=value, score_kind=kind)
                self.assertEqual(self.checks(), set())
                for other in {'EFFECT_ESTIMATE', 'LOADING', 'POSTERIOR_PROBABILITY', 'PROBABILITY', 'SCORE'} - {kind}:
                    self.document['claim_scores'][0]['score_kind'] = other
                    self.assertIn('source-metric-kind', self.checks())

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


class SourceBindingTests(unittest.TestCase):
    """An author quotes a source row and says what was asked; REVEAL ties the quote to the source that holds it."""
    RAF1 = {'gene_index': 12891, 'loading': 1.0, 'symbol': 'RAF1'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts, self.files = {}, []
        # Two factors' gene loadings share one identical row; a third source is the authoring kit, never eligible.
        self.first = self.source('first', {'operation': 'get_factor_loadings', 'arguments': {'factor_id': 'factor:kpn:0005479:Factor1', 'kind': 'gene'},
                                           'result': {'items': [{'gene_index': 7, 'loading': 0.75, 'symbol': 'KRAS'}, self.RAF1]}})
        self.second = self.source('second', {'operation': 'get_factor_loadings', 'arguments': {'factor_id': 'factor:kpn:0006109:Factor1', 'kind': 'gene'},
                                             'result': {'items': [self.RAF1, {'gene_index': 9, 'loading': 0.5, 'symbol': 'PTEN'}]}})
        self.kit = self.source('kit', {'result': {'items': [{'gene_index': 7, 'loading': 0.75, 'symbol': 'KRAS'}]}}, eligible=False)
        self.package = {'source_artifacts': self.artifacts, 'dapper_context': {'files': self.files},
                        'validation_context': {'format': 'reveal.validation-context/1', 'eligible_source_ids': [self.first, self.second]},
                        'pigean': {'model': 'synthetic', 'mechanisms': {}}}
        self.package_path = self.root / 'evidence-package.json'; self.package_path.write_bytes(canonical_json(self.package))

    def source(self, name, body, eligible=True):
        raw = canonical_json(body); (self.root / (name + '.json')).write_bytes(raw)
        identity = 'dapper:File.' + name
        self.artifacts[name] = {'path': name + '.json', 'sha256': sha256(raw), 'size_bytes': len(raw), 'format': 'json', 'dapper_file_id': identity}
        self.files.append({'id': identity, 'filename': name + '.json', 'sha256': sha256(raw), 'size_in_bytes': len(raw)})
        return identity

    def item(self, snippet, context='As queried', **extra):
        return {'id': 'urn:e:1', 'target_proposition': 'urn:p:1', 'direction': 'SUPPORTS', 'explanation': 'Shown directly.',
                'context': context, **({'snippet': snippet} if snippet is not None else {}), **extra}

    def bind(self, *items, **groups):
        document = {'evidence_items': list(items), **groups}
        before = json.dumps(document, sort_keys=True)
        bound, findings = bind_sources(document, self.package, self.package_path)
        self.assertEqual(json.dumps(document, sort_keys=True), before)       # the author's document is never changed
        return bound, findings

    def test_a_quoted_row_is_tied_to_the_one_source_that_holds_it_with_its_exact_place(self):
        kras = json.dumps({'gene_index': 7, 'loading': 0.75, 'symbol': 'KRAS'})
        # The kit source also holds this row; only eligible sources are searched, so the match is unique.
        for snippet in (kras, json.dumps(json.loads(kras), indent=2), '{"gene_index":7,"loading":0.75,"symbol":"KRAS"}'):
            bound, findings = self.bind(self.item(snippet, 'get_factor_loadings, gene loadings'))
            self.assertEqual(findings, [])
            item = bound['evidence_items'][0]
            self.assertEqual(item['was_derived_from'], [self.first])
            self.assertEqual(item['context'], 'get_factor_loadings, gene loadings. Source row: /result/items/0')
            self.assertEqual(bound['files'], [self.files[0]])                # the trusted File node, exactly as frozen
        # What was bound is exactly what the source checks accept: the snippet and the score agree with that row.
        document = {'claims': [{'id': 'urn:c:1', 'has_evidence': ['urn:e:1'], 'has_score': ['urn:s:1']}],
                    'claim_scores': [{'id': 'urn:s:1', 'metric': 'loading', 'value': 0.75, 'score_kind': 'LOADING'}],
                    'evidence_items': [self.item(kras)]}
        bound, findings = bind_sources(document, self.package, self.package_path)
        self.assertEqual(findings + source_findings(bound, self.package, self.package_path), [])
        wrong = deepcopy(bound); wrong['claim_scores'][0]['value'] = 0.5
        self.assertEqual([f['check'] for f in source_findings(wrong, self.package, self.package_path)], ['source-metric'])
        # A GeneSet the author names stays; the source File is added in front of it.
        bound, _ = self.bind(self.item(kras, was_derived_from=['dapper:GeneSet.x']))
        self.assertEqual(bound['evidence_items'][0]['was_derived_from'], [self.first, 'dapper:GeneSet.x'])
        # A quote inside a longer text value gets that value's place; a partial row gets the source and no place.
        text = self.source('note', {'segments': [{'text': 'Before. The assay showed a clear dose response in liver. After.'}]})
        self.package['validation_context']['eligible_source_ids'].append(text)
        bound, findings = self.bind(self.item('The assay showed a clear dose response in liver.'))
        self.assertEqual((findings, bound['evidence_items'][0]['context']), ([], 'As queried. Source row: /segments/0/text'))
        bound, findings = self.bind(self.item('"loading":0.75,"symbol":"KRAS"'))
        self.assertEqual((findings, bound['evidence_items'][0]['was_derived_from'], bound['evidence_items'][0]['context']), ([], [self.first], 'As queried'))

    def test_a_row_in_two_results_is_settled_by_what_the_author_says_was_asked_and_never_guessed(self):
        raf1 = json.dumps(self.RAF1)
        for said, expected, place in (('get_factor_loadings for factor:kpn:0005479:Factor1, kind=gene', self.first, '/result/items/1'),
                                      ('get_factor_loadings for factor:kpn:0006109:Factor1, kind=gene', self.second, '/result/items/0')):
            bound, findings = self.bind(self.item(raf1, said))
            self.assertEqual(findings, [])
            self.assertEqual(bound['evidence_items'][0]['was_derived_from'], [expected])
            self.assertTrue(bound['evidence_items'][0]['context'].endswith('Source row: ' + place))
        # The Proposition's own statement counts as what was said.
        bound, findings = self.bind(self.item(raf1), propositions=[{'id': 'urn:p:1', 'statement': 'RAF1 has loading 1 on factor:kpn:0006109:Factor1.'}])
        self.assertEqual((findings, bound['evidence_items'][0]['was_derived_from']), ([], [self.second]))
        # An identifier is named only as a whole: text about Factor10 does not name Factor1, and the reverse.
        ten = self.source('tenth', {'operation': 'get_factor_loadings', 'arguments': {'factor_id': 'factor:kpn:0005479:Factor10', 'kind': 'gene'},
                                    'result': {'items': [self.RAF1]}})
        self.package['validation_context']['eligible_source_ids'].append(ten)
        for said, expected in (('get_factor_loadings for factor:kpn:0005479:Factor10, kind=gene', ten),
                               ('get_factor_loadings for factor:kpn:0005479:Factor1, kind=gene', self.first),
                               ('get_factor_loadings for factor:kpn:0006109:Factor1.', self.second)):
            bound, findings = self.bind(self.item(raf1, said))
            self.assertEqual((findings, bound['evidence_items'][0]['was_derived_from']), ([], [expected]), said)
        self.package['validation_context']['eligible_source_ids'].remove(ten)
        # Nothing says which result is meant, or both are named: it is reported, and nothing is bound.
        for said in ('get_factor_loadings, kind unspecified', 'factor:kpn:0005479:Factor1 and factor:kpn:0006109:Factor1'):
            bound, findings = self.bind(self.item(raf1, said))
            self.assertEqual([(f['check'], f['where'], f['severity']) for f in findings], [('source-binding', 'urn:e:1', 'error')])
            self.assertIn('occurs in more than one result', findings[0]['message'])
            self.assertNotIn('was_derived_from', bound['evidence_items'][0]); self.assertNotIn('files', bound)
            self.assertNotIn('RAF1', findings[0]['message'])                 # a finding never repeats source content

    def test_a_quote_that_is_in_no_source_or_is_missing_is_reported_and_documents_that_name_files_are_untouched(self):
        for item, expected in ((self.item('{"gene_index":1,"loading":0.9,"symbol":"MADEUP"}'), 'not verbatim text of any source this run retrieved'),
                               (self.item(None), 'names no source REVEAL can find'), (self.item('KRAS'), 'names no source REVEAL can find')):
            bound, findings = self.bind(item)
            self.assertEqual([f['check'] for f in findings], ['source-binding']); self.assertIn(expected, findings[0]['message'])
        # A synthesis rests on other Claims and quotes nothing; an item that names its File is left exactly as written,
        # and binding an already bound document changes nothing.
        synthesis = {'id': 'urn:e:2', 'source_claims': ['urn:c:1'], 'context': 'The atomic Claims.'}
        # A synthesis may carry words of its own in snippet; they are not a quoted source and are never searched for.
        worded = {'id': 'urn:e:3', 'source_claims': ['urn:c:1'], 'context': 'The atomic Claims.', 'snippet': 'A summary found in no source at all.'}
        explicit = self.item('{"gene_index":9,"loading":0.5,"symbol":"PTEN"}', 'Captured row /result/items/1.', was_derived_from=[self.second])
        # Prior science the package carries as trusted is never rewritten, whatever it quotes or omits.
        prior = {'id': 'dapper:EvidenceItem.prior', 'target_proposition': 'urn:p:9', 'context': 'An earlier account.', 'snippet': json.dumps(self.RAF1)}
        self.package['dapper_context']['evidence_items'] = [prior]
        document = {'evidence_items': [synthesis, worded, explicit, deepcopy(prior)]}
        self.assertEqual(bind_sources(document, self.package, self.package_path), (document, []))
        del self.package['dapper_context']['evidence_items']
        once, _ = self.bind(self.item(json.dumps(self.RAF1), 'factor:kpn:0005479:Factor1'))
        self.assertEqual(bind_sources(once, self.package, self.package_path), (once, []))

    def test_a_quoted_tool_response_of_this_run_is_bound_with_its_own_file_record(self):
        ledger = Ledger(self.root / 'ledger', 'binding-job', 1)
        entry = ledger.start('query_graph', {'graph': 'prokn'}, 'prokn')
        ledger.finish(entry, {'content': [{'type': 'text', 'text': json.dumps({'triples': [{'s': 'urn:gene:RAF1', 'p': 'urn:rel:interacts', 'o': 'urn:gene:BRAF'}]})}]}, 'completed')
        ledger.freeze()
        manifest = self.root / 'ledger/manifest.json'
        document = {'evidence_items': [self.item('urn:gene:RAF1\\", \\"p\\": \\"urn:rel:interacts', 'query_graph on prokn for RAF1')]}
        quoted = json.loads(json.loads((self.root / 'ledger' / entry['response']['path']).read_bytes())['content'][0]['text'])
        document['evidence_items'][0]['snippet'] = json.dumps(quoted['triples'][0])[1:40]
        bound, findings = bind_sources(document, self.package, self.package_path, manifest)
        self.assertEqual(findings, [])
        node = bound['files'][0]
        self.assertEqual(node, {'id': 'urn:reveal:tool-response:sha256:' + entry['response']['sha256'], 'filename': entry['response']['sha256'] + '.json',
                                'mime_type': 'application/json', 'sha256': entry['response']['sha256'], 'size_in_bytes': entry['response']['size_bytes']})
        self.assertEqual(bound['evidence_items'][0]['was_derived_from'], [node['id']])
        self.assertIn(f"Source row: ledger_sequence={entry['sequence']};pointer=/content/0/text", bound['evidence_items'][0]['context'])
        validate_new_files(bound, {}, ledger_sources(manifest))


if __name__ == '__main__':
    unittest.main()
