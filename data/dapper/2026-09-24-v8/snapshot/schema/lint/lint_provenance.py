#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime", "linkml"]
# ///
"""DAPPER end-modality linter — one document per end result, fully checked.

An "end modality" is a terminal product of a pipeline: a bottom-line result, a
gene set. The intended unit of publication is ONE document per instantiation,
holding the end result object plus all the provenance around how it was
generated. This linter decides whether such a document is real DAPPER.

WHY THIS EXISTS, given that `linkml-validate` already exists
------------------------------------------------------------
`linkml-validate` does catch hallucinated FIELDS, which is the single biggest
risk when a counterpart (or a language model) writes these documents by hand.
Verified, not assumed: a Dataset carrying invented `p_value_threshold` and
`confidence` fields is rejected. That is the foundation this builds on, and it
is why `check_nodes` below delegates to the same validator rather than
reimplementing field checking.

But it leaves three holes, each of which this linter fills:

  1. NO DOCUMENT SHAPE. The group-keyed shape every example, the portal and
     mint.py use (`datasets:`, `activities:`, `was_generated_by_edges:`) is
     NOT in dapper.yaml — there is no `tree_root` class, so pointing
     `linkml-validate` at a whole graph document raises rather than validating
     it. The shape lives only in Python: `DOC_GROUPS` in
     dapper_identity.py and a SEPARATE `EDGE_GROUPS` in portal/build.py, which
     is a rendering concern that happens to also encode structure. This linter
     imports DOC_GROUPS (rather than restating it) and derives edge groups from
     the schema, so there is no third copy to drift.

  2. EDGES ARE BARELY CONSTRAINED. `subject`/`predicate`/`object` are all bare
     `uriorcurie`. The intended endpoint types exist only as prose on the Edge
     class (`description: Dataset → Agent`), so a `prov:wasGeneratedBy` edge
     pointing a Dataset at an Award validates clean. Measured: an edge with
     `predicate: prov:totallyMadeUpPredicate` passes `linkml-validate` without
     complaint, while an invented `weight` field on the same edge is caught.
     The hallucination that matters most for edges is exactly the one the
     schema misses. See `check_endpoints` and `check_predicates`.

  3. NOTHING CHECKS THE DOCUMENT IS COMPLETE OR CONNECTED. Every node can be
     individually valid while the document is useless: one typo'd id and the
     end result no longer reaches its own provenance. `check_refs`,
     `check_reachability` and the per-profile `check_required_edges` cover this.
     This generalises schema/examples/check_provenance_trace.py, which does the
     same job for one hardcoded document.

CLOSED MODE IS LOAD-BEARING. `JsonschemaValidationPlugin` defaults to
`closed=False`, and in that mode invented fields are ACCEPTED. The same bogus
Dataset that the CLI rejects passed clean in-process until `closed=True` was
set explicitly. Never remove it — it is the entire hallucinated-field check.

Usage:
    # lint one document, auto-detecting its modality
    uv run schema/lint/lint_provenance.py path/to/result.yaml

    # pin the modality (fails if the document is not that shape)
    uv run schema/lint/lint_provenance.py result.yaml --profile bottom-line-result

    uv run schema/lint/lint_provenance.py --list-profiles
    uv run schema/lint/lint_provenance.py --self-test   # lint every canonical example

Exit status is 0 only if there are no errors. Warnings do not fail the run
unless --strict is passed.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "dapper.yaml"
PROFILES_PATH = HERE / "profiles.yaml"

# dapper_identity is a standalone PEP-723 script, not an installed package —
# same reason tests/conftest.py puts it on sys.path rather than importing it as
# a module. DOC_GROUPS is imported rather than restated so the group-key map
# has exactly one definition in the repo.
sys.path.insert(0, str(REPO_ROOT / "schema" / "identity"))
from dapper_identity import (  # noqa: E402
    DOC_GROUPS,
    compute_id,
    digest_of,
    load_schema,
)
sys.path.insert(0, str(REPO_ROOT / "schema"))
from scientific_claims import check_scientific_content  # noqa: E402
from document_prefixes import transform_identifiers  # noqa: E402

# Top-level keys that are neither nodes nor edges. `_illustrative` marks nodes
# drawn for shape rather than transcribed from a real run — the honesty
# distinction the examples exist to make, read by the portal.
META_KEYS = {"_illustrative", "prefixes"}

SEVERITIES = ("error", "warning")


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    severity: str
    check: str
    where: str
    message: str
    why: str = ""

    def render(self) -> str:
        tag = "ERROR  " if self.severity == "error" else "warning"
        out = f"  {tag} [{self.check}] {self.where}\n           {self.message}"
        if self.why:
            out += f"\n           why: {self.why}"
        return out


@dataclass
class Report:
    path: Path
    profile: str | None = None
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, severity: str, check: str, where: str, message: str, why: str = "") -> None:
        assert severity in SEVERITIES, severity
        self.findings.append(Finding(severity, check, where, message, why))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


# ---------------------------------------------------------------------------
# document model
# ---------------------------------------------------------------------------
def _snake(name: str) -> str:
    """`HasDrsObject` -> `has_drs_object`. Mirrors the examples' group keys."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass
class Vocabulary:
    """What the schema and profiles.yaml jointly say a document may contain."""

    node_groups: dict[str, str]                   # group key -> class
    edge_groups: dict[str, str]                   # group key -> Edge class
    edge_endpoints: dict[str, dict[str, list[str]]]
    edge_predicates: dict[str, str]               # Edge class -> expected predicate
    profiles: dict[str, dict]
    relationship_slots: dict[str, dict[str, str]] # class -> slot -> predicate URI
    predicate_uris: dict[str, str]               # schema CURIE -> expanded URI
    schema: Any = field(repr=False)

    def predicate_uri(self, value: Any) -> str | None:
        return self.predicate_uris.get(value, value) if isinstance(value, str) else None

    @classmethod
    def build(cls, sv, profiles_doc: dict) -> "Vocabulary":
        overrides = profiles_doc.get("edge_group_overrides") or {}

        # Edge group keys are DERIVED from the schema's Edge subclasses, so a
        # new edge class is understood the moment it is added. Only real
        # exceptions come from profiles.yaml.
        edge_groups: dict[str, str] = {}
        edge_predicates: dict[str, str] = {}
        for class_name in sv.all_classes():
            if class_name == "Edge" or "Edge" not in sv.class_ancestors(class_name):
                continue
            key = overrides.get(class_name, f"{_snake(class_name)}_edges")
            edge_groups[key] = class_name

            # The expected predicate is already declared in the schema as the
            # `ifabsent` default on the edge's `predicate` slot, e.g.
            # `string(prov:used)`. Read it rather than restating it in
            # profiles.yaml — one source of truth, and no schema change needed.
            try:
                slot = sv.induced_slot("predicate", class_name)
            except Exception:
                slot = None
            match = re.fullmatch(r"string\((.+)\)", str(slot.ifabsent or "")) if slot else None
            if match:
                edge_predicates[class_name] = match.group(1)

        relationship_slots = {}
        predicate_uris = {p: sv.expand_curie(p) for p in edge_predicates.values()}
        for class_name in DOC_GROUPS.values():
            if class_name not in sv.all_classes():
                continue
            relationship_slots[class_name] = {}
            for slot in sv.class_induced_slots(class_name):
                if slot.is_a == "relationship":
                    predicate = str(slot.slot_uri or sv.get_uri(slot))
                    predicate_uris[predicate] = sv.expand_curie(predicate)
                    relationship_slots[class_name][slot.name] = predicate_uris[predicate]

        return cls(
            node_groups=dict(DOC_GROUPS),
            edge_groups=edge_groups,
            edge_endpoints=profiles_doc.get("edge_endpoints") or {},
            edge_predicates=edge_predicates,
            profiles=profiles_doc.get("profiles") or {},
            relationship_slots=relationship_slots,
            predicate_uris=predicate_uris,
            schema=sv,
        )


@dataclass(frozen=True)
class Relationship:
    subject: str
    predicate: str
    object: str
    where: str
    reified: bool


@dataclass
class Document:
    """A parsed group-keyed DAPPER document, indexed for graph walking."""

    path: Path
    raw: dict
    nodes: dict[str, tuple[str, str, dict]]       # id -> (group, class, node)
    edges: list[tuple[str, str, dict]]            # (group, edge class, edge)
    unknown_keys: list[str]
    node_records: list[tuple[str, str, dict]]     # includes missing/duplicate ids
    shape_errors: list[tuple[str, str]]
    prefix_errors: list[tuple[str, str]]
    identity_records: list[tuple[str, str, dict]] # original spellings for hashing

    @classmethod
    def load(cls, path: Path, vocab: Vocabulary) -> "Document":
        raw = yaml.safe_load(path.read_text())
        shape_errors = []
        if not isinstance(raw, dict):
            shape_errors.append((path.name, f"top level is {type(raw).__name__}, expected a mapping"))
            raw = {}

        resolved, prefix_errors = transform_identifiers(
            raw, vocab.schema, {**vocab.node_groups, **vocab.edge_groups}, compact_dapper=True)

        nodes: dict[str, tuple[str, str, dict]] = {}
        edges: list[tuple[str, str, dict]] = []
        unknown: list[str] = []
        node_records = []
        identity_records = []

        for key, value in resolved.items():
            if key in META_KEYS:
                continue
            if key not in vocab.node_groups and key not in vocab.edge_groups:
                unknown.append(key)
                continue
            if not isinstance(value, list):
                shape_errors.append((key, f"expected a list, got {type(value).__name__}"))
                continue
            for index, record in enumerate(value):
                where = f"{key}[{index}]"
                if not isinstance(record, dict):
                    shape_errors.append((where, f"expected a mapping, got {type(record).__name__}"))
                    continue
                if key in vocab.node_groups:
                    class_name = vocab.node_groups[key]
                    node_records.append((where, class_name, record))
                    identity_records.append((where, class_name, raw[key][index]))
                    node_id = record.get("id")
                    if not isinstance(node_id, str) or not node_id.strip():
                        shape_errors.append((where, "node needs a nonempty string id; mint ids before linting"))
                        continue
                    # Validate every record; index only the first duplicate for walking.
                    nodes.setdefault(node_id, (key, class_name, record))
                else:
                    edges.append((key, vocab.edge_groups[key], record))

        return cls(path=path, raw=raw, nodes=nodes, edges=edges, unknown_keys=unknown,
                   node_records=node_records, shape_errors=shape_errors,
                   prefix_errors=prefix_errors, identity_records=identity_records)

    def relationships(self, vocab: Vocabulary) -> list[Relationship]:
        """Normalize reified edges and schema-declared inline relationships."""
        links = []
        for group, class_name, edge in self.edges:
            subject, obj = edge.get("subject"), edge.get("object")
            predicate = vocab.predicate_uri(edge.get("predicate") or vocab.edge_predicates.get(class_name))
            if isinstance(subject, str) and isinstance(obj, str) and predicate:
                links.append(Relationship(subject, predicate, obj, group, True))
        for node_id, (group, class_name, node) in self.nodes.items():
            for slot, predicate in vocab.relationship_slots.get(class_name, {}).items():
                value = node.get(slot)
                for obj in value if isinstance(value, list) else [value]:
                    if isinstance(obj, str):
                        links.append(Relationship(node_id, predicate, obj,
                                                  f"{group}[{node_id}].{slot}", False))
            # A citation target is a relationship inside an inline value object.
            # Follow it for reference checks and reachability, never as evidence.
            if class_name == "Paragraph":
                slot = vocab.schema.induced_slot("target_id", "CitationOccurrence")
                predicate = vocab.schema.expand_curie(str(slot.slot_uri or vocab.schema.get_uri(slot)))
                for i, citation in enumerate(node.get("citations") or []):
                    target = citation.get("target_id") if isinstance(citation, dict) else None
                    if isinstance(target, str):
                        links.append(Relationship(node_id, predicate, target,
                            f"{group}[{node_id}].citations[{i}].target_id", False))
        return links

    def class_of(self, node_id: str) -> str | None:
        entry = self.nodes.get(node_id)
        return entry[1] if entry else None

    def out_edges(self, subject: str, group: str | None = None) -> list[dict]:
        return [
            e for g, _, e in self.edges
            if e.get("subject") == subject and (group is None or g == group)
        ]

    def nodes_of_class(self, class_name: str) -> list[tuple[str, dict]]:
        return [(i, n) for i, (_, c, n) in self.nodes.items() if c == class_name]


# ---------------------------------------------------------------------------
# generic checks — run for every modality
# ---------------------------------------------------------------------------
def check_shape(doc: Document, vocab: Vocabulary, rep: Report) -> None:
    """Every top-level key is a known node group, edge group, or meta key.

    An unrecognised group is the document-level equivalent of a hallucinated
    field, and it is worse than one because nothing downstream looks at it: a
    typo (`dataset:` for `datasets:`) or an invented container (`results:`)
    means those nodes are never minted, never validated and never rendered,
    while the document still parses and still looks complete.
    """
    for where, message in doc.shape_errors:
        rep.add("error", "shape", where, message)
    for key in doc.unknown_keys:
        value = doc.raw.get(key)
        hint = ""
        if isinstance(value, list) and any(isinstance(v, dict) and "id" in v for v in value):
            n = sum(1 for v in value if isinstance(v, dict) and "id" in v)
            hint = (f" It holds {n} node(s) with ids, which will never be validated "
                    f"or minted under this key.")
        rep.add("error", "shape", key,
                f"unknown top-level key `{key}`.{hint}",
                "Node lists must use a known group key (datasets:, gene_sets:, ...) and "
                "edge lists a known edge key (was_generated_by_edges:, ...).")


def check_nodes(doc: Document, validator, rep: Report) -> None:
    """Each node validates against its class, in closed mode.

    This is the hallucinated-field check, and the reason the linter exists at
    all is that it cannot be run on a whole document — only per node against a
    named class, which requires knowing the group-key map above.
    """
    for where, class_name, node in doc.node_records:
        for result in validator.validate(node, class_name).results:
            rep.add("error", "nodes", where,
                    f"{class_name}: {result.message}")


def check_edges(doc: Document, validator, rep: Report) -> None:
    """Each edge validates against its Edge class, in closed mode.

    Catches invented fields on an edge. Does NOT catch a bogus predicate or
    mistyped endpoints — those are `check_predicates` and `check_endpoints`,
    because the schema does not constrain them.
    """
    for group, class_name, edge in doc.edges:
        where = f"{group}[{edge.get('subject')} -> {edge.get('object')}]"
        for result in validator.validate(edge, class_name).results:
            rep.add("error", "edges", where, f"{class_name}: {result.message}")


def check_predicates(doc: Document, vocab: Vocabulary, rep: Report) -> None:
    """An edge's predicate matches the one its Edge class declares.

    WARNING, not error, by deliberate choice: the schema states the expected
    predicate only as an `ifabsent` DEFAULT, so a document that omits the
    predicate is legal and a document that disagrees is merely suspicious
    rather than provably wrong. Worth reporting anyway — a made-up predicate
    like `prov:totallyMadeUpPredicate` passes `linkml-validate` clean, so this
    is the only thing standing between a hallucinated relationship type and a
    document that looks entirely valid.
    """
    for group, class_name, edge in doc.edges:
        expected = vocab.edge_predicates.get(class_name)
        actual = edge.get("predicate")
        if expected is None or actual is None or vocab.predicate_uri(actual) == vocab.predicate_uri(expected):
            continue
        rep.add("warning", "predicates",
                f"{group}[{edge.get('subject')} -> {edge.get('object')}]",
                f"predicate is {actual!r}, but {class_name} declares {expected!r}")


def check_endpoints(doc: Document, vocab: Vocabulary, rep: Report) -> None:
    """Edge subjects and objects are nodes of the classes the edge allows.

    dapper.yaml types both ends as bare `uriorcurie` and records the real
    constraint only in prose on the Edge class, so without this a
    `prov:wasGeneratedBy` edge from a Dataset to an Award is accepted.
    Endpoint existence is checked for every reified edge. Endpoint types come
    from profiles.yaml and also apply to equivalent inline links to local nodes.
    """
    for group, class_name, edge in doc.edges:
        # `or {}` rather than skipping: the both-ends-present rule below is
        # universal, so it must run even for an edge class with no declared
        # endpoint types.
        spec = vocab.edge_endpoints.get(class_name) or {}
        for end in ("subject", "object"):
            node_id = edge.get(end)

            # A one-ended edge connects nothing to nothing, so it contributes no
            # provenance and trips no other check — silently inert. The schema
            # marks neither end `required`, so nothing else catches it either.
            if node_id is None or not str(node_id).strip():
                rep.add("error", "endpoints", f"{group}[{end}]",
                        f"{class_name} has no {end} — an edge needs both ends")
                continue

            actual = doc.class_of(str(node_id))
            if actual is None:
                # An endpoint resolving to nothing. `check_refs` covers the
                # `dapper:`-shaped ones, so defer those and report the rest here
                # — otherwise a fabricated prefix escapes both checks, and
                # `uriorcurie` is a bare string that accepts anything.
                if digest_of(str(node_id)) is None:
                    rep.add("error", "endpoints", f"{group}[{end}={node_id}]",
                            f"{class_name}.{end} resolves to no node in this document",
                            "An edge endpoint must be the id of a node defined here.")
                continue
            allowed = spec.get(end)
            if not allowed:
                continue
            if not any(c in vocab.schema.class_ancestors(actual) for c in allowed):
                rep.add("error", "endpoints", f"{group}[{end}={node_id}]",
                        f"{class_name}.{end} is a {actual}, but must be one of "
                        f"{', '.join(allowed)}")

    # Apply the same types to inline relationships when their targets are local.
    # External inline references (e.g. an ORCID) remain allowed; DAPPER references
    # to absent local records are reported by check_refs.
    by_predicate = {vocab.predicate_uri(vocab.edge_predicates.get(name)): spec
                    for name, spec in vocab.edge_endpoints.items()}
    for link in doc.relationships(vocab):
        if link.reified:
            continue
        spec = by_predicate.get(link.predicate) or {}
        for end in ("subject", "object"):
            actual = doc.class_of(getattr(link, end))
            allowed = spec.get(end)
            if actual is not None and allowed and not any(
                    c in vocab.schema.class_ancestors(actual) for c in allowed):
                rep.add("error", "endpoints", link.where,
                        f"inline relationship {end} is a {actual}, but must be one of "
                        f"{', '.join(allowed)}")


def check_relationship_ranges(doc: Document, sv, rep: Report) -> None:
    """Resolve class-valued references locally, accepting subclasses of the range.

    JSON Schema checks the identifier string, not the class of its target.
    External identifiers remain allowed; check_refs handles missing DAPPER nodes.
    """
    classes = sv.all_classes()
    for _, class_name, node in doc.node_records:
        for slot in sv.class_induced_slots(class_name):
            if slot.is_a != "relationship" or slot.range not in classes:
                continue
            value = node.get(slot.name)
            for ref in value if isinstance(value, list) else [value]:
                actual = doc.class_of(ref) if isinstance(ref, str) else None
                if actual is not None and slot.range not in sv.class_ancestors(actual):
                    rep.add("error", "endpoints", f"{node.get('id')}.{slot.name}",
                            f"reference is a {actual}, but must be {slot.range} or a subclass")


def check_gmt_files(doc: Document, rep: Report) -> None:
    """A supplied GMT link names a local content-addressed File, not a checksum."""
    for node_id, (_, class_name, node) in doc.nodes.items():
        if class_name not in {"GeneSet", "GeneSetCollection"}:
            continue
        for field in ("has_gmt_file", "in_gmt_file"):
            ref = node.get(field)
            if ref is None:
                continue
            if not isinstance(ref, str) or digest_of(ref) is None or doc.class_of(ref) is None:
                rep.add("error", "gmt-file", f"{node_id}.{field}",
                        "must reference a File or C2M2File record's DAPPER identifier in this document",
                        "Use dapper:File.<digest> (or a subclass/full URI), not a bare checksum or file URL.")


def check_gene_set_counts(doc: Document, rep: Report) -> None:
    """Check reported counts only where a complete membership list is supplied."""
    for node_id, (_, class_name, node) in doc.nodes.items():
        if class_name not in {"GeneSet", "GeneSetCollection"}:
            continue
        count_field = "n_sets" if class_name == "GeneSetCollection" else "n_genes"

        def compare(field, actual):
            if isinstance(node.get(field), int) and node[field] != actual:
                rep.add("error", "gene-set-counts", f"{node_id}.{field}",
                        f"reports {node[field]}, but supplied membership gives {actual}")

        if isinstance(node.get(count_field), int):
            compare("n_members", node[count_field])
        members = node.get("members")
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            continue
        if len(set(members)) != len(members):
            rep.add("error", "gene-set-counts", f"{node_id}.members", "members must be distinct")
        compare(count_field, len(set(members)))
        compare("n_members", len(set(members)))
        if class_name == "GeneSetCollection":
            genes = set()
            for ref in members:
                entry = doc.nodes.get(ref)
                gene_members = entry[2].get("members") if entry and entry[1] == "GeneSet" else None
                if not isinstance(gene_members, list) or not all(isinstance(g, str) for g in gene_members):
                    break  # Unknown membership cannot establish a union size.
                genes.update(gene_members)
            else:
                compare("n_genes", len(genes))


def check_gene_set_membership(doc: Document, rep: Report) -> None:
    """Optional inverse links must agree with explicitly supplied forward lists."""
    for gene_id, gene in doc.nodes_of_class("GeneSet"):
        parents = gene.get("in_gene_set_collection")
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            continue  # Schema validation handles malformed values; omission is allowed.
        if len(set(parents)) != len(parents):
            rep.add("error", "gene-set-membership", f"{gene_id}.in_gene_set_collection",
                    "collection references must be distinct")
        for collection_id in parents:
            entry = doc.nodes.get(collection_id)
            if not entry or entry[1] != "GeneSetCollection":
                continue  # Reference and range checks report missing/mistyped targets.
            members = entry[2].get("members")
            if isinstance(members, list) and gene_id not in members:
                rep.add("error", "gene-set-membership", f"{gene_id}.in_gene_set_collection",
                        f"names {collection_id}, whose complete members list omits this GeneSet")

    for collection_id, collection in doc.nodes_of_class("GeneSetCollection"):
        members = collection.get("members")
        if not isinstance(members, list):
            continue
        for gene_id in members:
            entry = doc.nodes.get(gene_id) if isinstance(gene_id, str) else None
            if not entry or entry[1] != "GeneSet":
                continue
            parents = entry[2].get("in_gene_set_collection")
            if isinstance(parents, list) and collection_id not in parents:
                rep.add("error", "gene-set-membership", f"{collection_id}.members",
                        f"names {gene_id}, whose supplied in_gene_set_collection list omits this collection")


def check_refs(doc: Document, vocab: Vocabulary, rep: Report) -> None:
    """DAPPER identifiers in relationship positions must resolve locally.

    The document is meant to be self-contained: the end result plus all of its
    provenance. A reference to a node that is not present means either a typo
    or provenance that was left behind in another file, and in both cases the
    trace stops there. Generalises the referential-integrity pass in
    check_provenance_trace.py.

    Only relationship slots and edge endpoints are scanned, never literals.
    Only `dapper:{Class}.{digest}` strings require local inline targets. The
    `dapper:` prefix is shared with the schema's OWN term namespace, so
    `predicate: dapper:hasDrsObject` is a vocabulary term that names no node
    and must not be chased — matching on the prefix alone reported every
    reified edge in the corpus as dangling. `digest_of` draws exactly this
    distinction (a term's local name can never contain a dot, which is why
    lint_identity.py check 9 guards that property).
    """
    dangling: dict[str, set[str]] = {}
    references = []
    # Inspect endpoints even when another field makes an edge malformed.
    for group, _, edge in doc.edges:
        references.extend((edge.get(end), f"{group}.{end}") for end in ("subject", "object"))
    references.extend((link.object, link.where) for link in doc.relationships(vocab) if not link.reified)
    for ref, where in references:
        if isinstance(ref, str) and digest_of(ref) is not None and ref not in doc.nodes:
            dangling.setdefault(ref, set()).add(where)
    for ref, wheres in sorted(dangling.items()):
        rep.add("error", "refs", sorted(wheres)[0],
                f"reference to {ref} does not resolve to any node in this document"
                + (f" (also at {len(wheres) - 1} other location(s))" if len(wheres) > 1 else ""))


def check_duplicate_ids(doc: Document, vocab: Vocabulary, rep: Report) -> None:
    """No identifier appears twice.

    Two nodes sharing an id makes every reference to it ambiguous, so the graph
    cannot be walked reliably.
    """
    seen: dict[str, str] = {}
    for where, _, node in doc.node_records:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            continue
        if node_id in seen:
            rep.add("error", "duplicate-ids", node_id,
                    f"appears in both {seen[node_id]} and {where}")
        seen[node_id] = where


def check_id_class(doc: Document, rep: Report) -> None:
    """A minted id's embedded class name matches the group it is filed under.

    Ids are `dapper:{ClassName}.{digest}`, so the id carries a second,
    independent claim about what the node is. A node sitting under `datasets:`
    with id `dapper:GeneSet.…` is a copy-paste that nothing else notices —
    validation passes (it is checked against Dataset, its group) while every
    consumer that parses the class out of the id disagrees.
    """
    for node_id, (group, class_name, _) in doc.nodes.items():
        if not node_id.startswith("dapper:"):
            continue  # external identifiers (orcid:, ror:) are left alone
        local = node_id.partition(":")[2]
        declared, dot, _digest = local.partition(".")
        if not dot:
            rep.add("error", "id-class", node_id,
                    "malformed DAPPER id — expected `dapper:{ClassName}.{digest}`")
        elif declared != class_name:
            rep.add("error", "id-class", node_id,
                    f"id names class {declared}, but the node is filed under "
                    f"`{group}:` which is {class_name}")


def check_ids_match_content(doc: Document, sv, rep: Report) -> None:
    """Every node's id still hashes to the content beneath it.

    Editing a node without re-minting leaves an id that addresses something
    else, and because the document still parses, validates and renders, nothing
    else notices. Only meaningful once ids are minted, so documents still
    carrying source keys are skipped rather than failed.
    """
    minted = [n for _, _, n in doc.node_records
              if isinstance(n.get("id"), str) and n["id"].startswith("dapper:")]
    if not minted:
        rep.add("warning", "identity", str(doc.path.name),
                "no minted `dapper:` ids — skipping the content-digest check",
                "Mint with `uv run schema/identity/dapper_identity.py assign <file>`.")
        return
    for (where, class_name, node), (_, _, resolved) in zip(doc.identity_records, doc.node_records):
        node_id = node.get("id")
        canonical_id = resolved.get("id")
        if not isinstance(canonical_id, str) or not canonical_id.startswith("dapper:"):
            continue
        expected = compute_id({k: v for k, v in node.items() if k != "id"},
                              class_name, sv, self_id=node_id)
        if canonical_id != expected:
            rep.add("error", "identity", where,
                    f"content hashes to {expected} — the id addresses different content",
                    "Re-mint with `uv run schema/identity/dapper_identity.py assign <file>`.")


# ---------------------------------------------------------------------------
# profile checks — per end modality
# ---------------------------------------------------------------------------
def _terminal_ids(doc: Document, profile: dict, vocab: Vocabulary) -> list[str]:
    """End results exclude resources consumed or derived from in this graph."""
    upstream_predicates = {vocab.predicate_uri(p) for p in (
        "prov:used", "prov:wasDerivedFrom", "prov:hadMember")}
    upstream = {link.object for link in doc.relationships(vocab)
                if link.predicate in upstream_predicates}
    # Inverse membership also identifies contained sets, even when the
    # collection's complete members list has not been enumerated.
    upstream.update(link.subject for link in doc.relationships(vocab)
                    if link.predicate == vocab.predicate_uri("dapper:inGeneSetCollection")
                    and doc.class_of(link.object) == "GeneSetCollection")
    classes = profile["terminal"]["class"]
    if isinstance(classes, str):
        classes = [classes]
    return [i for cls in classes for i, _ in doc.nodes_of_class(cls) if i not in upstream]


def check_terminal(doc: Document, profile: dict, vocab: Vocabulary, rep: Report) -> list[str]:
    """The document holds the expected number of end-result nodes."""
    spec = profile["terminal"]
    class_name = spec["class"]
    found = _terminal_ids(doc, profile, vocab)
    low, high = spec.get("min"), spec.get("max")

    if low is not None and len(found) < low:
        rep.add("error", "terminal", str(doc.path.name),
                f"found {len(found)} terminal {class_name} node(s), expected at least {low}",
                f"A {profile['title']} document is built around its {class_name}.")
    if high is not None and len(found) > high:
        rep.add("error", "terminal", str(doc.path.name),
                f"found {len(found)} terminal {class_name} node(s), expected at most {high}: "
                + ", ".join(found),
                "One document per instantiation — with more than one end result in a "
                "file, the provenance below cannot be attributed unambiguously.")
    return found


def check_required_edges(doc: Document, profile: dict, terminals: list[str],
                         vocab: Vocabulary, rep: Report) -> None:
    """Each end-result node carries the provenance edges its modality requires.

    The schema makes these edges individually optional — it has to, since the
    same classes are reused across modalities — so a bottom-line result with no
    generating Activity validates perfectly while carrying no provenance at all.
    """
    links = doc.relationships(vocab)
    for spec in profile.get("required_edges") or []:
        choices = []
        labels = []
        for option in spec.get("any_of", [spec]):
            group = option["group"]
            predicate = vocab.predicate_uri(vocab.edge_predicates.get(vocab.edge_groups[group]))
            want_classes = option.get("object_class")
            if isinstance(want_classes, str):
                want_classes = [want_classes]
            choices.append((predicate, want_classes))
            target = f" to {'/'.join(want_classes)}" if want_classes else ""
            labels.append(f"`{group}`{target}")
        minimum = spec.get("min", 1)
        severity = spec.get("severity", "error")
        for terminal in terminals:
            matching = {link.object for link in links
                        if link.subject == terminal
                        and any(link.predicate == predicate
                                and (want_classes is None or doc.class_of(link.object) in want_classes)
                                for predicate, want_classes in choices)}
            if len(matching) < minimum:
                rep.add(severity, "required-edges", terminal,
                        f"has {len(matching)} relationship(s) via {' or '.join(labels)} "
                        f"(reified or inline), expected "
                        f"at least {minimum}",
                        spec.get("why", ""))


def check_activities_have_inputs(doc: Document, profile: dict, vocab: Vocabulary, rep: Report) -> None:
    """Every Activity declares at least one input it used.

    An Activity with no inputs is either a genuine raw-data producer or a step
    whose inputs were dropped on the way in. The second is the common case when
    a document is assembled by hand, and it is invisible otherwise: the
    Activity is valid, the result still points at it, and the trace simply
    stops one hop early.
    """
    severity = profile.get("activities_require_inputs")
    if severity not in SEVERITIES:
        return
    consumers = {link.subject for link in doc.relationships(vocab)
                 if link.predicate == vocab.predicate_uri("prov:used")}
    for activity_id, node in doc.nodes_of_class("Activity"):
        if activity_id in consumers:
            continue
        rep.add(severity, "activity-inputs", activity_id,
                f"Activity {node.get('name', '')!r} declares no inputs",
                "Add `used_edges` naming what it consumed, or confirm it is a "
                "genuine raw-data producer.")


def check_reachability(doc: Document, profile: dict, terminals: list[str],
                       vocab: Vocabulary, rep: Report) -> None:
    """All provenance hangs off the end result, and nothing dangles.

    The traversal from each end result follows three kinds of step, and all
    three are needed to avoid false alarms:

      1. subject -> object along any edge. This walks provenance BACKWARDS by
         construction (`result -wasGeneratedBy-> activity -used-> input`), so
         one direction covers the whole input chain.
      2. REVERSE `was_generated_by`, from a reached Activity to everything else
         it produced. Without this, a run's other outputs look unreachable:
         in example_geneset_graph.yaml the six materialised C2M2 files
         (geneset.tsv, geneset.full.tsv, ...) point AT the activity, so they
         are siblings of the gene set rather than its descendants, and a
         subject -> object walk alone reported all six as disconnected.
      3. inline references. Not every link is a reified edge — a curated
         CellState names its curator through `was_attributed_to`, and some
         documents hang an AgenticWorkspace off an inline slot.

    Two things come out of the walk:

      * unreachable nodes — a node the end result cannot reach is not
        provenance for it. Either an id is wrong, or it is leftover content
        that belongs in another file. This is the check that catches a
        hallucinated extra object: it validates, it looks plausible, and it is
        connected to nothing.
      * raw sources — a node nothing generated is where the trace bottoms out.
        Reported for the record, since "the provenance is complete" ultimately
        means these are the real inputs.
    """
    if not terminals:
        return

    # Normalize both representations before walking: literal fields never add links.
    outgoing: dict[str, list[str]] = {}
    outputs_of: dict[str, list[str]] = {}
    generated = set()
    reverse_context = {vocab.predicate_uri(p) for p in profile.get("reverse_context_predicates", [])}
    reverse_context.add(vocab.predicate_uri("dapper:inGeneSetCollection"))
    for link in doc.relationships(vocab):
        outgoing.setdefault(link.subject, []).append(link.object)
        if link.predicate in reverse_context:
            outgoing.setdefault(link.object, []).append(link.subject)
        if link.predicate == vocab.predicate_uri("prov:wasGeneratedBy"):
            outputs_of.setdefault(link.object, []).append(link.subject)
            generated.add(link.subject)

    reached: set[str] = set()
    worklist = list(terminals)
    while worklist:
        node_id = worklist.pop()
        if node_id in reached or node_id not in doc.nodes:
            continue
        reached.add(node_id)

        worklist.extend(outgoing.get(node_id, []))
        worklist.extend(outputs_of.get(node_id, []))

    unreachable = sorted(set(doc.nodes) - reached)
    for node_id in unreachable:
        group, class_name, node = doc.nodes[node_id]
        rep.add("error", "reachability", f"{group}[{node_id}]",
                f"{class_name} {node.get('name', '')!r} is not reachable from the "
                f"{profile['terminal']['class']}",
                "Every node in the document should be provenance for the end result. "
                "Either a reference is wrong, or this node belongs in another file.")

    rep.counts["raw_sources"] = len([i for i in doc.nodes if i not in generated])


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def detect_profile(doc: Document, vocab: Vocabulary) -> tuple[str | None, list[str]]:
    """Pick the profile of terminal resources, excluding upstream inputs."""
    matches = [
        name for name, profile in vocab.profiles.items()
        if _terminal_ids(doc, profile, vocab)
    ]
    return (matches[0] if len(matches) == 1 else None), matches


def lint(path: Path, vocab: Vocabulary, sv, validator,
         profile_name: str | None = None) -> Report:
    rep = Report(path=path)
    try:
        doc = Document.load(path, vocab)
    except (OSError, yaml.YAMLError) as exc:
        rep.add("error", "shape", path.name, f"cannot read YAML document: {exc}")
        return rep
    rep.counts.update(nodes=len(doc.nodes), edges=len(doc.edges))

    # Generic checks run regardless of modality, and run even when no profile
    # matches — a document with hallucinated fields should report them rather
    # than bail out because its end result is unrecognised.
    check_shape(doc, vocab, rep)
    for where, message in doc.prefix_errors:
        rep.add("error", "prefixes", where, message)
    check_nodes(doc, validator, rep)
    check_edges(doc, validator, rep)
    check_predicates(doc, vocab, rep)
    check_endpoints(doc, vocab, rep)
    check_relationship_ranges(doc, sv, rep)
    check_gmt_files(doc, rep)
    check_gene_set_counts(doc, rep)
    check_gene_set_membership(doc, rep)
    check_refs(doc, vocab, rep)
    for where, problem in check_scientific_content(
            {nid: (cls, node) for nid, (_, cls, node) in doc.nodes.items()}):
        rep.add("error", "scientific-content", where, problem)
    check_duplicate_ids(doc, vocab, rep)
    check_id_class(doc, rep)
    check_ids_match_content(doc, sv, rep)

    if profile_name is None:
        profile_name, candidates = detect_profile(doc, vocab)
        if profile_name is None:
            known = ", ".join(sorted(vocab.profiles))
            detail = (f"matches {len(candidates)} profiles ({', '.join(sorted(candidates))})"
                      if candidates else "matches no profile")
            rep.add("error", "profile", str(path.name),
                    f"cannot determine the end modality — {detail}",
                    f"Pass --profile with one of: {known}")
            return rep
    elif profile_name not in vocab.profiles:
        raise SystemExit(f"unknown profile {profile_name!r}; "
                         f"known: {', '.join(sorted(vocab.profiles))}")

    rep.profile = profile_name
    profile = vocab.profiles[profile_name]

    terminals = check_terminal(doc, profile, vocab, rep)
    check_required_edges(doc, profile, terminals, vocab, rep)
    check_activities_have_inputs(doc, profile, vocab, rep)
    check_reachability(doc, profile, terminals, vocab, rep)
    return rep


def build_validator(schema_path: Path):
    from linkml.validator import Validator
    from linkml.validator.plugins import JsonschemaValidationPlugin

    # closed=True IS THE HALLUCINATED-FIELD CHECK. The default is False, and in
    # that mode a Dataset carrying invented `p_value_threshold` and `confidence`
    # fields validates clean — measured, not theorised. The CLI sets this; the
    # in-process API does not.
    return Validator(str(schema_path),
                     validation_plugins=[JsonschemaValidationPlugin(closed=True)])


def print_report(rep: Report, vocab: Vocabulary, quiet: bool = False) -> None:
    title = vocab.profiles.get(rep.profile, {}).get("title", rep.profile or "unknown")
    head = (f"{rep.path}: {rep.counts.get('nodes', 0)} node(s), "
            f"{rep.counts.get('edges', 0)} edge(s) [{title}]")
    if "raw_sources" in rep.counts:
        head += f", {rep.counts['raw_sources']} raw source(s)"
    print(head)
    for finding in rep.errors:
        print(finding.render())
    if not quiet:
        for finding in rep.warnings:
            print(finding.render())
    n_err, n_warn = len(rep.errors), len(rep.warnings)
    print(f"  {'FAIL' if n_err else 'PASS'} — {n_err} error(s), {n_warn} warning(s)\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lint a DAPPER end-modality provenance document.")
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--profile", help="end modality to lint against (default: auto-detect)")
    ap.add_argument("--schema", type=Path, default=SCHEMA_PATH,
                    help="DAPPER schema to validate against. Pin this to the release a "
                         "document was minted against when that differs from the repo's "
                         f"current schema (default: {SCHEMA_PATH.relative_to(REPO_ROOT)})")
    ap.add_argument("--list-profiles", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="lint every profile's canonical example")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    ap.add_argument("--quiet", action="store_true", help="only print errors")
    args = ap.parse_args()

    profiles_doc = yaml.safe_load(PROFILES_PATH.read_text())
    sv = load_schema(args.schema)
    vocab = Vocabulary.build(sv, profiles_doc)

    if args.list_profiles:
        print(f"{len(vocab.profiles)} profile(s) in {PROFILES_PATH.relative_to(REPO_ROOT)}:\n")
        for name, profile in sorted(vocab.profiles.items()):
            spec = profile["terminal"]
            bound = f"{spec.get('min')}..{spec.get('max') if spec.get('max') else '*'}"
            print(f"  {name}\n    {profile['title']} — terminal {spec['class']} ({bound})")
            print(f"    canonical example: {profile.get('canonical_example', '—')}")
            for req in profile.get("required_edges") or []:
                groups = " or ".join(option["group"] for option in req.get("any_of", [req]))
                print(f"    requires {groups} ({req.get('severity', 'error')})")
            print()
        return 0

    targets = list(args.files)
    if args.self_test:
        for profile in vocab.profiles.values():
            example = profile.get("canonical_example")
            if example:
                targets.append(REPO_ROOT / example)
    if not targets:
        ap.error("no files given (use --self-test or --list-profiles)")

    validator = build_validator(args.schema)
    failed = 0
    for path in targets:
        if not path.exists():
            print(f"{path}: no such file")
            failed += 1
            continue
        rep = lint(path, vocab, sv, validator, args.profile)
        print_report(rep, vocab, quiet=args.quiet)
        if rep.errors or (args.strict and rep.warnings):
            failed += 1

    print(f"{len(targets) - failed}/{len(targets)} document(s) clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
