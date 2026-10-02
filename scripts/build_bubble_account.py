#!/usr/bin/env python3
"""Rebuild the offline, structurally validated bubble account; never contact services."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.acceptance import LOCK, mint, object_envelope, release_root
from reveal_backend.citations import validate_record
from reveal_backend.evidence_package import canonical_json, sha256
from reveal_backend.repository import digest
from reveal_backend.scientific_account_lint import lint_scientific_account

VERSION = 'bubble-account-v1'
BASE = ROOT / 'design/data/cad-account'
DEFAULT_OUT = ROOT / 'data/fixtures' / VERSION
STAMP = '2026-10-01T00:00:00Z'
IMPORTED_SOURCE = ROOT / 'data/fixtures/bubble-account-v1-inputs/Coronary_Artery_Disease.yaml'
IMPORTED_SOURCE_SHA256 = '00f2a71dca11fd7d4a8c835ea9e978037dcbef88d3fb050c5e74192db2bd7338'


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n')


def build(output=DEFAULT_OUT):
    output = Path(output); (output / 'sources').mkdir(parents=True, exist_ok=True)
    original = json.loads((BASE / 'scientific-account.json').read_text())
    document = deepcopy(original)
    presentation = json.loads((BASE / 'presentation.json').read_text())
    gap = original['knowledge_gaps'][0]
    # Keep the imported source file as reproducible evidence of the revision,
    # rather than borrowing an older revision from the API contract example.
    imported_bytes = IMPORTED_SOURCE.read_bytes()
    if sha256(imported_bytes) != IMPORTED_SOURCE_SHA256:
        raise ValueError('Pinned imported DisMech source bytes changed')
    source_record = json.loads((BASE / 'sources/dismech-gap.source.json').read_text())
    imported = yaml.safe_load(imported_bytes)
    discussion = next(row for row in imported['discussions'] if row['discussion_id'] == source_record['discussion_id'])
    if discussion != source_record['raw']:
        raise ValueError('Retained discussion differs from the imported source revision')
    selection = {'source_id': source_record['id'], 'source_revision': IMPORTED_SOURCE_SHA256, 'id': gap['id']}
    files = {file['filename']: file for file in document['files']}
    classifications = {}
    for file in document['files']:
        raw = (BASE / 'sources' / file['filename']).read_bytes()
        if sha256(raw) != file['sha256'] or len(raw) != file['size_in_bytes']:
            raise ValueError('Base source bytes changed: ' + file['filename'])
        (output / 'sources' / file['filename']).write_bytes(raw)
        classifications[file['id']] = presentation['source_files'][file['id']]['origin']
        file['location'] = f'data/fixtures/{VERSION}/sources/{file["filename"]}'

    (output / 'sources' / IMPORTED_SOURCE.name).write_bytes(imported_bytes)
    imported_file = {'id': 'urn:fixture:file:imported-dismech', 'filename': IMPORTED_SOURCE.name,
        'mime_type': 'application/yaml', 'sha256': IMPORTED_SOURCE_SHA256, 'size_in_bytes': len(imported_bytes),
        'description': 'Exact local imported DisMech source-file revision. Its selected discussion equals the retained CAD design discussion.',
        'location': f'data/fixtures/{VERSION}/sources/{IMPORTED_SOURCE.name}'}
    document['files'].append(imported_file); files[IMPORTED_SOURCE.name] = imported_file
    classifications[imported_file['id']] = 'captured'
    document.setdefault('was_derived_from_edges', []).append({'subject': files['dismech-gap.source.json']['id'],
        'predicate': 'prov:wasDerivedFrom', 'object': imported_file['id']})

    def generated_file(name, payload, description, origin='derived_from_captured'):
        raw = canonical_json(payload)
        (output / 'sources' / name).write_bytes(raw)
        file = {'id': 'urn:fixture:file:' + name, 'filename': name, 'mime_type': 'application/json',
                'sha256': sha256(raw), 'size_in_bytes': len(raw), 'description': description,
                'location': f'data/fixtures/{VERSION}/sources/{name}'}
        document['files'].append(file); files[name] = file; classifications[file['id']] = origin
        return file

    genes_raw = files['pigean-gene-factor.response.json']
    sets_raw = files['pigean-gene-set-factor.response.json']
    genes = json.loads((BASE / 'sources' / genes_raw['filename']).read_text())['data']
    sets = json.loads((BASE / 'sources' / sets_raw['filename']).read_text())['data']
    gene_metrics = generated_file('gene-metrics.json', {'source_sha256': genes_raw['sha256'],
        'method': 'Select source-reported columns without recalibration or new inference.',
        'rows': [{key: row[key] for key in ('gene', 'factor_value', 'combined', 'log_bf', 'prior')} for row in genes]},
        'Deterministic projection of captured CFDE gene metrics; not a new biological experiment.')
    set_metrics = generated_file('gene-set-metrics.json', {'source_sha256': sets_raw['sha256'],
        'method': 'Select source-reported columns without recalibration or new inference.',
        'rows': [{key: row[key] for key in ('gene_set', 'factor_value', 'beta', 'beta_uncorrected')} for row in sets]},
        'Deterministic projection of captured CFDE gene-set metrics; not independent replication.')
    inventory = generated_file('evidence-inventory.json', {'scope': 'Authored fixture inventory; illustrative assertions remain unverified.',
        'items': [{'ordinal': index + 1, 'snippet': evidence['snippet'], **presentation['evidence_views'][evidence['id']]}
                  for index, evidence in enumerate(document['evidence_items'])]},
        'Offline inventory combining captured source locators with explicitly illustrative assertion labels.', 'mixed_fixture_inventory')
    project = {'id': 'urn:fixture:activity:project', 'name': 'Captured CFDE column projection',
        'description': 'Reproducible offline extraction of retained source columns, not a fresh CFDE query.',
        'command': 'python scripts/build_bubble_account.py', 'software_name': 'REVEAL bubble fixture builder', 'software_version': '1'}
    summarize = {'id': 'urn:fixture:activity:inventory', 'name': 'Fixture evidence inventory',
        'description': 'Offline assembly of source locators and explicit illustrative classifications; no scientific review.',
        'command': 'python scripts/build_bubble_account.py', 'software_name': 'REVEAL bubble fixture builder', 'software_version': '1'}
    document['activities'] += [project, summarize]
    document.setdefault('was_generated_by_edges', [])
    document.setdefault('was_derived_from_edges', [])
    for result, inputs, activity in ((gene_metrics, [genes_raw], project), (set_metrics, [sets_raw], project),
                                   (inventory, [gene_metrics, set_metrics, files['illustrative-kg-assertions.json']], summarize)):
        document['was_generated_by_edges'].append({'subject': result['id'], 'predicate': 'prov:wasGeneratedBy', 'object': activity['id']})
        for source_file in inputs:
            document['used_edges'].append({'subject': activity['id'], 'predicate': 'prov:used', 'object': source_file['id']})
            document['was_derived_from_edges'].append({'subject': result['id'], 'predicate': 'prov:wasDerivedFrom', 'object': source_file['id']})

    document['datasets'] = []
    dataset_for_file = {}
    for key, title, members, description in (
        ('genes', 'Captured CAD-in-T2D gene observations', [genes_raw, gene_metrics], 'Retained CFDE response and deterministic column projection. Scientific interpretation remains authored and unreviewed.'),
        ('sets', 'Captured CAD-in-T2D gene-set observations', [sets_raw, set_metrics], 'Retained CFDE gene-set results and deterministic projection; neither establishes causal direction.'),
        ('gap', 'Captured CAD reverse-causation discussion', [files['dismech-gap.source.json'], imported_file], 'Exact imported DisMech source revision and unchanged discussion defining the selected knowledge gap.'),
        ('illustrative', 'Illustrative membership and fixture inventory', [files['illustrative-kg-assertions.json'], inventory], 'Invented membership assertions and mixed provenance inventory for UI testing. Not retrieved from ProKN or BiomarkerKG.')):
        dataset = {'id': 'urn:fixture:dataset:' + key, 'name': title, 'description': description,
            'version': '1', 'resource_type': 'dataset', 'has_file': [file['id'] for file in members],
            'location': f'data/fixtures/{VERSION}/sources/', 'access_level': 'controlled'}
        document['datasets'].append(dataset)
        for file in members: dataset_for_file[file['id']] = dataset['id']
    # Explicit source membership makes every claim traversable to datasets.
    for evidence in document['evidence_items']:
        evidence['was_derived_from'] += sorted({dataset_for_file[identity] for identity in evidence['was_derived_from']})
    # Make the source-gap dataset part of claim-specific neutral evidence as well.
    limit = {'id': 'urn:fixture:evidence:causal-limitation', 'target_proposition': document['claims'][0]['proposition'],
        'direction': 'NEUTRAL', 'evidence_source': 'Captured DisMech gap rationale',
        'snippet': 'Distinguishing genuine amplification of genetic effects from reverse causation',
        'context': 'Source locator: dismech-gap.source.json#/raw/rationale. Imported framing, not independent support for SHH involvement.',
        'explanation': 'This source identifies a causal question; the captured factor loading does not answer it.',
        'was_derived_from': [files['dismech-gap.source.json']['id'], dataset_for_file[files['dismech-gap.source.json']['id']]],
        'was_generated_by': summarize['id'], 'was_attributed_to': [document['persons'][0]['id']]}
    document['evidence_items'].append(limit); document['claims'][0]['has_evidence'].append(limit['id'])
    presentation['evidence_views'][limit['id']] = {'origin': 'captured', 'graph': 'DisMech', 'locator': 'dismech-gap.source.json#/raw/rationale', 'metrics': []}
    account = document['scientific_accounts'][0]
    account['name'] = 'Canonical bubble example: genes and gene programs in CAD-in-T2D'
    account['context'] += ' Canonical bubble-account-v1 fixture, structurally validated only; not a live research result or independently reviewed account.'
    document['used_edges'].append({'subject': account['was_generated_by'], 'predicate': 'prov:used', 'object': inventory['id']})
    before = deepcopy(document)
    document = mint(document, output / 'scientific-account.json')
    mapping = {old['id']: new['id'] for group, rows in before.items() if isinstance(rows, list)
               for old, new in zip(rows, document[group]) if 'id' in old}
    if document['knowledge_gaps'][0] != gap:
        raise ValueError('Fixture changed the exact imported knowledge gap')
    def remap(value):
        if isinstance(value, dict): return {mapping.get(key, key): remap(item) for key, item in value.items()}
        if isinstance(value, list): return [remap(item) for item in value]
        return mapping.get(value, value) if isinstance(value, str) else value
    view = remap({key: presentation[key] for key in ('claim_views', 'evidence_views', 'gene_set_labels')})
    view.update(account_id=document['scientific_accounts'][0]['id'], gap_id=gap['id'], selected_gap=selection,
                mode='Canonical authored fixture; captured CFDE plus illustrative unreviewed interpretations')
    classifications = remap(classifications)
    view['source_files'] = {file['id']: {'path': 'sources/' + file['filename'], 'origin': classifications[file['id']]}
                            for file in document['files']}
    write_json(output / 'presentation.json', view)
    # Keep exact locators/metrics anchored to actual retained bytes.
    by_id = {file['id']: file for file in document['files']}
    for evidence in document['evidence_items']:
        direct = [by_id[identity] for identity in evidence['was_derived_from'] if identity in by_id]
        for file in direct:
            if evidence['snippet'] not in (output / 'sources' / file['filename']).read_text():
                raise ValueError('Evidence excerpt not present in retained bytes: ' + evidence['id'])
        locator = view['evidence_views'][evidence['id']]
        if locator['metrics']:
            filename, index = locator['locator'].split('#/data/')
            row = json.loads((output / 'sources' / filename).read_text())['data'][int(index)]
            if any(row[item['metric']] != item['value'] for item in locator['metrics']):
                raise ValueError('Source metric changed')
    report = lint_scientific_account(output / 'scientific-account.json', dapper_root=release_root(), release_lock=LOCK, mode='profile-only')
    if not report['valid']: raise ValueError(json.dumps(report, indent=2))
    report['scope'] = 'Pinned DAPPER structural/identity/reference validation only; not scientific acceptance.'
    write_json(output / 'validation.json', report)
    registry = remap(json.loads((BASE / 'citation-registry.json').read_text()))
    for record in registry:
        record.update(dapper_schema_version=json.loads(LOCK.read_text())['commit'],
                      repository='REVEAL canonical UI fixtures — unpublished and scientifically unreviewed')
        record['object_payload_ref'] = 'http://localhost:3000/api/backend/v1/objects/' + record['target_id']
        record['canonical_url'] = 'http://localhost:3000/id/' + record['target_id']
        record.pop('metadata_checksum', None); record['metadata_checksum'] = digest(record); validate_record(record)
    write_json(output / 'citation-registry.json', registry)
    envelope = object_envelope(document, view['account_id'], registry)
    envelope['research_statement'] = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
    write_json(output / 'account-envelope.json', envelope)
    relationships = {'dataset_files': {node['id']: node['has_file'] for node in document['datasets']},
        'claim_evidence': {node['id']: node['has_evidence'] for node in document['claims']},
        'evidence_sources': {node['id']: node['was_derived_from'] for node in document['evidence_items']},
        'provenance_edges': {key: value for key, value in document.items() if key.endswith('_edges')}}
    write_json(output / 'relationships.json', relationships)
    members = ['scientific-account.json', 'presentation.json', 'citation-registry.json', 'account-envelope.json', 'relationships.json']
    members += ['sources/' + file['filename'] for file in document['files']]
    contents = {name: sha256((output / name).read_bytes()) for name in sorted(members)}
    manifest = {'format': 'reveal.canonical-fixture/1', 'fixture_version': VERSION, 'authored_at': STAMP,
        'content_sha256': digest(contents), 'contents': contents, 'selected_gap': selection,
        'source_binding': {'source_file': source_record['source_file'], 'source_pointer': source_record['source_pointer'],
            'source_file_sha256': IMPORTED_SOURCE_SHA256, 'discussion_matches_design_capture': True},
        'account_id': view['account_id'], 'counts': {key: len(value) for key, value in document.items() if isinstance(value, list)},
        'identities': {key: [node['id'] for node in value if 'id' in node] for key, value in document.items() if isinstance(value, list)},
        'dapper_release_lock_sha256': sha256(LOCK.read_bytes()), 'scientific_acceptance': 'not_reviewed',
        'files': [{'id': file['id'], 'path': 'sources/' + file['filename'], 'sha256': file['sha256'],
                   'size_bytes': file['size_in_bytes'], 'origin': classifications[file['id']]} for file in document['files']]}
    write_json(output / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--check', action='store_true', help='Rebuild in a temporary directory and compare every generated byte')
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix='bubble-fixture-check-') as temp:
            manifest = build(Path(temp))
            names = list(manifest['contents']) + ['manifest.json', 'validation.json']
            changed = [name for name in names if not (args.output / name).exists() or (args.output / name).read_bytes() != (Path(temp) / name).read_bytes()]
            if changed: raise SystemExit('Fixture rebuild differs: ' + ', '.join(changed))
    else: manifest = build(args.output)
    print(json.dumps({'fixture': VERSION, 'content_sha256': manifest['content_sha256'], 'counts': manifest['counts']}, indent=2))


if __name__ == '__main__': main()
