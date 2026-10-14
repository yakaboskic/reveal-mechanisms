#!/usr/bin/env python3
"""Add hand-curated CFDE collection lineage (convert Activity prov:used upstream Datasets) to one reference
generation. `plan` (default) only reads; `apply` needs --apply and the reviewed --expect-plan sha; `rollback`
restores a backup and also needs --apply to write. See data/cfde-provenance/README.md."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
DEFAULT_CURATION = ROOT / 'data/cfde-provenance/2026-10-11/collection-lineage.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply', 'rollback'), nargs='?', default='plan')
    parser.add_argument('--curation', type=Path, default=DEFAULT_CURATION)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env', help='MySQL credentials (REVEAL_MYSQL_*); existing environment wins')
    parser.add_argument('--apply', action='store_true', help='Write; without it apply/rollback only report')
    parser.add_argument('--expect-plan', help='plan_sha256 printed by the reviewed plan')
    parser.add_argument('--backup-dir', type=Path, default=ROOT / '.runtime/cfde-lineage')
    parser.add_argument('--backup', type=Path, help='payloads.jsonl.gz written by apply (rollback only)')
    parser.add_argument('--allow-production', action='store_true')
    parser.add_argument('--output', type=Path, help='Also write the JSON report here')
    args = parser.parse_args(argv)
    if args.env_file and args.env_file.is_file():
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False, interpolate=False)
    from reveal_backend import collection_lineage as lineage
    from reveal_backend.runtime_config import mysql_connection
    connection = mysql_connection()
    try:
        if args.action == 'plan' or (args.action == 'apply' and not args.apply):
            plan, _ = lineage.plan_from(connection, args.curation)
            result = lineage.public(plan)
        elif args.action == 'apply':
            if not args.expect_plan: parser.error('apply --apply needs --expect-plan from the reviewed plan')
            result = lineage.apply(connection, args.curation, expect_plan=args.expect_plan, backup_dir=args.backup_dir,
                                   allow_production=args.allow_production)
        else:
            if not args.backup: parser.error('rollback needs --backup')
            result = lineage.rollback(connection, args.backup, apply=args.apply)
    except lineage.LineageRefused as error:
        print('Refused: ' + str(error), file=sys.stderr)
        return 1
    finally:
        try: connection.rollback()
        finally: connection.close()
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
