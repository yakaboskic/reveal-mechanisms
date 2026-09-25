#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "rdflib", "linkml-runtime", "ruamel.yaml"]
# ///
"""DAPPER-ID-1 — computed content identifiers for DAPPER instances.

Mints `dapper:{ClassName}.{digest}` for any instance of a `HashableNode`
subclass. The digest covers the slots marked `mixins: [hashable]` in
schema/dapper.yaml and nothing else, so re-running a pipeline, re-signing a
nanopublication, or mirroring an artifact never changes what it is.

The full profile is in the sibling README.md. Read it before changing anything
in here: every step below is load-bearing and several are counter-intuitive.

This is deliberately NOT a Trusty URI. schema/trusty-identifiers.md is explicit
that `RA` must not be reimplemented or approximated; a real `RA` code comes from
nanopub-java at publication time and lands in `Nanopublication.trusty_uri`.

Usage:
    # assign ids to every node in a graph document, in dependency order
    uv run schema/identity/dapper_identity.py assign schema/examples/example_geneset_graph.yaml

    # recompute and compare, without writing
    uv run schema/identity/dapper_identity.py verify schema/examples/example_geneset_graph.yaml

    # digest a single instance of a known class
    uv run schema/identity/dapper_identity.py digest schema/examples/example_dataset.yaml --class Dataset

    # replay the permanent test vectors (CI)
    uv run schema/identity/dapper_identity.py verify-vectors

Importable:
    from dapper_identity import compute_digest, compute_id, assign_ids, verify
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.compare import to_canonical_graph

PROFILE = "DAPPER-ID-1"
CURIE_PREFIX = "dapper"

HERE = Path(__file__).parent
SCHEMA_PATH = HERE.parent / "dapper.yaml"
VECTORS_PATH = HERE / "test_vectors.json"

# The subject is fixed so an instance's own id can never feed its own digest.
# Trusty URI solves the same circularity by blanking self-references; we avoid
# it by construction, since `id` is excluded and the subject is a constant.
SELF = URIRef("urn:dapper:self")

# Placeholder substituted for a node's own identifier wherever it appears inside
# its own content. Some fields embed the id — dig.geneset writes
# `dcc_url: urn:geneset:402cf4a1…` on the gene set that id names — so without
# this the digest would depend on the id it is trying to produce. Trusty URI
# solves the same circularity by replacing the artifact code with a space before
# hashing; this is the same trick with a clearer token.
SELF_REF = "urn:dapper:self-reference"

# Predicates are derived from the SLOT NAME, not `slot_uri`. Two reasons, both
# measured against the real schema:
#   1. 133 of 243 hashable slots declare no `slot_uri` at all.
#   2. `dcc_url` and `drc_url` BOTH map to `schema:url` in Set, GeneSet,
#      Activity and C2M2File. Keying on slot_uri would make those two fields
#      indistinguishable, so swapping their values would not change the digest.
# Slot names are unique within a class by construction, and decoupling the
# digest from ontology mappings means re-mapping a slot to a better URI later
# does not churn every identifier in the corpus.
SLOT_NS = "urn:dapper:slot:"
CLASS_PRED = URIRef("urn:dapper:class")

# Reject pathological inputs rather than canonicalizing forever. RDFC-1.0
# documents blank-node graphs with super-polynomial behaviour; we emit no blank
# nodes, but the limit is part of the published profile.
MAX_TRIPLES = 10_000

# Node-list key -> class, matching how the graph examples group their nodes.
# Covers EVERY concrete HashableNode class, so any DAPPER document mints
# completely — an incomplete map silently skips nodes (example_graph.yaml
# carried 12 unminted groups until this was filled in). Written out rather
# than derived: naive pluralisation produced `activitys` and would have
# renamed a key the examples already use.
DOC_GROUPS = {
    "files": "File",
    "c2m2_files": "C2M2File",
    "activities": "Activity",
    "gene_sets": "GeneSet",
    "gene_set_collections": "GeneSetCollection",
    "gene_programs": "GeneProgram",
    "cell_states": "CellState",
    "datasets": "Dataset",
    "sets": "Set",
    "mechanistic_models": "MechanisticModel",
    "propositions": "Proposition",
    "claims": "Claim",
    "scientific_accounts": "ScientificAccount",
    "questions": "Question",
    "knowledge_gaps": "KnowledgeGap",
    "paragraphs": "Paragraph",
    "claim_scores": "ClaimScore",
    "causal_steps": "CausalStep",
    "mechanisms": "Mechanism",
    "evidence_items": "EvidenceItem",
    "nanopublications": "Nanopublication",
    "nanopub_assertions": "NanopubAssertion",
    "nanopub_provenances": "NanopubProvenance",
    "nanopub_publication_infos": "NanopubPublicationInfo",
    "nanopub_signatures": "NanopubSignature",
    "agentic_workspaces": "AgenticWorkspace",
    "persons": "Person",
    "organizations": "Organization",
    "awards": "Award",
    "publications": "Publication",
    "licenses": "License",
    "lineage_steps": "LineageStep",
    "recommended_citations": "RecommendedCitation",
    "drs_objects": "DrsObject",
    "data_use_terms": "DataUseTerm",
    "ro_crate_packages": "RoCratePackage",
    "bio_compute_objects": "BioComputeObject",
    "mirror_provenances": "MirrorProvenance",
}


# --------------------------------------------------------------------------
# digest primitives
# --------------------------------------------------------------------------
def sha512t24u(blob: bytes) -> str:
    """GA4GH VRS truncated digest: SHA-512, leftmost 24 bytes, base64url.

    24 bytes is divisible by 3, so base64 emits no `=` padding and the result is
    a fixed 32 URL-safe characters with no stripping logic. Byte-for-byte the
    same function VRS specifies, so `sha512t24u(b"ACGT")` is
    'aKF498dAxcJAqme6QYQ7EZ07-fiw8Kw2' here too.
    """
    return base64.urlsafe_b64encode(hashlib.sha512(blob).digest()[:24]).decode("ascii")


DAPPER_NAMESPACE = "https://broadinstitute.github.io/dapper/ns#"


def compact_identifier(identifier: str) -> str:
    """Recognize the full URI spelling of DAPPER identifiers and vocabulary."""
    if isinstance(identifier, str) and identifier.startswith(DAPPER_NAMESPACE):
        return f"{CURIE_PREFIX}:" + identifier[len(DAPPER_NAMESPACE):]
    return identifier


def digest_of(identifier: str) -> str | None:
    """Extract a bare digest from either the CURIE or full DAPPER URI."""
    identifier = compact_identifier(identifier)
    if not isinstance(identifier, str) or not identifier.startswith(f"{CURIE_PREFIX}:"):
        return None
    _, _, rest = identifier.partition(":")
    _, dot, digest = rest.partition(".")
    return digest if dot and digest else None


def _substitute(value: Any) -> Any:
    """Replace a reference to another DAPPER node with that node's bare digest.

    VRS requires nested identifiable objects be replaced by their digest, and
    uses the BARE digest rather than the full CURIE (`"location":"wIlaGyk..."`,
    not `"ga4gh:SL.wIlaGyk..."`). We do the same. Because a `dapper:` id already
    contains its digest, this needs no document context — which is what makes an
    instance hash identically standalone and inside a graph document.

    External identifiers (MONDO:, orcid:, PMID:, s3://, https://) are NOT
    content-addressed and pass through unchanged.
    """
    d = digest_of(value)
    return d if d is not None else value


# --------------------------------------------------------------------------
# schema access
# --------------------------------------------------------------------------
def load_schema(path: Path | str = SCHEMA_PATH):
    from linkml_runtime.utils.schemaview import SchemaView

    return SchemaView(str(path))


def hashable_slot_names(sv, class_name: str) -> list[str]:
    """Slot names that constitute identity for this class, `id` excluded.

    `id` is the OUTPUT of the digest, so it can never be an input.
    """
    id_slot = sv.get_identifier_slot(class_name)
    id_name = id_slot.name if id_slot is not None else "id"
    names = []
    for slot in sv.class_induced_slots(class_name):
        if slot.name == id_name:
            continue
        if "hashable" in (slot.mixins or []):
            names.append(slot.name)
    return sorted(names)


def unmarked_slots(sv, class_name: str) -> list[str]:
    """Slots carrying neither marker — a lint failure, never silently allowed."""
    id_slot = sv.get_identifier_slot(class_name)
    id_name = id_slot.name if id_slot is not None else "id"
    out = []
    for slot in sv.class_induced_slots(class_name):
        if slot.name in (id_name, "name"):
            continue
        mixins = slot.mixins or []
        if "hashable" not in mixins and "unhashable" not in mixins:
            out.append(slot.name)
    return sorted(out)


# --------------------------------------------------------------------------
# the digest itself
# --------------------------------------------------------------------------
def _blank_self(value: Any, self_id: str | None, *, substring: bool = True) -> Any:
    """Neutralise a node's own identifier inside its own content.

    MUST mirror the rewrite rule exactly. A slot that is never rewritten must
    never be blanked either: `dcc_url: urn:geneset:402cf4a1…` holds the upstream
    system's own URN, which survives minting untouched. Blanking it by substring
    made the digest depend on whether the id had already been minted, so the
    same document hashed differently on a second run. Caught by the idempotence
    check, not by anything else.
    """
    if not self_id:
        return value
    if isinstance(value, str):
        if substring:
            return value.replace(self_id, SELF_REF)
        return SELF_REF if value == self_id else value
    if isinstance(value, list):
        return [_blank_self(v, self_id, substring=substring) for v in value]
    if isinstance(value, dict):
        return {k: _blank_self(v, self_id, substring=substring) for k, v in value.items()}
    return value


def _graph_for(instance: dict, class_name: str, sv, self_id: str | None = None) -> Graph:
    g = Graph()
    # The class name is part of the hashed content, exactly as VRS includes
    # "type". Belt and braces: two structurally identical instances of
    # different classes must not collide even before the prefix is applied.
    g.add((SELF, CLASS_PRED, Literal(class_name)))

    for slot_name in hashable_slot_names(sv, class_name):
        raw_value = instance.get(slot_name)
        # Blank self-references only where a rewrite could land, so the digest
        # cannot depend on whether minting has already happened. The three
        # branches mirror _rewrite_node's three rules EXACTLY — see the note in
        # _blank_self for why any divergence is a bug rather than a nuance.
        if _is_reference_slot(sv, class_name, slot_name) or isinstance(raw_value, (list, dict)):
            value = _blank_self(raw_value, self_id, substring=False)
        elif _looks_like_prose(raw_value):
            value = _blank_self(raw_value, self_id, substring=True)
        else:
            # A literal scalar is never rewritten, so it must never be blanked —
            # not even when it happens to equal the id. A Person whose node id
            # is their ORCID carries that same ORCID in `orcid`, and blanking it
            # made the digest depend on whether minting had already run: the
            # node hashed one way before and another way after, so a freshly
            # minted file failed its own verify.
            value = raw_value
        if value is None:
            continue
        if isinstance(value, list):
            # RDF triples are a SET, so emitting each element against the same
            # predicate would silently discard order. Author order is
            # citation-bearing (DataCite creator order), so the index goes into
            # the predicate. Reordering a creator list therefore DOES change the
            # identifier, which is the intended semantics.
            for i, item in enumerate(value):
                if item is None:
                    continue
                g.add((SELF, URIRef(f"{SLOT_NS}{slot_name}[{i}]"), _literal(item)))
        else:
            g.add((SELF, URIRef(f"{SLOT_NS}{slot_name}"), _literal(value)))
    return g


def _literal(value: Any) -> Literal:
    value = _substitute(value)
    if isinstance(value, (dict, list)):
        # Inline structured values are canonicalized as sorted-key JSON so their
        # key order cannot leak into identity.
        return Literal(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return Literal(value)


def canonical_bytes(g: Graph) -> bytes:
    """Canonicalize and serialize deterministically.

    THE SORT ON THE NEXT-TO-LAST LINE IS LOAD-BEARING. `to_canonical_graph`
    canonicalizes blank-node labels but does NOT order its serialized output, so
    without sorting, the same instance built in a different insertion order
    yields a DIFFERENT digest. That was observed, not theorised. Removing the
    sort makes identifiers silently non-reproducible across machines.
    """
    if len(g) > MAX_TRIPLES:
        raise ValueError(f"input exceeds DAPPER-ID-1 limit of {MAX_TRIPLES} triples: {len(g)}")
    canonical = to_canonical_graph(g)
    lines = sorted(line for line in canonical.serialize(format="nt").split("\n") if line.strip())
    return "\n".join(lines).encode("utf-8")


def compute_digest(instance: dict, class_name: str, sv, self_id: str | None = None) -> str:
    """The 32-character digest for one instance.

    `self_id` is the identifier this instance currently carries, if any. Any
    occurrence of it inside the instance's own content is blanked first, so the
    digest cannot depend on the identifier it is being used to compute.
    """
    return sha512t24u(canonical_bytes(_graph_for(instance, class_name, sv, self_id)))


def compute_id(instance: dict, class_name: str, sv, self_id: str | None = None) -> str:
    """The full identifier: `dapper:{ClassName}.{digest}`."""
    return f"{CURIE_PREFIX}:{class_name}.{compute_digest(instance, class_name, sv, self_id)}"


# --------------------------------------------------------------------------
# whole-document assignment
# --------------------------------------------------------------------------
def _iter_nodes(document: dict) -> Iterable[tuple[str, str, dict]]:
    for group, class_name in DOC_GROUPS.items():
        for node in document.get(group) or []:
            yield group, class_name, node


def _is_reference_slot(sv, class_name: str, slot_name: str) -> bool:
    """True if this slot holds a pointer to another node, not a scalar value.

    The schema marks every attribute `is_a: relationship` (a cross-reference) or
    `is_a: literal` (a scalar). That distinction is what tells a DAPPER reference
    apart from an external identifier that merely looks like one.
    """
    try:
        slot = sv.induced_slot(slot_name, class_name)
    except Exception:
        return False
    return slot is not None and slot.is_a == "relationship"


def _rewrite_scalar(value: Any, mapping: dict[str, str], *, substring: bool) -> Any:
    if isinstance(value, str):
        if not substring:
            return mapping.get(value, value)
        for old in sorted(mapping, key=len, reverse=True):
            if old in value:
                value = value.replace(old, mapping[old])
        return value
    if isinstance(value, list):
        return [_rewrite_scalar(v, mapping, substring=substring) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite_scalar(v, mapping, substring=substring) for k, v in value.items()}
    return value


def _looks_like_prose(value: Any) -> bool:
    """Free text that may MENTION an id, as opposed to a value that IS one.

    `Activity.command` reads `reveal enrichment --geneset geneset:402cf4a1…` and
    a regeneration prompt names the gene set it regenerates; those references are
    real and must track a re-mint. An `orcid:` or `ror:` value never contains a
    space, which is what separates the two cases.
    """
    return isinstance(value, str) and (" " in value or "\n" in value)


def _rewrite_node(node: dict, class_name: str, sv, mapping: dict[str, str]) -> dict:
    """Rewrite ids in one node, respecting what each slot actually holds.

    Three rules, and the middle one exists because of a real bug:

      reference slots   exact-match rewrite — these point at other nodes
      literal prose     substring rewrite — an id mentioned inside free text
      literal scalars   LEFT ALONE

    That last rule matters. In example_graph.yaml every Person, Organization and
    BioComputeObject used its EXTERNAL identifier as its node id:

        - id: orcid:0000-0002-1825-0097
          orcid: orcid:0000-0002-1825-0097

    A blanket substring rewrite replaced both, silently turning a real ORCID into
    a DAPPER digest. An ORCID, ROR, BCO id, checksum or file path is not a DAPPER
    reference and must never be rewritten, however much it looks like one.
    """
    out = {}
    for key, value in node.items():
        if key == "id":
            out[key] = value
            continue
        if _is_reference_slot(sv, class_name, key):
            out[key] = _rewrite_scalar(value, mapping, substring=False)
        elif _looks_like_prose(value) or isinstance(value, (list, dict)):
            # lists/dicts may contain prose; recurse with the prose rule but only
            # substitute whole-string matches for their scalar members
            out[key] = _rewrite_scalar(value, mapping, substring=_looks_like_prose(value))
        else:
            out[key] = value
    return out


def _rewrite(value: Any, mapping: dict[str, str]) -> Any:
    """Exact-match rewrite, for values with no slot context (document-level keys).

    This was a SUBSTRING rewrite, which made it the last id-blind rewrite in the
    codebase — the same rule `_rewrite_node` documents as having silently turned
    a real ORCID into a digest. Anything reaching here has no slot to consult, so
    it cannot tell an authoritative external identifier from a reference, and
    substring matching resolves that ambiguity in the destructive direction.

    Exact matching loses nothing. Every document-level key that genuinely needs
    rewriting holds WHOLE ids: edge lists carry `subject`/`object`, and
    `_illustrative` is a list of ids. Checked against every example and both
    converter outputs — no document-level value embeds an id inside a larger
    string.
    """
    return _rewrite_scalar(value, mapping, substring=False)


def unknown_node_groups(document: dict) -> dict[str, int]:
    """Keys holding node lists that DOC_GROUPS does not know, with node counts.

    An unrecognised group name is skipped by minting AND handed to `_rewrite`, so
    its nodes stay unminted while their contents are rewritten anyway. Silence
    was the whole problem: a typo (`gene_set:` for `gene_sets:`), a group name
    from an older schema, or a hand-written guess produced a half-minted document
    and a clean bill of health. The DOC_GROUPS comment records `example_graph.yaml`
    carrying 12 unminted groups for exactly this reason.

    `test_doc_groups_covers_every_concrete_hashable_node` already guards the
    other direction — schema class to map. This guards document to map.

    A node list is a list containing at least one dict with an `id`. Edge lists
    are lists of dicts too, but carry `subject`/`predicate`/`object` and no `id`,
    so they do not trip this.
    """
    found: dict[str, int] = {}
    for key, value in document.items():
        if key in DOC_GROUPS or not isinstance(value, list):
            continue
        count = sum(1 for v in value if isinstance(v, dict) and "id" in v)
        if count:
            found[key] = count
    return found


def _apply(target: Any, rewritten: Any) -> bool:
    """Copy `rewritten` into `target` in place; True if it fit.

    `_rewrite_scalar` is a pure function, so it hands back fresh lists and dicts.
    Assigning one of those over a round-tripped container drops every comment and
    blank line attached to it — two section headers in
    example_geneset_graph.yaml disappeared exactly this way. Since a rewrite only
    ever substitutes scalars, the shape is unchanged and the new values can be
    dropped into the original containers instead. Returns False if the shapes do
    not line up, so the caller can fall back to plain assignment.
    """
    if isinstance(target, list) and isinstance(rewritten, list) and len(target) == len(rewritten):
        pairs: Iterable = enumerate(rewritten)
    elif isinstance(target, dict) and isinstance(rewritten, dict) and set(target) == set(rewritten):
        pairs = rewritten.items()
    else:
        return False
    for key, value in pairs:
        if isinstance(target[key], (list, dict)):
            if not _apply(target[key], value):
                target[key] = value
        elif target[key] != value:
            target[key] = value
    return True


def _referenced_ids(value: Any, candidates: set[str]) -> set[str]:
    """Every known id appearing in `value`, whether as the whole string or inside it."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(c for c in candidates if c in value)
    elif isinstance(value, list):
        for v in value:
            found |= _referenced_ids(v, candidates)
    elif isinstance(value, dict):
        for v in value.values():
            found |= _referenced_ids(v, candidates)
    return found


def assign_ids(document: dict, sv, *, expanded: bool = False,
               preserve_external: bool = False) -> dict[str, str]:
    """Mint ids for every node in a graph document, in dependency order.

    Returns {old_id: new_id}. Mutates `document` in place: node ids are replaced
    and every reference to them anywhere in the document is rewritten.

    URI export uses expanded=True so nested references are hashed in their
    final serialization, and preserve_external=True to retain external IDs.

    Nodes are processed leaf-first over HASHABLE references only. That the graph
    is acyclic under that restriction is not luck — back-references
    (`asserted_in`, `pubinfo_of`, `provenance_of`, `has_signature_target`) are
    marked `unhashable`, which is what breaks the nanopublication cycles.
    `lint_identity.py` re-checks the property so it cannot regress.
    """
    nodes = {node["id"]: (class_name, node) for _, class_name, node in _iter_nodes(document)}

    # Dependency edges: node -> every known id appearing in its hashable slots,
    # including inside prose. Must use the same substring rule as `_rewrite`.
    all_ids = set(nodes)
    deps: dict[str, set[str]] = {}
    for nid, (class_name, node) in nodes.items():
        refs: set[str] = set()
        for slot_name in hashable_slot_names(sv, class_name):
            value = node.get(slot_name)
            # mirror _rewrite_node: a scalar literal is never rewritten, so it
            # never creates a dependency either
            if not _is_reference_slot(sv, class_name, slot_name) \
                    and not _looks_like_prose(value) \
                    and not isinstance(value, (list, dict)):
                continue
            refs |= _referenced_ids(value, all_ids)
        refs.discard(nid)
        deps[nid] = refs

    order: list[str] = []
    state: dict[str, int] = {}

    def visit(nid: str, trail: tuple[str, ...]) -> None:
        if state.get(nid) == 2:
            return
        if state.get(nid) == 1:
            cycle = " -> ".join(trail[trail.index(nid):] + (nid,))
            raise ValueError(
                f"cycle in hashable references, cannot assign ids: {cycle}\n"
                f"Break it by marking the back-reference slot `unhashable`."
            )
        state[nid] = 1
        for dep in sorted(deps[nid]):
            visit(dep, trail + (nid,))
        state[nid] = 2
        order.append(nid)

    for nid in sorted(nodes):
        visit(nid, ())

    mapping: dict[str, str] = {}
    for nid in order:
        if preserve_external and digest_of(nid) is None:
            mapping[nid] = nid
            continue
        class_name, node = nodes[nid]
        resolved = _rewrite_node(node, class_name, sv, mapping)
        resolved.pop("id", None)
        mapping[nid] = compute_id(resolved, class_name, sv, self_id=nid)
        if expanded:
            mapping[nid] = DAPPER_NAMESPACE + mapping[nid].partition(":")[2]

    # rewrite ids and every reference to them, document-wide
    for _, _, node in _iter_nodes(document):
        node["id"] = mapping.get(node["id"], node["id"])
    for key in list(document):
        if key in DOC_GROUPS:
            class_name = DOC_GROUPS[key]
            for node in document[key] or []:
                # Assign back INTO the existing node instead of replacing it in
                # the list, and only where the value actually moved. A
                # round-trip loader hangs comments and formatting off the
                # container objects themselves, so swapping in a fresh dict
                # would discard every comment in the file — which is the whole
                # reason the old text-replacement writer existed.
                for slot, value in _rewrite_node(node, class_name, sv, mapping).items():
                    if slot == "id" or value == node[slot]:
                        continue
                    if not _apply(node[slot], value):
                        node[slot] = value
        else:
            # Edge lists and document-level keys, same rule: in place, so the
            # section comments between them survive.
            rewritten = _rewrite(document[key], mapping)
            if not _apply(document[key], rewritten):
                document[key] = rewritten
    return mapping


def verify(document: dict, sv) -> list[str]:
    """Recompute every node's id; return human-readable mismatches."""
    problems = []
    for _, class_name, node in _iter_nodes(document):
        actual = node.get("id")
        expected = compute_id({k: v for k, v in node.items() if k != "id"},
                              class_name, sv, self_id=actual)
        if compact_identifier(actual) != expected:
            problems.append(f"{class_name}: id is {actual!r}, content hashes to {expected!r}")
    return problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _round_trip_yaml():
    """A YAML round-tripper that preserves comments, quoting and layout.

    `assign` used to write its result by running `str.replace(old, new)` over the
    raw file text, because `yaml.safe_dump` discards the curated comments in
    schema/examples. That made a SECOND rewrite rule, and unlike `_rewrite_node`
    it had no idea what a slot meant, so it rewrote ids that the first rule had
    deliberately left alone. Two demonstrated consequences:

      * a Person whose id was their ORCID had the `orcid` field overwritten with
        the new digest, destroying the only globally resolvable name on the
        record — and `verify` still reported 0 mismatches, because `_blank_self`
        neutralises that field on both sides;
      * a literal scalar embedding a reference (`script_url`) was rewritten on
        disk but not in the digest input, so a freshly minted file failed its own
        `verify` and `assign` only converged on the second run.

    Round-tripping keeps the comments AND writes exactly the document that
    `assign_ids` produced, so there is only one rewrite rule again.
    """
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never re-wrap a scalar; a re-wrap is pure diff noise
    y.indent(mapping=2, sequence=4, offset=2)
    # Round-tripping None emits a bare `key:`, where the examples write an
    # explicit `container_image: null`. Same value either way, but the bare form
    # reads like an oversight and shows up as diff noise on every re-mint.
    y.representer.add_representer(
        type(None),
        lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    return y


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{PROFILE} computed identifiers for DAPPER")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_assign = sub.add_parser("assign", help="mint ids for a graph document, in place")
    p_assign.add_argument("file", type=Path)
    p_assign.add_argument("--dry-run", action="store_true")

    p_verify = sub.add_parser("verify", help="recompute and compare, without writing")
    p_verify.add_argument("file", type=Path)

    p_digest = sub.add_parser("digest", help="digest a single instance")
    p_digest.add_argument("file", type=Path)
    p_digest.add_argument("--class", dest="class_name", required=True)

    sub.add_parser("verify-vectors", help="replay the permanent test vectors")
    ap.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    args = ap.parse_args()

    sv = load_schema(args.schema)

    if args.cmd == "digest":
        instance = _load(args.file)
        print(compute_id({k: v for k, v in instance.items() if k != "id"}, args.class_name, sv))
        return 0

    if args.cmd == "verify":
        document = _load(args.file)
        checked = sum(1 for _ in _iter_nodes(document))
        problems = verify(document, sv)
        unknown = unknown_node_groups(document)
        for p in problems:
            print(f"  MISMATCH {p}")
        for key, count in sorted(unknown.items()):
            print(f"  UNMINTED {key}: {count} node(s) under an unrecognised group key")
        if not checked and not unknown:
            # Finding nothing to check printed `0 mismatch(es)` — the same words
            # README.md offers as proof that every id matches its content. Say
            # which one actually happened.
            print(f"0 node(s) checked — {args.file} has no {CURIE_PREFIX} node lists")
            print("  Nodes live under a group key (persons:, gene_sets:, ...); a")
            print("  single-instance document has none, so nothing was compared.")
            return 0
        summary = f"{checked} node(s) checked, {len(problems)} mismatch(es)"
        if unknown:
            summary += f", {sum(unknown.values())} node(s) unminted"
        print(summary)
        return 1 if problems or unknown else 0

    if args.cmd == "assign":
        rt = _round_trip_yaml()
        document = rt.load(args.file.read_text())
        unknown = unknown_node_groups(document)
        if unknown:
            # Refuse rather than half-mint. These nodes get no ids, and _rewrite
            # edits their contents anyway, so writing the file would leave a
            # document that is partly minted and partly rewritten with no record
            # of which is which.
            print(f"unrecognised node group(s) in {args.file}:", file=sys.stderr)
            for key, count in sorted(unknown.items()):
                print(f"  {key}: {count} node(s) would never be minted", file=sys.stderr)
            print("  Node lists must use a known group key (persons:, gene_sets:, ...).",
                  file=sys.stderr)
            print("  Nothing written.", file=sys.stderr)
            return 1
        mapping = assign_ids(document, sv)
        for old, new in mapping.items():
            print(f"  {old}\n    -> {new}")
        print(f"{len(mapping)} id(s) assigned")
        if not mapping:
            # Nothing to mint means nothing to write. Rewriting the file anyway
            # reformatted single-instance examples — unwrapping a long scalar,
            # expanding a flow sequence — for no gain at all.
            print(f"  nothing to mint in {args.file}; left untouched")
            print("  Nodes live under a group key (persons:, gene_sets:, ...).")
            return 0
        if not args.dry_run:
            # Write the document assign_ids produced, nothing else. See
            # _round_trip_yaml() for what the previous text-rewrite destroyed.
            rt.dump(document, args.file)
            print(f"wrote {args.file} (comments preserved)")
        return 0

    if args.cmd == "verify-vectors":
        vectors = json.loads(VECTORS_PATH.read_text())
        failures = 0
        for v in vectors["vectors"]:
            got = compute_id(v["instance"], v["class"], sv)
            ok = got == v["expected_id"]
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'} {v['name']}")
            if not ok:
                print(f"        expected {v['expected_id']}\n        got      {got}")
        print(f"\n{len(vectors['vectors']) - failures}/{len(vectors['vectors'])} vectors reproduce")
        return 1 if failures else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
