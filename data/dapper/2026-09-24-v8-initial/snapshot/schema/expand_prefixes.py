#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime", "linkml==1.11.1"]
# ///
"""Export a DAPPER graph with absolute URIs and consistent content identifiers."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

import yaml

from document_prefixes import expand_document
from lint.lint_provenance import (
    PROFILES_PATH, SCHEMA_PATH, Vocabulary, build_validator, lint, load_schema, print_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", "-o", required=True, type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    parser.add_argument("--profile", help="end modality (default: auto-detect)")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("use a different output path to preserve the source document")
    try:
        sv = load_schema(args.schema)
        document = yaml.safe_load(args.input.read_text())
        expanded = expand_document(document, sv)
        text = yaml.safe_dump(expanded, sort_keys=False, allow_unicode=True)
        vocab = Vocabulary.build(sv, yaml.safe_load(PROFILES_PATH.read_text()))
        validator = build_validator(args.schema)
        # Validate before touching the requested output, including on failure.
        with tempfile.TemporaryDirectory(prefix="dapper-expand-") as tmp:
            candidate = Path(tmp) / args.output.name
            candidate.write_text(text)
            report = lint(candidate, vocab, sv, validator, args.profile)
        report.path = args.output
        print_report(report, vocab)
        if report.errors:
            return 1
        args.output.write_text(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Cannot expand document: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
