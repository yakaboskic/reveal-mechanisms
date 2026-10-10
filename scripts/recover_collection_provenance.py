#!/usr/bin/env python3
"""Recover historical provenance from checksum-matching originals; dry-run unless --apply."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend.provenance_supplements import recover_collection_provenance
from reveal_backend.repository import Repository


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation-id', required=True)
    parser.add_argument('--apply', action='store_true', help='Register verified immutable supplements')
    parser.add_argument('collections', type=Path, nargs='+', help='Original GeneSetCollection YAML files')
    args = parser.parse_args(argv)
    try:
        repository = Repository()
        for path in args.collections:
            print(json.dumps(recover_collection_provenance(repository, args.generation_id, path, apply=args.apply), sort_keys=True))
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
