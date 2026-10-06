#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema[format]>=4.18,<5"]
# ///
"""Validate external REVEAL citation metadata; does not mint or publish records.

    uv run schema/citation_metadata.py path/to/record.json

Persistence, authorization, DOI registration, title fidelity, and authenticating
source/ORCID assertions remain application responsibilities.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_PATH = Path(__file__).parent / "citations" / "citation-record.schema.json"


def check_citation_metadata(record: dict) -> list[str]:
    """Check the registry record's shape and date-selection invariants."""
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    problems = [f"{'.'.join(map(str, e.absolute_path)) or '$'}: {e.message}"
                for e in validator.iter_errors(record)]
    if problems:
        return problems
    provenance = record["date_provenance"]
    for field in ("generated_at", "first_minted_at", "published_at", "original_issued_date", "imported_at"):
        if record.get(field) is not None and field not in provenance:
            problems.append(f"{field}: a known date requires source provenance")
    original = record.get("original_issued_date")
    basis = record["issued_basis"]
    if record["origin"] == "imported" and original is not None:
        if basis != "original_issued_date":
            problems.append("issued_basis: an imported object's known original issue date must be retained")
    if record["origin"] == "native" and record["first_minted_at"] is not None and basis != "first_minted_at":
        problems.append("issued_basis: native objects with a known first mint use that date")
    if basis == "original_issued_date":
        if record["origin"] != "imported" or original is None or record["issued_date"] != original:
            problems.append("issued_date: original_issued_date basis requires an imported object's matching known date")
    elif basis == "first_minted_at":
        minted = record["first_minted_at"]
        if minted is None or record["issued_date"] != minted[:10]:
            problems.append("issued_date: first_minted_at basis requires the matching UTC mint date")
    elif record["issued_date"] is not None:
        problems.append("issued_date: unknown basis requires a null date")
    if record["publication_state"] == "unpublished" and record["published_at"] is not None:
        problems.append("published_at: an unpublished object cannot have a publication timestamp")
    if record.get("published_at") and record.get("first_minted_at"):
        from datetime import datetime
        if datetime.fromisoformat(record["published_at"].replace("Z", "+00:00")) < datetime.fromisoformat(record["first_minted_at"].replace("Z", "+00:00")):
            problems.append("published_at: platform publication cannot precede first durable registration")
    return problems


def check_citation_registry_links(paragraph: dict, records: list[dict]) -> list[str]:
    """Check pinned occurrences against supplied records, without fetching URLs.

    Callers validate graph shapes/content and each metadata record separately.
    This explicit check prevents a renderer silently substituting the latest
    metadata revision when the requested revision is unavailable.
    """
    keys = [(r["target_id"], r["metadata_revision"]) for r in records]
    problems = []
    if len(keys) != len(set(keys)):
        problems.append("registry: duplicate (target_id, metadata_revision)")
    available = set(keys)
    for i, citation in enumerate(paragraph.get("citations") or []):
        # Registry keys always use the canonical CURIE; URI exports may use the
        # equivalent namespace spelling for a citation target.
        target = citation["target_id"]
        namespace = "https://broadinstitute.github.io/dapper/ns#"
        if target.startswith(namespace):
            target = "dapper:" + target[len(namespace):]
        if (target, citation["citation_metadata_revision"]) not in available:
            problems.append(f"citations[{i}]: pinned target and metadata revision are absent from supplied registry records")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    try:
        problems = check_citation_metadata(json.loads(args.record.read_text()))
    except (OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Cannot read citation record: {exc}\n")
    for problem in problems:
        print(problem)
    if not problems:
        print("Citation metadata is valid; publication and external assertions were not verified.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
