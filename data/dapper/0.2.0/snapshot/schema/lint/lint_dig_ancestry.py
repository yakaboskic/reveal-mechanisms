#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime", "linkml==1.11.1"]
# ///
"""Validate DAPPER, then check DIG export folders against declared ancestry.

    uv run schema/lint/lint_dig_ancestry.py schema/examples/example_bottom_line_af_aa.yaml

This opt-in check is specific to DIG's public/staging bottom-line exports.
It performs no network requests and never fills or overwrites metadata.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import lint_provenance as lp


BUCKETS = {"dig-open-bottom-line-analysis", "dig-open-bottom-line-analysis-stg"}


def export_ancestry(location: str) -> str | None:
    """Read only bottom-line/{ancestry}/[phenotype.sumstats.tsv.gz] in DIG S3."""
    uri = urlsplit(location)
    if uri.scheme != "s3" or uri.netloc not in BUCKETS:
        return None
    parts = uri.path.removeprefix("/").rstrip("/").split("/")
    if len(parts) not in (2, 3) or parts[0] != "bottom-line" or not parts[1]:
        return None
    if len(parts) == 3 and not parts[2].endswith(".sumstats.tsv.gz"):
        return None
    return parts[1]


def check_ancestry(doc: lp.Document, vocab: lp.Vocabulary, sv, report: lp.Report) -> None:
    """Check a schema-valid graph, including inline and reified distributions."""
    allowed = sv.get_enum("AncestryEnum").permissible_values
    locations = {}
    for node_id, (_, cls, node) in doc.nodes.items():
        if cls not in ("Dataset", "File", "C2M2File") or not node.get("location"):
            continue
        code = export_ancestry(node["location"])
        if code is None:
            continue
        locations[node_id] = code
        if code not in allowed:
            report.add("error", "dig-ancestry", node_id,
                       f"DIG export folder {code!r} is not an AncestryEnum value")

    comparisons = {(node_id, node_id) for node_id in locations}
    has_file = sv.expand_curie("dapper:hasFile")
    comparisons.update((r.subject, r.object) for r in doc.relationships(vocab)
                       if r.predicate == has_file and r.object in locations)
    for dataset_id, location_id in sorted(comparisons):
        entry = doc.nodes.get(dataset_id)
        if not entry or entry[1] != "Dataset":
            continue
        declared = entry[2].get("ancestry")
        code = locations[location_id]
        if declared is not None and code in allowed and declared != code:
            report.add("error", "dig-ancestry", dataset_id,
                       f"ancestry {declared!r} conflicts with DIG export folder {code!r} "
                       f"at {location_id}.location")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    sv = lp.load_schema(lp.SCHEMA_PATH)
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    validator = lp.build_validator(lp.SCHEMA_PATH)
    failed = False
    for path in args.files:
        report = lp.lint(path, vocab, sv, validator)
        if not report.errors:
            check_ancestry(lp.Document.load(path, vocab), vocab, sv, report)
        lp.print_report(report, vocab)
        failed |= bool(report.errors)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
