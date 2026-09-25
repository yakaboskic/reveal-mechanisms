"""Unit tests for schema/identity/dapper_identity.py — the minting algorithm.

lint_identity.py already checks the SCHEMA-WIDE invariants (every Node marked,
every class hashable, no dotted terms) and `verify-vectors` replays the frozen
fixtures. Neither exercises the functions directly, so this file covers the
behaviour underneath them, and adds a regression test per bug the docstring in
lint_identity.py records as having actually happened.
"""
from __future__ import annotations

import copy
import json
import sys

import pytest
import yaml

from dapper_identity import (
    CURIE_PREFIX,
    DOC_GROUPS,
    VECTORS_PATH,
    assign_ids,
    compute_digest,
    compute_id,
    digest_of,
    hashable_slot_names,
    sha512t24u,
    verify,
)


# --------------------------------------------------------------------------
# the digest primitive
# --------------------------------------------------------------------------
def test_sha512t24u_matches_the_ga4gh_vrs_spec_vector():
    """Our digest primitive is byte-compatible with GA4GH VRS.

    Taken verbatim from the VRS specification. Interoperability with ga4gh:
    identifiers rests entirely on this one value, so it is a fixture, not an
    expectation: if it ever fails, fix the code, never this line.
    """
    assert sha512t24u(b"ACGT") == "aKF498dAxcJAqme6QYQ7EZ07-fiw8Kw2"


def test_digest_is_32_urlsafe_chars_with_no_padding():
    """The digest is safe to paste into a URL or a CURIE without escaping.

    24 bytes is divisible by 3, so base64 emits no `=` padding and needs no
    stripping logic. urlsafe_b64encode also rules out `+` and `/`, which would
    otherwise have to be percent-encoded wherever an id appears in a path.
    """
    d = sha512t24u(b"anything at all")
    assert len(d) == 32
    assert "=" not in d and "+" not in d and "/" not in d


# --------------------------------------------------------------------------
# digest_of — the id parser
# --------------------------------------------------------------------------
@pytest.mark.parametrize("identifier,expected", [
    ("dapper:Dataset.UIypYwZSXzkH0bQAutCVyev07HmQZZp8", "UIypYwZSXzkH0bQAutCVyev07HmQZZp8"),
    ("dapper:Dataset.", None),          # dot but no digest
    ("dapper:Dataset", None),           # no dot at all
    ("orcid:0000-0002-1825-0097", None),
    ("MONDO:0005148", None),
    ("s3://bucket/key.json", None),     # a dot, but not our prefix
    ("https://example.org/x.y", None),
    ("", None),
    (None, None),
    (42, None),
])
def test_digest_of_only_unpacks_our_own_identifiers(identifier, expected):
    """Only a well-formed `dapper:Class.digest` yields a digest; everything else is None.

    digest_of is what tells a DAPPER reference apart from an external identifier
    that merely resembles one, and _substitute() calls it on every value it
    hashes. If an ORCID, MONDO term or s3:// URL were misread as one of ours it
    would be replaced by a fragment of itself inside the digest input, silently
    changing the identifier of anything that cites it.
    """
    assert digest_of(identifier) == expected


def test_digest_of_partitions_on_the_FIRST_dot():
    """A dot inside a local name is swallowed — which is why lint rule 9 exists.

    Parsing splits on the first dot, so a self-defined predicate such as
    `dapper:Foo.v2` would be read as class `Foo` with digest `v2` and truncated.
    lint_identity.py rule 9 forbids dotted terms precisely because this parser
    cannot tell the two apart.
    """
    assert digest_of("dapper:Foo.v2.bar") == "v2.bar"


# --------------------------------------------------------------------------
# what constitutes identity
# --------------------------------------------------------------------------
def test_id_is_never_an_input_to_its_own_digest(sv):
    """`id` is the output of the digest, so it can never also be an input.

    Including it would make minting non-idempotent — every re-mint would hash a
    different id and produce a different one again.
    """
    assert "id" not in hashable_slot_names(sv, "Dataset")


def test_the_identifier_a_node_already_carries_does_not_affect_its_digest(sv):
    """An id embedded in a node's own content is blanked before hashing.

    Excluding the `id` slot is not enough on its own: a node can mention its own
    identifier inside a description or a self-reference. compute_digest() blanks
    self_id first, which is what lets the same content mint to the same digest
    whether it arrives bare or already identified.
    """
    instance = {"name": "x", "description": "y"}
    bare = compute_digest(instance, "Dataset", sv)
    with_self = compute_digest(instance, "Dataset", sv, self_id="dapper:Dataset." + "A" * 32)
    assert bare == with_self


def test_compute_id_is_prefix_class_digest(sv):
    """A minted id has exactly the three-part shape the rest of the system parses.

    portal/build.py splits ids for display and digest_of() unpacks them for
    substitution, so the `dapper:{ClassName}.{32 chars}` shape is a contract,
    not just a formatting choice.
    """
    got = compute_id({"name": "x"}, "Dataset", sv)
    prefix, _, rest = got.partition(":")
    cls, _, digest = rest.partition(".")
    assert prefix == CURIE_PREFIX
    assert cls == "Dataset"
    assert len(digest) == 32


def test_class_name_is_part_of_the_identity(sv):
    """Identical content under a different class is a different thing.

    The class name is hashed, not just prefixed onto the result. This is what
    made GeneProgram's id change when PR #4 renamed it from CellProgram even
    though its content had not moved — see the note on the minimal-gene-program
    vector.
    """
    content = {"name": "hypoxia response program"}
    assert compute_digest(content, "GeneSet", sv) != compute_digest(content, "GeneProgram", sv)


def test_unmarked_slots_are_a_schema_error_not_a_default(sv):
    """Every slot must declare `hashable` or `unhashable` — silence is not allowed.

    An unmarked slot would default into one bucket or the other by accident,
    quietly deciding whether editing that field changes the identifier. The
    schema is required to state it, which is why lint rule 1 exists; this checks
    the same property from the function that reports it.
    """
    from dapper_identity import unmarked_slots
    assert unmarked_slots(sv, "Dataset") == [], "every Dataset slot must declare a marker"


# --------------------------------------------------------------------------
# whole-document assignment
# --------------------------------------------------------------------------
def test_assign_ids_is_idempotent(sv):
    """Re-minting an already-minted document changes nothing.

    mint.py's contract is that running it twice on unchanged data reproduces the
    file exactly, so that a changed id is a real signal about changed content
    rather than churn.
    """
    doc = {"datasets": [{"id": "tmp-1", "name": "one"}, {"id": "tmp-2", "name": "two"}]}
    assign_ids(doc, sv)
    first = [n["id"] for n in doc["datasets"]]
    assign_ids(doc, sv)
    assert [n["id"] for n in doc["datasets"]] == first


def test_assign_ids_rewrites_references_to_the_new_ids(sv):
    """Replacing a node's id also updates everything that pointed at it.

    Minting rewrites ids across a whole document, so edges still holding the old
    value would dangle. Nothing downstream catches that — the document still
    parses and still validates class by class — so it is asserted here.
    """
    doc = {
        "activities": [{"id": "old-activity", "name": "run"}],
        "c2m2_files": [{"id": "old-file", "name": "out.tsv"}],
        "used_edges": [{"subject": "old-activity", "predicate": "prov:used", "object": "old-file"}],
    }
    assign_ids(doc, sv)
    edge = doc["used_edges"][0]
    assert edge["subject"] == doc["activities"][0]["id"]
    assert edge["object"] == doc["c2m2_files"][0]["id"]
    assert "old-" not in edge["subject"] + edge["object"]


@pytest.mark.parametrize("group,field,external", [
    ("persons", "orcid", "orcid:0000-0002-1825-0097"),
    ("organizations", "ror", "ror:02mtd9m52"),
])
def test_minting_never_overwrites_an_external_identifier(sv, group, field, external):
    """An ORCID or ROR is authoritative and must survive minting untouched.

    Regression for broadinstitute/dapper#1: nodes whose id WAS their external
    identifier had both the id and the field rewritten to a DAPPER digest,
    destroying the only globally resolvable name the record had.

    The `id` itself IS replaced — ids are computed, never authored, and mint.py
    reports how many hand-written ones it replaced. What has to survive is the
    FIELD, which is the record's only globally resolvable name.

    This test used to file its node under `agents`, which is not a key in
    DOC_GROUPS. assign_ids therefore saw an empty document, minted nothing, and
    the assertion passed without exercising anything at all — so the group key
    is asserted first.
    """
    assert group in DOC_GROUPS, "a node filed under an unknown key is never minted"
    doc = {group: [{"id": external, "name": "someone", field: external}]}
    assign_ids(doc, sv)
    node = doc[group][0]
    assert node[field] == external, "an ORCID/ROR is authoritative — leave it alone"
    assert node["id"].startswith(f"{CURIE_PREFIX}:"), "the id itself is computed, not authored"


def test_a_literal_scalar_equal_to_the_id_does_not_change_the_digest(sv):
    """Minting must not change what a node hashes to, even when a field IS the id.

    `_blank_self` used to neutralise any value that merely equalled the node's
    current id, including literal scalars that `_rewrite_node` deliberately never
    rewrites. A Person whose node id was their ORCID carries that same ORCID in
    `orcid`, so the field was blanked before minting and not after — the node
    hashed one way as input and another way as output, and the freshly minted
    file failed its own verify. Blanking must mirror rewriting exactly.
    """
    content = {"name": "someone", "orcid": "orcid:0000-0002-1825-0097"}
    before = compute_digest(content, "Person", sv, self_id="orcid:0000-0002-1825-0097")

    doc = {"persons": [{"id": "orcid:0000-0002-1825-0097", **content}]}
    assign_ids(doc, sv)
    minted = doc["persons"][0]
    after = compute_digest({k: v for k, v in minted.items() if k != "id"},
                           "Person", sv, self_id=minted["id"])
    assert before == after


def test_verify_is_clean_on_a_freshly_minted_document(sv):
    """The two halves of the system agree: what assign_ids writes, verify accepts.

    Baseline for the two tests below — without it, a `verify` that rejected
    everything would make them pass for the wrong reason.
    """
    doc = {"datasets": [{"id": "tmp-1", "name": "one"}, {"id": "tmp-2", "name": "two"}]}
    assign_ids(doc, sv)
    assert verify(doc, sv) == []


def test_verify_catches_content_edited_after_minting(sv):
    """Editing a hashable field without re-minting is detected.

    This is the drift the whole scheme exists to prevent: an identifier that no
    longer describes its content is a name that lies. It is also the exact bug
    found in example_bottom_line_result.yaml and example_graph.yaml.
    """
    doc = {"datasets": [{"id": "tmp-1", "name": "one"}]}
    assign_ids(doc, sv)
    tampered = copy.deepcopy(doc)
    tampered["datasets"][0]["name"] = "something else"
    problems = verify(tampered, sv)
    assert len(problems) == 1 and "Dataset" in problems[0]


def test_verify_catches_a_hand_written_identifier(sv):
    """A plausible-looking id that was never computed is still rejected.

    From the minter's docstring: you do not write ids by hand, ever. A
    well-formed but invented digest is indistinguishable from a real one by
    shape alone, so only recomputation can tell them apart.
    """
    doc = {"datasets": [{"id": "dapper:Dataset." + "A" * 32, "name": "one"}]}
    assert verify(doc, sv), "an id that does not match its content must be reported"


# --------------------------------------------------------------------------
# the `assign` CLI — the layer that actually writes the file
# --------------------------------------------------------------------------
#
# Two nodes, because the two failure modes mask each other. The Person catches
# the destroyed ORCID; on its own it does NOT catch the broken round trip, since
# overwriting `orcid` with the new id makes _blank_self neutralise it on both
# sides and verify goes quiet. The Activity is what exposes that: `script_url` is
# a literal scalar, so _rewrite_node leaves it out of the digest input while the
# text rewriter changed it on disk — digest and file then disagree.
DOC_WITH_COMMENTS = """\
# A curated comment, of the kind schema/examples carries to distinguish real
# transcribed provenance from illustrative steps.
persons:
  - id: orcid:0000-0002-1825-0097
    name: Jane A. Smith
    orcid: orcid:0000-0002-1825-0097   # authoritative — must not be rewritten
gene_sets:
  - id: geneset:raw1
    name: Example marker set
    organism: human
activities:
  - id: activity:raw2
    name: Enrichment run
    command: reveal enrichment --geneset geneset:raw1
    script_url: https://portal.example/gs/geneset:raw1/run.py
"""


def _run_assign(path, monkeypatch) -> None:
    import dapper_identity

    monkeypatch.setattr(sys, "argv", ["dapper_identity.py", "assign", str(path)])
    assert dapper_identity.main() == 0


def test_assign_cli_does_not_overwrite_an_external_identifier(tmp_path, monkeypatch):
    """The file on disk gets the same protection assign_ids gives in memory.

    The text rewriter replaced every occurrence of the old id, so a Person whose
    id was their ORCID had the `orcid` field overwritten with the new digest.
    `verify` could not catch it: `_blank_self` neutralised that field on both
    sides, so the tool reported `0 mismatch(es)` over destroyed data.
    """
    doc = tmp_path / "doc.yaml"
    doc.write_text(DOC_WITH_COMMENTS)
    _run_assign(doc, monkeypatch)

    node = yaml.safe_load(doc.read_text())["persons"][0]
    assert node["orcid"] == "orcid:0000-0002-1825-0097"
    assert node["id"].startswith(f"{CURIE_PREFIX}:Person.")


def test_assign_cli_preserves_comments(tmp_path, monkeypatch):
    """Comments survive the write — the requirement the text rewriter existed for.

    Preserving them is why the file was rewritten as text in the first place, so
    a fix that dropped them would trade one regression for another. The
    round-tripper keeps them without a second rewrite rule.
    """
    doc = tmp_path / "doc.yaml"
    doc.write_text(DOC_WITH_COMMENTS)
    _run_assign(doc, monkeypatch)

    text = doc.read_text()
    assert "# A curated comment" in text
    assert "# authoritative" in text


def test_assign_cli_output_passes_its_own_verify(sv, tmp_path, monkeypatch):
    """A freshly minted file is immediately valid.

    It was not: because the digest was computed from one document and the file
    written from another, `assign` emitted files that failed `verify` on the very
    next command — which the troubleshooting table reads as "the content was
    edited after minting", pointing at a cause that never happened.
    """
    doc = tmp_path / "doc.yaml"
    doc.write_text(DOC_WITH_COMMENTS)
    _run_assign(doc, monkeypatch)

    assert verify(yaml.safe_load(doc.read_text()), sv) == []


def test_assign_cli_leaves_a_document_it_cannot_mint_untouched(tmp_path, monkeypatch):
    """A file with nothing to mint is not rewritten at all.

    Only grouped node lists are mintable, so a single-instance document like
    example_nanopub.yaml yields no ids. Rewriting it anyway still pushed it
    through the dumper, which unwrapped a long `statement:` scalar and expanded a
    flow sequence to block style — a diff on a curated example in exchange for
    nothing. Byte comparison, because the point is that no write happened.
    """
    original = "# a single instance, not a graph document\nid: dapper:Nanopublication.x\nname: one\n"
    doc = tmp_path / "instance.yaml"
    doc.write_text(original)
    _run_assign(doc, monkeypatch)
    assert doc.read_text() == original


def test_assign_cli_is_idempotent_on_disk(tmp_path, monkeypatch):
    """Running it twice on unchanged data reproduces the file byte for byte.

    The headline guarantee in schema/identity/README.md, and the reason a changed
    id can be read as a real signal about changed content. In-memory idempotence
    is covered above; this asserts it survives the round trip through the file,
    which is where it was actually broken — `assign` converged only on run two.
    """
    doc = tmp_path / "doc.yaml"
    doc.write_text(DOC_WITH_COMMENTS)
    _run_assign(doc, monkeypatch)
    first = doc.read_text()
    _run_assign(doc, monkeypatch)
    assert doc.read_text() == first


# --------------------------------------------------------------------------
# unrecognised group keys — the document side of DOC_GROUPS
# --------------------------------------------------------------------------
def test_an_unknown_group_key_is_reported_not_skipped():
    """A node list under a name DOC_GROUPS does not know is caught, not ignored.

    Two things used to happen in silence: the nodes were never minted, and
    `_rewrite` edited their contents anyway with the id-blind substring rule, so
    an ORCID under an unrecognised key was overwritten by a digest that named a
    different node. A typo — `gene_set:` for `gene_sets:` — was enough.

    test_doc_groups_covers_every_concrete_hashable_node guards schema class to
    map; this guards document to map.
    """
    from dapper_identity import unknown_node_groups

    doc = {
        "persons": [{"id": "orcid:0000-0002-1825-0097", "name": "known"}],
        "agents": [{"id": "orcid:0000-0003-1234-5678", "name": "unknown group"}],
    }
    assert unknown_node_groups(doc) == {"agents": 1}


def test_edge_lists_are_not_mistaken_for_unminted_nodes():
    """Edge lists live outside DOC_GROUPS legitimately and must not be flagged.

    They are lists of dicts, like node lists, so the two are told apart by the
    presence of `id` — an edge carries subject/predicate/object and no id. Every
    example and both converter outputs were checked against this.
    """
    from dapper_identity import unknown_node_groups

    doc = {
        "_illustrative": ["dapper:AgenticWorkspace." + "A" * 32],
        "used_edges": [{"subject": "dapper:Activity.x", "predicate": "prov:used",
                        "object": "dapper:C2M2File.y"}],
    }
    assert unknown_node_groups(doc) == {}


def test_every_example_document_uses_only_known_group_keys(example_docs):
    """No committed example carries a node list the minter would skip.

    The DOC_GROUPS comment records example_graph.yaml having carried 12 unminted
    groups before the map was filled in. Nothing re-checked it afterwards, so
    the same drift could recur with a new example or a renamed group.
    """
    from dapper_identity import unknown_node_groups

    offenders = {
        name: unknown_node_groups(doc)
        for name, doc in example_docs.items()
        if isinstance(doc, dict) and unknown_node_groups(doc)
    }
    assert offenders == {}, f"unminted node groups in examples: {offenders}"


def test_assign_cli_refuses_a_document_it_can_only_partly_mint(tmp_path, monkeypatch):
    """`assign` writes nothing when some nodes are unreachable.

    Half-minting is worse than not minting: the recognised nodes get ids while
    the unrecognised ones get rewritten contents and no id, and the file records
    nothing about which is which. Asserted byte-for-byte, because the guarantee
    is that the file is left alone.
    """
    import dapper_identity

    original = (
        "persons:\n"
        "  - id: orcid:0000-0002-1825-0097\n"
        "    name: Jane A. Smith\n"
        "agents:\n"
        "  - id: orcid:0000-0003-1234-5678\n"
        "    name: Alex R. Lee\n"
    )
    doc = tmp_path / "doc.yaml"
    doc.write_text(original)

    monkeypatch.setattr(sys, "argv", ["dapper_identity.py", "assign", str(doc)])
    assert dapper_identity.main() == 1, "a partly mintable document must be refused"
    assert doc.read_text() == original


# --------------------------------------------------------------------------
# the frozen fixtures
# --------------------------------------------------------------------------
def _vectors():
    return json.loads(VECTORS_PATH.read_text())["vectors"]


@pytest.mark.parametrize("vec", _vectors(), ids=lambda v: v["name"])
def test_every_permanent_vector_still_reproduces(vec, sv):
    """Each frozen vector mints to the identifier recorded for it.

    From trusty-identifiers.md: keep test vectors forever, so that a library
    upgrade cannot silently change published ids. If one fails, fix the code —
    never the vector. A genuine algorithm change is DAPPER-ID-2 under a new
    prefix. Parametrized so a failure names the specific vector.
    """
    assert compute_id(vec["instance"], vec["class"], sv) == vec["expected_id"], vec.get("note", "")


def test_vector_classes_all_exist_in_the_schema(sv):
    """No vector is silently orphaned by a class being renamed or removed.

    A vector naming a class that no longer exists would fail confusingly, or
    stop covering anything at all. Renaming a class changes its digest, so this
    should force the rename to be dealt with deliberately.
    """
    for vec in _vectors():
        assert sv.get_class(vec["class"]) is not None, f"{vec['class']} is not in dapper.yaml"


# --------------------------------------------------------------------------
# DOC_GROUPS — the map the portal and the minter share
# --------------------------------------------------------------------------
def test_doc_groups_names_only_real_classes(sv):
    """Every document-list key maps to a class that actually exists.

    DOC_GROUPS is hand-maintained. An entry pointing at a renamed or deleted
    class would make the minter skip that list, leaving its nodes unminted with
    no error raised.
    """
    for group, class_name in DOC_GROUPS.items():
        assert sv.get_class(class_name) is not None, f"{group} -> {class_name} does not exist"


def test_doc_groups_covers_every_concrete_hashable_node(sv):
    """A new Node class must be added to DOC_GROUPS or it is invisible.

    Regression for the drift recorded in portal/build.py: a class in the schema
    but absent from DOC_GROUPS is skipped by both the minter and the portal —
    which once rendered an 18-node document as a single node. Computed from the
    schema rather than listed, so adding a class fails this test until it is
    registered.
    """
    mapped = set(DOC_GROUPS.values())
    missing = [
        c for c in sv.all_classes()
        if not sv.get_class(c).abstract
        and not sv.get_class(c).mixin
        and "HashableNode" in sv.class_ancestors(c)
        and c not in mapped and c != "HashableNode"
    ]
    assert missing == [], f"add these to DOC_GROUPS: {missing}"


def test_the_portal_reads_the_same_groups_as_the_minter():
    """The portal's text-parsed copy of DOC_GROUPS still matches the real dict.

    portal/build.py re-parses DOC_GROUPS out of the source with a regex instead
    of importing it, to keep the portal free of the rdflib/linkml dependency
    chain. That parse is the thing that can silently drift — a formatting change
    to the dict would break it with no error — so it is compared directly.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "portal"))
    from build import NODE_GROUPS

    assert NODE_GROUPS == DOC_GROUPS
