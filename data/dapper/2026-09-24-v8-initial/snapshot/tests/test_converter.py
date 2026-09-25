"""Unit tests for schema/converter/geneset_to_dapper.py.

The converter implements the crosswalk in
schema/docs/geneset-provenance-nih-dapp-adaptation.md. The parts worth pinning
down are the ones where dig.geneset and DAPPER disagree in shape:

  * edge DIRECTION — dig.geneset draws file --(data input)--> analysis, PROV
    says activity prov:used entity. Getting this backwards produces a graph
    that still validates and still renders, and is wrong.
  * sha256 — dig.geneset File nodes carry MD5 only; SHA-256 lives in the
    metadata sidecar and has to be joined across on local_id/path.
  * overlay — NIH attribution is not in dig.geneset at all, and must never
    overwrite a value the source data did supply.

Fixture: the real HuBMAP HZ2 run (9 File / 2 AnalysisType / 1 GeneSet nodes,
3 data-input and 8 data-output edges), not a hand-written miniature.
"""
from __future__ import annotations

import yaml

from geneset_to_dapper import (
    _activity,
    _c2m2_file,
    _clean,
    _find_pairs,
    _gene_set,
    apply_overlay,
    convert_graph,
    convert_one,
)
from conftest import HZ2


# --------------------------------------------------------------------------
# _clean
# --------------------------------------------------------------------------
def test_clean_drops_empty_values_but_keeps_falsy_data():
    """_clean() tidies absent values out of emitted YAML without eating real ones.

    The distinction that matters is absence vs. a falsy measurement: `n_genes: 0`
    and `emit_small_gene_sets: False` are claims the source data asserted, and
    dropping them would silently turn a stated zero into an unstated field.
    """
    out = _clean({"a": None, "b": "", "c": [], "d": {}, "e": 0, "f": False, "g": "x"})
    assert out == {"e": 0, "f": False, "g": "x"}, (
        "0 and False are real values — only None/''/[]/{} are absent"
    )


# --------------------------------------------------------------------------
# node mapping
# --------------------------------------------------------------------------
def test_c2m2_file_joins_sha256_from_the_metadata_sidecar():
    """A File node's checksum is assembled from two different source documents.

    dig.geneset's provenance graph carries MD5 on the File node; SHA-256 exists
    only in geneset.meta.json. _c2m2_file() joins them on local_id, so this
    asserts both survive onto one C2M2File rather than the join dropping either.
    """
    node = {
        "id": "n1",
        "name": "geneset.tsv",
        "c2m2_properties": {"local_id": "s3://bucket/geneset.tsv", "md5": "abc", "filename": "geneset.tsv"},
    }
    sha = {"s3://bucket/geneset.tsv": "deadbeef"}
    out = _c2m2_file(node, sha)
    assert out["md5"] == "abc"
    assert out["sha256"] == "deadbeef"


def test_c2m2_file_without_a_sidecar_entry_simply_omits_sha256():
    """A run with no metadata sidecar converts, just with sparser files.

    _find_pairs() explicitly allows a missing geneset.meta.json, so the join has
    to degrade to an absent field rather than raising or writing a null.
    """
    node = {"id": "n1", "c2m2_properties": {"local_id": "s3://bucket/other.tsv"}}
    assert "sha256" not in _c2m2_file(node, {})


def test_activity_lifts_environment_fields_and_synonyms_to_aliases():
    """Executable provenance is flattened out of two nested dig.geneset objects.

    DAPPER keeps replay fields flat on Activity, while dig.geneset nests them
    under analysis.environment and renames one (analysis.version ->
    code_version). This pins that flattening, including c2m2 synonyms becoming
    LinkML aliases.
    """
    node = {
        "id": "a1",
        "analysis": {
            "command": "run.py --x",
            "version": "1.2.3",
            "environment": {"repo_url": "https://example/repo", "container_image": "img:1"},
        },
        "c2m2_properties": {"synonyms": ["alt-name"]},
    }
    out = _activity(node)
    assert out["command"] == "run.py --x"
    assert out["code_version"] == "1.2.3", "analysis.version -> code_version"
    assert out["repo_url"] == "https://example/repo"
    assert out["container_image"] == "img:1"
    assert out["aliases"] == ["alt-name"]


def test_gene_set_merges_the_three_metadata_sections(hz2_meta):
    """One GeneSet is assembled from three disjoint parts of the sidecar.

    gene_set, summary and converter.parameters each contribute different slots,
    so a refactor that reads only one of them would still produce a valid
    GeneSet — just a quietly incomplete one. Asserted against the real HZ2
    metadata rather than a stub so the section names stay honest.
    """
    node = {"id": "gs1", "name": "unsigned_term_gene:402cf4a1"}
    out = _gene_set(node, hz2_meta)
    assert out["member_type"] == "gene_set"
    assert out["assay"] == "bulk"                 # meta.gene_set
    assert out["organism"] == "human"             # meta.gene_set
    assert out["n_genes"] == 1747                 # meta.gene_set
    assert out["n_sets"] == 487                   # meta.summary.n_sets_emitted
    assert out["term_prefix"] == "HuBMAP"         # meta.converter.parameters


def test_gene_set_prefers_the_nodes_own_description_over_the_sidecars():
    """Where both sources describe the set, the provenance graph node wins.

    Both dig.geneset's GeneSet node and the sidecar's gene_set block carry a
    description. The node is the one tied to the run being converted, so it
    takes precedence and the sidecar is only a fallback.
    """
    node = {"id": "gs1", "description": "from the node"}
    out = _gene_set(node, {"gene_set": {"description": "from the sidecar"}})
    assert out["description"] == "from the node"


# --------------------------------------------------------------------------
# convert_graph — the whole HZ2 graph
# --------------------------------------------------------------------------
def test_convert_graph_routes_every_node_type_to_its_bucket(hz2_graph, hz2_meta):
    """Each dig.geneset node type lands in the right DAPPER document list.

    Counts are asserted against the real HZ2 run (9 File, 2 AnalysisType,
    1 GeneSet), so a routing change that silently drops or double-counts a type
    shows up as a number rather than needing a shape assertion.
    """
    doc = convert_graph(hz2_graph, hz2_meta)
    assert len(doc["c2m2_files"]) == 9
    assert len(doc["activities"]) == 2
    assert len(doc["gene_set_collections"]) == 1


def test_used_edges_reverse_direction_relative_to_dig_geneset(hz2_graph, hz2_meta):
    """prov:used points the opposite way to dig.geneset's "data input" edge.

    dig draws file --(data input)--> analysis; PROV says activity prov:used
    entity, so subject and object swap. This is the highest-value assertion in
    the file: reverse it and the output still validates against the schema and
    still renders in the portal, it just describes the analysis backwards. Each
    converted edge is checked against the specific dig edge it came from.
    """
    doc = convert_graph(hz2_graph, hz2_meta)
    dig_inputs = [e for e in hz2_graph["edges"] if e["label"] in ("data input", "metadata input")]
    assert len(doc["used_edges"]) == len(dig_inputs) == 3

    for dig, dapper in zip(dig_inputs, doc["used_edges"]):
        assert dapper["predicate"] == "prov:used"
        assert dapper["subject"] == dig["target"], "subject is the ANALYSIS, dig's target"
        assert dapper["object"] == dig["source"], "object is the FILE, dig's source"


def test_was_generated_by_edges_also_reverse(hz2_graph, hz2_meta):
    """The output edge inverts too, and in the same way.

    dig draws analysis --(data output)--> file; PROV says file
    prov:wasGeneratedBy activity. Covered separately from prov:used because the
    two are handled by different branches of convert_graph, so one can be fixed
    or broken without the other.
    """
    doc = convert_graph(hz2_graph, hz2_meta)
    dig_outputs = [e for e in hz2_graph["edges"] if e["label"] == "data output"]
    assert len(doc["was_generated_by_edges"]) == len(dig_outputs) == 8

    for dig, dapper in zip(dig_outputs, doc["was_generated_by_edges"]):
        assert dapper["predicate"] == "prov:wasGeneratedBy"
        assert dapper["subject"] == dig["target"], "subject is the FILE, dig's target"
        assert dapper["object"] == dig["source"], "object is the ACTIVITY, dig's source"


def test_edge_role_records_which_kind_of_input_it_was(hz2_graph, hz2_meta):
    """Data vs. metadata input survives the collapse into one edge class.

    Both dig labels map onto the single Used edge, so without edge_role the
    distinction would be lost in conversion. Asserts the values are drawn from
    the known set and that the field is actually populated.
    """
    doc = convert_graph(hz2_graph, hz2_meta)
    roles = {e.get("edge_role") for e in doc["used_edges"]}
    assert roles <= {"data_input", "metadata_input"}
    assert roles, "the data/metadata input distinction must survive the conversion"


def test_unknown_node_and_edge_kinds_are_skipped_not_crashed(capsys):
    """Unrecognised upstream input is reported on stderr, never silently eaten.

    dig.geneset can add node types and edge labels at any time. The converter
    should keep going rather than abort a whole run, but a skipped element that
    said nothing would look exactly like a successful conversion — so this
    asserts both halves: nothing emitted, and both names named.
    """
    graph = {
        "nodes": [{"id": "x", "type": "Sasquatch"}],
        "edges": [{"source": "a", "target": "b", "label": "vibes"}],
    }
    doc = convert_graph(graph, {})
    assert doc == {}, "nothing recognised, so nothing emitted"
    err = capsys.readouterr().err
    assert "Sasquatch" in err and "vibes" in err, "but both are reported, not silent"


def test_empty_buckets_are_dropped_from_the_output(hz2_graph, hz2_meta):
    """An empty graph yields an empty document, not a skeleton of empty lists.

    convert_graph pre-seeds every bucket and filters at the end, so this guards
    the filter — without it the converter would write files full of `used_edges:
    []` noise.
    """
    doc = convert_graph({"nodes": [], "edges": []}, hz2_meta)
    assert doc == {}


# --------------------------------------------------------------------------
# overlay
# --------------------------------------------------------------------------
def test_overlay_fills_missing_attribution():
    """--overlay supplies the NIH attribution dig.geneset has no field for.

    Creators, awards, publications and citation exist nowhere in the upstream
    data, so the overlay is the only route by which a converted gene set becomes
    citable at all.
    """
    gs = {"id": "gs1"}
    apply_overlay(gs, {"has_creator": ["orcid:0000-0002-1825-0097"]})
    assert gs["has_creator"] == ["orcid:0000-0002-1825-0097"]


def test_overlay_never_overwrites_a_value_the_source_data_supplied():
    """Real provenance outranks the operator-supplied default.

    apply_overlay is setdefault, not update. If it were update, running a
    convenience overlay across a batch would quietly replace genuine upstream
    attribution with boilerplate — the same class of harm as the
    mirror_mutable invariant in the schema header.
    """
    gs = {"id": "gs1", "has_creator": ["orcid:REAL"]}
    apply_overlay(gs, {"has_creator": ["orcid:OVERLAY"]})
    assert gs["has_creator"] == ["orcid:REAL"], "setdefault, not update"


def test_overlay_of_none_is_a_no_op():
    """Converting without --overlay is the normal path and must not fail.

    convert_one passes whatever it was given straight through, so None has to be
    tolerated rather than guarded at every call site.
    """
    gs = {"id": "gs1"}
    apply_overlay(gs, None)
    assert gs == {"id": "gs1"}


# --------------------------------------------------------------------------
# input discovery
# --------------------------------------------------------------------------
def test_find_pairs_discovers_the_sibling_sidecar():
    """Pointing the converter at a run directory finds both of its documents.

    The sidecar is located by name next to the provenance file rather than
    passed explicitly, which is what makes the documented directory-and-s3
    invocations work.
    """
    pairs = _find_pairs(HZ2)
    assert len(pairs) == 1
    prov, meta = pairs[0]
    assert prov.name == "geneset.provenance.json"
    assert meta is not None and meta.name == "geneset.meta.json"


def test_find_pairs_reports_a_missing_sidecar_as_none(tmp_path):
    """A run with no sidecar is a supported input, not an error.

    _find_pairs returns None for the meta half so convert_one can proceed with
    sparser descriptive fields — the CLI warns about this rather than refusing.
    """
    (tmp_path / "geneset.provenance.json").write_text("{}")
    (prov, meta), = _find_pairs(tmp_path)
    assert meta is None, "a sparse run is allowed, not an error"


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------
def test_convert_one_writes_a_minted_graph_and_focus_node(tmp_path, sv):
    """The full documented invocation produces two files whose ids are honest.

    Exercises the whole path — read, convert, mint, write — and then checks the
    property that matters downstream: dig.geneset's opaque UUIDv5/24-hex ids are
    gone, and every id that replaced them actually hashes to the content it
    names, per verify().
    """
    from dapper_identity import verify

    written = convert_one(HZ2 / "geneset.provenance.json", HZ2 / "geneset.meta.json",
                          tmp_path, overlay={})
    names = sorted(p.name for p in written)
    assert names == ["402cf4a1f3682a2e5bf1b002.dapper.yaml",
                     "402cf4a1f3682a2e5bf1b002.geneset_collection.yaml"]

    doc = yaml.safe_load((tmp_path / "402cf4a1f3682a2e5bf1b002.dapper.yaml").read_text())

    # dig.geneset's UUIDv5 / 24-hex ids are gone, replaced by content digests.
    ids = [n["id"] for bucket in ("c2m2_files", "activities", "gene_set_collections") for n in doc[bucket]]
    assert ids and all(i.startswith("dapper:") for i in ids)

    # and the ids actually match the content they name
    assert verify(doc, sv) == []


def test_convert_one_is_deterministic(tmp_path, sv):
    """Converting the same run twice produces byte-identical output.

    Content-addressed identifiers are only meaningful if the pipeline that
    computes them is reproducible; any dict-ordering or timestamp leak would
    make the same data mint under two different ids.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    convert_one(HZ2 / "geneset.provenance.json", HZ2 / "geneset.meta.json", a, overlay={})
    convert_one(HZ2 / "geneset.provenance.json", HZ2 / "geneset.meta.json", b, overlay={})
    for f in sorted(p.name for p in a.iterdir()):
        assert (a / f).read_text() == (b / f).read_text(), f"{f} is not reproducible"


def test_edges_point_at_ids_that_exist_after_minting(tmp_path):
    """Re-minting rewrites node ids, and the edges have to follow.

    Minting replaces every id, so an edge still holding a dig.geneset id would
    dangle. Nothing else catches this: the document still parses, still
    validates class by class, and only fails when someone tries to walk it.
    """
    convert_one(HZ2 / "geneset.provenance.json", HZ2 / "geneset.meta.json", tmp_path, overlay={})
    doc = yaml.safe_load((tmp_path / "402cf4a1f3682a2e5bf1b002.dapper.yaml").read_text())

    known = {n["id"] for b in ("c2m2_files", "activities", "gene_set_collections") for n in doc[b]}
    for bucket in ("used_edges", "was_generated_by_edges"):
        for e in doc.get(bucket, []):
            assert e["subject"] in known, f"dangling subject {e['subject']} in {bucket}"
            assert e["object"] in known, f"dangling object {e['object']} in {bucket}"
