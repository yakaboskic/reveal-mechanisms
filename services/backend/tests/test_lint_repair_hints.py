"""Lint findings say what to copy, so an author repairs the draft in place instead of regenerating it."""
from copy import deepcopy
import json

import reveal_backend.authoring_structure as structure
from reveal_backend.source_validation import observation_findings

ROW = {'factor': 'Factor3', 'factor_value': 0.1497, 'gene': 'IL6', 'phenotype': 'LymphoCount'}
SOURCES = {'file': {'data': [ROW, {'factor_value': 0.5795, 'gene': 'OTHER'}]}}
DOCUMENT = {
    'claims': [{'id': 'claim', 'has_evidence': ['evidence'], 'has_score': ['score']}],
    'claim_scores': [{'id': 'score', 'metric': 'factor_value', 'value': 0.1497, 'score_kind': 'LOADING'}],
    'evidence_items': [{'id': 'evidence', 'was_derived_from': ['file'], 'context': 'Captured row /data/0.', 'snippet': json.dumps(ROW)}]}


def findings(**changes):
    document = deepcopy(DOCUMENT)
    for group, values in changes.items(): document[group][0].update(values)
    return {item['check']: item for item in observation_findings(document, SOURCES)}


def test_a_wrong_score_value_reports_the_cited_row_value_to_copy():
    repair = findings(claim_scores={'value': 0.5795})['source-metric']['repair']
    assert repair == {'metric': 'factor_value', 'value': '0.5795', 'source_values': ['0.1497']}


def test_a_metric_absent_from_the_cited_row_lists_the_row_numeric_fields():
    repair = findings(claim_scores={'metric': 'normalized_score'})['source-metric']['repair']
    assert repair['metric'] == 'normalized_score' and repair['source_metrics'] == ['factor_value']


def test_a_paraphrased_snippet_reports_the_cited_source_text():
    repair = findings(evidence_items={'snippet': 'IL6 loads strongly on Factor3'})['evidence-snippet']['repair']
    assert repair['excerpt_truncated'] is False
    assert json.loads(repair['source_excerpt']) == ROW  # The exact cited row, so a verbatim excerpt can be copied from it.


def response(findings, tmp_path):
    return structure.diagnostic_response({'valid': False, 'findings': findings}, output=tmp_path, filename='account-1.json')['structuredContent']


def test_feedback_keeps_bounded_repairs_but_never_rendered_messages(tmp_path):
    long_text = 'x' * 5000
    raw = [{'severity': 'error', 'check': 'source-metric', 'where': 'score', 'message': 'PRIVATE_RENDERED_MESSAGE',
            'repair': {'metric': 'loading', 'value': '1.0', 'source_values': ['0.9993'] * 50}},
           {'severity': 'error', 'check': 'evidence-snippet', 'where': 'evidence', 'message': 'PRIVATE_RENDERED_MESSAGE',
            'repair': {'source_excerpt': long_text, 'excerpt_truncated': True}},
           {'severity': 'error', 'check': 'source-file', 'where': 'file', 'message': 'PRIVATE_RENDERED_MESSAGE',
            'repair': {'leak': 'PRIVATE_REPAIR_OF_ANOTHER_CHECK'}}]
    value = response(raw, tmp_path)
    by_check = {item['check']: item for item in value['findings']}
    assert by_check['source-metric']['repair']['source_values'] == ['0.9993'] * 20
    assert len(by_check['evidence-snippet']['repair']['source_excerpt']) == 400
    assert 'repair' not in by_check['source-file']
    assert 'PRIVATE_' not in json.dumps(value)
    assert 'repair.source_values' in by_check['source-metric']['message']


def test_dangling_references_name_only_an_identifier(tmp_path):
    identifier = 'dapper:EvidenceItem.' + 'a' * 32
    value = response([
        {'severity': 'error', 'check': 'refs', 'where': 'claims[0].has_evidence',
         'message': f'reference to {identifier} does not resolve to any node in this document (also at 2 other location(s))'},
        {'severity': 'error', 'check': 'refs', 'where': 'claims[1].has_evidence',
         'message': 'reference to PRIVATE prose here does not resolve to any node in this document'}], tmp_path)
    first, second = value['findings']
    assert first['repair'] == {'unresolved': identifier} and 'repair.unresolved' in first['message']
    assert 'repair' not in second and 'PRIVATE' not in json.dumps(value)
