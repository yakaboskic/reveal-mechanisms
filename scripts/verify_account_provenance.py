#!/usr/bin/env python3
"""Read-only: run GET /v1/accounts/{id}/provenance in-process, as each account's owner, and summarize the traces.

--overlay substitutes the collection payloads a collection-lineage plan would write (see
apply_collection_lineage.py) on the reference connection, so the result of a patch can be reviewed before
anything is written. Nothing in this script writes to the database or to storage."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
DEFAULT_CURATION = ROOT / 'data/cfde-provenance/2026-10-11/collection-lineage.json'


class OverlayCursor:
    def __init__(self, cursor, payloads): self.cursor, self.payloads, self.sql = cursor, payloads, ''
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, args=()):
        self.sql = sql
        return self.cursor.execute(sql, args)
    def fetchall(self):
        rows = self.cursor.fetchall()
        if self.sql.startswith('SELECT collection_id,payload FROM cfde_gene_set_collections'):
            rows = [(identity, json.dumps(self.payloads[identity]) if identity in self.payloads else payload) for identity, payload in rows]
        return rows


class Overlay:
    """A reference connection whose collection payload reads return planned payloads."""
    def __init__(self, connection, payloads): self.connection, self.payloads = connection, payloads
    def cursor(self): return OverlayCursor(self.connection.cursor(), self.payloads)
    def rollback(self): return self.connection.rollback()
    def close(self): return self.connection.close()


def owner_of(tx, prefix, account_id):
    from reveal_backend.repository import digest
    rows = tx.execute(f"SELECT owner_id FROM {prefix}_records WHERE kind='account' AND "
                      "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.summary.account.id'))=%s", (account_id,)).fetchall()
    owners = {row[0] for row in rows}
    for owner in owners:
        if tx.get('account', digest([owner, account_id])): return owner
    raise SystemExit(f'No owned account record for {account_id} in {prefix}')


def summarize(response, elapsed):
    names = {node['id']: node.get('name') for node in response['nodes']}
    return {'snapshot': response['snapshot'].get('snapshot_id') if isinstance(response.get('snapshot'), dict) else None,
            'reference_generation_ids': response['snapshot'].get('reference_generation_ids'),
            'coverage': {key: response['coverage'][key] for key in ('status', 'trace_count', 'counts_are_lower_bounds') if key in response['coverage']},
            'issue_codes': dict(Counter(issue['code'] for issue in response['coverage'].get('issues', []))),
            'mechanisms': [{key: row.get(key) for key in ('mechanism_id', 'status', 'binding_origin', 'identity_lookup', 'retained_rows')}
                           for row in response['coverage'].get('retained_projection_coverage', [])],
            'datasets': [{'name': names.get(row['dataset_id']), 'claims': row['claim_count'], 'traces': row['trace_count'],
                          'input_roles': row.get('input_roles')} for row in response['dataset_reuse']],
            'organizations': [{'name': names.get(row['organization_id']), 'roles': sorted(row.get('roles', {}))}
                              for row in response['organization_reuse']],
            'curated_used_edges': sum(1 for edge in response['edges'] if edge['relation'] == 'used'
                                      and any(source.get('kind') == 'curated_lineage' for source in edge.get('provenance_source', []))),
            'response_bytes': len(json.dumps(response)), 'seconds': round(elapsed, 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True, choices=('reveal_workflow_qa', 'reveal_workflow_local', 'reveal'))
    parser.add_argument('--account', action='append', required=True, help='dapper:ScientificAccount id; repeat for several')
    parser.add_argument('--overlay', action='store_true', help='Overlay the payloads the collection-lineage plan would write')
    parser.add_argument('--curation', type=Path, default=DEFAULT_CURATION)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--limit', default='250')
    args = parser.parse_args(argv)
    if args.env_file.is_file():
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False, interpolate=False)
    os.environ.setdefault('REVEAL_GATEWAY_SECRET', 'read-only-preview-cursor-signing-key-0000')  # cursors only, this process
    from reveal_backend import runtime_config
    from reveal_backend.app import scientific_source
    from reveal_backend.provenance_api import read
    from reveal_backend.repository import Repository
    plan_sha = None
    if args.overlay:
        from reveal_backend import collection_lineage
        connection = runtime_config.mysql_connection()
        try: plan, _ = collection_lineage.plan_from(connection, args.curation)
        finally: connection.close()
        payloads = {entry['collection_id']: entry['payload'] for entry in plan['collections'] if entry['action'] == 'add'}
        plan_sha = plan['plan_sha256']
        original = runtime_config.reference_mysql_connection
        runtime_config.reference_mysql_connection = lambda *a, **k: Overlay(original(*a, **k), payloads)
    repository = Repository(table_prefix=args.prefix)
    results = {}
    for account_id in args.account:
        with repository.read_transaction() as tx:
            owner = owner_of(tx, args.prefix, account_id)
            started = time.monotonic()
            source = scientific_source(tx, account_id, owner, 'account', None, verify_payload=True)
            response = read(tx, account_id, source, {'limit': args.limit})
            results[account_id] = summarize(response, time.monotonic() - started)
    print(json.dumps({'prefix': args.prefix, 'overlay_plan_sha256': plan_sha, 'accounts': results}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
