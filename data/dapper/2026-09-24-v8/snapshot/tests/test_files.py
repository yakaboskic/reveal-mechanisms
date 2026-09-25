"""File provenance, DRS registration, and compatibility of inherited metadata."""
from copy import deepcopy
import importlib.util

from conftest import REPO_ROOT
from dapper_identity import assign_ids, compute_id, verify
from geneset_to_dapper import convert_graph


def test_file_relocation_and_drs_registration_preserve_identity(sv):
    node = {"filename": "intermediate.tsv", "sha256": "a" * 64, "size_in_bytes": 1024}
    original = compute_id(node, "File", sv)
    node.update(location="/work/run-2/intermediate.tsv",
                drs_representation=["drs://example.org/intermediate"])
    assert compute_id(node, "File", sv) == original
    node["sha256"] = "b" * 64
    assert compute_id(node, "File", sv) != original


def test_dataset_relocation_preserves_identity_but_distribution_changes_it(sv):
    dataset = {"name": "Result", "location": "s3://old/prefix/", "has_file": ["urn:file:one"]}
    original = compute_id(dataset, "Dataset", sv)
    dataset["location"] = "s3://new/prefix/"
    assert compute_id(dataset, "Dataset", sv) == original
    dataset["has_file"] = ["urn:file:two"]
    assert compute_id(dataset, "Dataset", sv) != original


def test_file_graph_mints_access_refs_without_changing_file_id(sv):
    doc = {
        "files": [{"id": "urn:example:file", "filename": "x.tsv", "sha256": "a" * 64,
                   "drs_representation": ["urn:example:drs"]}],
        "drs_objects": [{"id": "urn:example:drs", "self_uri": "drs://example.org/x"}],
        "drs_representation_edges": [{"subject": "urn:example:file",
                                      "predicate": "dapper:drsRepresentation",
                                      "object": "urn:example:drs"}],
    }
    expected = compute_id(doc["files"][0], "File", sv)
    assign_ids(doc, sv)
    assert doc["files"][0]["id"] == expected
    assert doc["files"][0]["drs_representation"] == [doc["drs_objects"][0]["id"]]
    assert doc["drs_representation_edges"][0]["object"] == doc["drs_objects"][0]["id"]
    assert verify(doc, sv) == []
    first = deepcopy(doc)
    assign_ids(doc, sv)
    assert doc == first


def test_geneset_gmt_reference_participates_in_identity(sv):
    gene_set = {"name": "Gene set", "has_gmt_file": "urn:file:first"}
    original = compute_id(gene_set, "GeneSet", sv)
    gene_set["has_gmt_file"] = "urn:file:revised"
    assert compute_id(gene_set, "GeneSet", sv) != original


def test_converter_accepts_generic_intermediate_and_preserves_provenance(sv):
    graph = {
        "nodes": [
            {"id": "input", "type": "File", "c2m2_properties": {"filename": "raw.tsv"}},
            {"id": "prepare", "type": "AnalysisType", "name": "prepare"},
            {"id": "intermediate", "type": "File", "filename": "normalized.tsv",
             "location": "/work/normalized.tsv", "size_in_bytes": 0},
            {"id": "analyze", "type": "AnalysisType", "name": "analyze"},
        ],
        "edges": [
            {"source": "input", "target": "prepare", "label": "data input"},
            {"source": "prepare", "target": "intermediate", "label": "data output"},
            {"source": "intermediate", "target": "analyze", "label": "data input"},
        ],
    }
    doc = convert_graph(graph, {"input": {"files": [
        {"local_path": "/work/normalized.tsv", "sha256": "a" * 64}]}})
    assert len(doc["c2m2_files"]) == len(doc["files"]) == 1
    assert doc["files"][0]["sha256"] == "a" * 64
    assert doc["files"][0]["size_in_bytes"] == 0
    assign_ids(doc, sv)
    intermediate = doc["files"][0]["id"]
    assert doc["used_edges"][-1]["object"] == intermediate
    assert doc["was_generated_by_edges"][0]["subject"] == intermediate
    assert verify(doc, sv) == []


def test_portal_renders_file_access_and_inherited_c2m2_fields():
    spec = importlib.util.spec_from_file_location("dapper_portal", REPO_ROOT / "portal/build.py")
    portal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal)
    schema = portal.load_schema()
    assert {"filename", "sha256", "local_id", "dcc_url"} <= set(schema["C2M2File"]["attributes"])
    graph = portal.build_graph({"file": "example_file_graph.yaml", "key": "files",
                                "title": "Files", "blurb": "", "start": ""})
    assert any(n["cls"] == "File" and n["family"] == "data" for n in graph["nodes"])
    assert any(e["predicate"] == "dapper:drsRepresentation" for e in graph["edges"])
