#!/usr/bin/env python3
"""Validate DisMech exports, load Aurora with --apply, or verify stored records."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend.dismech_import import load_export, open_export, verify_export
from reveal_backend.mysql_database import DEFAULT_HOST, connect, validate_database


def main(argv=None):
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('load', 'verify'), nargs='?', default='load')
    parser.add_argument('--source', type=Path, default=ROOT / 'data/dismech')
    parser.add_argument('--gaps', type=Path, default=ROOT / 'data/dismech-gaps/2026-09-24')
    parser.add_argument('--host', default=os.getenv('REVEAL_MYSQL_HOST', DEFAULT_HOST))
    parser.add_argument('--port', type=int, default=os.getenv('REVEAL_MYSQL_PORT', '3306'))
    parser.add_argument('--user', default=os.getenv('REVEAL_MYSQL_USER', 'cyaka'))
    parser.add_argument('--database', default=os.getenv('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'))
    parser.add_argument('--ca-file', default=os.getenv('REVEAL_MYSQL_CA_FILE') or None)
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--apply', action='store_true', help='Create DisMech tables and load data; default is offline validation')
    parser.add_argument('--report', type=Path, help='Write a JSON result, without credentials')
    args = parser.parse_args(argv)
    if args.command == 'verify' and args.apply:
        parser.error('verify is read-only and does not accept --apply')
    if args.batch_size < 1:
        parser.error('--batch-size must be positive')
    validate_database(args.database)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    export = open_export(args.source, args.gaps)
    result = {'import_id': export.import_id, 'database': args.database, 'status': 'validated', 'apply': args.apply,
              'counts': export.manifest['counts'], 'gaps_by_kind': export.manifest['gaps_by_kind'],
              'attachment_resolution': export.manifest['attachment_resolution']}
    if args.apply or args.command == 'verify':
        connection = connect(host=args.host, port=args.port, user=args.user, database=args.database, ca_file=args.ca_file)
        try:
            result.update(load_export(connection, export, batch_size=args.batch_size) if args.apply else verify_export(connection, export))
        finally:
            connection.close()
    output = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
