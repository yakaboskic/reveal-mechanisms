#!/usr/bin/env python3
"""Backfill `account_data_sources` for saved accounts and their publication snapshots.

New accounts are traced when their agent run or local submission saves them. This traces accounts saved
before that, and re-traces after a reference release, lineage curation or tracing policy change (`--force`;
records traced under an earlier tracing policy are re-traced without it). The trace is the release-era trace
without the portal (provenance_api.trace_local): it reads the reference tables and retained captures only. Each
record comes from the exact source the account endpoint serves: an owner's account from its indexed
document and retained capture records, a publication from its frozen snapshot. Dry run by default;
`--apply` writes one small transaction per record. Records that already match (current format and policy,
same document) are skipped unless `--force`.

Pass the environment's storage settings, as the API uses them, so retained captures can be read:

    REVEAL_ARTIFACT_STORE=s3 REVEAL_S3_BUCKET=cyaka-reveal-data REVEAL_S3_PREFIX=qa/ AWS_PROFILE=broad \\
        .venv/bin/python scripts/backfill_account_data_sources.py --prefix reveal_workflow_qa [--apply]
"""
import argparse
from collections import Counter
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prefix', required=True, choices=('reveal_workflow_qa', 'reveal_workflow_local', 'reveal'),
                        help='application table prefix')
    parser.add_argument('--account', action='append', help='dapper:ScientificAccount id; repeat for several (default: all)')
    parser.add_argument('--apply', action='store_true', help='write records (default: report only)')
    parser.add_argument('--force', action='store_true', help='re-trace records that already match')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    args = parser.parse_args(argv)
    if args.env_file.is_file():
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False, interpolate=False)
    # The trace reads this environment's reference release (<prefix>_ref_*, reference_pin): pin the process to --prefix so
    # its records are never traced against another environment's release.
    os.environ['REVEAL_APPLICATION_TABLE_PREFIX'] = args.prefix
    from reveal_backend import account_data_sources as sources
    from reveal_backend.app import scientific_source
    from reveal_backend.auth import Problem
    from reveal_backend.provenance_api import artifact_records
    from reveal_backend.publication import published
    from reveal_backend.repository import Repository
    repository = Repository(table_prefix=args.prefix)
    wanted = set(args.account or ())
    with repository.read_transaction() as tx:
        owners = [(row['owner'], row['data']['account_id']) for row in tx.list('account_membership')
                  if not wanted or row['data'].get('account_id') in wanted]
        publications = [(row['owner'], row['data']['account_id'], row['data']['snapshot_id'], row) for row in published(tx)
                        if not wanted or row['data']['account_id'] in wanted]
    report = {'prefix': args.prefix, 'applied': args.apply, 'statuses': Counter(), 'skipped': 0, 'failed': [], 'accounts': []}

    def trace(key, owner, account_id, resolve):
        with repository.read_transaction() as tx:
            existing = tx.get(sources.KIND, key)
            source = resolve(tx)
            document_sha = source['document_sha256']
            if (existing and not args.force and existing['owner'] == owner and sources.current(existing['data'])
                    and existing['data'].get('document_sha256') == document_sha
                    and existing['data'].get('account_payload_sha256') == source['root_payload']):
                report['skipped'] += 1; return
            records = artifact_records(tx, source)
        # The reference reads run outside the application read (never nested in its lease); no portal request is made.
        record = sources.trace_source(repository, account_id, source, records, document_sha)
        if args.apply:
            with repository.transaction() as tx: tx.put(sources.KIND, key, owner, record)
        report['statuses'][record['status']] += 1
        report['accounts'].append({'key': key, 'account_id': account_id, 'status': record['status'], **sources.summary(record),
                                   'datasets': [row.get('name') or row['id'] for row in record.get('datasets', [])]})

    for owner, account_id in sorted(owners):
        try:
            trace(sources.owner_key(owner, account_id), owner, account_id,
                  lambda tx: scientific_source(tx, account_id, owner, 'account', None, verify_payload=True))
        except Problem as error:
            report['failed'].append({'account_id': account_id, 'owner_scope': 'owner', 'code': error.code})
    for owner, account_id, snapshot_id, row in publications:
        def public(tx, account_id=account_id, snapshot_id=snapshot_id):
            source = scientific_source(tx, account_id, None, 'account', None, verify_payload=True)
            if source['public']['data'].get('snapshot_id') != snapshot_id:
                raise Problem(409, 'PUBLICATION_SHADOWED', 'Another publication of this account is served first.')
            return source
        try:
            trace(sources.publication_key(snapshot_id), owner, account_id, public)
        except Problem as error:
            report['failed'].append({'account_id': account_id, 'owner_scope': 'publication', 'code': error.code})
    report['statuses'] = dict(report['statuses'])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
