#!/usr/bin/env python3
"""Explicit, resumable DisMech context embeddings; existing EAGGL vectors stay unchanged."""
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


@contextmanager
def capture_lock(output):
    """Keep concurrent explicit commands from changing the same local capture."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    path = output.parent / ('.' + output.name + '.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another embedding command is using this output directory.') from error
        try:
            yield output
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def arguments(argv=None):
    from reveal_backend.mysql_database import DEFAULT_HOST, validate_database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'embed', 'migrate', 'load', 'verify'))
    parser.add_argument('--output', type=Path, default=ROOT / '.runtime/dismech-embeddings')
    parser.add_argument('--source', type=Path, default=ROOT / 'data/dismech')
    parser.add_argument('--gaps', type=Path, default=ROOT / 'data/dismech-gaps/2026-09-24')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--eaggl-run-id', help='Read this completed EAGGL embedding run from Aurora during prepare')
    target.add_argument('--target-run-file', type=Path, help='Prepare offline from complete read_target_run metadata including calibration probes; load verifies the live run')
    parser.add_argument('--host', default=os.getenv('REVEAL_MYSQL_HOST', DEFAULT_HOST))
    parser.add_argument('--port', type=int, default=int(os.getenv('REVEAL_MYSQL_PORT', '3306')))
    parser.add_argument('--user', default=os.getenv('REVEAL_MYSQL_USER', 'cyaka'))
    parser.add_argument('--database', default=os.getenv('REVEAL_MYSQL_DATABASE', 'cyaka_reveal_mechanisms'))
    parser.add_argument('--ca-file', default=os.getenv('REVEAL_MYSQL_CA_FILE') or None)
    parser.add_argument('--batch-size', type=int, help='Embedding texts per request (default 32, maximum 100), or database rows per load batch (default 500)')
    parser.add_argument('--max-workers', type=int, default=2, help='Concurrent embedding requests, 1 to 4, default 2')
    parser.add_argument('--max-batches', type=int, help='Embed only: stop after this many committed groups, each at most batch-size times max-workers texts')
    parser.add_argument('--max-retries', type=int, default=3, help='Retries per embedding batch, default 3')
    parser.add_argument('--timeout', type=float, default=120, help='Embedding HTTP timeout per attempt in seconds')
    parser.add_argument('--local-device', choices=('mps', 'cpu'), help='Embed offline with optional isolated SentenceTransformers dependencies')
    parser.add_argument('--local-revision', help='Exact cached Hugging Face model commit, required with --local-device')
    parser.add_argument('--local-cache-dir', type=Path, help='Hugging Face hub cache containing the exact snapshot')
    parser.add_argument('--local-calibration-reference', type=Path, help='Frozen remote DisMech reference bundle')
    parser.add_argument('--local-reference-sha256', help='Explicitly pinned SHA256 of the frozen reference bundle')
    parser.add_argument('--legacy-generation-attestation', type=Path, help='JSON retrospective attribution of pre-journaling vectors; required for local calibration')
    parser.add_argument('--apply', action='store_true', help='Required for explicit schema migration or database loading')
    parser.add_argument('--report', type=Path, help='Write a JSON result without credentials')
    args = parser.parse_args(argv)
    if args.command == 'prepare' and not (args.eaggl_run_id or args.target_run_file):
        parser.error('prepare requires --eaggl-run-id or --target-run-file')
    if args.command != 'prepare' and (args.eaggl_run_id or args.target_run_file):
        parser.error('target-run options are only valid for prepare')
    if args.command in ('migrate', 'load') and not args.apply:
        parser.error(args.command + ' changes Aurora and requires --apply')
    if args.command not in ('migrate', 'load') and args.apply:
        parser.error('--apply is only valid for migrate and load')
    if args.batch_size is not None and args.batch_size < 1:
        parser.error('--batch-size must be positive')
    if args.command == 'embed' and args.batch_size is not None and args.batch_size > 100:
        parser.error('embedding batch size cannot exceed 100 texts')
    if args.max_batches is not None and (args.command != 'embed' or args.max_batches < 1):
        parser.error('--max-batches is a positive limit for embed only')
    if not 1 <= args.max_workers <= 4 or args.max_retries < 0 or not (0 < args.timeout < float('inf')):
        parser.error('workers must be 1 to 4, timeout positive, and retries nonnegative')
    local_options = (args.local_device, args.local_revision, args.local_cache_dir,
                     args.local_calibration_reference, args.local_reference_sha256)
    if any(local_options) and (args.command != 'embed' or not all(local_options)
                               or not args.legacy_generation_attestation):
        parser.error('local inference is embed-only and requires every --local-* option and --legacy-generation-attestation')
    if args.local_device and args.max_workers != 1:
        parser.error('local inference requires --max-workers 1')
    if args.legacy_generation_attestation and args.command != 'embed':
        parser.error('--legacy-generation-attestation is embed-only')
    validate_database(args.database)
    return args


def main(argv=None):
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    args = arguments(argv)
    from reveal_backend import dismech_embeddings as embeddings
    from reveal_backend.dismech_import import open_export
    from reveal_backend.mysql_database import connect

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    @contextmanager
    def database():
        connection = connect(host=args.host, port=args.port, user=args.user,
                             database=args.database, ca_file=args.ca_file)
        try:
            yield connection
        finally:
            connection.close()

    if args.command == 'migrate':
        with database() as connection:
            embeddings.migrate(connection)
        result = {'command': 'migrate', 'database': args.database, 'migration': '006', 'applied': True}
    else:
        with capture_lock(args.output) as output:
            if args.command == 'prepare':
                export = open_export(args.source, args.gaps)
                if args.target_run_file:
                    target_run = json.loads(args.target_run_file.read_text())
                else:
                    with database() as connection:
                        target_run = embeddings.read_target_run(connection, args.eaggl_run_id)
                result = embeddings.prepare_capture(output, export, target_run)
            elif args.command == 'embed':
                optional = {}
                if args.legacy_generation_attestation:
                    optional['legacy_generation_attestation'] = json.loads(args.legacy_generation_attestation.read_text())
                if args.local_device:
                    from reveal_backend.local_embeddings import prepare_local_embedder
                    runner = prepare_local_embedder(output, reference_path=args.local_calibration_reference,
                        reference_sha256=args.local_reference_sha256,
                        attestation=optional['legacy_generation_attestation'], revision=args.local_revision,
                        cache_dir=args.local_cache_dir, device=args.local_device)
                    optional.update(embedder=runner, generation_metadata=runner.generation_metadata)
                result = embeddings.embed_capture(output, batch_size=args.batch_size or 32,
                    max_workers=args.max_workers, max_retries=args.max_retries, timeout=args.timeout,
                    max_batches=args.max_batches, **optional)
            elif args.command == 'load':
                with database() as connection:
                    result = embeddings.load_capture(connection, output, batch_size=args.batch_size or 500)
            else:
                with database() as connection:
                    result = embeddings.verify_capture(connection, output)
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
