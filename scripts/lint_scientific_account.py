#!/usr/bin/env python3
"""Lint one hydrated ScientificAccount document using the pinned DAPPER release."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.scientific_account_lint import lint_scientific_account


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    parser.add_argument('--dapper-root', type=Path, default=os.getenv('REVEAL_DAPPER_ROOT'))
    parser.add_argument('--release-lock', type=Path, default=ROOT / 'services/backend/agent-runtime/dapper-release.json')
    parser.add_argument('--evidence-package', type=Path, default=os.getenv('REVEAL_EVIDENCE_PACKAGE'))
    parser.add_argument('--mode', choices=['draft', 'final', 'profile-only'], default='draft')
    parser.add_argument('--strict', action='store_true', help='Treat upstream warnings as failures')
    parser.add_argument('--output', type=Path, help='Save the JSON report as well as printing it')
    args = parser.parse_args(argv)
    if not args.dapper_root: parser.error('--dapper-root is required outside a prepared agent workspace')
    if args.mode != 'profile-only' and not args.evidence_package: parser.error('--evidence-package is required for REVEAL linting')
    if args.output and args.output.resolve() in {args.document.resolve(), args.evidence_package.resolve() if args.evidence_package else None, args.release_lock.resolve()}:
        parser.error('Report output must not overwrite an input')
    report = lint_scientific_account(args.document, dapper_root=args.dapper_root, release_lock=args.release_lock,
                                     evidence_package=args.evidence_package, mode=args.mode, strict=args.strict)
    text = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(text)
    print(text, end='')
    return 0 if report['valid'] else (2 if report.get('operational_error') else 1)


if __name__ == '__main__':
    raise SystemExit(main())
