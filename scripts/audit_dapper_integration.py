#!/usr/bin/env python3
"""Offline compatibility audit against the captured DAPPER v8 design dependency.

Reads exported data only; does not migrate MySQL or change the sibling repository.
Use the GeneSet import's frozen dependency to reproduce the original import.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=ROOT / 'data/dapper/2026-09-24-v8')
    args = parser.parse_args()
    snapshot = args.audit / 'snapshot'
    pin = json.loads((args.audit / 'snapshot.json').read_text())
    for path, expected in pin['files'].items():
        assert hashlib.sha256((snapshot / path).read_bytes()).hexdigest() == expected, path
    sys.path[:0] = [str(snapshot / 'schema/identity'), str(snapshot / 'schema')]
    import dapper_identity as identity
    import yaml
    from jsonschema.validators import validator_for
    from linkml.generators.jsonschemagen import JsonSchemaGenerator

    schema_path = snapshot / 'schema/dapper.yaml'
    sv = identity.load_schema(schema_path)
    generated = json.loads(JsonSchemaGenerator(str(schema_path), not_closed=False).serialize())
    def validator(cls):
        # Validate the chosen closed class, not the generator's empty root class.
        # Draft 2020-12 also evaluates siblings of $ref, unlike Draft 7.
        selected = {'$schema': generated['$schema'], '$defs': generated['$defs'],
                    '$ref': '#/$defs/' + cls}
        return validator_for(selected)(selected)
    gene_validator, activity_validator = validator('GeneSet'), validator('Activity')
    report = {
        'snapshot_sha256': pin['snapshot_sha256'],
        'root_schema_sha256': pin['root_schema_sha256'],
        'dependencies': {p: importlib.metadata.version(p) for p in
                         ['linkml', 'linkml-runtime', 'rdflib', 'jsonschema', 'PyYAML', 'pytest']},
        'classes': {},
    }
    for cls in ['Question', 'KnowledgeGap', 'ScientificAccount', 'Paragraph',
                'CitationOccurrence', 'Claim', 'Proposition', 'ClaimScore',
                'EvidenceItem', 'GeneSet', 'GeneSetCollection', 'File', 'Activity', 'AgenticWorkspace']:
        report['classes'][cls] = {
            'required': sorted(s.name for s in sv.class_induced_slots(cls) if s.required),
            'hashable': identity.hashable_slot_names(sv, cls),
            'unhashable': sorted(s.name for s in sv.class_induced_slots(cls) if 'unhashable' in (s.mixins or [])),
        }

    # Full export scan for present-field coverage, deterministic identity/schema samples.
    export = ROOT / 'data/cfde-genesets/2026-09-24'
    samples, shapes, fields, rows = [], Counter(), set(), 0
    with gzip.open(export / 'records.jsonl.gz', 'rt') as stream:
        for i, line in enumerate(stream):
            node = json.loads(line)['gene_set']
            fields.update(node)
            shapes[tuple(sorted(node))] += 1
            rows += 1
            if i % 10000 == 0:
                samples.append(node)
        if samples[-1]['id'] != node['id']:
            samples.append(node)
    assert not fields - {s.name for s in sv.class_induced_slots('GeneSet')}
    for node in samples:
        gene_validator.validate(node)
        assert identity.compute_id(node, 'GeneSet', sv) == node['id'], node['id']
    activity = json.loads((export / 'activity.json').read_text())
    activity_validator.validate(activity)
    assert identity.compute_id(activity, 'Activity', sv) == activity['id']
    old_schema = identity.load_schema(export / 'dapper/schema/dapper.yaml')
    old_hashable = set(identity.hashable_slot_names(old_schema, 'GeneSet'))
    new_hashable = set(identity.hashable_slot_names(sv, 'GeneSet'))
    report['import_compatibility'] = {
        'records_scanned': rows, 'identity_and_closed_schema_samples_passed': len(samples),
        'activity_identity_and_closed_schema_passed': True,
        'present_fields': sorted(fields),
        'field_shapes': [{'fields': list(k), 'count': v} for k, v in shapes.items()],
        'added_hashable_fields': sorted(new_hashable - old_hashable),
        'removed_hashable_fields': sorted(old_hashable - new_hashable),
        'note': 'Field coverage scanned for all records; identity/schema replay is sampled, not a new full import validation.',
    }

    with gzip.open(ROOT / 'data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz', 'rt') as stream:
        gaps = [json.loads(line) for line in stream]
    missing = lambda field: [g['id'] for g in gaps if not isinstance(g['raw'].get(field), str) or not g['raw'][field].strip()]
    report['gap_mapping'] = {
        'count': len(gaps), 'kinds': dict(Counter(g['kind'] for g in gaps)),
        'missing_nonblank_prompt': len(missing('prompt')),
        'missing_nonblank_rationale': len(missing('rationale')),
        'missing_rationale_examples': missing('rationale')[:5],
        'with_disease_term_id': sum(bool((g.get('disease_term') or {}).get('term', {}).get('id')) for g in gaps),
    }
    # Demonstrate constraints that the REST/database implementation must honor.
    original = {'filename': 'evidence.json', 'sha256': 'a' * 64, 'location': 's3://example/a'}
    relocated = {**original, 'location': 's3://example/b'}
    assert identity.compute_id(original, 'File', sv) == identity.compute_id(relocated, 'File', sv)
    sample = samples[0]
    inverse = {**sample, 'in_gene_set_collection': ['urn:example:collection']}
    assert identity.compute_id(sample, 'GeneSet', sv) == identity.compute_id(inverse, 'GeneSet', sv)
    representation = {**sample, 'in_gmt_file': 'urn:example:gmt', 'gmt_entry': sample['id']}
    assert identity.compute_id(sample, 'GeneSet', sv) == identity.compute_id(representation, 'GeneSet', sv)
    gap = {'text': 'Which process explains this observation?', 'gap_description': 'The explanatory process is unknown.'}
    contextual = {**gap, 'about_entities': ['https://example.org/disease']}
    assert identity.compute_id(gap, 'KnowledgeGap', sv) != identity.compute_id(contextual, 'KnowledgeGap', sv)
    compact = {**gap, 'about_entities': ['MONDO:0005148']}
    expanded = {**gap, 'about_entities': ['http://purl.obolibrary.org/obo/MONDO_0005148']}
    assert identity.compute_id(compact, 'KnowledgeGap', sv) != identity.compute_id(expanded, 'KnowledgeGap', sv)
    collection = {'name': 'Illustrative large collection', 'members': ['urn:example:gene-set:' + str(i) for i in range(10001)]}
    try:
        identity.compute_id(collection, 'GeneSetCollection', sv)
    except ValueError as exc:
        collection_limit = str(exc)
    else:
        raise AssertionError('Expected identity triple limit')
    report['application_boundaries'] = {
        'file_location_changes_payload_without_changing_id': True,
        'geneset_inverse_collection_link_changes_payload_without_changing_id': True,
        'geneset_gmt_representation_changes_payload_without_changing_id': True,
        'gap_context_changes_id': True,
        'external_curie_expansion_can_change_id': True,
        'large_collection_identity_limit': collection_limit,
        'scientific_account_profile': yaml.safe_load((snapshot / 'schema/lint/profiles.yaml').read_text())['profiles']['scientific-account']['terminal'],
    }
    (args.audit / 'compatibility.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ['classes']}, indent=2))


if __name__ == '__main__':
    main()
