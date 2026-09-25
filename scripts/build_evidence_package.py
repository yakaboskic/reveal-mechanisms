#!/usr/bin/env python3
"""Collect evidence from factor/gap IDs, or replay frozen inputs without network access."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend.evidence_collector import collect_package
from reveal_backend.evidence_package import DapperRuntime, EvidenceBuildError, build_package, load_build_input


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    collect = commands.add_parser('collect', help='Resolve IDs and make bounded CFDE/BioIndex queries')
    collect.add_argument('--dismech-id', required=True, help='Full source gap ID or an unambiguous discussion_id')
    collect.add_argument('--factor', action='append', required=True, help='Full interactive EAGGL factor ID; repeat for multiple anchors')
    collect.add_argument('--model', default='cfde-inc-v2')
    collect.add_argument('--limit', type=int, default=100, help='Bound rows/candidates per query (1–100)')
    collect.add_argument('--max-nodes', type=int, default=250)
    collect.add_argument('--max-edges', type=int, default=1000)
    collect.add_argument('--dismech-source', type=Path, default=ROOT.parent / 'dismech')
    collect.add_argument('--dismech-index', type=Path, default=ROOT / 'data/dismech-gaps/2026-09-24')
    collect.add_argument('--geneset-import', type=Path, default=ROOT / 'data/cfde-genesets/2026-09-24')
    replay = commands.add_parser('replay', help='Rebuild a capture locally; makes no API calls')
    replay.add_argument('--input', required=True, type=Path, help='Saved build-input.json')
    replay.add_argument('--source-root', type=Path, help='Artifact root; defaults to the input file directory')
    for command in [collect, replay]:
        command.add_argument('--dapper-snapshot', type=Path, default=ROOT / 'data/dapper/2026-09-24-v8')
        command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        runtime = DapperRuntime(args.dapper_snapshot)
        if args.command == 'collect':
            result = collect_package(gap_id=args.dismech_id, factor_ids=args.factor, output=args.output, dapper=runtime,
                                     project_root=ROOT, dismech_source=args.dismech_source, dismech_index=args.dismech_index,
                                     geneset_import=args.geneset_import, model=args.model, limit=args.limit,
                                     max_nodes=args.max_nodes, max_edges=args.max_edges)
            package_path = args.output / 'package'
        else:
            spec, blobs = load_build_input(args.input, args.source_root or args.input.parent)
            result = build_package(spec, blobs, runtime)
            result.write(args.output); package_path = args.output
        print(json.dumps({'package': str(package_path), 'package_sha256': result.manifest['package_sha256'],
                          'coverage': result.package['coverage'], 'readiness': result.package['readiness']}, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Evidence package could not be built: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
