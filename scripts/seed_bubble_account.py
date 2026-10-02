#!/usr/bin/env python3
"""Inspect, seed, or verify the canonical account in isolated local application tables."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--dry-run', action='store_true', help='Validate files, exact imported gap, and owner; never upload or write')
    modes.add_argument('--apply', action='store_true', help='Retain verified bytes and insert the complete private account read model')
    modes.add_argument('--verify', action='store_true', help='Read back complete seeded records and exact retained artifact versions')
    parser.add_argument('--owner', required=True, help='Existing local workspace owner UUID; never creates a principal')
    parser.add_argument('--table-prefix', required=True, choices=['reveal_workflow_local'])
    parser.add_argument('--base-url', default='http://localhost:3000')
    parser.add_argument('--fixture', type=Path, default=ROOT / 'data/fixtures/bubble-account-v1')
    parser.add_argument('--env-file', type=Path, help='Explicit local runtime environment file; no implicit .env loading')
    parser.add_argument('--receipt', type=Path, help='Write a receipt outside the fixture directory after successful execution')
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file(): parser.error('Environment file does not exist')
        load_dotenv(args.env_file, override=False)
    if os.getenv('REVEAL_APPLICATION_TABLE_PREFIX') != args.table_prefix:
        parser.error('Configured application prefix must exactly match --table-prefix reveal_workflow_local')
    if args.receipt and args.receipt.resolve().is_relative_to(args.fixture.resolve()):
        parser.error('Receipt must not modify the canonical fixture directory')
    from reveal_backend.artifact_store import s3_enabled, store
    from reveal_backend.catalog import Catalog
    from reveal_backend.fixture_seed import load_fixture, dry_run, apply_seed, verify_seed
    from reveal_backend.repository import Repository
    fixture = load_fixture(args.fixture)
    repository = Repository(table_prefix=args.table_prefix)
    catalog = Catalog()
    if args.dry_run:
        result = dry_run(repository, args.owner, fixture, catalog, args.base_url)
    else:
        if not s3_enabled(): parser.error('Apply/verify requires configured versioned development S3 storage')
        storage = store()
        # Verify prefix, owner and exact gap before any S3 operation.
        dry_run(repository, args.owner, fixture, catalog, args.base_url)
        storage.check()
        if args.apply:
            apply_seed(repository, args.owner, fixture, catalog, storage, args.base_url)
        result = verify_seed(repository, args.owner, fixture, catalog, storage, args.base_url)
    text = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(text)
    print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
