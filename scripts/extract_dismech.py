#!/usr/bin/env python3
"""Extract DisMech vocabularies without modifying the source repository.

Includes every YAML document under kb and the local import closure of the main
LinkML schema. Ontology exports contain observed bindings, not entire ontologies.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import unicodedata
import yaml

LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def walk(value, pointer=""):
    yield pointer, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from walk(item, pointer + "/" + str(i))


def json_file(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def jsonl(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def extract(source, output):
    output.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--", "kb", "src/dismech/schema"], text=True)
    source_files, failures, mechanisms, entities, hypotheses, terms, vocabulary = [], [], [], [], [], {}, {}
    schemas, queue = {}, [source / "src/dismech/schema/dismech.yaml"]
    while queue:
        path = queue.pop()
        if path in schemas:
            continue
        doc = yaml.load(path.read_text(), Loader=LOADER)
        schemas[path] = doc
        for name in doc.get("imports", []):
            if ":" not in name:
                queue.append(path.parent / (name + ".yaml"))
    enum_defs = []
    for path, schema in sorted(schemas.items()):
        for name, definition in schema.get("enums", {}).items():
            enum_defs.append({"name": name, "source_file": str(path.relative_to(source)), **definition})
    json_file(output / "schema-vocabularies.json", {"schema_id": schemas[source / "src/dismech/schema/dismech.yaml"]["id"],
                "enums": enum_defs, "prefixes": schemas[source / "src/dismech/schema/dismech.yaml"].get("prefixes", {}),
                "scope": "Main schema and all local imports; dynamic enum constraints preserved, not expanded"})

    def add_vocab(kind, label, file, pointer, record_id, term_id=None, extra=None):
        if not label:
            return
        key = (kind, term_id, normalize(label))
        if key not in vocabulary:
            vocabulary[key] = {"id": stable_id("vocab:", *key), "kind": kind, "label": str(label),
                               "term_id": term_id, "occurrences": []}
        occurrence = {"source_file": file, "json_pointer": pointer, "record_id": record_id}
        if extra:
            occurrence.update(extra)
        vocabulary[key]["occurrences"].append(occurrence)

    for index, path in enumerate(sorted((source / "kb").rglob("*.yaml")), 1):
        relative = str(path.relative_to(source))
        raw = path.read_bytes()
        source_files.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})
        try:
            doc = yaml.load(raw, Loader=LOADER)
            if not isinstance(doc, dict):
                raise ValueError("Expected a mapping at document root")
        except Exception as exc:
            failures.append({"source_file": relative, "error": str(exc)})
            continue
        source_kind = path.relative_to(source / "kb").parts[0]
        document_id = "dismech:" + str(path.relative_to(source / "kb").with_suffix(""))
        name = doc.get("name", path.stem)
        entity = {"id": document_id, "name": name, "kind": source_kind, "source_file": relative,
                  "description": doc.get("description"), "disease_term": doc.get("disease_term"),
                  "synonyms": doc.get("synonyms", []), "mappings": doc.get("mappings"),
                  "classifications": doc.get("classifications"), "parents": doc.get("parents", [])}
        entities.append(entity)
        nodes = list(walk(doc))
        file_mechanisms = []
        for pointer, value in nodes:
            if pointer.endswith("/pathophysiology") and isinstance(value, list):
                for i, mechanism in enumerate(value):
                    if not isinstance(mechanism, dict):
                        failures.append({"source_file": relative, "json_pointer": f"{pointer}/{i}", "error": "Non-mapping mechanism"})
                        continue
                    ptr = f"{pointer}/{i}"
                    # Source pointer is a locator. Revision + source hash pins its identity.
                    record_id = document_id + "#" + ptr
                    row = {"id": record_id, "document_id": document_id, "document_name": name,
                           "source_kind": source_kind, "source_file": relative, "json_pointer": ptr,
                           "name": mechanism.get("name"), "description": mechanism.get("description"),
                           "conforms_to": mechanism.get("conforms_to"), "raw": mechanism}
                    mechanisms.append(row)
                    file_mechanisms.append(row)
                    add_vocab("mechanism", mechanism.get("name"), relative, ptr + "/name", record_id)
                    for synonym in mechanism.get("synonyms", []) or []:
                        add_vocab("mechanism_synonym", synonym, relative, ptr + "/synonyms", record_id)
            if pointer.endswith("/mechanistic_hypotheses") and isinstance(value, list):
                hypotheses.extend({"document_id": document_id, "source_file": relative,
                                   "json_pointer": f"{pointer}/{i}", "raw": v} for i, v in enumerate(value))
        for pointer, value in nodes:
            if isinstance(value, dict) and "id" in value and ("label" in value or pointer.endswith("/term")):
                term_id = str(value["id"])
                if ":" in term_id:
                    label = value.get("label") or term_id
                    entry = terms.setdefault(term_id, {"id": term_id, "labels": [], "occurrences": []})
                    if label not in entry["labels"]:
                        entry["labels"].append(label)
                    context = next((m["id"] for m in file_mechanisms if pointer.startswith(m["json_pointer"] + "/")), None)
                    entry["occurrences"].append({"source_file": relative, "json_pointer": pointer,
                                                "document_id": document_id, "mechanism_id": context})
            if isinstance(value, dict) and value.get("preferred_term"):
                path_parts = pointer.split("/")
                kind = next((part for part in reversed(path_parts) if part and not part.isdigit()), "descriptor")
                term = value.get("term") or {}
                context = next((m["id"] for m in file_mechanisms if pointer.startswith(m["json_pointer"] + "/")), document_id)
                add_vocab(kind, value["preferred_term"], relative, pointer, context,
                          term.get("id") if isinstance(term, dict) else None,
                          {"descriptor": value})
            # Preserve free-text mechanism references and module conformance too.
            if pointer.endswith("/mechanisms") and isinstance(value, list):
                for i, label in enumerate(value):
                    if isinstance(label, str):
                        add_vocab("mechanism_reference", label, relative, f"{pointer}/{i}", document_id)
            if pointer.endswith("/conforms_to") and isinstance(value, str):
                add_vocab("module_conformance", value, relative, pointer, document_id)
        if index % 500 == 0:
            print(f"DisMech: {index} YAML files parsed", flush=True)
    edges = []
    for mechanism in mechanisms:
        for i, edge in enumerate(mechanism["raw"].get("downstream", []) or []):
            edges.append({"id": mechanism["id"] + f"/downstream/{i}", "source_id": mechanism["id"],
                          "document_id": mechanism["document_id"], "source_file": mechanism["source_file"],
                          "json_pointer": mechanism["json_pointer"] + f"/downstream/{i}", "raw": edge,
                          "target_resolution": "unresolved_source_reference"})
    outputs = {"entities.jsonl.gz": entities, "mechanisms.jsonl.gz": mechanisms,
               "causal-edges.jsonl.gz": edges, "hypotheses.jsonl.gz": hypotheses,
               "ontology-terms.jsonl.gz": sorted(terms.values(), key=lambda x: x["id"]),
               "vocabulary.jsonl.gz": sorted(vocabulary.values(), key=lambda x: x["id"])}
    for filename, rows in outputs.items():
        jsonl(output / filename, rows)
    json_file(output / "t2d-mechanisms.json", [m for m in mechanisms if m["source_file"] == "kb/disorders/Type_2_Diabetes_Mellitus.yaml"])
    for path in schemas:
        source_files.append({"path": str(path.relative_to(source)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    json_file(output / "source-files.json", source_files)
    (output / "SOURCE-LICENSE.txt").write_text((source / "LICENSE").read_text())
    manifest = {"source_repository": "https://github.com/monarch-initiative/dismech", "source_path": str(source),
                "source_commit": commit, "source_has_local_changes": bool(status.strip()),
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "complete": not failures,
                "yaml_files": len(source_files) - len(schemas), "schema_files": len(schemas),
                "documents_by_kind": dict(Counter(e["kind"] for e in entities)),
                "mechanism_records": len(mechanisms), "causal_edges": len(edges),
                "unique_mechanism_labels": len({normalize(m["name"]) for m in mechanisms if m["name"]}),
                "ontology_terms": len(terms), "vocabulary_entries": len(vocabulary),
                "vocabulary_by_kind": dict(Counter(v["kind"] for v in vocabulary.values())),
                "enum_definitions": len(enum_defs),
                "enum_values": sum(len(e.get("permissible_values", {})) for e in enum_defs),
                "files": {name: {"rows": len(rows), "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()} for name, rows in outputs.items()},
                "errors": failures,
                "limits": ["Observed ontology bindings only; not full external ontology vocabularies",
                           "Source assertions and evidence retained without independent scientific validation",
                           "Mechanism names are lexical entries, not global concept identities",
                           "Downstream target references are preserved; cross-record resolution is deferred"]}
    json_file(output / "manifest.json", manifest)
    print(json.dumps({k: v for k, v in manifest.items() if k not in ["vocabulary_by_kind", "files"]}), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.home() / "src/research/dismech")
    parser.add_argument("--output", type=Path, default=Path("data/dismech"))
    args = parser.parse_args()
    extract(args.source.resolve(), args.output)
