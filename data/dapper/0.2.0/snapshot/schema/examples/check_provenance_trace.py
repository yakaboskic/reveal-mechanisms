#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""Check that example_claim_provenance_trace.yaml actually traces end to end.

Validating each node against the schema proves the SHAPES are right. It does not
prove the graph is CONNECTED — a typo'd id yields a document full of individually
valid nodes that no longer traces anywhere. This script checks the two things the
example exists to demonstrate:

  1. Referential integrity — every internal id referenced anywhere resolves to a
     node defined in the document.
  2. Reachability — starting from the scientific account and following only
     the documented edges, you arrive at every raw C2M2 source file.
  3. Nanopublication well-formedness — every nanopub has all three of
     hasAssertion / hasProvenance / hasPublicationInfo. The schema makes these
     individually optional, so linkml-validate cannot catch a nanopub that is
     missing two of its four graphs.

A C2M2 file counts as a RAW SOURCE only if nothing generated it; intermediate
files are traversed through, not treated as endpoints.

Usage:
    uv run schema/examples/check_provenance_trace.py
    uv run schema/examples/check_provenance_trace.py --quiet

Exits non-zero if anything dangles or any raw source is unreachable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

DEFAULT_FILE = Path(__file__).parent / "example_claim_provenance_trace.yaml"
def find_start(doc: dict) -> str:
    """Find the scientific account at the top of the migrated provenance trace."""
    accounts = doc.get("scientific_accounts") or []
    if len(accounts) != 1:
        raise SystemExit("expected one scientific account to trace from")
    return accounts[0]["id"]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "identity"))
from dapper_identity import DOC_GROUPS, digest_of
NODE_GROUPS = list(DOC_GROUPS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--quiet", action="store_true", help="only report failures")
    args = ap.parse_args()

    doc = yaml.safe_load(args.file.read_text())
    nodes: dict[str, tuple[str, dict]] = {}
    for group in NODE_GROUPS:
        for node in doc.get(group, []) or []:
            nodes[node["id"]] = (group, node)

    def edges_out(subject: str, key: str) -> list[str]:
        return [e["object"] for e in doc.get(key, []) or [] if e["subject"] == subject]

    # --- 1. referential integrity -----------------------------------------
    dangling: set[str] = set()
    ref_count = 0

    def scan(obj) -> None:
        nonlocal ref_count
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k != "id":
                    scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)
        elif isinstance(obj, str) and digest_of(obj):
            ref_count += 1
            if obj not in nodes:
                dangling.add(obj)

    scan(doc)
    print(f"referential integrity: {ref_count} internal refs, {len(dangling)} dangling")
    for d in sorted(dangling):
        print(f"   DANGLING: {d}")

    # --- 2. reachability ---------------------------------------------------
    reached: set[str] = set()

    def walk(nid: str, depth: int, label: str, seen: frozenset[str]) -> None:
        if nid in seen:  # cycle guard
            return
        seen = seen | {nid}
        group, node = nodes.get(nid, ("?", {}))
        if not args.quiet:
            print(f"{'  ' * depth}{label}{nid}  [{group}] {node.get('name', '')[:48]}")
        if group == "c2m2_files" and not edges_out(nid, "was_generated_by_edges"):
            reached.add(nid)
            if not args.quiet:
                print(f"{'  ' * (depth + 1)}^^ RAW SOURCE")
            return
        for field in ("component_claims", "has_evidence", "source_claims", "from_nanopub",
                      "has_provenance", "was_generated_by", "was_derived_from"):
            values = node.get(field)
            for ref in values if isinstance(values, list) else [values]:
                if isinstance(ref, str) and ref in nodes:
                    walk(ref, depth + 1, f"-{field}-> ", seen)
        for prov in edges_out(nid, "has_provenance_edges"):
            walk(prov, depth + 1, "-np:hasProvenance-> ", seen)
        if group == "nanopub_provenances":
            act = node.get("generated_by_activity")
            if act:
                walk(act, depth + 1, "-generatedByActivity-> ", seen)
        for used in edges_out(nid, "used_edges"):
            walk(used, depth + 1, "-prov:used-> ", seen)
        for gen in edges_out(nid, "was_generated_by_edges"):
            walk(gen, depth + 1, "-prov:wasGeneratedBy-> ", seen)

    if not args.quiet:
        print("\nfull recursive trace:")
    walk(find_start(doc), 0, "", frozenset())

    raw = {
        i for i, (g, _) in nodes.items()
        if g == "c2m2_files" and not edges_out(i, "was_generated_by_edges")
    }
    intermediates = {i for i, (g, _) in nodes.items() if g == "c2m2_files"} - raw
    print(f"\nraw C2M2 sources reached: {len(reached)}/{len(raw)}")
    for s in sorted(raw):
        print(f"   {'REACHED  ' if s in reached else 'UNREACHED'} {s}")
    for i in sorted(intermediates):
        print(f"   (intermediate, traversed) {i}")

    # --- 3. nanopublication well-formedness ---------------------------------
    # The spec requires exactly one each of hasAssertion / hasProvenance /
    # hasPublicationInfo. A nanopub with only an assertion is malformed, and
    # nothing else in this repo would notice — linkml-validate happily accepts
    # it because all three slots are individually optional in the schema.
    required = ["has_assertion", "has_provenance", "has_publication_info"]
    malformed: list[str] = []
    for node in doc.get("nanopublications", []) or []:
        missing = [r for r in required if not node.get(r)]
        if missing:
            malformed.append(f"{node['id']} missing {', '.join(missing)}")
    n_np = len(doc.get("nanopublications", []) or [])
    print(f"\nnanopub well-formedness: {n_np - len(malformed)}/{n_np} complete")
    for m in malformed:
        print(f"   MALFORMED: {m}")

    ok = not dangling and reached == raw and not malformed
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
