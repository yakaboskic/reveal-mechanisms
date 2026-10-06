"""Library/row/file semantics and counts that cannot be checked per node."""
from copy import deepcopy
import hashlib

import pytest
import yaml

from conftest import EXAMPLES, SCHEMA
from dapper_identity import assign_ids, compute_id, compact_identifier, verify
from document_prefixes import expand_document
from geneset_to_dapper import convert_graph
import lint_provenance as lp


@pytest.fixture(scope="module")
def context(sv):
    return lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text())), lp.build_validator(SCHEMA)


@pytest.fixture
def rows():
    return yaml.safe_load((EXAMPLES / "example_geneset_collection_rows.yaml").read_text())


def lint(doc, tmp_path, sv, context):
    assign_ids(doc, sv)
    path = tmp_path / "collection.yaml"
    path.write_text(yaml.safe_dump(doc))
    return lp.lint(path, context[0], sv, context[1])


def test_two_rows_have_three_unique_genes_and_one_terminal(rows, tmp_path, sv, context):
    assert not lint(rows, tmp_path, sv, context).findings
    doc = lp.Document.load(tmp_path / "collection.yaml", context[0])
    assert lp._terminal_ids(doc, context[0].profiles["geneset"], context[0]) == [rows["gene_set_collections"][0]["id"]]
    contents = (EXAMPLES / "data/example_gene_sets.gmt").read_bytes()
    assert hashlib.sha256(contents).hexdigest() == rows["files"][1]["sha256"]
    parsed = {line.split("\t")[0]: line.split("\t")[2:] for line in contents.decode().splitlines()}
    for node in rows["gene_sets"]:
        assert parsed[node["gmt_entry"]] == node["members"]
    assert "GeneSet" not in sv.class_ancestors("GeneSetCollection")


@pytest.mark.parametrize("missing", ["in_gmt_file", "gmt_entry"])
def test_row_selector_requires_both_parts(missing, rows, tmp_path, sv, context):
    del rows["gene_sets"][0][missing]
    assert any(f.check == "nodes" for f in lint(rows, tmp_path, sv, context).errors)


@pytest.mark.parametrize("group,field,value", [
    ("gene_sets", "n_genes", 3), ("gene_sets", "n_members", 1),
    ("gene_set_collections", "n_sets", 1), ("gene_set_collections", "n_members", 3),
    ("gene_set_collections", "n_genes", 4),
])
def test_inconsistent_counts_fail(group, field, value, rows, tmp_path, sv, context):
    rows[group][0][field] = value
    assert any(f.check == "gene-set-counts" for f in lint(rows, tmp_path, sv, context).errors)


@pytest.mark.parametrize("group,field,value", [
    ("gene_sets", "n_genes", -1), ("gene_sets", "n_sets", 2),
    ("gene_set_collections", "n_sets", -1), ("gene_set_collections", "n_genes", -1),
])
def test_invalid_count_domains_fail(group, field, value, rows, tmp_path, sv, context):
    rows[group][0][field] = value
    assert any(f.check == "nodes" for f in lint(rows, tmp_path, sv, context).errors)


def test_duplicate_members_fail(rows, tmp_path, sv, context):
    rows["gene_set_collections"][0]["members"].append(rows["gene_set_collections"][0]["members"][0])
    assert any("distinct" in f.message for f in lint(rows, tmp_path, sv, context).errors)


def test_unknown_gene_membership_does_not_invent_union(rows, tmp_path, sv, context):
    del rows["gene_sets"][0]["members"]
    rows["gene_set_collections"][0]["n_genes"] = 4
    assert not lint(rows, tmp_path, sv, context).findings


def test_row_identity_is_independent_of_representation(rows, sv):
    row = rows["gene_sets"][0]
    changed = dict(row, gmt_entry="another_row", in_gmt_file="urn:file:another-export")
    assert compute_id(row, "GeneSet", sv) == compute_id(changed, "GeneSet", sv)
    changed["members"] = row["members"] + ["EXGENE:Gene4"]
    assert compute_id(row, "GeneSet", sv) != compute_id(changed, "GeneSet", sv)


def test_gmt_can_contain_its_gene_sets_ids_without_a_hash_cycle(rows, sv):
    assign_ids(rows, sv)
    before = [r["id"] for r in rows["gene_sets"]]
    content = "".join(r["id"] + "\tna\t" + "\t".join(r["members"]) + "\n" for r in rows["gene_sets"])
    for row in rows["gene_sets"]:
        row["gmt_entry"] = row["id"]
    rows["files"][1]["sha256"] = hashlib.sha256(content.encode()).hexdigest()
    rows["files"][1]["size_in_bytes"] = len(content.encode())
    assign_ids(rows, sv)
    assert [r["id"] for r in rows["gene_sets"]] == before
    assert all(r["gmt_entry"] == r["id"] for r in rows["gene_sets"])
    assert all(r["in_gmt_file"] == rows["files"][1]["id"] for r in rows["gene_sets"])
    assert not verify(rows, sv)
    mapping = assign_ids(rows, sv)
    assert all(a == b for a, b in mapping.items())


def test_inverse_membership_does_not_change_ids_or_create_hash_cycles(rows, sv):
    assert all(row["in_gene_set_collection"] == [rows["gene_set_collections"][0]["id"]]
               for row in rows["gene_sets"])
    mapping = assign_ids(rows, sv)
    assert all(old == new for old, new in mapping.items())
    row = rows["gene_sets"][0]
    without = {k: v for k, v in row.items() if k != "in_gene_set_collection"}
    assert compute_id(row, "GeneSet", sv) == compute_id(without, "GeneSet", sv)
    # A member-content edit remints its collection and rewrites the inverse link.
    row["name"] = "Updated human-readable gene-set name"
    previous = rows["gene_set_collections"][0]["id"]
    assign_ids(rows, sv)
    collection = rows["gene_set_collections"][0]
    assert collection["id"] != previous
    assert row["id"] in collection["members"]
    assert all(n["in_gene_set_collection"] == [collection["id"]] for n in rows["gene_sets"])
    assert not verify(rows, sv)


def test_forward_only_membership_remains_valid(rows, tmp_path, sv, context):
    for row in rows["gene_sets"]:
        del row["in_gene_set_collection"]
    assert not lint(rows, tmp_path, sv, context).findings


def test_inverse_only_membership_traces_contained_rows(rows, tmp_path, sv, context):
    del rows["gene_set_collections"][0]["members"]
    for row in rows["gene_sets"]:
        del row["was_generated_by"]  # reachable only through inverse membership
    assert not lint(rows, tmp_path, sv, context).findings
    doc = lp.Document.load(tmp_path / "collection.yaml", context[0])
    assert lp._terminal_ids(doc, context[0].profiles["geneset"], context[0]) == [rows["gene_set_collections"][0]["id"]]


def test_one_gene_set_can_belong_to_two_collections(rows, tmp_path, sv, context):
    extra = deepcopy(rows["gene_set_collections"][0])
    extra.update(id="urn:example:second-collection", name="Another collection",
                 members=[rows["gene_sets"][0]["id"]], n_sets=1, n_members=1, n_genes=2)
    rows["gene_set_collections"].append(extra)
    rows["gene_sets"][0]["in_gene_set_collection"].append(extra["id"])
    assert not lint(rows, tmp_path, sv, context).findings


@pytest.mark.parametrize("side", ["forward", "inverse"])
def test_contradictory_membership_directions_fail(side, rows, tmp_path, sv, context):
    if side == "forward":
        rows["gene_set_collections"][0]["members"].pop(0)
    else:
        rows["gene_sets"][0]["in_gene_set_collection"] = []
    assert any(f.check == "gene-set-membership" for f in lint(rows, tmp_path, sv, context).errors)


def test_duplicate_collection_backlinks_fail(rows, tmp_path, sv, context):
    row = rows["gene_sets"][0]
    row["in_gene_set_collection"] *= 2
    assert any(f.check == "gene-set-membership" and "distinct" in f.message
               for f in lint(rows, tmp_path, sv, context).errors)


@pytest.mark.parametrize("target", ["wrong-class", "missing", "undeclared"])
def test_bad_collection_backlinks_fail(target, rows, tmp_path, sv, context):
    ref, expected = {"wrong-class": (rows["files"][0]["id"], "endpoints"),
                     "missing": ("dapper:GeneSetCollection." + "a" * 32, "refs"),
                     "undeclared": ("undefined:collection", "prefixes")}[target]
    rows["gene_sets"][0]["in_gene_set_collection"] = [ref]
    findings = lint(rows, tmp_path, sv, context).errors
    assert any(f.check == expected for f in findings), findings


def test_expanded_membership_is_consistent_and_idempotent(rows, tmp_path, sv, context):
    expanded = expand_document(rows, sv)
    collection = expanded["gene_set_collections"][0]
    assert all(n["in_gene_set_collection"] == [collection["id"]] for n in expanded["gene_sets"])
    assert set(collection["members"]) == {n["id"] for n in expanded["gene_sets"]}
    assert not verify(expanded, sv)
    assert expand_document(expanded, sv) == expanded
    path = tmp_path / "expanded.yaml"
    path.write_text(yaml.safe_dump(expanded))
    assert not lp.lint(path, context[0], sv, context[1]).findings


def test_external_collection_reference_does_not_hide_local_end_results(rows, tmp_path, sv, context):
    removed = rows.pop("gene_set_collections")[0]["id"]
    rows["_illustrative"].remove(removed)
    for row in rows["gene_sets"]:
        row["in_gene_set_collection"] = ["https://example.org/collections/library"]
    assert not lint(rows, tmp_path, sv, context).findings


@pytest.mark.parametrize("location", ["/humgen/input.gmt", "data/input.gmt", r"C:\data\input.gmt"])
def test_filesystem_locations_remain_literal(location, rows, sv):
    rows["files"][1]["location"] = location
    expanded = expand_document(rows, sv)
    assert expanded["files"][1]["location"] == location
    assert compact_identifier(expanded["files"][1]["id"]) == rows["files"][1]["id"]


def test_undefined_location_prefix_fails(rows, tmp_path, sv, context):
    rows["files"][1]["location"] = "missing:input.gmt"
    assert any(f.check == "prefixes" for f in lint(rows, tmp_path, sv, context).errors)
    with pytest.raises(ValueError):
        expand_document(rows, sv)


@pytest.mark.parametrize("group,cls", [("files", "File"), ("c2m2_files", "C2M2File"), ("datasets", "Dataset")])
def test_declared_location_expands_without_changing_identity(group, cls, sv):
    doc = {"prefixes": {"humgen": "file:///humgen/"}, group: [{"id": "urn:example:item", "name": "A resource", "location": "humgen:data/file"}]}
    assign_ids(doc, sv)
    expanded = expand_document(doc, sv)
    assert expanded[group][0]["location"] == "file:///humgen/data/file"
    assert compact_identifier(expanded[group][0]["id"]) == doc[group][0]["id"]


@pytest.mark.parametrize("count", [0, 1, 2])
def test_converter_library_metadata_means_collection_even_with_one_set(count):
    doc = convert_graph({"nodes": [{"id": "urn:example:set", "type": "GeneSet"}], "edges": []},
                        {"summary": {"n_sets_emitted": count, "n_genes": 0}})
    assert doc["gene_set_collections"][0]["n_sets"] == count
    assert doc["gene_set_collections"][0]["n_genes"] == 0
    assert "gene_sets" not in doc


def test_converter_without_library_metadata_retains_individual_set():
    doc = convert_graph({"nodes": [{"id": "urn:example:set", "type": "GeneSet"}], "edges": []}, {})
    assert doc["gene_sets"][0]["member_type"] == "gene"


def test_converter_links_only_unique_gmt_from_same_activity(hz2_graph, hz2_meta):
    doc = convert_graph(hz2_graph, hz2_meta)
    library = doc["gene_set_collections"][0]
    gmt = next(n for n in doc["c2m2_files"] if n["filename"] == "genesets.gmt")
    assert library["has_gmt_file"] == gmt["id"]
    graph = deepcopy(hz2_graph)
    extra = deepcopy(next(n for n in graph["nodes"] if n["id"] == gmt["id"]))
    extra["id"] = "urn:example:other-gmt"
    graph["nodes"].append(extra)
    edge = deepcopy(next(e for e in graph["edges"] if e["target"] == gmt["id"]))
    edge["target"] = extra["id"]
    graph["edges"].append(edge)
    assert "has_gmt_file" not in convert_graph(graph, hz2_meta)["gene_set_collections"][0]
