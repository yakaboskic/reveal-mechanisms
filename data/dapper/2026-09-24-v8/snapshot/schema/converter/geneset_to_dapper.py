#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime"]
# ///
"""Convert dig.geneset provenance into DAPPER instances.

Reads the lab's `geneset.provenance.json` (+ sibling `geneset.meta.json`) emitted by
`flannick/dig-gene-set-extractors` and writes validated DAPPER nodes/edges:

  <geneset_id>.dapper.yaml   the full provenance graph (File / C2M2File / Activity / GeneSet
                             nodes + Used / WasGeneratedBy edges)
  <geneset_id>.geneset.yaml  a standalone single GeneSet
  <geneset_id>.geneset_collection.yaml  a standalone GeneSetCollection for a library

The mapping is the crosswalk documented in
`reports/geneset-provenance-nih-dapp-adaptation.md`. dig.geneset carries no NIH
attribution (creators/awards/publications/citation); supply those with --overlay.

Usage:
        uv run schema/converter/geneset_to_dapper.py <input> [-o OUT_DIR]
        [--overlay overlay.yaml] [--validate] [--schema path/to/dapper.yaml]

<input> is one of:
    a geneset.provenance.json file (meta.json auto-discovered alongside),
    a local directory (walked recursively for every geneset.provenance.json),
    an s3:// URI or prefix (objects pulled with `aws s3` then converted).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

# dig.geneset edge label -> (DAPPER edge bucket, PROV predicate)
_INPUT_LABELS = {"data input", "metadata input"}
_OUTPUT_LABEL = "data output"
_EDGE_ROLE = {"data input": "data_input", "metadata input": "metadata_input"}

# DAPPER classes emitted, keyed by the output-doc list name.
_NODE_BUCKETS = ("files", "c2m2_files", "activities", "gene_sets", "gene_set_collections")
_EDGE_BUCKETS = ("used_edges", "was_generated_by_edges")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None/"" so emitted YAML stays tidy."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _c2m2_file(node: dict[str, Any], sha_by_localid: dict[str, str]) -> dict[str, Any]:
    c = node.get("c2m2_properties", {}) or {}
    local_id = c.get("local_id")
    return _clean(
        {
            "id": node.get("id"),
            "name": node.get("name"),
            "description": node.get("description"),
            "filename": c.get("filename"),
            "persistent_id": c.get("persistent_id"),
            "local_id": local_id,
            "c2m2_uuid": c.get("_uuid"),
            "md5": c.get("md5"),
            "sha256": sha_by_localid.get(local_id),
            "size_in_bytes": c.get("size_in_bytes"),
            "dcc_url": node.get("dcc_url"),
            "drc_url": node.get("drc_url"),
        }
    )


def _file(node: dict[str, Any], sha_by_localid: dict[str, str]) -> dict[str, Any]:
    """A File without C2M2 properties carries generic file metadata directly."""
    fields = ("id", "name", "description", "filename", "md5", "sha256",
              "size_in_bytes", "mime_type", "location", "drs_representation")
    out = {key: node.get(key) for key in fields}
    if not out["sha256"]:
        out["sha256"] = sha_by_localid.get(node.get("location"))
    return _clean(out)


def _activity(node: dict[str, Any]) -> dict[str, Any]:
    a = node.get("analysis", {}) or {}
    env = a.get("environment", {}) or {}
    syn = (node.get("c2m2_properties", {}) or {}).get("synonyms") or []
    out = _clean(
        {
            "id": node.get("id"),
            "name": node.get("name"),
            "description": node.get("description"),
            "command": a.get("command"),
            "observed_command": a.get("observed_command"),
            "script_url": a.get("script_url"),
            "repo_url": env.get("repo_url"),
            "code_version": a.get("version"),
            "entrypoint": env.get("entrypoint"),
            "container_image": env.get("container_image"),
            "dcc_url": node.get("dcc_url"),
            "drc_url": node.get("drc_url"),
        }
    )
    if syn:
        out["aliases"] = list(syn)
    return out


def _is_collection(node: dict[str, Any], meta: dict[str, Any]) -> bool:
    return (node.get("type") == "GeneSetCollection"
            or (meta.get("summary") or {}).get("n_sets_emitted") is not None
            or node.get("n_sets") is not None)


def _gene_set(node: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    gs = meta.get("gene_set", {}) or {}
    summary = meta.get("summary", {}) or {}
    params = (meta.get("converter", {}) or {}).get("parameters", {}) or {}
    return _clean(
        {
            "id": node.get("id"),
            "name": node.get("name"),
            "description": node.get("description") or gs.get("description"),
            "member_type": "gene_set" if _is_collection(node, meta) else "gene",
            "assay": gs.get("assay"),
            "data_type": gs.get("data_type"),
            "organism": gs.get("organism"),
            "genome_build": gs.get("genome_build"),
            "n_genes": gs.get("n_genes") if gs.get("n_genes") is not None else summary.get("n_genes"),
            "n_sets": summary.get("n_sets_emitted", node.get("n_sets")),
            "term_prefix": params.get("term_prefix"),
            "dcc_url": node.get("dcc_url"),
            "drc_url": node.get("drc_url"),
        }
    )


def convert_graph(graph: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Map one dig.geneset ProvenanceGraph ({nodes, edges}) to a DAPPER doc."""
    # sha256 lookup: dig.geneset File nodes carry MD5; the metadata sidecar carries SHA-256.
    sha_by_localid: dict[str, str] = {}
    for f in (meta.get("input", {}) or {}).get("files", []) or []:
        for key in ("path", "local_path"):
            if f.get(key) and f.get("sha256"):
                sha_by_localid[f[key]] = f["sha256"]

    doc: dict[str, list] = {b: [] for b in (*_NODE_BUCKETS, *_EDGE_BUCKETS)}
    for node in graph.get("nodes", []):
        ntype = node.get("type")
        if ntype == "File":
            if node.get("c2m2_properties"):
                doc["c2m2_files"].append(_c2m2_file(node, sha_by_localid))
            else:
                doc["files"].append(_file(node, sha_by_localid))
        elif ntype == "AnalysisType":
            doc["activities"].append(_activity(node))
        elif ntype in {"GeneSet", "GeneSetCollection"}:
            bucket = "gene_set_collections" if _is_collection(node, meta) else "gene_sets"
            doc[bucket].append(_gene_set(node, meta))
        else:
            print(f"  ! skipping unknown node type: {ntype!r}", file=sys.stderr)

    for edge in graph.get("edges", []):
        label = edge.get("label")
        source, target = edge.get("source"), edge.get("target")
        if label in _INPUT_LABELS:
            # dig: file --(data input)--> analysis. PROV: activity prov:used entity.
            doc["used_edges"].append(
                _clean(
                    {
                        "subject": target,
                        "predicate": "prov:used",
                        "object": source,
                        "edge_role": _EDGE_ROLE[label],
                    }
                )
            )
        elif label == _OUTPUT_LABEL:
            # dig: analysis --(data output)--> file. PROV: file prov:wasGeneratedBy activity.
            doc["was_generated_by_edges"].append(
                {"subject": target, "predicate": "prov:wasGeneratedBy", "object": source}
            )
        else:
            print(f"  ! skipping unknown edge label: {label!r}", file=sys.stderr)

    # Link a library to a unique GMT emitted by its own producing activity.
    # Multiple candidates require an explicit mapping, not a guessed filename.
    generated = doc["was_generated_by_edges"]
    gmt_ids = {n["id"] for group in ("files", "c2m2_files") for n in doc[group]
               if str(n.get("filename", "")).lower().endswith(".gmt")}
    for collection in doc["gene_set_collections"]:
        producers = {e["object"] for e in generated if e["subject"] == collection["id"]}
        candidates = {e["subject"] for e in generated
                      if e["object"] in producers and e["subject"] in gmt_ids}
        if len(candidates) == 1:
            collection["has_gmt_file"] = next(iter(candidates))
    return {k: v for k, v in doc.items() if v}


def apply_overlay(gene_set: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Inject NIH attribution (creators/awards/publications/citation/…) onto the focus set."""
    for key, value in (overlay or {}).items():
        gene_set.setdefault(key, value)


_AUTHORITATIVE = ("has_creator", "funded_by", "is_described_by", "has_recommended_citation")


def _find_pairs(root: Path) -> list[tuple[Path, Path | None]]:
    """Return (provenance.json, meta.json|None) pairs under root (file or dir)."""
    if root.is_file():
        provs = [root]
    else:
        provs = sorted(root.rglob("geneset.provenance.json"))
    pairs = []
    for p in provs:
        meta = p.with_name("geneset.meta.json")
        pairs.append((p, meta if meta.exists() else None))
    return pairs


def _pull_s3(uri: str, dest: Path) -> Path:
    """Download a geneset.provenance.json (and sibling meta) or a prefix tree via aws s3."""
    dest.mkdir(parents=True, exist_ok=True)
    if uri.endswith(".json"):
        base = uri.rsplit("/", 1)[0]
        for name in ("geneset.provenance.json", "geneset.meta.json"):
            subprocess.run(["aws", "s3", "cp", f"{base}/{name}", str(dest / name)], check=(name == "geneset.provenance.json"))
        return dest
    # prefix: mirror only the json sidecars, preserving structure
    subprocess.run(
        ["aws", "s3", "cp", uri.rstrip("/") + "/", str(dest), "--recursive",
         "--exclude", "*", "--include", "*geneset.provenance.json", "--include", "*geneset.meta.json"],
        check=True,
    )
    return dest


def _mint_ids(doc: dict[str, Any]) -> None:
    """Replace dig.geneset's identifiers with DAPPER content digests, in place.

    dig.geneset mints UUIDv5 for files and analyses and a 24-hex id for the gene
    set. Those are deterministic but opaque and computed by a recipe we do not
    control. DAPPER re-mints everything as `dapper:{ClassName}.{digest}` so one
    documented algorithm covers the whole corpus — see schema/identity/README.md.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "identity"))
    from dapper_identity import assign_ids, load_schema  # noqa: PLC0415

    assign_ids(doc, load_schema(Path(__file__).parent.parent / "dapper.yaml"))


def convert_one(prov_path: Path, meta_path: Path | None, out_dir: Path,
                overlay: dict[str, Any]) -> list[Path]:
    payload = json.loads(prov_path.read_text())
    meta = json.loads(meta_path.read_text()) if meta_path else {}
    written: list[Path] = []
    for geneset_id, graph in payload.items():
        if not isinstance(graph, dict) or "nodes" not in graph:
            continue
        doc = convert_graph(graph, meta)
        _mint_ids(doc)
        # standalone focus GeneSet node (enriched with overlay attribution)
        # ids were just re-minted, so the dig.geneset focus_node_id no longer
        # matches; the focus is simply the gene set this payload is keyed by.
        collection = bool(doc.get("gene_set_collections"))
        focus = (doc.get("gene_set_collections") or doc.get("gene_sets") or [None])[0]
        safe = geneset_id.replace(":", "_").replace("/", "_")
        graph_out = out_dir / f"{safe}.dapper.yaml"
        graph_out.write_text(_dump(doc))
        written.append(graph_out)
        if focus:
            focus_node = dict(focus)
            apply_overlay(focus_node, overlay)
            missing = [k for k in _AUTHORITATIVE if k not in focus_node]
            if missing:
                print(f"  · {geneset_id}: no NIH attribution for {missing} "
                      f"(supply via --overlay)", file=sys.stderr)
            suffix = "geneset_collection" if collection else "geneset"
            node_out = out_dir / f"{safe}.{suffix}.yaml"
            node_out.write_text(_dump(focus_node))
            written.append(node_out)
    return written


def _dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, allow_unicode=True)


# ---- optional self-validation via linkml-validate -------------------------------------
_BUCKET_CLASS = {
    "files": "File",
    "c2m2_files": "C2M2File",
    "activities": "Activity",
    "gene_sets": "GeneSet",
    "gene_set_collections": "GeneSetCollection",
    "used_edges": "Used",
    "was_generated_by_edges": "WasGeneratedBy",
}


def validate_doc(doc_path: Path, schema: Path) -> bool:
    import os

    doc = yaml.safe_load(doc_path.read_text())
    ok = True
    # Drop VIRTUAL_ENV so the nested `uv run` doesn't warn about an env mismatch.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    with tempfile.TemporaryDirectory() as td:
        for bucket, items in doc.items():
            cls = _BUCKET_CLASS.get(bucket)
            if not cls:
                continue
            for i, item in enumerate(items):
                tmp = Path(td) / f"{bucket}_{i}.yaml"
                tmp.write_text(_dump(item))
                res = subprocess.run(
                    ["uv", "run", "--with", "linkml", "linkml-validate",
                     "-s", str(schema), "-C", cls, str(tmp)],
                    capture_output=True, text=True, env=env,
                )
                if res.returncode != 0 or "No issues found" not in res.stdout:
                    ok = False
                    detail = next(
                        (ln for ln in (res.stdout + res.stderr).splitlines()
                         if "[ERROR]" in ln or "Additional" in ln or "required" in ln.lower()),
                        (res.stdout + res.stderr).strip().splitlines()[-1] if (res.stdout + res.stderr).strip() else "unknown error",
                    )
                    print(f"  ✗ {doc_path.name} {cls}[{i}]: {detail.strip()}", file=sys.stderr)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert dig.geneset provenance to DAPPER.")
    ap.add_argument("input", help="geneset.provenance.json, a directory, or an s3:// URI/prefix")
    ap.add_argument("-o", "--out-dir", default="dapper-out", type=Path)
    ap.add_argument("--overlay", type=Path, help="YAML of NIH attribution to inject on the focus GeneSet")
    ap.add_argument("--validate", action="store_true", help="linkml-validate every emitted node")
    ap.add_argument("--schema", type=Path,
                    default=Path(__file__).resolve().parents[1] / "dapper.yaml")
    args = ap.parse_args()

    overlay = yaml.safe_load(args.overlay.read_text()) if args.overlay else {}
    args.out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        if args.input.startswith("s3://"):
            root = _pull_s3(args.input, Path(td) / "s3")
        else:
            root = Path(args.input)
        pairs = _find_pairs(root)
        if not pairs:
            print(f"No geneset.provenance.json found under {args.input}", file=sys.stderr)
            return 2
        all_written: list[Path] = []
        for prov, meta in pairs:
            if meta is None:
                print(f"  ! {prov}: no sibling geneset.meta.json — descriptive fields will be sparse",
                      file=sys.stderr)
            print(f"→ {prov}")
            all_written += convert_one(prov, meta, args.out_dir, overlay)

    print(f"\nWrote {len(all_written)} file(s) to {args.out_dir}/")
    if args.validate:
        graph_docs = [p for p in all_written if p.name.endswith(".dapper.yaml")]
        ok = all(validate_doc(p, args.schema) for p in graph_docs)
        print("Validation:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
