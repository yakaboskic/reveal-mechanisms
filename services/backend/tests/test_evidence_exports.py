"""Durable closure/replay acceptance cases use isolated storage and no scientific calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from reveal_backend import research_execution as execution, user_inputs
from reveal_backend.auth import Problem
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import Repository
from reveal_backend.research_tools import dispatch, definitions
from reveal_backend.research_work import ResearchWorkService, issue_grant
from test_local_launcher import launcher


@pytest.fixture
def research(tmp_path, monkeypatch):
    repo = Repository(str(tmp_path/'app.sqlite')); repo.migrate()
    monkeypatch.setattr(user_inputs, 'artifacts_root', lambda: tmp_path/'store')
    monkeypatch.setattr(execution, 'artifacts_root', lambda: tmp_path/'store')
    monkeypatch.setattr(user_inputs, 's3_enabled', lambda: False)
    service = ResearchWorkService(repo)
    package = {'selection': {'knowledge_gap_id': 'dapper:KnowledgeGap.fixture'},
        'research_request_id': 'request', 'reference_generation_id': 'a'*64,
        'source_artifacts': {}, 'dapper_context': {}}
    work = {'id': 'work', 'owner_user_id': 'owner', 'research_request_id': 'request',
        'state': 'ready', 'expires_at': '2999-01-01T00:00:00Z', 'package_id': 'seed',
        'package_sha256': sha256(canonical_json(package)), 'reference_generation_id': 'a'*64}
    with repo.transaction() as tx:
        tx.put('principal', 'owner', 'owner', {'me': {'user_id': 'owner', 'principal_kind': 'registered'}})
        tx.put('request', 'request', 'owner', {'id': 'request', 'composer': {}})
        tx.put('local_work', 'work', 'owner', work)
        tx.put('research_package', 'seed', 'owner', {'package': package, 'artifacts': []})
        grant = issue_grant(tx, 'owner', 'work', 'fixture')
    def receipt(index):
        raw = ('{"items":[{"loading":0.123456789012345678901234567890,"n":'+str(index)+'}]}').encode()
        source = {'path': f'sources/query-{index}.json', 'sha256': sha256(raw), 'size_bytes': len(raw),
            'format': 'json', 'dapper_file_id': 'dapper:File.fixture'+str(index)}
        artifact = service.retain('owner', 'work', raw, source['path'])
        context = {'artifact_ids': {source['path']: artifact['id']}, 'source_artifacts': {str(index): source},
            'dapper_context': {'files': [{'id': source['dapper_file_id'], 'sha256': source['sha256'], 'size_in_bytes': len(raw)}]},
            'eligible_source_ids': [source['dapper_file_id']]}
        with repo.transaction() as tx:
            tx.put('evidence_receipt', 'receipt-'+str(index), 'owner', {'id': 'receipt-'+str(index),
                'local_work_id': 'work', 'research_request_id': 'request', 'context': context})
        return 'receipt-'+str(index), artifact, raw
    return SimpleNamespace(repo=repo, service=service, work=work, package=package,
        auth='Bearer '+grant['token'], grant=grant, receipt=receipt, root=tmp_path)


def export(research, ids, key='export'):
    return dispatch(research.service, research.auth, 'export_evidence_context', {
        'local_work_id': 'work', 'receipt_ids': ids, 'idempotency_key': key})


def result(research, operation):
    return dispatch(research.service, research.auth, 'get_operation', {
        'local_work_id': 'work', 'operation_id': operation})


def test_seed_dismech_attachment_survives_receipt_free_export_after_restart(research):
    raw = canonical_json({'id': 'dismech:fixture#/pathophysiology/0',
                          'raw': {'description': 'Pinned independent source observation.'}})
    file_id = 'dapper:File.dismech-fixture'
    relative = 'sources/' + sha256(raw) + '.json'
    artifact = research.service.retain('owner', 'work', raw, relative)
    source = {'path': relative, 'sha256': sha256(raw), 'size_bytes': len(raw), 'format': 'json',
              'dapper_file_id': file_id, 'origin': 'reveal:pinned-dismech-attachment'}
    package = deepcopy(research.package)
    package.update(source_artifacts={'dismech-attachment-0': source}, eligible_source_ids=[file_id],
                   dapper_context={'files': [{'id': file_id, 'sha256': sha256(raw), 'size_in_bytes': len(raw)}]})
    with research.repo.transaction() as tx:
        tx.put('research_package', 'seed', 'owner', {'package': package, 'artifacts': [artifact]})
        work = tx.get('local_work', 'work')['data']; work['package_sha256'] = sha256(canonical_json(package))
        tx.put('local_work', 'work', 'owner', work)
    queued = export(research, [], 'seed-only')
    ResearchWorkService(research.repo).run_operation(queued['operation_id'])
    completed = result(research, queued['operation_id'])
    assert completed['state'] == 'succeeded', completed
    closure = completed['result']
    assert closure['receipt_ids'] == []
    assert next(row for row in closure['artifacts'] if row['path'] == relative)['id'] == artifact['id']
    with research.repo.read_transaction() as tx:
        retained = tx.get('research_artifact', closure['package_artifact']['id'])['data']
    exported = json.loads(execution.read_artifact_bytes(retained))
    assert exported['validation_context']['eligible_source_ids'] == [file_id]
    assert exported['source_artifacts']['dismech-attachment-0'] == source
    assert execution.read_artifact_bytes(artifact) == raw
    from reveal_backend.scientific_account_lint import eligible_source_files
    assert eligible_source_files(exported, research.root / 'evidence-package.json')[0] == {file_id}


@pytest.mark.parametrize('count', [1, 2, 17, 20])
def test_delayed_storage_prompt_ack_bounded_reads_and_restart(research, count):
    receipts = [research.receipt(index) for index in range(count)]
    active = 0; peak = 0; reads = []; lock = threading.Lock(); original = user_inputs.read
    def delayed(storage):
        nonlocal active, peak
        with lock: active += 1; peak = max(peak, active); reads.append(storage['sha256'])
        try:
            time.sleep(.015)
            return original(storage)
        finally:
            with lock: active -= 1
    with patch.object(user_inputs, 'read', side_effect=delayed), patch.object(user_inputs, 'retain', wraps=user_inputs.retain) as retained:
        started = time.monotonic(); queued = export(research, [row[0] for row in receipts])
        assert time.monotonic()-started < 1 and reads == []
        assert export(research, [row[0] for row in receipts]) == queued
        # New service instance reconstructs the operation solely from durable DB state.
        restored = ResearchWorkService(research.repo)
        restored.run_operation(queued['operation_id'])
        completed = result(research, queued['operation_id'])
        assert completed['state'] == 'succeeded', completed
        closure = completed['result']
        assert closure['format'].endswith('/2') and 'package' not in closure
        assert len(reads) == count and peak <= 4
        assert retained.call_count == 1  # only the new closed package, no source copies
        restored.run_operation(queued['operation_id'])
        assert retained.call_count == 1 and len(reads) == count
        assert closure['receipt_ids'] == sorted(row[0] for row in receipts)
        assert set(closure['phase_timings_ms']) == {'selection_authorization_lookup_ms', 'reads_ms', 'merge_hash_ms', 'retention_ms', 'response_construction_ms'}
        assert [a['id'] for a in closure['artifacts'] if a['path'] != 'evidence-package.json'] == [
            row[1]['id'] for row in sorted(receipts, key=lambda row: row[1]['filename'])]
    with research.repo.read_transaction() as tx:
        assert tx.get('local_work', 'work')['data']['last_action'] == 'export_evidence_context'
        assert len(tx.list('research_operation')) == 1


def test_expired_worker_lease_reclaims_one_logical_export(research):
    receipt, _, _ = research.receipt(0); queued = export(research, [receipt])
    with research.repo.transaction() as tx:
        row = tx.get('research_operation', queued['operation_id']); op = row['data']
        op.update(state='running', lease_token='lost-worker', lease_until='2000-01-01T00:00:00Z')
        tx.put('research_operation', queued['operation_id'], row['owner'], op)
    ResearchWorkService(research.repo).run_operation(queued['operation_id'])
    assert result(research, queued['operation_id'])['state'] == 'succeeded'
    assert export(research, [receipt])['operation_id'] == queued['operation_id']
    with pytest.raises(Problem, match='different input'):
        export(research, [])


def test_duplicate_blob_aliases_read_once_and_do_not_add_storage(research):
    receipt, artifact, raw = research.receipt(0)
    alias = research.service.retain('owner', 'work', raw, 'alias.json')
    assert alias['storage'] == artifact['storage']
    with research.repo.transaction() as tx:
        original = tx.get('evidence_receipt', receipt)['data']; other = deepcopy(original)
        other['id'] = 'alias'; tx.put('evidence_receipt', 'alias', 'owner', other)
    with patch.object(user_inputs, 'read', wraps=user_inputs.read) as reads:
        queued = export(research, [receipt, 'alias']); research.service.run_operation(queued['operation_id'])
        assert result(research, queued['operation_id'])['state'] == 'succeeded'
        assert reads.call_count == 1


def test_cross_work_selection_and_commit_revocation_fail_closed(research):
    receipt, _, _ = research.receipt(0)
    with research.repo.transaction() as tx:
        record = tx.get('evidence_receipt', receipt)['data']; record['local_work_id'] = 'other'
        tx.put('evidence_receipt', receipt, 'owner', record)
    queued = export(research, [receipt]); research.service.run_operation(queued['operation_id'])
    with research.repo.read_transaction() as tx:
        operation = tx.get('research_operation', queued['operation_id'])['data']
        assert operation['state'] == 'failed' and operation['error']['code'] == 'NOT_FOUND'
    receipt, _, _ = research.receipt(1)
    queued = export(research, [receipt], 'revoked')
    with patch.object(execution, 'export_context', wraps=execution.export_context) as builder:
        original = execution.export_context
        def revoke(*args):
            value = original(*args)
            with research.repo.transaction() as tx:
                identity = hashlib.sha256(research.grant['token'].encode()).hexdigest()
                row = tx.get('research_access', identity); row['data']['revoked_at'] = '2026-01-01'
                tx.put('research_access', identity, 'owner', row['data'])
            return value
        builder.side_effect = revoke
        research.service.run_operation(queued['operation_id'])
    with research.repo.read_transaction() as tx:
        operation = tx.get('research_operation', queued['operation_id'])['data']
        assert operation['state'] == 'failed' and 'result' not in operation


def test_launcher_verified_incremental_materialization_and_modified_bytes(research):
    receipt, _, raw = research.receipt(0); queued = export(research, [receipt])
    research.service.run_operation(queued['operation_id']); closure = result(research, queued['operation_id'])['result']
    root = research.root/'workspace'; (root/'input').mkdir(parents=True)
    (root/'input/evidence-package.json').write_bytes(canonical_json(research.package))
    (root/'input/manifest.json').write_text(json.dumps({'files': {}, 'package_sha256': research.work['package_sha256']}))
    scope = {**research.work, 'local_work_id': 'work', 'mcp_url': 'http://localhost/mcp'}
    reads = []
    def http(url, **kwargs):
        reads.append(url); identity = url.split('/')[-2]
        with research.repo.read_transaction() as tx: artifact = tx.get('research_artifact', identity)['data']
        return execution.read_artifact_bytes(artifact)
    bridge = launcher.Bridge(root, scope, connection=SimpleNamespace(token=lambda: 'test-only'), http=http)
    bridge.unpack = lambda name, args, token: (queued if name == 'export_evidence_context' else
        result(research, queued['operation_id']))
    args = {'receipt_ids': [receipt], 'idempotency_key': 'export'}
    (root/'output').mkdir()
    draft = {'scientific_accounts': [{'id': 'account', 'component_claims': ['claim']}],
        'claims': [{'id': 'claim', 'has_evidence': ['evidence']}],
        'evidence_items': [{'id': 'evidence', 'was_derived_from': ['dapper:File.fixture0']}]}
    (root/'output/account-1.json').write_text(json.dumps(draft))
    delivered = bridge.materialize(args)
    assert delivered['selected_evidence_present_locally']
    assert delivered['accounts'][0]['cited_evidence_present_locally']
    draft['evidence_items'][0]['was_derived_from'].append('dapper:File.not-selected')
    (root/'output/account-1.json').write_text(json.dumps(draft))
    assert not bridge.materialize(args)['accounts'][0]['cited_evidence_present_locally']
    assert len(reads) == 2
    assert bridge.materialize(args)['state'] == 'succeeded' and len(reads) == 2
    from reveal_backend.evidence_reader import WorkspaceReader
    content = WorkspaceReader(root).read(artifact_id='0', sha256=sha256(raw), pointer='/items/0/loading')
    assert '0.123456789012345678901234567890' in content['content_json']
    source = root/'evidence/closures'/closure['package_sha256']/'sources/query-0.json'
    source.write_bytes(b'tampered')
    with pytest.raises(launcher.SetupError, match='checksum'):
        bridge.materialize(args)
    assert len(reads) == 2


def test_validation_submission_effects_have_distinct_accurate_contracts():
    tools = {tool['name']: tool for tool in definitions()}
    validate = tools['validate_submission']; submit = tools['submit_accounts']
    assert 'Retains private validation results' in validate['description']
    assert 'does not accept accounts or publish' in validate['description']
    assert 'accept' in submit['description'] and submit['description'] != validate['description']
    assert not validate['annotations']['readOnlyHint'] and not submit['annotations']['readOnlyHint']


def test_anonymous_capture_download_has_no_credentials_and_preserves_bytes(research):
    _, artifact, raw = research.receipt(0)
    root = research.root/'anonymous'; (root/'input').mkdir(parents=True)
    (root/'input/manifest.json').write_text('{"files":{}}')
    scope = {'mcp_url': 'https://reveal.example/mcp', 'reference_generation_id': 'a'*64}
    identity = 'd'*64
    source = {'path': 'sources/query-0.json', 'sha256': sha256(raw), 'size_bytes': len(raw)}
    capture = {'capture_id': identity, 'reference_generation_id': 'a'*64,
        'artifacts': [{**source, 'download_url': 'https://reveal.example/v1/public-research/captures/'+identity+'/artifacts/'+sha256(raw)}]}
    calls = []
    def http(url, **kwargs): calls.append(kwargs); return raw
    bridge = launcher.Bridge(root, scope, connection=SimpleNamespace(token=Mock(side_effect=AssertionError('No credentials'))), http=http)
    bridge.unpack = lambda name, args, token: capture if token is None else None
    value = bridge.download_public_capture({'capture_id': identity})
    assert value['state'] == 'succeeded' and len(calls) == 1 and calls[0]['token'] is None
    assert (root/value['path']/source['path']).read_bytes() == raw
    bridge.download_public_capture({'capture_id': identity})
    assert len(calls) == 1


def test_private_reader_is_scoped_and_exact_without_scientific_calls(research):
    _, artifact, raw = research.receipt(0)
    arguments = {'local_work_id': 'work', 'artifact_id': artifact['id'], 'sha256': artifact['sha256'], 'pointer': '/items/0/loading'}
    value = dispatch(research.service, research.auth, 'read_evidence', arguments)
    assert value['content_json'] == '0.123456789012345678901234567890'
    alias = dispatch(research.service, research.auth, 'read_evidence', {**arguments, 'artifact_id': '0'})
    assert alias['content_json'] == value['content_json']
    with pytest.raises(Problem):
        dispatch(research.service, research.auth, 'read_evidence', {**arguments, 'local_work_id': 'foreign'})
    with pytest.raises(Problem):
        dispatch(research.service, research.auth, 'read_evidence', {**arguments, 'sha256': 'f'*64})
    with research.repo.read_transaction() as tx:
        assert tx.list('research_operation') == [] and len(tx.list('evidence_receipt')) == 1


def test_mixed_query_import_reuse_closure_preserves_relationship_provenance(research, monkeypatch):
    from reveal_backend import scientific_reuse
    query, _, _ = research.receipt(0)
    imported, _, _ = research.receipt(1)
    _, original, raw = research.receipt(2)
    gene_set = 'dapper:GeneSet.unchanged'; file_id = 'dapper:File.fixture2'
    graph = {'files': [{'id': file_id, 'sha256': sha256(raw), 'size_in_bytes': len(raw)}],
        'gene_sets': [{'id': gene_set, 'was_derived_from': [file_id]}],
        'propositions': [{'id': 'dapper:Proposition.relationship', 'subject_entity': gene_set,
            'relation': 'urn:relation:involved-in', 'object_entity': 'dapper:Mechanism.unchanged'}],
        'mechanisms': [{'id': 'dapper:Mechanism.unchanged'}],
        'claims': [{'id': 'dapper:Claim.prior', 'proposition': 'dapper:Proposition.relationship',
            'has_evidence': ['dapper:EvidenceItem.original'], 'was_attributed_to': ['urn:actor:original']}],
        'evidence_items': [{'id': 'dapper:EvidenceItem.original', 'was_derived_from': [file_id, gene_set],
            'context': '/items/0/loading', 'snippet': '0.123456789012345678901234567890'}]}
    reused_context = {'dapper_context': graph, 'eligible_source_ids': [file_id],
        'artifact_records': {sha256(raw): {'storage': original['storage'], 'file': graph['files'][0]}},
        'selection': {'object_id': 'dapper:Claim.prior'}}
    visible = [True]
    def resolve(tx, owner, request_id, ids):
        if not visible[0]: raise Problem(403, 'REUSE_AUTHORITY_UNAVAILABLE', 'Borrowed evidence withdrawn.')
        return {'contexts': [deepcopy(reused_context)] if ids else [], 'existing_accounts': [], 'receipt_ids': ids}
    monkeypatch.setattr(scientific_reuse, 'resolve_receipts', resolve)
    with research.repo.transaction() as tx:
        record = tx.get('evidence_receipt', imported)['data']; record['reuse_receipt_ids'] = ['reuse']
        tx.put('evidence_import', 'import', 'owner', record)
    args = {'local_work_id': 'work', 'receipt_ids': [query], 'import_ids': ['import'], 'idempotency_key': 'mixed'}
    queued = dispatch(research.service, research.auth, 'export_evidence_context', args)
    research.service.run_operation(queued['operation_id']); closure = result(research, queued['operation_id'])['result']
    assert closure['reuse_receipt_ids'] == ['reuse']
    with research.repo.read_transaction() as tx:
        package_artifact = tx.get('research_artifact', closure['package_artifact']['id'])['data']
        borrowed = next(item for item in closure['artifacts'] if item['path'].startswith('reuse/'))
        borrowed_artifact = tx.get('research_artifact', borrowed['id'])['data']
    package = json.loads(execution.read_artifact_bytes(package_artifact))
    assert package['dapper_context']['gene_sets'] == graph['gene_sets']
    assert package['dapper_context']['claims'] == graph['claims']
    assert package['dapper_context']['evidence_items'] == graph['evidence_items']
    assert execution.read_artifact_bytes(borrowed_artifact) == raw
    assert borrowed_artifact['storage'] == original['storage']
    visible[0] = False
    with pytest.raises(Problem): result(research, queued['operation_id'])
    with research.repo.read_transaction() as tx:
        with pytest.raises(Problem): execution.authorize_artifact(tx, 'owner', borrowed_artifact)


def test_export_descriptor_matches_generated_api_contract(research):
    from jsonschema import Draft202012Validator, ValidationError
    receipt, _, _ = research.receipt(0); queued = export(research, [receipt])
    research.service.run_operation(queued['operation_id'])
    closure = result(research, queued['operation_id'])['result']
    spec = json.loads((Path(__file__).resolve().parents[3]/'api/openapi.json').read_bytes())
    validator = Draft202012Validator({'components': spec['components'], '$ref': '#/components/schemas/EvidenceContextExport'})
    validator.validate(closure)
    invalid = deepcopy(closure); invalid['package_artifact']['storage'] = {'key': 'must-not-leak'}
    with pytest.raises(ValidationError): validator.validate(invalid)
