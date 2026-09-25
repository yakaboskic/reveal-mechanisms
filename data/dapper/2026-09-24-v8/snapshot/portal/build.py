#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Build the DAPPER provenance portal — a single self-contained index.html.

Reads the schema and the example documents next door, and inlines them together
with Cytoscape.js + ELK from `lib/`.

Everything is embedded — data AND libraries — so the result is ONE file that
works off disk, behind any static host, on a plane, and inside a hospital
network that blocks CDNs. A page on file:// also cannot fetch its siblings, so
inlining is what makes `open index.html` work at all.

Usage:
    uv run portal/build.py
    uv run portal/build.py --check   # fail if index.html is stale

Re-run it after editing any example or the schema.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
# `schema/` is the model boundary for this repository (see README), so the
# schema and its examples live under it while the portal sits beside it.
SCHEMA_DIR = REPO_ROOT / "schema"
EXAMPLES = SCHEMA_DIR / "examples"
SCHEMA = SCHEMA_DIR / "dapper.yaml"
LIB = HERE / "lib"
OUT = HERE / "index.html"

# Load order matters: elkjs defines the global cytoscape-elk binds to.
LIB_FILES = ["cytoscape.min.js", "elk.bundled.js", "cytoscape-elk.js", "js-yaml.min.js"]

# Node-list key -> LinkML class. Parsed out of the identity module rather than
# duplicated here: the portal had drifted to 14 groups while the minter had 27,
# so an uploaded document rendered 1 node instead of 18. Read as source rather
# than imported so the portal keeps its light dependency set (no rdflib).
def _node_groups() -> dict[str, str]:
    src = (Path(__file__).parent.parent / "schema" / "identity" / "dapper_identity.py").read_text()
    block = src[src.index("DOC_GROUPS = {"):]
    block = block[: block.index("}") + 1]
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))


NODE_GROUPS = _node_groups()

# Edge-list key -> (LinkML class, layout/trace direction).
#
# `flow` orders inputs before outputs. It never changes the stored statement:
# subject/predicate/object are retained separately from layout source/target.
EDGE_GROUPS = {
    "used_edges": ("Used", "reverse"),
    "was_generated_by_edges": ("WasGeneratedBy", "reverse"),
    "asserted_in_edges": ("AssertedIn", "forward"),
    # has_assertion_edges / has_provenance_edges are deliberately NOT drawn as
    # edges — they are containment, rendered by nesting the graphs inside the
    # nanopublication instead.
    # workspace -> activity in flow terms: the plan comes before the run
    "has_agentic_workspace_edges": ("HasAgenticWorkspace", "reverse"),
    # dataset -> its retrievable payload; predicate already points the way the story reads
    "has_drs_object_edges": ("HasDrsObject", "forward"),
    "has_file_edges": ("HasFile", "forward"),
    "drs_representation_edges": ("DrsRepresentation", "forward"),
}

# Several links live as INLINE FIELDS rather than reified edges. They carry the
# trace and the nanopub's internal structure; without them the graph-part nodes
# float unconnected. "in" = the field's target flows into this node.
INLINE_LINKS = {
    "has_file": ("dapper:hasFile", "out"),
    "has_gmt_file": ("dapper:hasGmtFile", "in"),
    "in_gmt_file": ("dapper:inGmtFile", "in"),
    "in_gene_set_collection": ("dapper:inGeneSetCollection", "out"),
    "members": ("prov:hadMember", "in"),
    "was_generated_by": ("prov:wasGeneratedBy", "in"),
    "was_derived_from": ("prov:wasDerivedFrom", "in"),
    "proposition": ("dapper:proposition", "in"),
    "has_score": ("dapper:has_score", "in"),
    "component_claims": ("dapper:component_claims", "in"),
    "conclusion_claims": ("dapper:conclusion_claims", "in"),
    "hypothesis": ("dapper:hypothesis", "in"),
    "question": ("dapper:question", "in"),
    "about_entities": ("dapper:about_entities", "in"),
    "mechanistic_model": ("dapper:mechanistic_model", "in"),
    "has_causal_step": ("dapper:has_causal_step", "in"),
    "via_mechanism": ("dapper:via_mechanism", "in"),
    "has_evidence": ("dapper:has_evidence", "in"),
    "source_claims": ("dapper:source_claims", "in"),
    "target_proposition": ("dapper:target_proposition", "in"),
    "from_nanopub": ("dapper:from_nanopub", "in"),
    "scientific_account": ("dapper:scientific_account", "in"),
    "subject_entity": ("dapper:subject_entity", "in"),
    "object_entity": ("dapper:object_entity", "out"),
    "generated_by_activity": ("prov:wasGeneratedBy", "in"),
    "asserts": ("hycl:claims", "in"),
    "has_agentic_workspace": ("dapper:has_agentic_workspace", "in"),
    "provenance_of": ("dapper:provenance_of", "out"),
    # CellState -> GeneProgram: the program is upstream of the state it
    # constitutes, same "in" direction as generated_by_activity.
    "has_program": ("dapper:hasProgram", "in"),
    "drs_representation": ("dapper:drsRepresentation", "out"),
}

# Visual family: the narrative arc data -> process -> claim -> publication.
FAMILY = {
    "File": "data",
    "C2M2File": "data",
    "DrsObject": "data",
    "GeneSet": "data",
    "GeneSetCollection": "data",
    "GeneProgram": "data",
    "CellState": "data",
    "Dataset": "data",
    "Activity": "process",
    "MechanisticModel": "claim",
    "Claim": "claim",
    "ScientificAccount": "claim",
    "Question": "claim",
    "KnowledgeGap": "claim",
    "Paragraph": "publication",
    "EvidenceItem": "claim",
    "Mechanism": "claim",
    "Proposition": "claim",
    "ClaimScore": "data",
    "CausalStep": "claim",
    "Nanopublication": "publication",
    "NanopubAssertion": "part",
    "NanopubProvenance": "part",
    "NanopubPublicationInfo": "part",
    "NanopubSignature": "part",
    "AgenticWorkspace": "workspace",
}

# Which nodes are explicitly marked illustrative for exploring concepts.
# Read from the example's own `_illustrative:` list rather than inferred from the
# id, because ids are now content digests (`dapper:Class.digest`) and carry no
# provenance hint. Honesty about this distinction is the point of the examples,
# so it is declared explicitly rather than guessed.


GRAPH_DOCS = [
    {
        "file": "example_scientific_account.yaml",
        "key": "scientific_account",
        "title": "Question, hypothesis, and claims",
        "blurb": (
            "A fictional study of Gene X and insulin secretion. One Proposition plays "
            "the hypothesis role; separate Claims report a result and assess its biological "
            "meaning. Inspect the evidence use, assumptions, and saved results paragraph."
        ),
        "start": "dapper:ScientificAccount.4EdFhOCSYboZhnZiHbT3qnyYeqyyvbIs",
    },
    {
        "file": "example_pigean_claims.yaml",
        "key": "pigean_claims",
        "title": "PIGEAN: scientific account",
        "blurb": (
            "Entirely illustrative: one scientific account organizes three assessments as an "
            "annotation-based explanation. P1/P2/P3 are hypothetical probabilities, "
            "not real PIGEAN/EAGGL outputs. Follow both the GWAS and gene-set "
            "generation branches to their input files."
        ),
        "start": "dapper:ScientificAccount.Ddyw_8CFbVpwdBfXogo2GlOHDJzZpdUw",
    },
    {
        "file": "example_file_graph.yaml",
        "key": "files",
        "title": "Intermediate file and DRS access",
        "blurb": (
            "An illustrative File produced by one activity and consumed by another. "
            "An optional DrsObject exposes the same bytes, without changing the file's identity."
        ),
        "start": "dapper:File.aYgsG7KuOqG7gJ_yC9i9zCmvjMsTCMQ3",
    },
    {
        "file": "example_claim_provenance_trace.yaml",
        "key": "trace",
        "title": "Claim to C2M2 dataset",
        "blurb": (
            "A scientific claim followed back to the raw files it rests on. The "
            "nanopublication's provenance graph and the dig.geneset C2M2 graph are the "
            "same graph, so the walk needs no special bridge."
        ),
        "start": "dapper:ScientificAccount.nq-PjWsCbchEhDTX3ni0VqQ08mS8IRn7",
    },
    {
        "file": "example_geneset_graph.yaml",
        "key": "geneset",
        "title": "Gene-set provenance",
        "blurb": (
            "The two-activity DAG behind HuBMAP gene-set library 402cf4a1, transcribed from "
            "geneset.provenance.json. Every file, activity and edge is real; the one dashed "
            "node is an illustrative agentic workspace that could re-run both steps."
        ),
        "start": "dapper:GeneSetCollection.DLyinzh-eee_daEShnIt7vnUbDoGmK91",
    },
    {
        "file": "example_geneset_collection_rows.yaml",
        "key": "gmt_rows",
        "title": "GMT collection and individual sets",
        "blurb": "An illustrative two-row GMT: two sets of two genes share one gene, giving three distinct genes across the collection.",
        "start": "dapper:GeneSetCollection.1FoLv1ndB0HjzITZhThnn3w0TfQgGCOb",
    },
    {
        "file": "example_cell_graph.yaml",
        "key": "cellstate",
        "title": "Gene program to cell state",
        "blurb": (
            "The single-cell block's two new node types: three real NMF "
            "gene-loading factors (GeneProgram, with member_weights) all "
            "feeding into one curated pancreatic ductal epithelial identity "
            "state (CellState) with the internal marker-curation schema "
            "fields. Two of the three programs also carry an optional "
            "`quality_score` assessment (GeneProgramQualityScore); the third omits "
            "it. The pairing is illustrative, exercising multi-program "
            "has_program rather than asserting a biological claim. Also shows "
            "the two provenance branches: an illustrative NMF Activity wired "
            "to the GenePrograms via Used/WasGeneratedBy edges (the same shape "
            "as the gene-set DAG), and the curated CellState attributed "
            "directly to a curator with no generating Activity at all."
        ),
        "start": "dapper:CellState.IHQJzYsI_9y0ko81oIgyNSgdi15cOn85",
    },
    {
        "file": "example_bottom_line_result.yaml",
        "key": "bottom_line",
        "title": "GWAS bottom line",
        "blurb": (
            "An illustrative variant-level GWAS bottom line: one Dataset generated by a "
            "LoadTransEthnicStage-style Activity, with its multi-file Spark output "
            "represented as a single manifest-backed DrsObject rather than as individual "
            "variant-level nodes."
        ),
        "start": "dapper:Dataset.3_QblmXDJVn8WX9JZWuf7tVXDM8201F8",
    },
    {
        "file": "example_bottom_line_af_aa.yaml",
        "key": "bottom_line_af_aa",
        "title": "AF / AA bottom-line mapping",
        "blurb": (
            "A reconstruction of the supplied AF / AA provenance payload: "
            "S3 prefix collections are Datasets, stages are Activities, and "
            "the published .tsv.gz is a File distribution. QC and staging "
            "outputs inferred by the source are labeled in their descriptions. "
            "This is a schema-valid mapping, not verified execution provenance."
        ),
        "start": "dapper:Dataset.CBE87jpWVPa6Xtocr66ztw75eR0CUw53",
    },
]

def is_illustrative(node_id: str, illustrative: set[str]) -> bool:
    return node_id in illustrative


def read_schema() -> dict:
    """Read local LinkML modules without adding a LinkML runtime dependency."""
    # Local modules contribute classes and slots to the root schema. Resolve
    # them without pulling LinkML itself into this lightweight portal builder.
    schema = {section: {} for section in ("classes", "slots", "enums", "types", "prefixes")}
    visited = set()

    def read_module(path: Path) -> None:
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        module = yaml.safe_load(path.read_text())
        for imported in module.get("imports") or []:
            if ":" not in imported:
                read_module(path.parent / (imported if imported.endswith(".yaml") else imported + ".yaml"))
        for section in schema:
            schema[section].update(module.get(section) or {})

    read_module(SCHEMA)
    return schema


def doc_url(section: str, name: str) -> str:
    return f"model/reference/{section}/{name}/"


def mappings(body: dict) -> list[dict]:
    """Keep mapping strength: a close match is not an equivalence assertion."""
    return [{"relation": relation, "target": target}
            for relation in ("exact", "close", "broad", "narrow", "related")
            for target in body.get(f"{relation}_mappings") or []]


def vocabulary_links(schema: dict) -> dict:
    """Known native terms and aliases only; computed record IDs have no page."""
    links = {}
    for section in ("classes", "slots", "enums", "types"):
        definitions = dict(schema[section])
        if section == "slots":
            for body in schema["classes"].values():
                definitions.update(body.get("attributes") or {})
        for name, body in definitions.items():
            url = doc_url(section, name)
            links[name] = url
            uri = (body or {}).get({"classes": "class_uri", "slots": "slot_uri",
                                   "enums": "enum_uri", "types": "uri"}[section])
            if uri and uri.startswith("dapper:"):
                links[uri.removeprefix("dapper:")] = url
    for name, body in schema["classes"].items():
        predicate = (body.get("slot_usage") or {}).get("predicate") or {}
        default = predicate.get("ifabsent", "")
        if default.startswith("string(dapper:") and default.endswith(")"):
            links.setdefault(default[len("string(dapper:"):-1], doc_url("classes", name))
    return links


def load_schema(schema: dict | None = None) -> dict:
    """Embed model links, enum meanings and inherited authoritative fields."""
    schema = read_schema() if schema is None else schema
    definitions = schema.get("classes") or {}

    def inherited_attributes(class_name: str) -> dict:
        body = definitions.get(class_name) or {}
        attrs = {}
        for parent in ([body["is_a"]] if body.get("is_a") else []) + (body.get("mixins") or []):
            attrs.update(inherited_attributes(parent))
        for slot_name in body.get("slots") or []:
            attrs[slot_name] = (schema.get("slots") or {}).get(slot_name) or {}
        attrs.update(body.get("attributes") or {})
        for slot_name, override in (body.get("slot_usage") or {}).items():
            attrs[slot_name] = {**attrs.get(slot_name, {}), **(override or {})}
        return attrs

    classes: dict[str, dict] = {}
    for name, body in (schema.get("classes") or {}).items():
        body = body or {}
        attrs = {}
        for slot_name, slot in inherited_attributes(name).items():
            slot = slot or {}
            ann = slot.get("annotations") or {}
            attrs[slot_name] = {
                "url": doc_url("slots", slot_name),
                "mappings": mappings(slot),
                "description": (slot.get("description") or "").strip(),
                # A mirror must never rewrite these (see the mirroring invariant in schema/dapper.yaml).
                "authoritative": ann.get("dapper:mirror_mutable") is False,
            }
            enum_name = slot.get("range")
            if enum_name in schema["enums"]:
                enum = schema["enums"][enum_name] or {}
                attrs[slot_name]["enum"] = {
                    "name": enum_name, "url": doc_url("enums", enum_name),
                    "values": {str(value): {**(body or {}), "mappings": mappings(body or {})}
                               for value, body in (enum.get("permissible_values") or {}).items()},
                }
        ann = body.get("annotations") or {}
        classes[name] = {
            "url": doc_url("classes", name),
            "uri": body.get("class_uri") or f"dapper:{name}",
            "description": (body.get("description") or "").strip(),
            "npGraph": ann.get("dapper:np_graph") or "",
            "mappings": mappings(body),
            "attributes": attrs,
            "family": FAMILY.get(name, "part"),
        }
    return classes


def edge_predicates(schema: dict) -> dict:
    """Resolve omitted predicates from the Edge class's schema default."""
    result = {}
    for cls, _ in EDGE_GROUPS.values():
        predicate = (schema["classes"][cls].get("slot_usage") or {}).get("predicate") or {}
        default = predicate.get("ifabsent", "")
        if default.startswith("string(") and default.endswith(")"):
            result[cls] = default[len("string("):-1]
    return result


def build_graph(spec: dict, defaults: dict | None = None) -> dict:
    defaults = edge_predicates(read_schema()) if defaults is None else defaults
    raw = yaml.safe_load((EXAMPLES / spec["file"]).read_text())
    illustrative = set(raw.get("_illustrative") or [])
    nodes, edges = [], []
    for key, cls in NODE_GROUPS.items():
        for node in raw.get(key) or []:
            nodes.append({
                "id": node["id"],
                "cls": cls,
                "family": FAMILY.get(cls, "part"),
                "illustrative": is_illustrative(node["id"], illustrative),
                "label": node.get("filename") or node.get("name") or node["id"].split(":")[-1],
                "fields": {k: v for k, v in node.items() if k != "id"},
            })
    for key, (cls, flow) in EDGE_GROUPS.items():
        for edge in raw.get(key) or []:
            src, dst = edge["subject"], edge["object"]
            if flow == "reverse":
                src, dst = dst, src
            edges.append({"source": src, "target": dst, "cls": cls,
                          "subject": edge["subject"], "object": edge["object"],
                          "predicate": edge.get("predicate") or defaults.get(cls, "")})

    # A nanopublication IS its four named graphs. Nest them inside it so the
    # document boundary is visible: what is inside the box belongs to the
    # nanopub, and every edge crossing the boundary is a REFERENCE to something
    # that lives elsewhere. That distinction is the whole point of the picture.
    by_id = {n["id"]: n for n in nodes}
    for node in nodes:
        if node["cls"] != "Nanopublication":
            continue
        for field in ("has_assertion", "has_provenance",
                      "has_publication_info", "has_signature_element"):
            child = node["fields"].get(field)
            if isinstance(child, str) and child in by_id:
                by_id[child]["parent"] = node["id"]

    ids = {n["id"] for n in nodes}
    seen = {(e["subject"], e["predicate"], e["object"]) for e in edges}
    for node in nodes:
        for field, (predicate, direction) in INLINE_LINKS.items():
            value = node["fields"].get(field)
            # some inline links are multivalued (has_agentic_workspace), some
            # are single (asserts) — normalise before walking
            targets = value if isinstance(value, list) else [value]
            for target in targets:
                if not isinstance(target, str) or target not in ids:
                    continue
                src, dst = (target, node["id"]) if direction == "in" else (node["id"], target)
                statement = (node["id"], predicate, target)
                if src == dst or statement in seen:
                    continue
                seen.add(statement)
                edges.append({"source": src, "target": dst, "cls": "(inline)",
                              "subject": node["id"], "object": target,
                              "predicate": predicate})

    # drop edges pointing at nodes this document does not define
    edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    return {**{k: spec[k] for k in ("key", "title", "blurb", "start")},
            "source": spec["file"], "nodes": nodes, "edges": edges}


def read_lib() -> str:
    chunks = []
    for name in LIB_FILES:
        path = LIB / name
        if not path.exists():
            raise SystemExit(
                f"missing {path}\n"
                f"Fetch the bundled libraries, e.g.:\n"
                f"  curl -sL -o {path} https://unpkg.com/{name.split('.')[0]}/dist/{name}")
        js = path.read_text(encoding="utf-8", errors="replace")
        # a literal </script> inside a bundle would close the tag early
        js = js.replace("</script", r"<\/script")
        chunks.append(f"/* ---- bundled: {name} ---- */\n{js}")
    return "\n".join(chunks)


def render(payload: dict, lib_js: str) -> str:
    return (TEMPLATE
            .replace("/*__VENDOR__*/", lib_js)
            .replace("/*__DATA__*/", json.dumps(payload, indent=None).replace("<", r"\u003c")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if index.html is out of date")
    args = ap.parse_args()

    schema = read_schema()
    defaults = edge_predicates(schema)
    payload = {"schema": load_schema(schema),
               "graphs": [build_graph(g, defaults) for g in GRAPH_DOCS],
               # Emitted so the in-browser uploader can build a graph from an
               # arbitrary DAPPER document using the same rules as this script,
               # rather than a second hand-maintained copy of them.
               "config": {"nodeGroups": NODE_GROUPS,
                          "prefixes": schema["prefixes"],
                          "vocabulary": vocabulary_links(schema),
                          "edgeGroups": {k: list(v) for k, v in EDGE_GROUPS.items()},
                          "edgePredicates": defaults,
                          "inlineLinks": {k: list(v) for k, v in INLINE_LINKS.items()},
                          "family": FAMILY}}
    html = render(payload, read_lib())

    if args.check:
        if (OUT.read_text() if OUT.exists() else "") != html:
            print("index.html is stale — re-run: uv run portal/build.py")
            return 1
        print("index.html is up to date")
        return 0

    OUT.write_text(html)
    n_nodes = sum(len(g["nodes"]) for g in payload["graphs"])
    n_edges = sum(len(g["edges"]) for g in payload["graphs"])
    print(f"wrote {OUT}")
    print(f"  {len(payload['graphs'])} graphs · {n_nodes} nodes · {n_edges} edges · "
          f"{len(payload['schema'])} schema classes")
    print(f"  {OUT.stat().st_size / 1024:.0f} KB, fully self-contained")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DAPPER · provenance inspector</title>
<style>
:root {
  --paper: #F7F8F6;
  --paper-sunk: #EEF0EC;
  --ink: #12161C;
  --ink-soft: #4A5259;
  --muted: #79817D;
  --rule: #D2D7CF;
  --rule-strong: #B4BBB2;

  /* node families: the arc data -> process -> claim -> publication */
  --data: #48566B;
  --process: #1F5C4D;
  --claim: #6B5FA0;
  --publication: #8A6A1F;
  --part: #7E8780;
  --workspace: #2A7A8C;

  --trace: #B4531F;
  --focus: #1F5C4D;
  --link-model: #6A42A4;
  --link-instance: #00645A;
  --link-external: #245EA2;
  /* text drawn ON a filled family colour — flips with the theme so it stays legible */
  --on-fill: #FFFFFF;

  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #12161C; --paper-sunk: #171C23; --ink: #E7EBE5; --ink-soft: #AAB2AC;
    --muted: #7E877F; --rule: #2A3138; --rule-strong: #3C444C;
    --data: #8FA3C0; --process: #64B39C; --claim: #A79BDA; --publication: #D0AA55;
    --part: #8C958D; --workspace: #6FBACB; --trace: #E68A4E; --focus: #64B39C;
    --on-fill: #12161C;
    --link-model: #C0A2F6; --link-instance: #69C9B6; --link-external: #8FBAF8;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: var(--sans); font-size: 14px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.eyebrow {
  font-family: var(--mono); font-size: 10px; letter-spacing: .14em;
  text-transform: uppercase; color: var(--muted);
}

.shell { display: grid; grid-template-rows: auto 1fr; height: 100vh; }
header {
  display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap;
  padding: 14px 20px; border-bottom: 1px solid var(--rule);
}
h1 { margin: 0; font-size: 15px; font-weight: 620; letter-spacing: -.01em; }
h1 span { color: var(--muted); font-weight: 400; }
.tagline { color: var(--ink-soft); font-size: 13px; margin: 0; flex: 1 1 320px; min-width: 0; }
.model-docs { color: var(--focus); font-size: 13px; text-underline-offset: 3px; }

.body { display: grid; grid-template-columns: 224px minmax(0,1fr) 340px; min-height: 0; }
@media (max-width: 1080px) {
  .body { grid-template-columns: 1fr; grid-template-rows: auto minmax(420px,1fr) auto; }
  .rail, .inspector { border: none; border-bottom: 1px solid var(--rule); }
}
.rail { border-right: 1px solid var(--rule); overflow-y: auto; padding: 16px 14px 28px; }
.inspector { border-left: 1px solid var(--rule); overflow-y: auto; padding: 16px 16px 40px; }
#cy { width: 100%; height: 100%; background: var(--paper-sunk); min-height: 0; }

.rail-group { margin-bottom: 22px; }
.rail-group > .eyebrow { display: block; margin-bottom: 8px; }
.pick {
  display: block; width: 100%; text-align: left; cursor: pointer;
  background: none; border: 1px solid transparent; border-radius: 3px;
  padding: 7px 9px; margin-bottom: 2px; color: var(--ink);
  font-family: inherit; font-size: 13px; line-height: 1.35;
}
.pick:hover { background: var(--paper-sunk); }
.pick[aria-current="true"] { background: var(--paper-sunk); border-color: var(--rule-strong); font-weight: 560; }
.pick { overflow: hidden; }
/* long example filenames were running past the rail edge */
.pick small {
  display: block; font-family: var(--mono); font-size: 10px; color: var(--muted);
  margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

#drop {
  border: 1.5px dashed var(--rule-strong); border-radius: 4px; padding: 14px 10px;
  text-align: center; cursor: pointer; color: var(--ink-soft); background: none;
  transition: border-color .15s, background .15s;
}
#drop:hover, #drop.over { border-color: var(--focus); background: var(--paper-sunk); color: var(--ink); }
#drop strong { display: block; font-size: 12.5px; font-weight: 600; }
#drop span { display: block; font-family: var(--mono); font-size: 10px; margin-top: 3px; }
.note.bad { border-left-color: var(--trace); color: var(--trace); }
.note.good { border-left-color: var(--focus); }

.legend-row { display: flex; align-items: center; gap: 9px; margin-bottom: 7px; font-size: 12px; color: var(--ink-soft); }
.legend-row svg { flex: none; }
.note { font-size: 12px; color: var(--ink-soft); border-left: 2px solid var(--rule-strong); padding-left: 10px; margin: 0; }

button.action {
  width: 100%; cursor: pointer; font-family: var(--mono); font-size: 11px;
  letter-spacing: .1em; text-transform: uppercase; padding: 9px 10px;
  background: var(--ink); color: var(--paper); border: none; border-radius: 3px;
}
button.action:hover { background: var(--focus); }
button.action.ghost { background: none; color: var(--ink-soft); border: 1px solid var(--rule-strong); }
button.action.ghost:hover { background: var(--paper-sunk); color: var(--ink); }
button.action + button.action { margin-top: 6px; }
.btn-row { display: flex; gap: 4px; }
/* flex-basis 0 so both share the row evenly regardless of label length */
.btn-row button.action { margin-top: 0; flex: 1 1 0; width: auto; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

.insp-empty { color: var(--muted); font-size: 13px; }
.insp-title { margin: 10px 0 8px; font-size: 19px; line-height: 1.3; font-weight: 620; overflow-wrap: anywhere; }
.insp-kind { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.insp-kind .badge { margin: 0; }
.inspector details { margin-top: 12px; font-size: 12px; }
.inspector summary { cursor: pointer; color: var(--ink-soft); }
.model-details { border-top: 1px solid var(--rule); padding-top: 12px; }
.value-title { color: var(--ink-soft); font-size: 11.5px; }
.insp-cls {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase;
  padding: 3px 7px; border-radius: 2px; color: var(--on-fill);
}
.insp-id { font-family: var(--mono); font-size: 11.5px; word-break: break-all; margin: 10px 0 4px; line-height: 1.45; }
.insp-id .pfx { color: var(--muted); }
.insp-desc { font-size: 12.5px; color: var(--ink-soft); margin: 10px 0 0; }
.badge {
  display: inline-block; font-family: var(--mono); font-size: 9px; letter-spacing: .1em;
  text-transform: uppercase; padding: 2px 6px; border-radius: 2px; margin: 8px 6px 0 0;
  border: 1px solid var(--rule-strong); color: var(--ink-soft);
}
.badge.warn { border-color: var(--trace); color: var(--trace); }

.fields { margin: 18px 0 0; border-top: 1px solid var(--rule); }
.field { border-bottom: 1px solid var(--rule); padding: 9px 0; }
.field dt { font-family: var(--mono); font-size: 10.5px; letter-spacing: .06em; color: var(--ink-soft); display: flex; align-items: center; gap: 6px; }
.field dd { margin: 4px 0 0; font-size: 12.5px; word-break: break-word; }
.field dd.mono { font-family: var(--mono); font-size: 11.5px; }
.list-item:not(:last-child)::after { content: ", "; color: var(--ink-soft); }
.field .why { font-size: 11.5px; color: var(--muted); margin-top: 3px; font-style: italic; }
.lock { color: var(--trace); font-size: 10px; }
.ref {
  display: block; background: none; border: none; padding: 0; cursor: pointer;
  font-family: var(--mono); font-size: 11.5px; color: var(--link-external); text-align: left;
  text-decoration: underline; text-underline-offset: 2px; word-break: break-all;
}
.term-link { color: var(--link-external); text-decoration: underline; text-underline-offset: 2px; overflow-wrap: anywhere; }
.ref:hover, .term-link:hover { text-decoration-thickness: 2px; }
.link-model, .ref.link-model { color: var(--link-model); }
.link-instance, .ref[data-goto] { color: var(--link-instance); }
.link-external { color: var(--link-external); }
.insp-cls.link-model { color: var(--link-model); background: none; border: 1px solid currentColor; }
.link-legend { display: flex; flex-wrap: wrap; gap: 5px 12px; font-size: 11.5px; }
.link-legend span::before { content: "●"; margin-right: 5px; font-size: 9px; }
.ref.inline { display: inline; font: inherit; word-break: normal; overflow-wrap: anywhere; }
.nested-fields { margin: 0; padding-left: 12px; border-left: 1px solid var(--rule); }
.nested-fields dt { margin-top: 6px; }
.connections { margin-top: 18px; font-size: 12px; }
.connections summary { cursor: pointer; color: var(--ink-soft); }
.connections li { margin: 8px 0; }
.connections ul { padding-left: 16px; }
.view-label { display: block; margin-top: 12px; font-size: 12px; color: var(--ink-soft); }
#edge-mode { width: 100%; margin: 5px 0 8px; padding: 6px; font: inherit; font-size: 12px;
  border: 1px solid var(--rule-strong); border-radius: 3px; color: var(--ink); background: var(--paper); }
a:focus-visible, button:focus-visible, summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
</style>
</head>
<body>
<div class="shell">
  <header>
    <h1>DAPPER <span>· provenance inspector</span></h1>
    <p class="tagline" id="blurb"></p>
    <a href="model/" class="model-docs link-model">Browse the model →</a>
  </header>

  <div class="body">
    <aside class="rail">
      <div class="rail-group">
        <span class="eyebrow">Graphs</span>
        <div id="graph-picker"></div>
        <a class="term-link note" id="graph-source" target="_blank" rel="noopener noreferrer">View graph YAML</a>
      </div>

      <div class="rail-group">
        <span class="eyebrow">Your data</span>
        <div id="drop" tabindex="0" role="button"
             aria-label="Open a DAPPER YAML file to inspect it">
          <strong>Drop a DAPPER YAML</strong>
          <span>or click to choose</span>
        </div>
        <input type="file" id="file" accept=".yaml,.yml" hidden>
        <p class="note" id="upload-note" style="margin-top:8px">
          Nothing is uploaded anywhere — the file is read in your browser.
        </p>
      </div>

      <div class="rail-group">
        <span class="eyebrow">Trace</span>
        <button class="action" id="btn-trace">Trace upstream</button>
        <button class="action ghost" id="btn-clear">Clear</button>
        <p class="note" style="margin-top:10px" id="trace-note"></p>
      </div>

      <div class="rail-group">
        <span class="eyebrow">View</span>
        <div class="btn-row">
          <button class="action ghost" id="btn-fit">Fit</button>
          <button class="action ghost" id="btn-reset">Reset</button>
        </div>
        <label class="view-label" for="edge-mode">Arrows and labels</label>
        <select id="edge-mode">
          <option value="statements">Stored predicates</option>
          <option value="flow">Forward flow</option>
        </select>
        <p class="note" id="edge-note">Inputs appear before outputs. Arrows follow the stored subject → predicate → object.</p>
      </div>

      <div class="rail-group">
        <span class="eyebrow">Links</span>
        <div class="link-legend">
          <span class="link-model">Model definition</span>
          <span class="link-instance">Graph instance</span>
          <span class="link-external">Other resource</span>
        </div>
      </div>

      <div class="rail-group">
        <span class="eyebrow">Kind</span>
        <div id="legend-family"></div>
      </div>

      <div class="rail-group">
        <span class="eyebrow">Illustrative content</span>
        <div id="legend-real"></div>
        <p class="note" style="margin-top:8px">
          Dashed nodes mark illustrative examples used to explore concepts.
        </p>
      </div>
    </aside>

    <main id="cy"></main>
    <aside class="inspector" id="inspector"></aside>
  </div>
</div>

<script>/*__VENDOR__*/</script>
<script>
const DATA = /*__DATA__*/;

const FAMILY_LABEL = { data: "Data", process: "Activity", claim: "Claim",
                       publication: "Publication", part: "Graph / supporting node",
                       workspace: "Agentic workspace" };
const FAMILY_VAR = { data: "--data", process: "--process", claim: "--claim",
                     publication: "--publication", part: "--part",
                     workspace: "--workspace" };
// shape encodes kind, so the graph is readable before you read a label
const FAMILY_SHAPE = { data: "cut-rectangle", process: "barrel", claim: "hexagon",
                       publication: "octagon", part: "round-rectangle",
                       workspace: "ellipse" };
// SVG stand-ins for the legend swatches
const FAMILY_SWATCH = {
  data: "M4,1 L26,1 L29,5 L29,19 L4,19 Z",
  process: "M3,4 Q3,1 9,1 L23,1 Q29,1 29,4 L29,16 Q29,19 23,19 L9,19 Q3,19 3,16 Z",
  claim: "M9,1 L24,1 L30,10 L24,19 L9,19 L3,10 Z",
  publication: "M9,1 L24,1 L30,6 L30,14 L24,19 L9,19 L3,14 L3,6 Z",
  part: "M4,2 L29,2 L29,18 L4,18 Z",
  workspace: "M2,10 A14,9 0 1,0 30,10 A14,9 0 1,0 2,10 Z",
};

const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const state = { graphKey: DATA.graphs[0].key, selected: null, traced: null, edgeMode: "statements" };
let cy = null;

cytoscape.use(cytoscapeElk);

function edgeAppearance(edge, mode) {
  const reversed = edge.source !== edge.subject;
  const predicate = edge.predicate || edge.cls;
  // prov:generated is a declared inverse. Other inverse labels below are
  // reading aids, not invented vocabulary terms. The inspector always shows
  // the original RDF statement and links its original predicate.
  const inverseLabels = {
    "prov:wasGeneratedBy": "prov:generated",
    "prov:used": "was used by",
    "prov:wasDerivedFrom": "source for",
    "prov:hadMember": "member of",
    "dapper:supportedByNanopub": "supports",
    "dapper:hasAgenticWorkspace": "workspace for",
    "dapper:has_agentic_workspace": "workspace for",
    "dapper:component_claims": "component of",
    "dapper:subject_entity": "subject of",
    "dapper:hasProgram": "program of",
    "hycl:claims": "asserted by",
  };
  const backwardArrow = mode === "statements" && reversed;
  return {
    label: mode === "flow" && reversed ? (inverseLabels[predicate] || `inverse of ${predicate}`) : predicate,
    sourceArrow: backwardArrow ? "triangle" : "none",
    targetArrow: backwardArrow ? "none" : "triangle",
  };
}

function buildElements(graph) {
  const els = [];
  for (const n of graph.nodes) {
    const d = { id: n.id, cls: n.cls, family: n.family, label: n.label,
                illustrative: n.illustrative ? 1 : 0 };
    if (n.parent) d.parent = n.parent;
    els.push({ data: d });
  }
  for (const e of graph.edges) {
    els.push({ data: { id: JSON.stringify([e.subject, e.predicate, e.object]),
                       source: e.source, target: e.target,
                       subject: e.subject, object: e.object, predicate: e.predicate,
                       ...edgeAppearance(e, state.edgeMode) } });
  }
  return els;
}

function styleSheet() {
  const sheet = [
    { selector: "node", style: {
        // size to the label: these names vary from 8 to 40 characters, and a
        // fixed box either clips them or wastes half the canvas
        shape: "round-rectangle", width: "label", height: "label", padding: 14,
        label: "data(label)", "text-wrap": "wrap", "text-max-width": 190,
        "text-valign": "center", "text-halign": "center",
        "font-family": css("--sans"), "font-size": 11.5, "font-weight": 600,
        "line-height": 1.25,
        color: css("--on-fill"), "border-width": 1.5,
        "transition-property": "opacity", "transition-duration": "180ms",
    }},
    { selector: "node[illustrative = 1]", style: {
        // drawn but not built — hollow body, dashed outline
        "background-opacity": 0, "border-style": "dashed", color: css("--ink"),
    }},
    { selector: "edge", style: {
        width: 1.4, "line-color": css("--rule-strong"),
        "source-arrow-color": css("--rule-strong"), "source-arrow-shape": "data(sourceArrow)",
        "target-arrow-color": css("--rule-strong"), "target-arrow-shape": "data(targetArrow)",
        "arrow-scale": .85, "curve-style": "bezier",
        label: "data(label)", "font-family": css("--mono"), "font-size": 8,
        color: css("--muted"), "font-size": 9,
        "text-background-color": css("--paper-sunk"), "text-background-opacity": 1,
        "text-background-padding": 3, "text-margin-y": -2,
    }},
    { selector: ".dim", style: { opacity: .16 } },
    { selector: "edge.lit", style: {
        "line-color": css("--trace"), "source-arrow-color": css("--trace"), "target-arrow-color": css("--trace"),
        width: 2.4, color: css("--trace"),
    }},
    { selector: "edge.context", style: {
        "line-color": css("--ink-soft"), "source-arrow-color": css("--ink-soft"),
        "target-arrow-color": css("--ink-soft"), color: css("--ink-soft"),
    }},
  ];
  for (const [fam, v] of Object.entries(FAMILY_VAR)) {
    sheet.push({ selector: `node[family = "${fam}"]`, style: {
      shape: FAMILY_SHAPE[fam], "background-color": css(v), "border-color": css(v),
    }});
  }
  // A nanopublication with children is drawn as a labelled BOX around them,
  // not as a glyph: it is a container of four named graphs, and seeing the
  // boundary is what tells you the big upstream DAG is outside it.
  sheet.push({ selector: "node:parent", style: {
      shape: "round-rectangle", "background-opacity": 0.06,
      "background-color": css("--publication"), "border-color": css("--publication"),
      "border-width": 1.5, "border-style": "solid", padding: 16,
      "text-valign": "top", "text-halign": "center", "text-margin-y": -6,
      color: css("--publication"), "font-size": 11, "font-weight": 650,
      "text-max-width": 320,
  }},
  { selector: "node:parent[illustrative = 1]", style: { "border-style": "dashed" } });

  // angled silhouettes cut their own corners, so the label needs to sit further
  // inside the bounding box than a rectangle's would
  sheet.push({ selector: 'node[family = "process"]', style: { padding: 18 } });
  sheet.push({ selector: 'node[family = "claim"]', style: { padding: 26 } });
  sheet.push({ selector: 'node[family = "publication"]', style: { padding: 22 } });
  sheet.push({ selector: 'node[family = "workspace"]', style: { padding: 34 } });
  // Apply after family and compound styles so selection has one clean border.
  sheet.push({ selector: "node:selected", style: {
    "outline-width": 0, "border-width": 2.25, "border-color": css("--trace"),
  }});
  return sheet;
}

function renderGraph() {
  const graph = DATA.graphs.find(g => g.key === state.graphKey);
  document.getElementById("blurb").textContent = graph.blurb;
  if (cy) cy.destroy();
  cy = cytoscape({
    container: document.getElementById("cy"),
    elements: buildElements(graph),
    style: styleSheet(),
    // ELK layered: same top-to-bottom DAG as dagre, but it understands the
    // nanopublication containers and lays their contents out inside the box.
    layout: {
      name: "elk", padding: 28,
      elk: {
        algorithm: "layered",
        "elk.direction": "DOWN",
        "elk.spacing.nodeNode": 34,
        "elk.layered.spacing.nodeNodeBetweenLayers": 58,
        "elk.hierarchyHandling": "INCLUDE_CHILDREN",
        "elk.padding": "[top=34,left=16,bottom=16,right=16]",
      },
    },
    wheelSensitivity: .2, minZoom: .2, maxZoom: 2.5,
  });
  cy.on("tap", "node", ev => {
    state.selected = ev.target.id();
    renderInspector(); renderRail();
  });
  cy.on("tap", ev => { if (ev.target === cy) clearTrace(); });
  cy.ready(() => {
    cy.zoom(1);
    const bb = cy.elements().boundingBox();
    cy.pan({ x: cy.width() / 2 - (bb.x1 + bb.x2) / 2, y: 36 - bb.y1 });
  });
  if (state.selected && cy.$id(state.selected).length) cy.$id(state.selected).select();
}

/* ---- trace: everything the selected node depends on ---- */
function applyTrace(startId) {
  const start = cy.$id(startId);
  if (!start.length) return;
  // Walk the DAG, but also pull in anything a traced node CONTAINS or is
  // contained by — a nanopub's provenance graph is part of the nanopub, not a
  // separate hop. Repeat until the set stops growing.
  let keep = start.union(start.ancestors()).union(start.descendants());
  while (true) {
    const next = keep.union(keep.predecessors()).union(keep.ancestors()).union(keep.descendants());
    if (next.length === keep.length) break;
    keep = next;
  }
  // A selected result's distributions and access records are useful context,
  // even though they are not upstream dependencies. Do not walk from these
  // attachments into unrelated downstream analyses or sibling outputs.
  const attachmentPredicates = new Set(["dapper:hasFile", "dapper:hasDrsObject", "dapper:drsRepresentation"]);
  let attachments = start;
  while (true) {
    const edges = attachments.nodes().outgoers("edge").filter(e => attachmentPredicates.has(e.data("predicate")));
    const next = attachments.union(edges).union(edges.targets());
    if (next.length === attachments.length) break;
    attachments = next;
  }
  const visibleNodes = keep.nodes().union(attachments.nodes());
  attachments = attachments.union(cy.edges().filter(e =>
    visibleNodes.contains(e.source()) && visibleNodes.contains(e.target())));
  const context = attachments.difference(keep);
  const visible = keep.union(attachments);
  cy.elements().removeClass("lit context").addClass("dim");
  keep.removeClass("dim").addClass("lit");
  context.removeClass("dim").addClass("context");
  state.traced = startId;
  const roots = keep.nodes().filter(n => !n.isParent() && n.incomers("edge").length === 0);
  document.getElementById("trace-note").textContent =
    `${keep.nodes().length} nodes in this trace from “${start.data("label") || start.id()}”, including grouped contents. ` +
    `${roots.length} root node${roots.length === 1 ? "" : "s"} with no incoming connections. ` +
    (context.nodes().length ? `${context.nodes().length} linked file/access record${context.nodes().length === 1 ? " stays" : "s stay"} visible. ` : "") +
    `Everything else is dimmed.`;
  cy.animate({ fit: { eles: visible, padding: 40 } }, { duration: 320,
    complete: () => { if (cy.zoom() > 1.1) { cy.zoom(1.1); cy.center(visible); } } });
}

function clearTrace() {
  if (!cy) return;
  cy.elements().removeClass("dim lit context");
  state.traced = null;
  document.getElementById("trace-note").textContent =
    "Select any node to trace its upstream connections. Its grouped contents, files, and access records stay visible. With no selection, tracing starts at this graph’s default node.";
}

/* ---- inspector ---- */
const inspector = document.getElementById("inspector");
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function splitId(id) {
  const i = Math.max(id.lastIndexOf(":"), id.lastIndexOf("/"));
  return i < 0 ? ["", id] : [id.slice(0, i + 1), id.slice(i + 1)];
}

function curieUrl(value) {
  const match = /^([A-Za-z][A-Za-z0-9._-]*):([^\s<>"`]+)$/.exec(value);
  if (!match) return null;
  // Only known vocabulary terms have documentation. Never send a computed
  // dapper:File.<digest> record ID to a class or namespace page.
  if (match[1] === "dapper") return own(DATA.config.vocabulary, match[2]) || null;
  const base = (DATA.config.prefixes || {})[match[1]];
  if (typeof base !== "string" || !httpUrl(base)) return null;
  return base + match[2] + (match[1] === "KPN.TRAIT" ? "/" : "");
}

function own(object, key) {
  return object && Object.prototype.hasOwnProperty.call(object, key) ? object[key] : undefined;
}

function httpUrl(value) {
  if (!/^https?:\/\//i.test(value) || /[\s<>"`]/.test(value)) return null;
  try { return new URL(value).hostname ? value : null; } catch { return null; }
}

function referenceUrl(value) {
  const direct = httpUrl(value);
  if (direct) return direct;
  // S3 objects open over HTTPS; a trailing slash denotes a prefix listing.
  // https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
  const s3 = /^s3:\/\/([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])(?:\/(.*))?$/.exec(value);
  if (s3) {
    const base = `https://s3.amazonaws.com/${s3[1]}/`, key = s3[2] || "";
    return !key || key.endsWith("/")
      ? base + "?list-type=2&prefix=" + encodeURIComponent(key)
      : base + key.split("/").map(encodeURIComponent).join("/");
  }
  return curieUrl(value);
}

function linkKind(url) {
  const prefixes = DATA.config.prefixes || {};
  const model = url.startsWith("model/") ||
    ["dapper_class", "dapper_slot", "dapper_enum", "dapper_type"].some(prefix =>
      typeof prefixes[prefix] === "string" && url.startsWith(prefixes[prefix]));
  return model ? "model" : "external";
}

function linkHtml(url, label, className = "term-link", title = "", kind = linkKind(url)) {
  return `<a class="${className} link-${kind}" href="${esc(url)}" target="_blank" rel="noopener noreferrer"${title ? ` title="${esc(title)}"` : ""}>${esc(label)}</a>`;
}

function referenceHtml(value) {
  const url = referenceUrl(value);
  return url ? linkHtml(url, value) : esc(value);
}

function textHtml(value, ids = new Set()) {
  // Link references inside descriptions without interpreting user data as HTML
  // or Markdown. Strip sentence punctuation, preserving balanced DOI brackets.
  const pattern = /\b(?:https?:\/\/|s3:\/\/|[A-Za-z][A-Za-z0-9._-]*:)[^\s<>"`]+/g;
  let html = "", last = 0;
  for (const match of value.matchAll(pattern)) {
    let token = match[0].replace(/[.,;!?]+$/, "");
    for (const [open, close] of [["(", ")"], ["[", "]"], ["{", "}"]]) {
      while (token.endsWith(close) && token.split(close).length > token.split(open).length)
        token = token.slice(0, -1);
    }
    html += esc(value.slice(last, match.index));
    html += ids.has(token)
      ? `<button class="ref inline" data-goto="${esc(token)}">${esc(token)}</button>`
      : referenceHtml(token);
    html += esc(match[0].slice(token.length));
    last = match.index + match[0].length;
  }
  return html + esc(value.slice(last));
}

function mappingsHtml(items) {
  return (items || []).map(m => `<span>${esc(m.relation)}: ${referenceHtml(m.target)}</span>`).join("; ");
}

function valueHtml(v, ids, meta = {}) {
  if (Array.isArray(v))
    return `<span class="list-item">${v.map(x => valueHtml(x, ids, meta)).join('</span><span class="list-item">')}</span>`;
  if (v && typeof v === "object")
    return `<dl class="nested-fields">${Object.entries(v).map(([k, value]) =>
      `<dt>${esc(k)}</dt><dd>${valueHtml(value, ids)}</dd>`).join("")}</dl>`;
  const s = String(v);
  if (ids.has(s)) return `<button class="ref" data-goto="${esc(s)}">${esc(s)}</button>`;
  const pv = own(meta.enum && meta.enum.values, s);
  if (pv) {
    const titleUrl = pv.meaning && referenceUrl(pv.meaning);
    const title = pv.title && (titleUrl ? linkHtml(titleUrl, pv.title) : esc(pv.title));
    return linkHtml(meta.enum.url, s, "ref mono", `Browse ${meta.enum.name}`) +
      (title ? `<span class="value-title">${title}</span>` : "");
  }
  const resolved = referenceUrl(s);
  if (resolved)
    return linkHtml(resolved, s, "ref mono", s.startsWith("s3://") ? "Open S3 location over HTTPS" : "");
  return textHtml(s, ids);
}

function fieldsHtml(fields, cls, ids) {
  let html = `<dl class="fields">`;
  for (const [k, v] of Object.entries(fields)) {
    const meta = (cls.attributes || {})[k] || {};
    const lock = meta.authoritative
      ? `<span class="lock" title="Authoritative — a mirror must not rewrite this">&#9679; locked</span>` : "";
    const name = meta.url ? linkHtml(meta.url, k, "term-link", `Browse the ${k} field`) : esc(k);
    html += `<div class="field"><dt>${name} ${lock}</dt><dd>${valueHtml(v, ids, meta)}</dd>`;
    html += `</div>`;
  }
  return html + `</dl>`;
}

function modelDetailsHtml(cls, fields) {
  let html = `<details class="model-details"><summary>Model details</summary>`;
  if (cls.description) html += `<p class="insp-desc">${textHtml(cls.description)}</p>`;
  if (cls.uri) html += `<p class="insp-desc"><strong>Class URI</strong> ${referenceHtml(cls.uri)}</p>`;
  if (cls.mappings && cls.mappings.length)
    html += `<p class="insp-desc"><strong>Mappings</strong> ${mappingsHtml(cls.mappings)}</p>`;
  html += `<dl class="fields">`;
  for (const [key, value] of Object.entries(fields)) {
    const meta = own(cls.attributes, key);
    if (!meta) continue;
    html += `<div class="field"><dt>${meta.url ? linkHtml(meta.url, key) : esc(key)}</dt><dd>`;
    if (meta.description) html += `<div class="why">${textHtml(meta.description)}</div>`;
    if (meta.mappings && meta.mappings.length)
      html += `<div class="why">Mappings: ${mappingsHtml(meta.mappings)}</div>`;
    if (meta.enum) {
      html += `<div class="why">Vocabulary: ${linkHtml(meta.enum.url, meta.enum.name)}</div>`;
      for (const v of Array.isArray(value) ? value : [value]) {
        const pv = own(meta.enum.values, String(v));
        if (!pv) continue;
        const notes = [pv.meaning && `Meaning: ${referenceHtml(pv.meaning)}`, mappingsHtml(pv.mappings)].filter(Boolean);
        if (notes.length) html += `<div class="why">${esc(v)} — ${notes.join("; ")}</div>`;
        if (pv.description) html += `<div class="why">${textHtml(pv.description)}</div>`;
      }
    }
    html += `</dd></div>`;
  }
  return html + `</dl></details>`;
}

function paragraphCitationsHtml(paragraph, graph) {
  const citations = paragraph.fields.citations || [];
  if (!citations.length) return "";
  const localIds = new Set(graph.nodes.map(n => n.id));
  const text = Array.from(paragraph.fields.text || "");
  return `<p><strong>Cited objects</strong></p><ul>` + citations.map(citation => {
    const quote = text.slice(citation.start, citation.end).join("");
    return `<li>${valueHtml(citation.target_id, localIds)} · metadata revision ${esc(citation.citation_metadata_revision)}<br>“${esc(quote)}”</li>`;
  }).join("") + `</ul>`;
}

function accountOverviewHtml(node, graph) {
  if (node.cls !== "ScientificAccount") return "";
  const byId = new Map(graph.nodes.map(n => [n.id, n]));
  const fields = node.fields;
  let html = `<section class="scientific-account"><h3>Scientific account</h3>`;
  const prose = (label, text) => text ? `<p><strong>${esc(label)}</strong><br>${esc(text)}</p>` : "";
  if (fields.question) {
    const question = byId.get(fields.question);
    html += prose("Question", question?.fields.text || fields.question);
    if (question?.cls === "KnowledgeGap") html += prose("Knowledge gap", question.fields.gap_description);
    html += prose("Gap kind", question?.fields.gap_kind);
    if (question?.fields.about_entities?.length) html += `<p><strong>Entity context</strong><br>${valueHtml(question.fields.about_entities, new Set(byId.keys()))}</p>`;
  }
  if (fields.hypothesis) {
    const proposition = byId.get(fields.hypothesis);
    html += prose("Hypothesis under investigation", proposition?.fields.statement || fields.hypothesis);
  }
  html += prose("Context", fields.context);
  for (const assumption of fields.assumptions || []) html += prose("Assumption", assumption);
  const conclusions = new Set(fields.conclusion_claims || []);
  for (const id of [...(fields.component_claims || []).filter(id => !conclusions.has(id)), ...conclusions]) {
    const claim = byId.get(id);
    if (!claim) continue;
    const proposition = byId.get(claim.fields.proposition);
    const label = conclusions.has(id) ? "Conclusion claim" : "Claim";
    const assessmentText = claim.fields.statement || `Assessment of the proposition: ${proposition?.fields.statement || claim.label}`;
    html += `<p><strong>${label}</strong> <button class="ref inline" data-goto="${esc(id)}">Inspect claim</button><br>${esc(assessmentText)}</p>`;
    html += prose("Assessment direction", claim.fields.direction);
    for (const evidenceId of claim.fields.has_evidence || []) {
      const evidence = byId.get(evidenceId);
      if (!evidence) continue;
      html += `<details><summary>Evidence and interpretation</summary>`;
      html += prose("Direction", evidence.fields.direction);
      html += prose("Rationale", evidence.fields.explanation);
      html += prose("Context", evidence.fields.context);
      for (const assumption of evidence.fields.assumptions || []) html += prose("Assumption", assumption);
      html += `<button class="ref inline" data-goto="${esc(evidenceId)}">Inspect evidence and sources</button></details>`;
    }
  }
  html += prose("Closing remarks", fields.closing_remarks);
  for (const paragraph of graph.nodes.filter(n => n.cls === "Paragraph" && n.fields.scientific_account === node.id)) {
    html += `<details><summary>Results paragraph</summary><p>${esc(paragraph.fields.text)}</p>${paragraphCitationsHtml(paragraph, graph)}<button class="ref inline" data-goto="${esc(paragraph.id)}">Inspect this expression</button></details>`;
  }
  return html + `</section>`;
}

function renderInspector() {
  if (!state.selected) {
    inspector.innerHTML = `<span class="eyebrow">Inspector</span>
      <p class="insp-empty" style="margin-top:10px">Select a node to inspect its fields and connections.
      Follow links to model definitions, controlled vocabularies, and external resources.</p>`;
    return;
  }
  const graph = DATA.graphs.find(g => g.key === state.graphKey);
  const node = graph.nodes.find(n => n.id === state.selected);
  if (!node) { inspector.innerHTML = ""; return; }
  const cls = DATA.schema[node.cls] || {};
  const color = `var(${FAMILY_VAR[node.family] || "--part"})`;
  const [pfx, tail] = splitId(node.id);

  const badges = node.illustrative ? [`<span class="badge warn">Illustrative</span>`] : [];
  if (cls.npGraph) badges.push(`<span class="badge">${cls.npGraph} graph</span>`);

  const idUrl = referenceUrl(node.id);
  const idHtml = idUrl ? linkHtml(idUrl, node.id, "term-link", "Open this record's identifier", "instance") : `<span class="pfx">${esc(pfx)}</span>${esc(tail)}`;
  const classHtml = cls.url
    ? linkHtml(cls.url, node.cls, "insp-cls term-link", `Browse the ${node.cls} class`)
    : `<span class="insp-cls" style="background:${color}">${esc(node.cls)}</span>`;
  const ids = new Set(graph.nodes.map(n => n.id));
  const titleField = node.fields.name ? "name" : node.fields.filename ? "filename" : null;
  const title = titleField ? node.fields[titleField] : node.label;
  let html = `<span class="eyebrow">Inspector</span>
    <h2 class="insp-title">${esc(title)}</h2>
    <div class="insp-kind">${classHtml}${badges.join("")}</div>
    <details><summary>Identifier</summary><p class="insp-id">${idHtml}</p></details>`;
  if (node.fields.description)
    html += `<details><summary>Description</summary><p class="insp-desc">${valueHtml(node.fields.description, ids)}</p></details>`;
  html += accountOverviewHtml(node, graph);
  // Name is already the heading; long narrative and model explanations are
  // optional so that the record's actual values are easy to scan.
  const fields = Object.fromEntries(Object.entries(node.fields)
    .filter(([key]) => key !== titleField && key !== "description"));
  const recordFields = fieldsHtml(fields, cls, ids);
  html += node.cls === "ScientificAccount" ? `<details><summary>All record fields</summary>${recordFields}</details>` : recordFields;
  const connections = graph.edges.filter(e => e.subject === node.id || e.object === node.id);
  if (connections.length) {
    html += `<details class="connections"><summary>Graph connections (${connections.length})</summary>
      <p class="note">Stored statements: subject → predicate → object.</p><ul>`;
    for (const e of connections) {
      const incoming = e.object === node.id;
      const peer = graph.nodes.find(n => n.id === (incoming ? e.subject : e.object));
      const peerHtml = `<button class="ref inline" data-goto="${esc(peer.id)}" title="${esc(peer.id)}">${esc(peer.label)}</button>`;
      html += `<li>${incoming ? peerHtml : "This node"} — ${referenceHtml(e.predicate || e.cls)} → ${incoming ? "this node" : peerHtml}</li>`;
    }
    html += `</ul></details>`;
  }
  html += modelDetailsHtml(cls, node.fields);
  inspector.innerHTML = html;
  wireRefs();
}

function wireRefs() {
  inspector.querySelectorAll(".ref[data-goto]").forEach(b => {
    b.addEventListener("click", () => {
      const id = b.dataset.goto;
      state.selected = id;
      if (cy) {
        cy.$("node:selected").unselect();
        const n = cy.$id(id);
        if (n.length) { n.select(); cy.animate({ center: { eles: n } }, { duration: 300 }); }
      }
      renderInspector();
    });
  });
}

/* ---- rail ---- */
function renderRail() {
  const gp = document.getElementById("graph-picker");
  gp.innerHTML = "";
  for (const g of DATA.graphs) {
    const b = document.createElement("button");
    b.className = "pick";
    b.setAttribute("aria-current", String(g.key === state.graphKey));
    b.innerHTML = `${esc(g.title)}<small>${esc(g.source)}</small>`;
    b.addEventListener("click", () => {
      state.graphKey = g.key; state.selected = null; state.traced = null;
      renderGraph(); clearTrace(); renderRail(); renderInspector();
    });
    gp.appendChild(b);
  }

  const fam = document.getElementById("legend-family");
  fam.innerHTML = "";
  for (const [key, label] of Object.entries(FAMILY_LABEL)) {
    const row = document.createElement("div");
    row.className = "legend-row";
    row.innerHTML = `<svg width="32" height="20" viewBox="0 0 32 20" aria-hidden="true">
      <path d="${FAMILY_SWATCH[key]}" fill="var(${FAMILY_VAR[key]})"/></svg><span>${label}</span>`;
    fam.appendChild(row);
  }

  document.getElementById("legend-real").innerHTML = `
    <div class="legend-row"><svg width="32" height="20" viewBox="0 0 32 20" aria-hidden="true">
      <path d="${FAMILY_SWATCH.data}" fill="none" stroke="var(--data)" stroke-width="1.5"
        stroke-dasharray="4 3"/></svg><span>Illustrative</span></div>`;
  const graph = DATA.graphs.find(g => g.key === state.graphKey);
  const source = document.getElementById("graph-source");
  source.hidden = graph.key === "__upload__";
  if (!source.hidden) source.href = "model/examples/" + encodeURIComponent(graph.source);
  else source.removeAttribute("href");
}

/* ---- actions ---- */
document.getElementById("edge-mode").addEventListener("change", e => {
  state.edgeMode = e.target.value;
  // Arrowheads and wording can change without moving nodes or changing the
  // input-to-output topology used by ELK and upstream tracing.
  cy.edges().forEach(edge => edge.data(edgeAppearance(edge.data(), state.edgeMode)));
  document.getElementById("edge-note").textContent = state.edgeMode === "flow"
    ? "Arrows follow input → output flow. Inverse labels read in that direction; Graph connections shows the stored statements."
    : "Inputs appear before outputs. Arrows follow the stored subject → predicate → object.";
});
document.getElementById("btn-trace").addEventListener("click", () => {
  const graph = DATA.graphs.find(g => g.key === state.graphKey);
  const start = state.selected || graph.start;
  state.selected = start;
  cy.$("node:selected").unselect();
  const n = cy.$id(start);
  if (n.length) n.select();
  applyTrace(start);
  renderInspector();
});
document.getElementById("btn-clear").addEventListener("click", clearTrace);
document.getElementById("btn-fit").addEventListener("click",
  () => cy.animate({ fit: { padding: 30 } }, { duration: 260 }));
document.getElementById("btn-reset").addEventListener("click", () => {
  clearTrace();
  cy.$("node:selected").unselect();
  state.selected = null;
  cy.animate({ fit: { padding: 30 } }, { duration: 260 });
  renderInspector(); renderRail();
});

/* ---- open a DAPPER document from disk ------------------------------------
   Builds a graph from an arbitrary DAPPER YAML using DATA.config, which the
   build script emits from the same constants it uses itself, so the browser and
   the build agree by construction instead of by a second hand-kept copy.
   The file is read with FileReader and never leaves the machine.
--------------------------------------------------------------------------- */
function graphFromDoc(raw, filename) {
  const C = DATA.config;
  const illustrative = new Set(raw._illustrative || []);
  const nodes = [], edges = [];

  for (const group in C.nodeGroups) {
    const cls = C.nodeGroups[group];
    for (const n of raw[group] || []) {
      const fields = Object.assign({}, n);
      delete fields.id;
      nodes.push({
        id: n.id, cls: cls, family: C.family[cls] || "part",
        illustrative: illustrative.has(n.id),
        label: n.filename || n.name || String(n.id).split(/[:.]/).pop(),
        fields: fields
      });
    }
  }
  const ids = new Set(nodes.map(n => n.id));

  for (const group in C.edgeGroups) {
    const cls = C.edgeGroups[group][0], flow = C.edgeGroups[group][1];
    for (const e of raw[group] || []) {
      let src = e.subject, dst = e.object;
      if (flow === "reverse") { const t = src; src = dst; dst = t; }
      edges.push({ source: src, target: dst, cls: cls, subject: e.subject, object: e.object,
        predicate: e.predicate || C.edgePredicates[cls] || "" });
    }
  }

  const byId = {};
  nodes.forEach(n => { byId[n.id] = n; });
  const NESTED = ["has_assertion", "has_provenance",
                  "has_publication_info", "has_signature_element"];
  for (const n of nodes) {
    if (n.cls !== "Nanopublication") continue;
    for (const f of NESTED) {
      const child = n.fields[f];
      if (typeof child === "string" && byId[child]) byId[child].parent = n.id;
    }
  }

  const seen = new Set(edges.map(e => JSON.stringify([e.subject, e.predicate, e.object])));
  for (const n of nodes) {
    for (const field in C.inlineLinks) {
      const predicate = C.inlineLinks[field][0], dir = C.inlineLinks[field][1];
      const v = n.fields[field];
      const targets = Array.isArray(v) ? v : [v];
      for (const t of targets) {
        if (typeof t !== "string" || !ids.has(t)) continue;
        const src = dir === "in" ? t : n.id;
        const dst = dir === "in" ? n.id : t;
        const key = JSON.stringify([n.id, predicate, t]);
        if (src === dst || seen.has(key)) continue;
        seen.add(key);
        edges.push({ source: src, target: dst, cls: "(inline)", subject: n.id, object: t, predicate: predicate });
      }
    }
  }

  // Start at a terminal node regardless of domain; the user can select any
  // other starting point. Compound contents are reached through their group.
  const validEdges = edges.filter(e => ids.has(e.source) && ids.has(e.target));
  const outgoing = new Set(validEdges.map(e => e.source));
  const startNode = nodes.find(n => n.cls === "ScientificAccount") ||
    nodes.find(n => !n.parent && !outgoing.has(n.id)) || nodes[0];
  return {
    key: "__upload__", title: filename, blurb: "", source: filename,
    start: startNode ? startNode.id : null,
    nodes: nodes,
    edges: validEdges
  };
}

/* Structural checks only, deliberately. Recomputing a digest here would need
   byte-for-byte parity with rdflib's n-triples output, and a wrong green tick is
   worse than none — `dapper_identity.py verify` is the authority. These checks
   need no such parity and catch what actually breaks a graph. */
function inspectDoc(g) {
  const counts = {};
  g.nodes.forEach(n => { counts[n.id] = (counts[n.id] || 0) + 1; });
  const dupes = Object.keys(counts).filter(k => counts[k] > 1);

  const ids = new Set(g.nodes.map(n => n.id));
  const dangling = new Set();
  const scan = v => {
    if (typeof v === "string") {
      if (v.indexOf("dapper:") === 0 && !ids.has(v)) dangling.add(v);
    } else if (Array.isArray(v)) v.forEach(scan);
    else if (v && typeof v === "object") Object.keys(v).forEach(k => scan(v[k]));
  };
  g.nodes.forEach(n => scan(n.fields));

  const minted = g.nodes.filter(n => String(n.id).indexOf("dapper:") === 0).length;
  const lines = [g.nodes.length + " nodes", g.edges.length + " edges",
                 minted + "/" + g.nodes.length + " ids minted"];
  if (dupes.length) lines.push(dupes.length + " duplicate id(s)");
  if (dangling.size) lines.push(dangling.size + " dangling ref(s)");
  return { lines: lines, ok: !dupes.length && !dangling.size };
}

function loadDoc(text, filename) {
  const note = document.getElementById("upload-note");
  let raw;
  try {
    raw = jsyaml.load(text);
  } catch (err) {
    note.className = "note bad";
    note.textContent = "Could not parse " + filename + ": " + err.message;
    return;
  }
  if (!raw || typeof raw !== "object") {
    note.className = "note bad";
    note.textContent = filename + " is not a DAPPER document.";
    return;
  }
  const g = graphFromDoc(raw, filename);
  if (!g.nodes.length) {
    note.className = "note bad";
    note.textContent = "No DAPPER nodes in " + filename +
      ". Expected node lists such as datasets, files, or activities.";
    return;
  }
  const report = inspectDoc(g);
  g.blurb = filename + " — " + report.lines.join(" · ");
  DATA.graphs = DATA.graphs.filter(x => x.key !== "__upload__");
  DATA.graphs.unshift(g);
  state.graphKey = "__upload__";
  state.selected = null;
  state.traced = null;
  renderGraph(); clearTrace(); renderRail(); renderInspector();
  note.className = report.ok ? "note good" : "note bad";
  note.textContent = report.ok
    ? filename + ": " + report.lines.join(", ") + ". Structure checks out."
    : filename + ": " + report.lines.join(", ") + ".";
}

const dropZone = document.getElementById("drop");
const filePicker = document.getElementById("file");
dropZone.addEventListener("click", () => filePicker.click());
dropZone.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); filePicker.click(); }
});
filePicker.addEventListener("change", e => {
  const f = e.target.files[0];
  if (f) f.text().then(t => loadDoc(t, f.name));
});
["dragenter", "dragover"].forEach(ev => dropZone.addEventListener(ev, e => {
  e.preventDefault(); dropZone.classList.add("over");
}));
["dragleave", "drop"].forEach(ev => dropZone.addEventListener(ev, e => {
  e.preventDefault(); dropZone.classList.remove("over");
}));
dropZone.addEventListener("drop", e => {
  const f = e.dataTransfer.files[0];
  if (f) f.text().then(t => loadDoc(t, f.name));
});

renderRail();
renderGraph();
clearTrace();
renderInspector();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
