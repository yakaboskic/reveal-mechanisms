#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""Convert a DAPPER document written for one release to the next, and re-mint it.

    uv run converter/migrate.py doc.yaml --from 0.2.1-a1 -o doc.0.3.yaml --id-map ids.json

Every DAPPER release that changes a shape ships a converter step here, from the
previous release to it (see docs/guides/releasing.md). Steps chain, so a
document can move several releases at once. A step either converts faithfully
or refuses: when content cannot be carried into the new shape without inventing
something, the run stops, lists every problem and writes nothing. Content the
new release has no place for is listed under `notes`, never dropped silently.

After conversion the document is re-minted with this checkout's identity module
and verified. `--id-map` writes {old id: new id} for every record, so a store
can keep the old id resolvable, marked as deprecated by the new one.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
for sub in ("identity", "tools"):
    sys.path.insert(0, str(ROOT / sub))


class Refusal(Exception):
    """The document cannot be converted without inventing content."""

    def __init__(self, problems: list[str]):
        super().__init__(f"{len(problems)} problem(s)")
        self.problems = problems


def records(doc: dict):
    """Every node record (a dict with an id) in every node group."""
    for group, value in doc.items():
        if isinstance(value, list) and not group.endswith("_edges"):
            for r in value:
                if isinstance(r, dict) and "id" in r:
                    yield group, r


def rewrite(value, mapping: dict[str, str]):
    """Replace whole-string references, recursively."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [rewrite(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: rewrite(v, mapping) for k, v in value.items()}
    return value


def has_cycle(edges: list[tuple[str, str]]) -> bool:
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    state: dict[str, int] = {}

    def visit(u):
        state[u] = 1
        for v in graph.get(u, ()):
            if state.get(v) == 1 or (not state.get(v) and visit(v)):
                return True
        state[u] = 2
        return False
    return any(not state.get(u) and visit(u) for u in list(graph))


# ---------------------------------------------------------------------------
# 0.2.1-a1 -> 0.3.0-a1 (docs/releases/0.3.0-a1.md, "Migration from 0.2.0")
# ---------------------------------------------------------------------------

# Placeholder ids for records a step creates; minting replaces them. They embed no
# other id, since identity finds references by substring.
NEW = "urn:dapper-migrate:"
DISMECH_FIELDS = ("dismech_class", "dismech_entry_key", "dismech_disease_term", "dismech_source_url")
CONTEXT_FIELDS = (("ancestry", "AncestryContext"), ("trait", "TraitContext"))


def to_0_3_0(doc: dict, dismech_pages: dict[str, str] | None = None) -> list[str]:
    problems, notes = [], []
    renames: dict[str, str] = {}

    # CausalSteps become CausalLinks between CausalVariables, placed in their models.
    steps = {s["id"]: s for s in doc.pop("causal_steps", None) or [] if isinstance(s, dict)}
    for key in [k for k in doc if k in ("has_causal_step_edges",)]:
        doc.pop(key)
    variables: dict[str, dict] = {}

    def variable(entity: str) -> str:
        if entity not in variables:
            variables[entity] = {"id": f"{NEW}variable-{len(variables) + 1}", "entity": entity,
                                 "description": f"{entity}; converted from DAPPER 0.2, which did not record "
                                                "which aspect of it varies"}
        return variables[entity]["id"]

    unnamed: list[dict] = []

    def unnamed_variable(sid: str, step: dict, role: str) -> str:
        """0.2 let a step leave its subject or object unnamed; 0.3 links variables. The missing end becomes a
        CausalVariable of its own, with no entity, described from the step: nothing is merged or invented."""
        what = "What this step acts on" if role == "effect" else "What drives this step"
        known = step.get("step_subject") if role == "effect" else step.get("step_object")
        where = ", ".join(x for x in (f"step {step['step_index']}" if "step_index" in step else "",
                                      f"{'from' if role == 'effect' else 'to'} {known}" if known else "") if x)
        text = (step.get("description") or "").strip()
        unnamed.append({"id": f"{NEW}unnamed-{len(unnamed) + 1}",
                        "description": f"{what}, which the DAPPER 0.2 step did not name"
                                       + (f" ({where})" if where else "") + (f": {text}" if text else "")})
        notes.append(f"{sid}: step_{'object' if role == 'effect' else 'subject'} was not named; the link's "
                     f"{role} is an unnamed CausalVariable")
        return unnamed[-1]["id"]

    links: dict[str, dict] = {}

    def link(step: dict, subject_default: str | None = None, object_default: str | None = None) -> dict:
        """The step's CausalLink. A missing subject or object takes the model's own endpoint when the step is the
        model's first or last (`subject_default`, `object_default`), and is otherwise an unnamed variable."""
        sid = step["id"]
        if sid in links:
            return links[sid]
        subject = step.get("step_subject") or subject_default
        obj = step.get("step_object") or object_default
        if not step.get("step_subject") and subject_default:
            notes.append(f"{sid}: step_subject was not named; the model's subject_entity {subject_default} is its cause")
        if not step.get("step_object") and object_default:
            notes.append(f"{sid}: step_object was not named; the model's object_entity {object_default} is its effect")
        cause = variable(subject) if subject else unnamed_variable(sid, step, "cause")
        effect = variable(obj) if obj else unnamed_variable(sid, step, "effect")
        record = {"id": f"{NEW}link-{len(links) + 1}",
                  "statement": step.get("description") or (f"{subject} is causally upstream of {obj}." if subject and obj
                                                           else "A causal step converted from DAPPER 0.2."),
                  "relation_kind": "CAUSATION", "cause": cause, "effect": effect}
        for f in DISMECH_FIELDS:  # carried to the link, then converted with every other record's
            if f in step:
                record[f] = step[f]
        renames[sid] = record["id"]
        links[sid] = record
        return record

    for model in doc.get("mechanistic_models") or []:
        mid = model.get("id")
        step_ids = model.pop("has_causal_step", None) or []
        subject, obj = model.pop("subject_entity", None), model.pop("object_entity", None)
        ordered = sorted((steps[s] for s in step_ids if s in steps), key=lambda s: s.get("step_index", 0))
        for s in step_ids:
            if s not in steps:
                problems.append(f"{mid}: has_causal_step names {s}, which is not in this document")
        if not ordered and subject and obj:
            ordered = [{"id": f"{mid}#endpoints", "step_subject": subject, "step_object": obj,
                        "description": f"{subject} is causally upstream of {obj}."}]
            notes.append(f"{mid}: no CausalSteps; its subject_entity -> object_entity became its one link")
        if not ordered:
            problems.append(f"{mid}: a MechanisticModel needs CausalSteps or subject_entity and object_entity")
            continue
        entries, ends = [], []
        for i, s in enumerate(ordered):
            l = link(s, subject if i == 0 else None, obj if i == len(ordered) - 1 else None)
            entry = {"link": l["id"]}
            if s.get("causal_link_type"):
                entry["link_type"] = s["causal_link_type"]
            if s.get("intermediate_mechanisms"):
                entry["intermediates"] = list(s["intermediate_mechanisms"])
            if s.get("via_mechanism"):
                entry["via_mechanism"] = list(s["via_mechanism"])
            entries.append(entry)
            ends.append((l["cause"], l["effect"]))
        model["links"] = entries
        model["variables"] = list(dict.fromkeys(v for pair in ends for v in pair))
        for end in (subject, obj):
            if end and variable(end) not in model["variables"]:
                notes.append(f"{mid}: endpoint {end} is in none of its steps and is not carried over")
        if has_cycle(ends):
            model["has_feedback"] = True
    for sid, s in steps.items():
        if sid not in links:
            link(s)
            notes.append(f"{sid}: a CausalStep in no model became a standalone CausalLink")
    if variables or unnamed:
        doc.setdefault("causal_variables", []).extend([*variables.values(), *unnamed])
    if links:
        doc.setdefault("causal_links", []).extend(links.values())

    # Accounts are framed by an ordered list of Questions, KnowledgeGaps and Hypotheses.
    hypotheses: dict[str, str] = {}
    for account in doc.get("scientific_accounts") or []:
        question, hypothesis = account.pop("question", None), account.pop("hypothesis", None)
        if "framing" in account:
            continue
        framing = [question] if question else []
        if hypothesis:
            if hypothesis not in hypotheses:
                hypotheses[hypothesis] = f"{NEW}hypothesis-{len(hypotheses) + 1}"
                doc.setdefault("hypotheses", []).append({"id": hypotheses[hypothesis], "proposition": hypothesis})
            framing.append(hypotheses[hypothesis])
        if not framing:
            problems.append(f"{account.get('id')}: an account needs a question or a hypothesis to be framed")
        account["framing"] = framing

    for group, r in list(records(doc)):
        # mechanistic_model accepted any ExplanatoryModel; it is now explanatory_model.
        if "mechanistic_model" in r:
            r["explanatory_model"] = r.pop("mechanistic_model")
        # ancestry and trait move into sorted, typed contexts.
        moved = [{"context_type": cls, field: r.pop(field)} for field, cls in CONTEXT_FIELDS if field in r]
        if moved:
            r["contexts"] = sorted((r.get("contexts") or []) + moved, key=lambda c: c["context_type"])
        # DISMECH pointers become CorrespondsToDismech edges, outside identity.
        found = {f: r.pop(f) for f in DISMECH_FIELDS if f in r}
        if found:
            page = found.get("dismech_source_url") or (dismech_pages or {}).get(found.get("dismech_disease_term", ""))
            if not page:
                problems.append(f"{r['id']}: DISMECH fields without dismech_source_url; the edge's object is "
                                "the disease's DISMECH page, which cannot be inferred (pass --dismech-pages "
                                f"with an entry for {found.get('dismech_disease_term') or 'its disease'})")
                continue
            edge = {"subject": r["id"], "predicate": "skos:closeMatch", "object": page}
            if found.get("dismech_class"):
                edge["dismech_class"] = found["dismech_class"]
            if found.get("dismech_entry_key"):
                edge["entry_key"] = found["dismech_entry_key"]
            doc.setdefault("corresponds_to_dismech_edges", []).append(edge)
            if found.get("dismech_disease_term"):
                notes.append(f"{r['id']}: dismech_disease_term {found['dismech_disease_term']} has no place on "
                             "CorrespondsToDismech; the DISMECH page names the disease")

    if problems:
        raise Refusal(problems)
    if renames:
        for key in list(doc):
            if key != "_illustrative":
                doc[key] = rewrite(doc[key], renames)
        if isinstance(doc.get("_illustrative"), list):
            doc["_illustrative"] = [renames.get(x, x) for x in doc["_illustrative"]]
    return notes


# Each step: from-version -> (to-version, function). The function edits the
# document in place, returns notes, and raises Refusal when it cannot convert.
STEPS = {"0.2.1-a1": ("0.3.0-a1", to_0_3_0)}


def current_version() -> str:
    text = (ROOT / "schema" / "dapper.yaml").read_text()
    return next(line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("version:"))


def migrate(doc: dict, from_version: str, to_version: str | None = None,
            dismech_pages: dict[str, str] | None = None) -> tuple[dict, dict, list[str]]:
    """Convert, re-mint and verify. Returns (document, {old id: new id}, notes)."""
    from dapper_identity import assign_ids, load_schema, verify
    from scientific_claims import check_scientific_content, index_document

    target = to_version or current_version()
    if target != current_version():
        raise Refusal([f"this checkout is DAPPER {current_version()}; it can only mint {target} documents "
                       f"from a checkout of {target}"])
    doc, notes, version = deepcopy(doc), [], from_version
    while version != target:
        if version not in STEPS:
            raise Refusal([f"no converter step from DAPPER {version}; a release must ship one (docs/guides/releasing.md)"])
        version, step = STEPS[version]
        notes += [f"{version}: {n}" for n in step(doc, dismech_pages)]
    original = {r["id"] for _, r in records(doc)}
    sv = load_schema()
    mapping = assign_ids(doc, sv)
    problems = verify(doc, sv) + [f"{w}: {p}" for w, p in check_scientific_content(index_document(doc))]
    if problems:
        raise Refusal(problems)
    ids = {old: mapping.get(old, old) for old in sorted(original) if not old.startswith(NEW)}
    return doc, ids, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("document", type=Path)
    parser.add_argument("--from", dest="from_version", required=True, help="the release the document was written for")
    parser.add_argument("--to", dest="to_version", help="target release (default: this checkout's)")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--id-map", type=Path, help="write {old id: new id} as JSON")
    parser.add_argument("--dismech-pages", type=Path,
                        help="JSON {disease CURIE: DISMECH page URL} for records whose DISMECH fields name a disease "
                             "term but no page")
    args = parser.parse_args()
    text = args.document.read_text()
    header = "".join(l for l in text.splitlines(True) if l.startswith("#"))
    try:
        pages = json.loads(args.dismech_pages.read_text()) if args.dismech_pages else None
        doc, ids, notes = migrate(yaml.safe_load(text), args.from_version, args.to_version, pages)
    except Refusal as refusal:
        print(f"refused: {args.document}", file=sys.stderr)
        for p in refusal.problems:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)
    args.output.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110))
    if args.id_map:
        args.id_map.write_text(json.dumps(ids, indent=2) + "\n")
    changed = sum(old != new for old, new in ids.items())
    print(f"{args.document} -> {args.output}: {len(ids)} records, {changed} new ids")
    for n in notes:
        print(f"  note: {n}")


if __name__ == "__main__":
    main()
