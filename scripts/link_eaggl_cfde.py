#!/usr/bin/env python3
"""Map EAGGL to CFDE by trait/factor number and resolve CFDE summary GeneSets."""
import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.eaggl_cfde_links import load_mapping, lookup_factor, prepare_mapping
from reveal_backend.mysql_database import DEFAULT_HOST, connect


def main(argv=None):
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['load', 'lookup'])
    parser.add_argument('--cfde-catalog', type=Path, default=ROOT / 'data/cfde')
    parser.add_argument('--eaggl-import-id')
    parser.add_argument('--gene-set-import-id')
    parser.add_argument('--run-id')
    parser.add_argument('--factor-id')
    parser.add_argument('--apply', action='store_true', help='Write links; default load only reads/plans')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--host', default=os.getenv('REVEAL_MYSQL_HOST', DEFAULT_HOST))
    parser.add_argument('--port', type=int, default=os.getenv('REVEAL_MYSQL_PORT', '3306'))
    parser.add_argument('--user', default=os.getenv('REVEAL_MYSQL_USER', 'cyaka'))
    parser.add_argument('--database', default=os.getenv('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'))
    parser.add_argument('--ca-file', default=os.getenv('REVEAL_MYSQL_CA_FILE') or None)
    args = parser.parse_args(argv)
    if args.command == 'lookup' and (not args.factor_id or args.apply):
        parser.error('lookup requires --factor-id and is read-only')
    if args.command == 'load' and (args.factor_id or args.run_id):
        parser.error('--factor-id and --run-id apply to lookup only')
    c = connect(host=args.host, port=args.port, user=args.user, database=args.database, ca_file=args.ca_file)
    try:
        if args.command == 'lookup':
            result = lookup_factor(c, args.factor_id, run_id=args.run_id, eaggl_import_id=args.eaggl_import_id)
        else:
            plan = prepare_mapping(c, args.cfde_catalog, eaggl_import_id=args.eaggl_import_id, gene_set_import_id=args.gene_set_import_id)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.with_suffix('.manifest.json').write_text(json.dumps(plan['manifest'], indent=2) + '\n')
            result = {'run_id': plan['manifest']['run_id'], 'status': 'planned', 'apply': args.apply,
                      'counts': plan['manifest']['counts'], 'unmatched_by_reason': plan['manifest']['unmatched_by_reason']}
            if args.apply:
                result.update(load_mapping(c, plan))
    finally:
        c.close()
    output = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True); args.report.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
