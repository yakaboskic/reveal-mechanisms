#!/usr/bin/env python3
"""Validate catalog coverage, dependency hashes, identities and CFDE aliases offline."""
import argparse
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import import_cfde_genesets as source


def graph_ids(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from graph_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from graph_ids(item)
    elif isinstance(value, str) and value.startswith('gene_set:'):
        yield value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=source.ROOT / 'data/cfde-genesets/2026-09-24')
    args = parser.parse_args()
    root = args.output
    manifest = source.check_export(root)
    catalog = root / 'geneset-keys.json.gz'
    assert source.file_hash(catalog) == manifest['catalog_sha256'], 'Catalog checksum changed'
    snapshot = root / 'dapper'
    for rel, sha in manifest['dapper']['files'].items():
        assert source.file_hash(snapshot / rel) == sha, 'DAPPER dependency changed: ' + rel
    identity, schema = source.identity_module(snapshot)
    activity = source.read_json(root / 'activity.json')
    assert identity.compute_id(activity, 'Activity', schema) == activity['id']
    keys = source.selected_keys(source.read_json(catalog), manifest['model'])
    fixtures = set()
    for path in (source.ROOT / 'data/interactive/2026-09-24').glob('*.json'):
        fixtures.update(graph_ids(source.read_json(path)))
    missing = set(fixtures)
    ids, checked = set(), 0
    for index, (key, row) in enumerate(zip_longest(keys, source.rows(root / 'records.jsonl.gz'))):
        assert key is not None and row is not None, 'Catalog/export length mismatch'
        assert row['source_key'] == key and row['model'] == manifest['model'], 'Source scope/order mismatch'
        assert row['node_id'] == 'gene_set:' + key, 'CFDE alias mismatch'
        node = row['gene_set']
        assert node['id'] not in ids, 'Duplicate DAPPER identity'
        ids.add(node['id'])
        assert {k: v for k, v in node.items() if k != 'id'} == source.metadata_record(key, manifest['model'], activity['id']), 'Metadata mapping mismatch'
        if index % 10000 == 0 or index == len(keys) - 1:
            assert identity.compute_id(node, 'GeneSet', schema) == node['id'], 'DAPPER identity replay failed'
            checked += 1
        missing.discard(row['node_id'])
    assert len(ids) == manifest['expected_rows'], 'Unique identity count mismatch'
    report = {'import_id': manifest['import_id'], 'validated_at': datetime.now(timezone.utc).isoformat(),
              'catalog_keys': len(keys), 'unique_dapper_ids': len(ids), 'identity_replay_samples': checked,
              'interactive_fixture_gene_sets': len(fixtures), 'fixture_aliases_resolved': len(fixtures) - len(missing),
              'unresolved_fixture_aliases': sorted(missing), 'complete': not missing}
    source.write_json(root / 'validation.json', report)
    assert not missing, 'Fixture aliases not covered; inspect validation.json'
    print(source.canonical(report))


if __name__ == '__main__':
    main()
