#!/usr/bin/env python3
"""Validate the delivered September 24 inventory offline, without service writes.

This checks captured evidence and source referential integrity. It does not test
an application implementation or imply future upstream responses stay identical.
"""
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'data'


def read(path):
    return json.loads(path.read_text())


def rows(path):
    with gzip.open(path, 'rt') as stream:
        return [json.loads(line) for line in stream]


def validate_gaps():
    root = ROOT / 'dismech-gaps/2026-09-24'
    manifest = read(root / 'manifest.json')
    assert manifest['complete'] and not manifest['errors']
    exports = {}
    for filename, metadata in manifest['files'].items():
        path = root / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata['sha256'], filename
        exports[filename] = rows(path)
        assert len(exports[filename]) == metadata['rows'], filename
    discussions = exports['discussions.jsonl.gz']
    gaps = exports['knowledge-gaps.jsonl.gz']
    attachments = exports['gap-attachments.jsonl.gz']
    assert len(discussions) == manifest['discussions']
    assert len(gaps) == manifest['knowledge_gaps']
    assert len(attachments) == manifest['attachments']
    assert len({r['id'] for r in discussions}) == len(discussions)
    gap_by_id = {r['id']: r for r in gaps}
    assert len(gap_by_id) == len(gaps)
    assert gaps == [r for r in discussions if r['kind'] in manifest['gap_kinds']]
    assert Counter(r['kind'] for r in gaps) == manifest['gaps_by_kind']
    assert Counter('null' if r['status'] is None else r['status'] for r in gaps) == manifest['gaps_by_status']
    assert Counter(a['resolution'] for a in attachments) == manifest['attachment_resolution']
    assert all(r['discussion_id'] and r['has_stable_source_id'] for r in gaps)
    assert len({(a['gap_id'], a['attachment_index']) for a in attachments}) == len(attachments)
    assert len(attachments) == sum(len(r['raw'].get('attaches_to') or []) for r in gaps)
    for attachment in attachments:
        gap = gap_by_id[attachment['gap_id']]
        assert gap['raw']['attaches_to'][attachment['attachment_index']] == attachment['source_reference']
        if attachment['resolution'] == 'ambiguous_target':
            assert attachment['match_count'] > 1 and 'target_id' not in attachment

    # Same-revision mechanism targets must join to the original mechanism export.
    original = read(ROOT / 'dismech/manifest.json')
    assert manifest['source_commit'] == original['source_commit']
    source_hashes = {r['path']: r['sha256'] for r in read(root / 'source-files.json')}
    assert len(source_hashes) == manifest['yaml_files']
    mechanisms = {r['id'] for r in rows(ROOT / 'dismech/mechanisms.jsonl.gz')}
    mechanism_attachments = [a for a in attachments
                            if a['resolution'] == 'resolved' and a['target_kind'] == 'pathophysiology']
    for attachment in mechanism_attachments:
        assert attachment['target_id'] in mechanisms, attachment
        assert attachment['target_source_file'] in source_hashes

    fixture = read(root / 'aip-gap-example.json')
    assert fixture['gap'] == gap_by_id[fixture['gap']['id']]
    assert fixture['attachments'] == [a for a in attachments if a['gap_id'] == fixture['gap']['id']]
    assert Counter(a['target_kind'] for a in fixture['attachments']) == {'pathophysiology': 3, 'phenotypes': 1}
    assert all(a['resolution'] == 'resolved' for a in fixture['attachments'])
    return len(gaps), len(attachments), len(mechanism_attachments)


def validate_api():
    root = ROOT / 'interactive/2026-09-24'
    fixtures = {p.stem: read(p) for p in root.glob('*.json')}
    expected_errors = {'connections-invalid-target': 422, 'openapi-root': 404}
    for name, fixture in fixtures.items():
        assert fixture['status'] == expected_errors.get(name, 200), name
        if fixture['request'] is not None:
            assert fixture['request']['model'] == 'cfde-inc-v2', name
    factors = rows(ROOT / 'cfde/factors.jsonl.gz')
    # Construct a sample crosswalk from source fields; don't parse opaque API IDs.
    aliases = {}
    for factor in factors:
        raw = factor['raw']
        key = ':'.join(['factor', raw['trait_group'], factor['phenotype_key'], raw['gene_set_size'], raw['factor']])
        assert key not in aliases
        aliases[key] = raw['label']
    catalog = fixtures['catalog-factor-age']['response']['items']
    assert len(catalog) == 8
    for item in catalog:
        assert aliases[item['node_id']] == item['label']
    anchor_sets = []
    for target, count in [('gene', 0), ('gene_set', 63), ('trait', 0), ('factor', 0)]:
        fixture = fixtures['connections-' + target]
        response, request = fixture['response'], fixture['request']
        anchor_ids = {a['node_id'] for a in request['anchor_items']}
        anchor_sets.append(anchor_ids)
        assert request['target_type'] == target
        assert request['connection_scope'] == 'direct' and request['reducer'] == 'mean'
        assert request['limit'] == 100 and set(request['exclude_node_ids']) == anchor_ids
        assert response['candidate_count'] == len(response['candidates']) == count
        node_ids = {n['id'] for n in response['graph']['nodes']}
        assert not node_ids.intersection(anchor_ids)
        merged = node_ids | anchor_ids
        for edge in response['graph']['edges']:
            assert edge['source'] in merged and edge['target'] in merged
            assert set(edge['path_nodes']) <= merged
    assert all(s == anchor_sets[0] for s in anchor_sets)
    assert anchor_sets[0] == {a['node_id'] for a in catalog[:2]}
    contextual = fixtures['contextual-edges-user-example']
    assert len(contextual['request']['node_ids']) == 17
    assert len(contextual['response']['edges']) == 15
    for edge in contextual['response']['edges']:
        assert {edge['source'], edge['target']} <= set(contextual['request']['node_ids'])
    membership = fixtures['geneset-direct-gene']
    assert membership['response']['candidate_count'] == 8
    for edge in membership['response']['graph']['edges']:
        assert edge['family'] == 'gene_gene_set_membership'
        assert edge['source'].startswith('gene:') and edge['target'].startswith('gene_set:')
    assert 'response' not in fixtures['openapi-api']  # HTTP 200 HTML is not a specification.
    assert 'html' in fixtures['openapi-api']['content_type']
    return len(fixtures), len(catalog)


def validate_database_capture():
    audit = read(ROOT / 'infrastructure/mysql-audit-2026-09-24.json')
    assert audit['connected'] and audit['tls_cipher']
    assert audit['mode'] == 'read_only'
    assert audit['creation_executed'] is False and audit['database_created'] is False
    assert {'privileges': 'ALL PRIVILEGES', 'scope': '`cyaka\\_%`.*'} in audit['relevant_grants']
    assert audit['mysql_vector_function_probe']['supported'] is None


def main():
    gaps, attachments, mechanisms = validate_gaps()
    probes, matches = validate_api()
    validate_database_capture()
    print(f'Validated {gaps:,} gaps, {attachments:,} attachments ({mechanisms:,} mechanism joins), '
          f'{probes} API captures, {matches} factor aliases, and the read-only MySQL audit.')


if __name__ == '__main__':
    main()
