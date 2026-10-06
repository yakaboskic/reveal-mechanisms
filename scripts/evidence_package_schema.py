#!/usr/bin/env python3
"""Generate the evidence-package JSON Schema or validate a collected YAML/JSON package."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))

from reveal_backend.evidence_package import DapperRuntime, EvidenceBuildError, decode
from reveal_backend.evidence_schema import generate_schema, validate_package_shape
from reveal_backend.runtime_config import dapper_snapshot_for_pin


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path, default=ROOT / 'schema/evidence-package.yaml')
    subparsers = parser.add_subparsers(dest='command', required=True)
    generate = subparsers.add_parser('generate', help='Compile the canonical LinkML schema')
    generate.add_argument('--output', type=Path, default=ROOT / 'schema/evidence-package.schema.json')
    generate.add_argument('--check', action='store_true', help='Fail if the checked-in generated schema is stale')
    validate = subparsers.add_parser('validate', help='Check package structure and pinned DAPPER object identities')
    validate.add_argument('package', type=Path)
    args = parser.parse_args(argv)
    try:
        schema = generate_schema(args.schema)
        if args.command == 'generate':
            encoded = (json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode()
            if args.check:
                if args.output.read_bytes() != encoded:
                    raise EvidenceBuildError('Generated schema is stale; run the generate command')
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(encoded)
            print(json.dumps({'schema': str(args.output), 'checked': args.check}))
        else:
            package = decode(args.package.read_bytes(), 'yaml' if args.package.suffix in ('.yaml', '.yml') else 'json')
            validate_package_shape(package, schema)
            runtime = DapperRuntime(dapper_snapshot_for_pin(package['dapper_pin']))
            runtime.resolver(package['prefixes'])
            if package['dapper_context']['prefixes'] != package['prefixes']:
                raise EvidenceBuildError('Embedded DAPPER prefix map differs from the package')
            nodes = runtime.validate(package['dapper_context'])
            print(json.dumps({'package': str(args.package), 'package_version': package['package_version'],
                              'schema_valid': True, 'checked_dapper_objects': len(nodes)}))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
