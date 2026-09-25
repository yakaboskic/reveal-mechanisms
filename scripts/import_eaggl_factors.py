#!/usr/bin/env python3
"""Prepare EAGGL bundles, embed their names, load MySQL, and search on demand."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend.eaggl_bundle import open_capture, prepare, write_json
from reveal_backend.eaggl_database import DEFAULT_HOST, connect, load_capture, validate_database
from reveal_backend.eaggl_embeddings import embed_capture, local_search_index, read_embeddings


@contextmanager
def capture_lock(output):
    # The OS releases this advisory lock even if a process is killed.
    with (output / '.import.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another command is using this capture') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    # Read only this project's configuration, regardless of the working directory.
    # Preserve shell overrides and literal dollar signs in API keys/passwords.
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare', help='Validate and freeze a bundle locally; no network calls')
    p.add_argument('--bundle', type=Path)
    for name in ('metadata', 'loadings', 'factor-ids', 'genes', 'graph'):
        p.add_argument('--' + name, type=Path)
    p.add_argument('--source-namespace', default='eaggl')
    p.add_argument('--source-version', required=True, help='Explicit atlas/build version; never inferred as cfde-inc-v2')
    p.add_argument('--output', required=True, type=Path)
    e = commands.add_parser('embed', help='Call the embedding service, caching each successful batch')
    e.add_argument('--output', required=True, type=Path)
    e.add_argument('--model')
    e.add_argument('--model-revision', default=os.getenv('EMBEDDING_MODEL_REVISION', 'unspecified'), help='Record the deployed model revision when known')
    e.add_argument('--service-url')
    e.add_argument('--provider', default=os.getenv('EMBEDDING_PROVIDER', 'huggingface'))
    e.add_argument('--batch-size', type=int, default=100)
    e.add_argument('--max-retries', type=int, default=3)
    e.add_argument('--timeout', type=float, default=120)
    load = commands.add_parser('load', help='Dry-run by default; --apply writes the selected MySQL database')
    load.add_argument('--output', required=True, type=Path)
    load.add_argument('--run-id')
    load.add_argument('--host', default=os.getenv('REVEAL_MYSQL_HOST', DEFAULT_HOST))
    load.add_argument('--port', type=int, default=os.getenv('REVEAL_MYSQL_PORT', '3306'))
    load.add_argument('--user', default=os.getenv('REVEAL_MYSQL_USER', 'cyaka'))
    load.add_argument('--database', default=os.getenv('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'))
    load.add_argument('--ca-file', default=os.getenv('REVEAL_MYSQL_CA_FILE') or None)
    load.add_argument('--apply', action='store_true')
    load.add_argument('--batch-size', type=int, default=5000)
    search = commands.add_parser('search', help='On-demand cosine search against a prepared embedding capture')
    search.add_argument('--output', required=True, type=Path)
    search.add_argument('--run-id')
    query = search.add_mutually_exclusive_group(required=True)
    query.add_argument('--query')
    query.add_argument('--factor-id', help='Use a stored factor vector without calling the service')
    search.add_argument('--top-k', type=int, default=10)
    search.add_argument('--trait', help='Exact source trait filter')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if args.command == 'prepare':
        kwargs = vars(args).copy(); kwargs.pop('command')
        result = prepare(**kwargs)
    else:
        with capture_lock(args.output):
            if args.command == 'embed':
                kwargs = vars(args).copy(); kwargs.pop('command')
                result = embed_capture(**kwargs)
            elif args.command == 'search':
                index = local_search_index(args.output, args.run_id)
                result = index.search(query=args.query, factor_id=args.factor_id, top_k=args.top_k, trait=args.trait)
            else:
                validate_database(args.database)
                if args.batch_size < 1:
                    raise ValueError('Batch size must be positive')
                manifest, factors, _, _, _ = open_capture(args.output)
                result = {'database': args.database, 'import_id': manifest['import_id'], 'counts': manifest['counts'], 'apply': args.apply}
                # A source-only dry run works before embedding credentials are configured.
                if (args.output / 'embeddings.sqlite3').exists() or args.apply or args.run_id:
                    run, _ = read_embeddings(args.output, factors, manifest['import_id'], args.run_id)
                    result['embedding_run'] = run
                else:
                    result['embedding_status'] = 'not generated; run embed before load --apply'
                if args.apply:
                    connection = connect(host=args.host, port=args.port, user=args.user,
                                         database=args.database, ca_file=args.ca_file)
                    try:
                        result = load_capture(connection, args.output, run_id=args.run_id, batch_size=args.batch_size)
                    finally:
                        connection.close()
                    write_json(args.output / 'database-load.json', result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
