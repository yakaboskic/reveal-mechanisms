#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""CI gate for DAPPER-ID-1 identity invariants.

`linkml-validate` proves instances match the schema. It cannot prove that
identifiers are *computable*, *stable* or *unique*, and every check below exists
because its absence produced a real bug during implementation:

  1. total marking     — every slot is hashable XOR unhashable
  2. hierarchy         — every concrete Node descends from HashableNode
  3. non-empty digest  — no class hashes over nothing
                         (NanopubSignature was fully unhashable, so every
                          signature in the corpus collided on one digest)
  4. acyclic           — the hashable reference graph is a DAG
                         (nanopub back-references formed 3 cycles)
  5. ids match content — every example id equals its recomputed digest
  6. unique ids        — no two nodes in a document share an id
                         (two under-specified stubs earned the same address)
  7. no stale refs     — no example still uses a legacy, non-content-addressed
                         identifier scheme (caught example_nanopub pointing at
                          nih:np/... ids that name nothing; 26 such refs existed)
  8. external ids kept  — minting never overwrites an ORCID, ROR or BCO id
                         (broadinstitute/dapper#1: nodes whose id WAS their
                          external identifier had both rewritten)
  9. no dotted terms    — no self-defined `dapper:` predicate has a local
                          name containing `.` (digest_of() partitions on the
                          first `.` to pull a minted id apart from its class
                          name; a term like `dapper:Foo.v2` would be
                          misparsed as an id reference and truncated)

Usage:
    uv run schema/identity/lint_identity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import re

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from dapper_identity import (  # noqa: E402
    DOC_GROUPS,
    assign_ids,
    SCHEMA_PATH,
    _iter_nodes,
    compute_id,
    hashable_slot_names,
    load_schema,
    unmarked_slots,
)

EXAMPLES = Path(__file__).parent.parent / "examples"
GRAPH_DOCS = [
    "example_scientific_account.yaml",
    "example_pigean_claims.yaml",
    "example_geneset_graph.yaml",
    "example_geneset_collection_rows.yaml",
    "example_geneset_hubmap.yaml",
    "example_claim_provenance_trace.yaml",
    "example_cell_graph.yaml",
    "example_bottom_line_result.yaml",
    "example_bottom_line_af_aa.yaml",
    "example_file_graph.yaml",
]


def main() -> int:
    sv = load_schema(SCHEMA_PATH)
    failures: list[str] = []

    concrete = [
        cn for cn in sv.all_classes()
        if not sv.get_class(cn).abstract and not sv.get_class(cn).mixin
    ]

    # Only Node descendants get identifiers. Edge classes carry
    # subject/predicate/object and are deliberately not content-addressed, so
    # marking their slots would be meaningless.
    node_desc = [cn for cn in concrete if "Node" in sv.class_ancestors(cn)]

    # --- 1. total marking --------------------------------------------------
    unmarked = {cn: unmarked_slots(sv, cn) for cn in node_desc}
    unmarked = {cn: s for cn, s in unmarked.items() if s}
    print(f"1. total marking      : {len(node_desc) - len(unmarked)}/{len(node_desc)} Node classes fully marked")
    for cn, slots in sorted(unmarked.items()):
        failures.append(f"{cn}: slots with neither hashable nor unhashable: {slots}")

    # --- 2. hierarchy ------------------------------------------------------
    missing = [cn for cn in node_desc if "HashableNode" not in sv.class_ancestors(cn)]
    print(f"2. hierarchy          : {len(node_desc) - len(missing)}/{len(node_desc)} Node classes are HashableNode")
    for cn in missing:
        failures.append(f"{cn} descends from Node but not HashableNode — it would get no identifier")

    # --- 3. non-empty digest input ----------------------------------------
    empty = [cn for cn in node_desc if not hashable_slot_names(sv, cn)]
    print(f"3. non-empty digest   : {len(node_desc) - len(empty)}/{len(node_desc)} classes hash over >=1 slot")
    for cn in empty:
        failures.append(
            f"{cn} has NO hashable slots — every instance would collide on one constant digest"
        )

    # --- 4/5/6. per-document checks ---------------------------------------
    for name in GRAPH_DOCS:
        path = EXAMPLES / name
        if not path.exists():
            continue
        doc = yaml.safe_load(path.read_text())
        nodes = {n["id"]: (c, n) for _, c, n in _iter_nodes(doc)}

        # 4. acyclic over hashable references
        deps = {}
        for nid, (cn, node) in nodes.items():
            refs = set()
            for slot in hashable_slot_names(sv, cn):
                val = node.get(slot)
                for item in (val if isinstance(val, list) else [val]):
                    if isinstance(item, str) and item in nodes and item != nid:
                        refs.add(item)
            deps[nid] = refs
        state, cycles = {}, []

        def visit(nid, trail):
            if state.get(nid) == 2:
                return
            if state.get(nid) == 1:
                cycles.append(" -> ".join(trail[trail.index(nid):] + (nid,)))
                return
            state[nid] = 1
            for d in sorted(deps[nid]):
                visit(d, trail + (nid,))
            state[nid] = 2

        for nid in sorted(nodes):
            visit(nid, ())
        for c in cycles:
            failures.append(f"{name}: cycle in hashable references: {c}")

        # 5. ids match content — only meaningful once ids are minted
        minted = [i for i in nodes if i.startswith("dapper:")]
        mismatches = []
        if minted:
            for nid, (cn, node) in nodes.items():
                expected = compute_id({k: v for k, v in node.items() if k != "id"}, cn, sv, self_id=nid)
                if nid != expected:
                    mismatches.append(f"{name}: {cn} id {nid} != content digest {expected}")
        failures.extend(mismatches)

        # 6. unique ids
        seen, dupes = set(), set()
        for _, _, node in _iter_nodes(doc):
            if node["id"] in seen:
                dupes.add(node["id"])
            seen.add(node["id"])
        for d in sorted(dupes):
            failures.append(f"{name}: duplicate id {d} — two nodes share one address")

        status = "minted" if minted else "not yet minted"
        print(
            f"   {name}: {len(nodes)} nodes, {len(cycles)} cycle(s), "
            f"{len(mismatches)} id mismatch(es), {len(dupes)} duplicate(s) [{status}]"
        )

    # --- 7. no legacy (non-content-addressed) identifier schemes in examples ---
    stale_pat = re.compile(
        r"^(nih:(np|hypothesis|causalstep|mechanism|evidence|activity|geneset|"
        r"citation|workspace|award)/|file:(?!//)|analysis:|geneset:)"
    )
    stale_total = 0
    for path in sorted(EXAMPLES.glob("example_*.yaml")):
        doc = yaml.safe_load(path.read_text())
        hits = []

        def scan(obj, where):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if not where and k == "prefixes":
                        continue  # Namespace declarations are not record references.
                    scan(v, f"{where}.{k}" if where else k)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    scan(v, f"{where}[{i}]")
            elif isinstance(obj, str) and stale_pat.match(obj):
                hits.append(f"{where} -> {obj}")

        scan(doc, "")
        stale_total += len(hits)
        for h in hits:
            failures.append(f"{path.name}: stale legacy identifier at {h}")
    print(f"7. no stale refs      : {stale_total} legacy identifier(s) remaining")

    # --- 8. minting must not clobber external identifiers -------------------
    # Reconstructs the exact shape from broadinstitute/dapper#1: a node whose id
    # IS its own external identifier. A substring rewriter turns the ORCID into a
    # digest; a slot-aware one leaves it alone. Synthetic rather than read from
    # an example, so the guard survives the examples being rewritten.
    probe = {
        "persons": [{"id": "orcid:0000-0002-1825-0097", "name": "Probe Person",
                     "orcid": "orcid:0000-0002-1825-0097"}],
        "organizations": [{"id": "ror:0155zta11", "name": "Probe Org",
                           "ror": "ror:0155zta11"}],
        "activities": [{"id": "analysis:probe", "name": "probe run",
                        "command": "tool --person orcid:0000-0002-1825-0097"}],
    }
    assign_ids(probe, sv)
    person, org, activity = (probe["persons"][0], probe["organizations"][0],
                             probe["activities"][0])
    kept = []
    if person["orcid"] != "orcid:0000-0002-1825-0097":
        failures.append(f"minting overwrote Person.orcid with {person['orcid']}")
    else:
        kept.append("orcid")
    if org["ror"] != "ror:0155zta11":
        failures.append(f"minting overwrote Organization.ror with {org['ror']}")
    else:
        kept.append("ror")
    # the other half of the rule: an id MENTIONED in prose must still be rewritten
    if "dapper:Person." not in activity["command"]:
        failures.append("an id mentioned in Activity.command was not rewritten")
    else:
        kept.append("prose rewritten")
    print(f"8. external ids kept  : {', '.join(kept)}")

    # --- 9. self-defined dapper: predicates must not contain a dot ---------
    # digest_of() (dapper_identity.py) treats any `dapper:X.Y` string as a
    # minted id and strips it to Y. A schema-defined predicate default that
    # happens to contain a dot in its local name would be silently truncated
    # when it flows through a hashable slot value.
    dotted = []
    for cn in concrete:
        for slot in sv.class_induced_slots(cn):
            default = str(slot.ifabsent or "")
            m = re.match(r"string\(dapper:([^)]+)\)", default)
            if m and "." in m.group(1):
                dotted.append(f"{cn}.{slot.name} ifabsent default dapper:{m.group(1)}")
    print(f"9. no dotted terms    : {len(dotted)} self-defined predicate(s) with a dot in the local name")
    for d in dotted:
        failures.append(f"{d} — will be misparsed as a minted id by digest_of()")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("PASS — all identity invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
