#!/usr/bin/env python3
"""Prepare DisMech/EAGGL demo payloads, evaluate with Jev, and rank candidates."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))


def main(argv=None):
    load_dotenv(ROOT / '.env', override=False, interpolate=False)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Read verified local captures; write requests without network calls')
    prepare.add_argument('--source', type=Path, default=ROOT / 'data/dismech')
    prepare.add_argument('--gaps', type=Path, default=ROOT / 'data/dismech-gaps/2026-09-24')
    prepare.add_argument('--eaggl', type=Path, default=ROOT / 'data/eaggl/captures/legacy-711')
    prepare.add_argument('--contexts', type=Path, default=ROOT / '.runtime/dismech-embeddings')
    prepare.add_argument('--cfde', type=Path, default=ROOT / 'data/cfde')
    prepare.add_argument('--model', default='jev-1.13.0', help='Pinned model; override explicitly to compare a newer release')
    prepare.add_argument('--top-factors', type=int, default=20)
    prepare.add_argument('--top-genes', type=int, default=20)
    prepare.add_argument('--other-gaps', type=int, default=12)
    prepare.add_argument('--max-request-bytes', type=int, default=48_000,
                         help='Byte guard, NOT an exact Jev token limit; removes comparison questions first')
    prepare.add_argument('--limit', type=int)
    prepare.add_argument('--disease', help='Case-insensitive substring of source disease name')
    prepare.add_argument('--gap-id', action='append', default=[], help='Exact DisMech source ID; repeat to select several')
    prepare.add_argument('--include-resolved', action='store_true')
    prepare.add_argument('--questions-file', type=Path, help='Custom SystemOne questions object; disables standard weighted ranking')
    run = commands.add_parser('run', help='Dry run by default; --send submits requests to TypeSafe AI')
    run.add_argument('--send', action='store_true')
    run.add_argument('--batch-size', type=int, default=25, help='Requests per locally scheduled batch; one gap per HTTP call')
    run.add_argument('--workers', type=int, default=4)
    run.add_argument('--limit', type=int, help='Maximum uncached gaps in this invocation, not maximum HTTP attempts')
    run.add_argument('--retries', type=int, default=2, help='Extra attempts per request on transient errors')
    run.add_argument('--timeout', type=float, default=120)
    recover = commands.add_parser('recover', help='Revalidate saved HTTP 200 failures locally; makes no API calls')
    report = commands.add_parser('report', help='Write ranked JSON, CSV and a diverse shortlist from saved responses')
    report.add_argument('--shortlist-size', type=int, default=10)
    report.add_argument('--max-per-disease', type=int, default=2)
    report.add_argument('--min-confidence', type=float, default=.5)
    for command in (prepare, run, recover, report):
        command.add_argument('--output', type=Path, default=ROOT / '.runtime/jev-demo')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        from reveal_backend.demo_prioritization import LocalCorpus, prepare as prepare_payloads
        questions = json.loads(args.questions_file.read_text()) if args.questions_file else None
        if questions is not None:
            from reveal_backend.jev_batch import validate_questions
            validate_questions(questions)
        print('Validating local snapshots and stored embeddings; no remote calls.', flush=True)
        corpus = LocalCorpus(args.source, args.gaps, args.eaggl, args.contexts, args.cfde)
        result = prepare_payloads(corpus, args.output, model=args.model, top_factors=args.top_factors,
            top_genes=args.top_genes, other_gaps=args.other_gaps, limit=args.limit, disease=args.disease,
            gap_ids=args.gap_id, include_resolved=args.include_resolved, max_bytes=args.max_request_bytes, questions=questions)
    elif args.command == 'run':
        from reveal_backend.jev_batch import run as run_batches
        result = run_batches(args.output, api_key=os.getenv('TYPESAFE_API_KEY'), send=args.send,
            batch_size=args.batch_size, workers=args.workers, limit=args.limit, retries=args.retries, timeout=args.timeout)
    elif args.command == 'recover':
        from reveal_backend.jev_batch import recover as recover_results
        result = recover_results(args.output)
    else:
        from reveal_backend.jev_batch import report as report_results
        result = report_results(args.output, shortlist_size=args.shortlist_size,
            max_per_disease=args.max_per_disease, min_confidence=args.min_confidence)
    print(json.dumps(result, indent=2))
    return 1 if result.get('failed_now', 0) or result.get('blocked_requests', 0) or result.get('unrecoverable_saved_errors', 0) else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
