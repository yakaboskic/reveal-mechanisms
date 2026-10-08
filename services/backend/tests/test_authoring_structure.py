"""Locked shape preflight and bounded hosted feedback without scientific calls."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from reveal_backend import authoring_structure as structure, box_remote, scientific_account_lint
from reveal_backend.box_mcp import Ledger, ScopedTools
from reveal_backend.evidence_package import canonical_json

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / 'services/backend/agent-runtime/dapper-release.json'
RELEASE = Path(os.environ.get('REVEAL_TEST_DAPPER_RELEASE', ROOT / '.deployment-assets/dapper'))


@pytest.fixture
def draft():
    return json.loads((ROOT / 'services/backend/agent-runtime/authoring-skeleton.json').read_bytes())


def preflight(draft):
    if not RELEASE.exists(): pytest.skip('A local checkout of the locked release is required')
    return structure.preflight_document(draft, dapper_root=RELEASE, release_lock=LOCK)


def test_skeleton_defers_only_omitted_attribution_and_dependency_bodies(draft):
    before = canonical_json(draft)
    report = preflight(draft)
    assert report['valid'], report
    # One account, six Claims (five atomic families and a gap-relevance synthesis), their Propositions and
    # EvidenceItems, and five ClaimScores; the server-declared prefixes are the only non-node key.
    assert report['counts'] == {'nodes': 24, 'errors': 0, 'warnings': 0}
    assert report['dapper_release']['tag'] == '0.2.0'
    assert report['document_sha256'] == hashlib.sha256(before).hexdigest()
    assert canonical_json(draft) == before
    assert set(draft) == {'prefixes', 'scientific_accounts', 'propositions', 'claims', 'evidence_items', 'claim_scores'}
    assert draft['prefixes'] == {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:', 'biolink': 'https://w3id.org/biolink/vocab/'}


def test_skeleton_is_the_authored_part_of_the_complete_example(draft):
    example = json.loads((ROOT / 'services/backend/agent-runtime/authoring-examples.json').read_bytes())['documents'][0]
    for group in ('propositions', 'evidence_items', 'claim_scores'):
        assert draft[group] == example[group]
    for group in ('claims', 'scientific_accounts'):
        assert draft[group] == [{k: v for k, v in node.items() if k not in ('was_attributed_to', 'was_generated_by')} for node in example[group]]
    assert draft['prefixes'] == example['prefixes']


def test_freeform_scores_source_claim_reuse_and_supplied_trusted_groups_remain_valid():
    bundle = json.loads((ROOT / 'services/backend/agent-runtime/authoring-examples.json').read_bytes())
    draft = bundle['documents'][0]
    assert draft['claim_scores'] and any(item.get('source_claims') for item in draft['evidence_items'])
    report = preflight(draft)
    assert report['valid'], report
    # A text-only (free-form) synthesis Proposition remains valid.
    synthesis = next(item for item in draft['propositions'] if item['proposition_kind'] == 'BIOLOGICAL_INTERPRETATION')
    for key in ('subject_entity', 'relation', 'object_entity'): synthesis.pop(key)
    report = preflight(draft)
    assert report['valid'], report


def test_invented_fields_and_bare_relations_have_compact_exact_diagnostics(draft):
    private = 'PRIVATE_RESEARCHER_PROSE_' * 20_000
    claim = draft['claims'][0]
    claim['subject_proposition'] = claim.pop('proposition')
    claim.update(assessment=private, scope=private)
    draft['evidence_items'][0]['source_ref'] = {'pointer': '/private'}
    draft['scientific_accounts'][0]['required_question'] = draft['scientific_accounts'][0].pop('question')
    draft['scientific_accounts'][0]['context'] = private
    draft['propositions'][0]['relation'] = 'associated_with'
    report = preflight(draft)
    assert not report['valid'] and not report.get('operational_error'), report
    findings = report['findings']
    assert any(f['where'] == 'claims[0].proposition' and f['rule'] == 'required' for f in findings)
    assert any(f['where'] == 'claims[0]' and f['fields'] == ['assessment', 'scope', 'subject_proposition'] for f in findings if f['rule'] == 'additionalProperties')
    assert any(f['where'] == 'evidence_items[0]' and f.get('fields') == ['source_ref'] for f in findings)
    assert any(f['where'] == 'propositions[0].relation' and f['rule'] == 'identifier' for f in findings)
    assert 'PRIVATE_RESEARCHER_PROSE' not in json.dumps(report)
    assert len(canonical_json(report)) < 6000
    assert claim['assessment'] == private


@pytest.mark.parametrize('change,where', [
    (lambda d: d['claims'][0].update(was_attributed_to=[]), 'claims[0].was_attributed_to'),
    (lambda d: d['claims'][0].update(was_generated_by=123), 'claims[0].was_generated_by'),
    (lambda d: d['claims'][0].update(direction='positive'), 'claims[0].direction'),
    (lambda d: d['propositions'][0].pop('object_entity'), 'propositions[0].object_entity'),
    (lambda d: d['claims'][0].update(proposition=d['evidence_items'][0]['id']), 'claims[0].proposition'),
    (lambda d: d['evidence_items'][0].pop('direction'), 'evidence_items[0].direction'),
    (lambda d: d['evidence_items'][0].update(target_proposition='urn:example:other'), 'evidence_items[0].target_proposition'),
])
def test_supplied_invalid_fields_and_local_graph_rules_are_not_deferred(draft, change, where):
    change(draft)
    report = preflight(draft)
    assert not report['valid'], report
    assert any(f['where'].startswith(where) for f in report['findings']), report


def test_schema_lock_failure_is_operational_and_has_no_untrusted_exception_text(draft, tmp_path):
    lock = json.loads(LOCK.read_bytes()); lock['files']['schema/claims.yaml'] = '0' * 64
    path = tmp_path / 'lock.json'; path.write_text(json.dumps(lock))
    report = structure.preflight_document(draft, dapper_root=RELEASE, release_lock=path)
    assert not report['valid'] and report['operational_error']
    assert len(canonical_json(report)) < 1000
    assert 'Traceback' not in json.dumps(report)


def test_malformed_reference_types_keep_precise_schema_errors(draft):
    draft['claims'][0]['proposition'] = {'private': 'DO_NOT_ECHO'}
    draft['claims'][0]['has_evidence'] = [draft['evidence_items'][0]['id'], None, {'private': 'DO_NOT_ECHO'}]
    draft['scientific_accounts'][0]['component_claims'].extend([{}, 42])
    draft['evidence_items'][0]['source_claims'] = [None, {'private': 'DO_NOT_ECHO'}]
    report = preflight(draft)
    assert not report['valid'] and not report.get('operational_error'), report
    assert any(f['rule'] == 'type' and f['where'] == 'claims[0].proposition' for f in report['findings'])
    assert any(f['rule'] == 'type' and f['where'] == 'claims[0].has_evidence[2]' for f in report['findings'])
    assert 'DO_NOT_ECHO' not in json.dumps(report)


def test_circular_source_claims_are_checked_but_missing_reused_bodies_are_deferred(draft):
    draft['evidence_items'][0]['source_claims'] = [draft['claims'][0]['id']]
    report = preflight(draft)
    assert any(f['rule'] == 'evidence_cycle' for f in report['findings']), report
    draft['evidence_items'][0]['source_claims'] = ['urn:example:authorized-reused-claim']
    report = preflight(draft)
    assert report['valid'], report


@pytest.fixture
def hosted_paths(tmp_path, monkeypatch):
    state = tmp_path / 'state'; state.mkdir()
    output = tmp_path / 'output'; output.mkdir()
    (state / 'runtime.json').write_text(json.dumps({'dapper_root': str(RELEASE), 'evidence_package': 'unused'}))
    monkeypatch.setattr(box_remote, 'STATE', state)
    monkeypatch.setattr(box_remote, 'OUTPUT', output)
    monkeypatch.setattr(box_remote, 'BASE', tmp_path)
    monkeypatch.setattr(box_remote.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    return state, output


def test_invalid_writer_and_direct_lint_stop_before_export_or_account_changes(draft, hosted_paths, monkeypatch):
    state, output = hosted_paths
    draft['claims'][0]['assessment'] = 'Synthetic invalid field'
    actual = preflight(draft)
    assert not actual['valid']
    monkeypatch.setattr(structure, 'preflight_document', lambda *a, **k: deepcopy(actual))
    calls = []
    monkeypatch.setattr(box_remote, 'RESEARCH', SimpleNamespace(materialize=lambda: calls.append('export')))
    monkeypatch.setattr(scientific_account_lint, 'lint_scientific_account', lambda *a, **k: calls.append('lint'))
    path = output / 'account-1.json'; original = canonical_json(draft); path.write_bytes(original)
    assert box_remote.write_draft_tool('account-1.json', deepcopy(draft))['isError']
    assert path.read_bytes() == original
    result = box_remote.lint_tool('account-1.json', Ledger(state / 'ledger', 'synthetic-job', 1))
    assert result['isError'] and calls == []
    assert path.read_bytes() == original
    assert Path(result['structuredContent']['report']['path']).is_relative_to(output / 'reports')
    assert list(output.glob('account-*.*')) == [path]


def test_lint_retains_all_normalized_findings_without_repeating_large_account(tmp_path):
    private = 'PRIVATE_SCIENTIFIC_TEXT' * 30_000
    original = {'valid': False, 'report_version': 'reveal.account-lint/1',
        'counts': {'errors': 150, 'warnings': 0}, 'findings': [
            {'severity': 'error', 'check': 'nodes', 'where': f'claims[{index}]',
             'message': f"Claim: {{'statement': '{private}'}} is not valid under any schema in /has_evidence", 'why': ''}
            for index in range(150)]}
    result = structure.diagnostic_response(original, output=tmp_path, filename='account-1.json')
    summary = result['structuredContent']; descriptor = summary['report']
    assert descriptor['retained'] and summary['finding_count'] == 150
    assert summary['findings_included'] + summary['findings_omitted'] == 150
    assert summary['findings_omitted'] > 0
    assert summary['groups'] == [{'severity': 'error', 'check': 'nodes', 'rule': 'schema', 'count': 150}]
    assert len(canonical_json(summary)) < structure.MAX_PREVIEW_BYTES + 700
    raw = Path(descriptor['path']).read_bytes()
    assert descriptor['sha256'] == hashlib.sha256(raw).hexdigest()
    report = json.loads(raw)
    assert report['findings_complete'] and len(report['findings']) == 150
    assert {f['where'] for f in report['findings']} == {f'claims[{index}]/has_evidence' for index in range(150)}
    assert report['counts'] == {'errors': 150, 'warnings': 0}
    assert 'PRIVATE_SCIENTIFIC_TEXT' not in raw.decode() + json.dumps(result)
    assert private in original['findings'][0]['message']
    assert len(raw) < 40_000
    again = structure.diagnostic_response(original, output=tmp_path, filename='account-1.json')
    assert again['structuredContent']['report'] == descriptor
    assert len(list((tmp_path / 'reports').iterdir())) == 1


def test_report_storage_limits_or_symlinks_never_change_accounts(tmp_path, monkeypatch):
    report = {'valid': False, 'findings': [structure._finding('shape', 'claims', 'type', 'Use an array.')]}
    outside = tmp_path / 'outside'; outside.mkdir()
    output = tmp_path / 'output'; output.mkdir()
    (output / 'reports').symlink_to(outside, target_is_directory=True)
    result = structure.diagnostic_response(report, output=output, filename='account-1.json')
    assert not result['structuredContent']['report']['retained']
    assert list(outside.iterdir()) == []
    (output / 'reports').unlink()
    monkeypatch.setattr(structure, 'MAX_REPORT_BYTES', 50)
    result = structure.diagnostic_response(report, output=output, filename='account-1.json')
    assert not result['structuredContent']['report']['retained']
    assert result['structuredContent']['finding_count'] == 1
    assert list(output.iterdir()) == []


def test_source_feedback_and_advisories_preserve_actionable_nonvalue_rules(tmp_path):
    checks = ['evidence-snippet', 'source-metric-kind', 'source-file', 'evidence-target', 'evidence-interpretation', 'draft-id-collision']
    report = {'valid': False, 'findings': [{'severity': 'error', 'check': check, 'where': 'evidence_items[0]',
        'message': 'PRIVATE_SOURCE_VALUE'} for check in checks],
        'advisories': [{'severity': 'advisory', 'check': 'cfde-grounding-missing',
            'where': 'scientific_accounts[0]', 'message': 'PRIVATE_SOURCE_VALUE'}]}
    response = structure.diagnostic_response(report, output=tmp_path, filename='account-1.json')['structuredContent']
    assert all('Inspect this location' not in f['message'] for f in response['findings'])
    assert 'one width' in response['findings'][-1]['message'] and response['findings'][-1]['rule'] == 'draft-id-collision'
    assert 'supported independent evidence remains eligible' in response['advisories'][0]['message']
    stored = json.loads(Path(response['report']['path']).read_bytes())
    assert stored['advisory_count'] == 1 and len(stored['advisories']) == 1
    assert 'PRIVATE_SOURCE_VALUE' not in json.dumps(response) + json.dumps(stored)


def test_passed_lint_stops_repairs_and_preserves_optional_advice(tmp_path):
    report = {'valid': True, 'counts': {'errors': 0, 'warnings': 0}, 'findings': [],
        'advisories': [{'severity': 'advisory', 'check': 'cfde-grounding-missing',
            'where': 'scientific_accounts[0]', 'message': 'optional'}]}
    result = structure.diagnostic_response(report, output=tmp_path, filename='account-1.json')
    assert not result['isError']
    value = result['structuredContent']
    assert 'Draft lint passed. Stop schema repairs' in value['guidance']
    assert 'independent validation and acceptance' in value['guidance']
    assert value['advisory_count'] == 1 and value['counts']['errors'] == 0


def test_schema_diagnostic_path_parser_cannot_copy_quoted_account_text(tmp_path):
    report = {'valid': False, 'findings': [{'severity': 'error', 'check': 'nodes', 'where': 'claims[0]',
        'message': "Claim: {'statement': 'Words in /PRIVATE_SOURCE then more text'} is invalid in /has_evidence"}]}
    response = structure.diagnostic_response(report, output=tmp_path, filename='account-1.json')
    assert response['structuredContent']['findings'][0]['where'] == 'claims[0]/has_evidence'
    assert 'PRIVATE_SOURCE' not in json.dumps(response)


def test_reports_are_readable_to_agent_under_restrictive_runner_umask(tmp_path):
    original = os.umask(0o077)
    try:
        report = {'valid': True, 'findings': []}
        value = structure.diagnostic_response(report, output=tmp_path, filename='account-1.json')['structuredContent']
    finally:
        os.umask(original)
    path = Path(value['report']['path'])
    assert value['report']['retained']
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    path.chmod(0o600)
    value = structure.diagnostic_response(report, output=tmp_path, filename='account-1.json')['structuredContent']
    assert value['report']['retained'] and path.stat().st_mode & 0o777 == 0o644


def test_remaining_capture_budget_and_operational_error_are_truthful(tmp_path, monkeypatch):
    from reveal_backend import box_upload
    monkeypatch.setattr(box_upload, 'MAX_TOTAL', 100_001)
    output = tmp_path / 'output'; output.mkdir()
    account = output / 'account-1.json'; account.write_bytes(b'preserved draft')
    report = {'valid': False, 'operational_error': True, 'findings': [
        structure._finding('structure-runtime', 'runtime', 'unavailable', 'Retry the checker.')]}
    value = structure.diagnostic_response(report, output=output, filename='account-1.json', capture_roots=(output,))['structuredContent']
    assert not value['report']['retained']
    assert 'without changing scientific claims' in value['guidance']
    assert account.read_bytes() == b'preserved draft'
    assert list((output / 'reports').iterdir()) == []


def test_mcp_hints_accept_optional_schema_and_explain_actual_fields(tmp_path, draft):
    tools = ScopedTools([], Ledger(tmp_path / 'ledger', 'synthetic-job', 1), write_draft=lambda *a: None, lint=lambda *a: None)
    definitions = {item['name']: item for item in tools.definitions()}
    draft['claim_scores'] = [{'id': 'urn:example:score', 'score_kind': 'LOADING', 'value': 0.125}]
    draft['propositions'][0] = {'id': 'urn:example:proposition', 'statement': 'Synthetic narrative.'}
    draft['gene_sets'] = [{'id': 'urn:example:set'}]
    jsonschema.validate({'filename': 'account-1.json', 'document': draft}, definitions['write_account_draft']['inputSchema'])
    description = definitions['write_account_draft']['description']
    assert 'Claim.proposition' in description and 'ScientificAccount.question' in description
    assert 'source_ref is not an EvidenceItem field' in description
    assert 'readable report path/hash' in definitions['lint_account']['description']
