#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""Point this at your gene sets. Get back one YAML collection with IDs minted.

    uv run schema/identity/mint.py /path/to/genesets -o collection.yaml

That is the whole thing. `<input>` can be any of:

    a folder            walked recursively for geneset.provenance.json
    an s3:// prefix     the JSON sidecars are pulled down first
    one .json file      a single geneset.provenance.json
    a .yaml collection  re-mint an existing collection (see "Idempotent" below)

YOU DO NOT WRITE IDS BY HAND. Ever. An identifier is computed from the content
it names, so a hand-written one can drift out of sync with what it points at and
start lying. Leave `id` out of your source data; this script fills it in. If it
finds hand-written ids in the input it will say so and replace them.

Idempotent: running it twice on unchanged data produces byte-identical output.
If an id changes, the content changed — that is a signal, not noise. Diff the
YAML to see what moved.

Deduplication is free. Two gene sets that both used `human_gene_info` produce
the same digest for it, so the merged collection carries one copy instead of
two. The script reports how many duplicates collapsed.

See README.md in this folder for the algorithm and why it is not a Trusty URI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "converter"))

from dapper_identity import (  # noqa: E402
    DOC_GROUPS,
    assign_ids,
    load_schema,
    unknown_node_groups,
)

SCHEMA = HERE.parent / "dapper.yaml"


def _collect_inputs(target: Path | str) -> list[Path]:
    """Find every geneset.provenance.json under `target`."""
    from geneset_to_dapper import _pull_s3  # noqa: PLC0415

    if isinstance(target, str) and target.startswith("s3://"):
        import tempfile

        dest = Path(tempfile.mkdtemp(prefix="dapper-mint-"))
        print(f"pulling JSON sidecars from {target} ...")
        target = _pull_s3(target, dest)

    target = Path(target)
    if target.is_file():
        return [target]
    found = sorted(target.rglob("geneset.provenance.json"))
    return found


def _merge(into: dict[str, list], doc: dict[str, Any]) -> None:
    for group in DOC_GROUPS:
        for node in doc.get(group) or []:
            into.setdefault(group, []).append(node)
    for key, value in doc.items():
        if key not in DOC_GROUPS and isinstance(value, list):
            into.setdefault(key, []).extend(value)


def _dedupe(collection: dict[str, list]) -> int:
    """Collapse nodes that share an id. Content addressing makes this exact."""
    removed = 0
    for group in list(collection):
        seen, keep = set(), []
        for node in collection[group]:
            nid = node.get("id") if isinstance(node, dict) else None
            if nid is not None and nid in seen:
                removed += 1
                continue
            if nid is not None:
                seen.add(nid)
            keep.append(node)
        collection[group] = keep
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mint DAPPER content identifiers for a collection of gene sets.",
        epilog="Example: uv run schema/identity/mint.py runs/hubmap -o hubmap.collection.yaml",
    )
    ap.add_argument("input", help="folder, s3:// prefix, geneset.provenance.json, or a .yaml collection")
    ap.add_argument("-o", "--out", type=Path, default=Path("collection.yaml"))
    ap.add_argument("--schema", type=Path, default=SCHEMA)
    args = ap.parse_args()

    sv = load_schema(args.schema)
    collection: dict[str, list] = {}

    if str(args.input).endswith((".yaml", ".yml")):
        # re-mint an existing collection
        collection = yaml.safe_load(Path(args.input).read_text()) or {}
        print(f"re-minting {args.input}")
    else:
        from geneset_to_dapper import convert_graph  # noqa: PLC0415
        import json

        sources = _collect_inputs(args.input)
        if not sources:
            print(f"No geneset.provenance.json found under {args.input}", file=sys.stderr)
            print("Point this at the folder your gene-set runs were written to.", file=sys.stderr)
            return 1
        print(f"found {len(sources)} gene-set provenance file(s)")
        for path in sources:
            payload = json.loads(path.read_text())
            meta_path = path.parent / "geneset.meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            for _, graph in payload.items():
                if isinstance(graph, dict) and "nodes" in graph:
                    _merge(collection, convert_graph(graph, meta))

    handwritten = [
        n["id"] for g in DOC_GROUPS for n in (collection.get(g) or [])
        if isinstance(n, dict) and isinstance(n.get("id"), str)
        and not n["id"].startswith("dapper:")
    ]
    total = sum(len(collection.get(g) or []) for g in DOC_GROUPS)

    # The converter always emits known group names, but the .yaml re-mint path
    # takes whatever the user hands it. A group name this map does not recognise
    # is skipped silently, so its nodes would land in the output unminted.
    unknown = unknown_node_groups(collection)
    if unknown:
        print(f"unrecognised node group(s) in {args.input}:", file=sys.stderr)
        for key, count in sorted(unknown.items()):
            print(f"  {key}: {count} node(s) would never be minted", file=sys.stderr)
        print("  Node lists must use a known group key (persons:, gene_sets:, ...).",
              file=sys.stderr)
        print("  Nothing written.", file=sys.stderr)
        return 1

    assign_ids(collection, sv)
    collapsed = _dedupe(collection)

    header = (
        "# DAPPER gene-set collection — identifiers minted by schema/identity/mint.py\n"
        "#\n"
        "# Every `id` here is COMPUTED from the content beneath it, not written by\n"
        "# hand. Re-running mint.py on unchanged data reproduces this file exactly.\n"
        "# If an id changes, the content changed.\n"
        "#\n"
        f"#   nodes: {total - collapsed}\n"
        f"#   duplicates collapsed: {collapsed}\n"
        "#\n"
        "# Regenerate:  uv run schema/identity/mint.py <input> -o " + args.out.name + "\n"
        "# Verify:      uv run schema/identity/dapper_identity.py verify " + args.out.name + "\n\n"
    )
    args.out.write_text(header + yaml.safe_dump(collection, sort_keys=False, width=100))

    print(f"minted {total} id(s)")
    if handwritten:
        print(f"  replaced {len(handwritten)} hand-written id(s) — ids are computed, not authored")
    if collapsed:
        print(f"  collapsed {collapsed} duplicate node(s) that shared content")
    print(f"wrote {args.out}")
    print(f"\ncheck it:  uv run schema/identity/dapper_identity.py verify {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
