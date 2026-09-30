#!/usr/bin/env python3
"""Validate the contract, all examples, scientific identities and negative cases."""
from copy import deepcopy
import hashlib
import gzip
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate_spec
import yaml

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'api'
DAPPER = ROOT / 'data/dapper/2026-09-24-v8/snapshot/schema'
sys.path[:0] = [str(DAPPER / 'identity'), str(DAPPER / 'lint'), str(DAPPER)]
from dapper_identity import DOC_GROUPS, verify, load_schema
from scientific_claims import check_scientific_content, index_document
from citation_metadata import check_citation_metadata, check_citation_registry_links
from lint_provenance import Vocabulary, lint, build_validator


def main():
    spec = json.loads((API / 'openapi.json').read_text())
    assert yaml.safe_load((API / 'openapi.yaml').read_text()) == spec
    validate_spec(spec)
    components = spec['components']
    def validate(value, schema):
        root = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'components': components, **schema}
        Draft202012Validator(root, format_checker=FormatChecker()).validate(value)
    refs = []
    def walk(value):
        if isinstance(value, list):
            for item in value: walk(item)
        if isinstance(value, dict):
            if '$ref' in value:
                r = value['$ref'];refs.append(r)
                assert r.startswith('#/'), 'Expected bundled local ref: ' + r
                target = spec
                for key in r[2:].split('/'):
                    target = target[key.replace('~1', '/').replace('~0', '~')]
            for item in value.values(): walk(item)
    walk(spec)
    names, response_count, request_count, parameter_count = set(), 0, 0, 0
    for path, methods in spec['paths'].items():
        assert 'analysis-runs' not in path and 'paragraph-runs' not in path
        for method, op in methods.items():
            assert op['operationId'] not in names;names.add(op['operationId'])
            assert op.get('x-codeSamples'), (path, 'missing request example')
            for p in op['parameters']:
                validate(p['example'], p['schema']);parameter_count += 1
            for media in op.get('requestBody', {}).get('content', {}).values():
                assert media.get('examples'), op['operationId']
                for ex in media['examples'].values():
                    validate(ex['value'], media['schema']);request_count += 1
            for status, response in op['responses'].items():
                if status == '307':
                    assert op['operationId'] == 'downloadArtifact'
                    assert response['headers']['Location']['schema']['type'] == 'string'
                    assert not response.get('content'), 'Artifact redirects must not proxy the response body'
                    continue
                assert response.get('content'), (path, status)
                for media_type, media in response['content'].items():
                    assert media.get('examples'), (path, status, media_type)
                    for ex in media['examples'].values():
                        try: validate(ex['value'], media['schema'])
                        except ValidationError as err:
                            raise AssertionError(f'{op["operationId"]} {status} {media_type}: {err.message} at {list(err.path)}') from err
                        response_count += 1
                        if media_type == 'text/event-stream':
                            for line in ex['value'].splitlines():
                                if line.startswith('data: '):
                                    validate(json.loads(line[6:]), response.get('x-event-schema', {'$ref': '#/components/schemas/JobEvent'}))
    sv = load_schema(DAPPER / 'dapper.yaml')
    vocab = Vocabulary.build(sv, yaml.safe_load((DAPPER / 'lint/profiles.yaml').read_text()))
    vocab.profiles.update(yaml.safe_load((API / 'paragraph-profile.yaml').read_text()))
    dapper_validator = build_validator(DAPPER / 'dapper.yaml')
    scientific = {}
    for filename in ['scientific-account.dapper.json', 'paragraph.dapper.json', 'knowledge-gap.dapper.json']:
        doc = json.loads((API / 'examples' / filename).read_text())
        assert not verify(doc, sv), filename
        assert not check_scientific_content(index_document(doc)), filename
        count = sum(len(doc.get(group, [])) for group in DOC_GROUPS)
        assert count > 0
        if 'scientific_accounts' in doc:
            profile = 'reveal-paragraph' if 'paragraphs' in doc else 'scientific-account'
            result = lint(API / 'examples' / filename, vocab, sv, dapper_validator, profile_name=profile)
            assert not result.errors, [(x.check, x.message) for x in result.errors]
            scientific[filename] = {'nodes': count, 'profile': profile, 'errors': 0, 'warnings': [{'check': x.check, 'message': x.message} for x in result.warnings]}
        else:
            scientific[filename] = {'nodes': count, 'identity_and_scientific_content': 'passed', 'profile': 'standalone inquiry; no terminal profile exists upstream'}
    records = json.loads((API / 'examples/citation-registry.json').read_text())
    for r in records:
        assert not check_citation_metadata(r), check_citation_metadata(r)
    paragraph = json.loads((API / 'examples/paragraph.dapper.json').read_text())['paragraphs'][0]
    assert not check_citation_registry_links(paragraph, records)
    raw = (API / 'examples/cfde-response.json').read_bytes()
    account_doc = json.loads((API / 'examples/scientific-account.dapper.json').read_text())
    for f in account_doc['files']:
        data = (API / 'examples' / f['filename']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == f['sha256'] and len(data) == f['size_in_bytes']
    gene_set = account_doc['gene_sets'][0]
    with gzip.open(ROOT / 'data/cfde-genesets/2026-09-24/records.jsonl.gz', 'rt') as records_file:
        for line in records_file:
            original = json.loads(line)['gene_set']
            if original['id'] == gene_set['id']:
                assert original == gene_set, 'Existing GeneSet payload must be preserved'
                break
        else: raise AssertionError('Fixture GeneSet must exist in the frozen full catalog import')

    exchanges = json.loads((API / 'examples/exchanges.json').read_text())
    assert {x['operation_id'] for x in exchanges} == names
    job_examples = [x for x in exchanges if x['operation_id'] == 'createJob']
    assert len({x['request']['headers']['Idempotency-Key'] for x in job_examples}) == len(job_examples)
    for ex in exchanges:
        op = spec['paths'][ex['path_template']][ex['method'].lower()]
        if 'requestBody' in op:
            validate(ex['request']['body'], op['requestBody']['content']['application/json']['schema'])
        for param in op['parameters']:
            where = {'path': 'path', 'query': 'query', 'header': 'headers'}[param['in']]
            values = ex['request'][where]
            if param['required']: assert param['name'] in values
            if param['name'] in values: validate(values[param['name']], param['schema'])
        for status, response in ex['responses'].items():
            schema = op['responses'][status]['content'][response['content_type']]['schema']
            for value in response['examples'].values(): validate(value, schema)

    rejected = []
    def reject(label, value, schema):
        try: validate(value, {'$ref': '#/components/schemas/' + schema})
        except ValidationError: rejected.append(label)
        else: raise AssertionError('Invalid example accepted: ' + label)
    reject('client-selected draft owner', {'owner_user_id': '11111111-1111-4111-8111-111111111111'}, 'DraftCreate')
    reject('job kind mismatch', {'kind': 'paragraph', 'draft_id': '22222222-2222-4222-8222-222222222222', 'draft_version': 2}, 'JobCreate')
    reject('missing optimistic version', {'composer': {}}, 'DraftPatch')
    reject('invented DAPPER claim field', {'id': 'dapper:Claim.' + 'a'*32, 'confidence': 0.99}, 'DapperClaim')
    reject('unminted account reference', {'kind': 'paragraph', 'account_id': 'account-one'}, 'ParagraphJobInput')
    reject('zero citation metadata revision', {'target_id': paragraph['citations'][0]['target_id'], 'citation_metadata_revision': 0}, 'CitationTarget')
    score = {'id': 'dapper:ClaimScore.'+'a'*32, 'score_kind': 'PROBABILITY', 'value': 1.5, 'metric': 'probability', 'interpretation': 'Probability of an explicitly defined event.'}
    reject('probability above one', score, 'DapperClaimScore')
    validate({**score, 'score_kind': 'LOADING'}, {'$ref': '#/components/schemas/DapperClaimScore'})
    job = deepcopy(next(x for x in exchanges if x['operation_id'] == 'getJob')['responses']['200']['examples']['completed_analysis'])
    job['result']['kind'] = 'paragraph'
    reject('job result kind mismatch', job, 'Job')
    job['result'] = None
    reject('successful job without a result', job, 'Job')
    composer = deepcopy(next(x for x in exchanges if x['operation_id'] == 'createDraft')['request']['body']['composer'])
    reject('free-text inquiry in composer', {**composer, 'inquiry': {'text': 'invented question'}}, 'Composer')
    reject('client-edited DisMech context', {**composer, 'dismech_context': []}, 'Composer')
    reject('source gap missing source identity', {**composer, 'source_gap': {'id': composer['source_gap']['id'], 'source_revision': 'a'*64}}, 'Composer')
    validate({**composer, 'source_gap': None, 'eaggl_anchors': []}, {'$ref':'#/components/schemas/Composer'})
    # Drafts can be empty. Server submission must check resolved gap/anchor requirements.
    requests = [x for x in exchanges if x['operation_id'] == 'getResearchRequest']
    frozen = requests[0]['responses']['200']['examples']['submitted']
    assert frozen['question_id'] == account_doc['scientific_accounts'][0]['question'] == composer['source_gap']['id']
    assert frozen['document']['knowledge_gaps'][0] == account_doc['knowledge_gaps'][0]
    package_dir = API / 'examples/evidence-package'
    package = json.loads((package_dir / 'evidence-package.json').read_text())
    manifest = json.loads((package_dir / 'manifest.json').read_text())
    assert hashlib.sha256((package_dir / 'evidence-package.json').read_bytes()).hexdigest() == manifest['package_sha256']
    from jsonschema.validators import validator_for
    package_schema = json.loads((ROOT / 'schema/evidence-package.schema.json').read_text())
    validator_for(package_schema)(package_schema, format_checker=FormatChecker()).validate(package)
    validate(package, {'$ref':'#/components/schemas/EvidencePackage'})
    assert package['selection']['knowledge_gap_id'] == composer['source_gap']['id']
    mechanism = next(x for x in exchanges if x['operation_id'] == 'getMechanism')['responses']['200']['examples']['eaggl_factor']
    assert package['pigean']['mechanisms'][mechanism['source_id']]['dapper_id'] == mechanism['object']['id']
    assert check_citation_registry_links(paragraph, []), 'Missing citation revisions must fail'
    for path in (API / 'examples/factors').glob('*.json'):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem
    report = {'openapi': spec['openapi'], 'openapi_sha256': hashlib.sha256((API / 'openapi.json').read_bytes()).hexdigest(),
        'paths': len(spec['paths']), 'operations': len(names), 'local_refs_resolved': len(refs),
        'request_body_examples_validated': request_count, 'response_examples_validated': response_count,
        'parameter_examples_validated': parameter_count, 'request_exchanges_validated': len(exchanges),
        'dapper_documents': scientific, 'citation_records_validated': len(records), 'negative_cases_rejected': rejected,
        'cfde_artifact_checksum': 'passed', 'existing_geneset_payload_and_identity_preserved': gene_set['id'],
        'no_live_backend_or_agent_executed': True, 'passed': True}
    (API / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
