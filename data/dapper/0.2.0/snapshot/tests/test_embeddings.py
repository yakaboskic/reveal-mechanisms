"""Embedding records: identity policy, locators, backlinks, and linting."""
from __future__ import annotations

from copy import deepcopy

import pytest
import yaml

from conftest import EXAMPLES, SCHEMA
from dapper_identity import assign_ids, compute_id, unknown_node_groups, verify

import lint_provenance as lp


@pytest.fixture(scope="module")
def context(sv):
    return lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text())), lp.build_validator(SCHEMA)


@pytest.fixture
def graph():
    return yaml.safe_load((EXAMPLES / "example_embedding_graph.yaml").read_text())


def lint(doc, tmp_path, sv, context, profile=None):
    path = tmp_path / "embedding.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return lp.lint(path, context[0], sv, context[1], profile)


EMBEDDING = {
    "name": "Embedding of a gene set",
    "embedding_of": "dapper:GeneSet.sKbhzueXIbO9Xms62_nyJxlZ13en1fRO",
    "embedding_model": "toy-4d",
    "embedding_provider": "example",
    "dimensions": 4,
    "dtype": "float16",
    "normalization": "none",
    "embedded_text": "First illustrative gene set",
    "text_template": "example-v1",
    "vector_sha256": "6566c469ce79b2bc95f3ffcff7ab1167e1e894aeb4f05aea78c2a95ff074e131",
    "was_generated_by": "dapper:Activity.f3NvLfo3qeBImT1Iqolj0z3sdOc_mAnX",
    "has_vector_file": "dapper:File.aYgsG7KuOqG7gJ_yC9i9zCmvjMsTCMQ3",
    "vector_row": 0,
}


def test_relocating_the_vector_keeps_the_id(sv):
    original = compute_id(EMBEDDING, "Embedding", sv)
    moved = dict(EMBEDDING, has_vector_file="dapper:File.71tWGS-PbGUXPR1jD_ZsXMJDbsnWtr3N", vector_row=17)
    assert compute_id(moved, "Embedding", sv) == original


def test_different_vector_bytes_or_target_change_the_id(sv):
    original = compute_id(EMBEDDING, "Embedding", sv)
    assert compute_id(dict(EMBEDDING, vector_sha256="0" * 64), "Embedding", sv) != original
    assert compute_id(dict(EMBEDDING, embedding_of="dapper:GeneSet.w1bw7dosxdeYRx1V5p7UALgkxtTfdcKi"),
                      "Embedding", sv) != original
    assert compute_id(dict(EMBEDDING, embedded_text="Something else"), "Embedding", sv) != original


def test_attaching_an_embedding_keeps_the_target_id(sv):
    gene_set = {"name": "Gene set", "member_type": "gene", "members": ["EXGENE:Gene1"], "n_genes": 1}
    original = compute_id(gene_set, "GeneSet", sv)
    gene_set["has_embedding"] = ["dapper:Embedding.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    assert compute_id(gene_set, "GeneSet", sv) == original
    dataset = {"name": "Result", "has_embedding": ["dapper:Embedding.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]}
    assert compute_id(dataset, "Dataset", sv) == compute_id({"name": "Result"}, "Dataset", sv)


def test_example_mints_and_verifies_without_changing_gene_set_ids(graph, sv):
    before = {n["id"] for n in graph["gene_sets"]} | {graph["gene_set_collections"][0]["id"]}
    assign_ids(graph, sv)
    after = {n["id"] for n in graph["gene_sets"]} | {graph["gene_set_collections"][0]["id"]}
    assert before == after
    assert unknown_node_groups(graph) == {}
    assert verify(graph, sv) == []
    ids = {e["id"] for e in graph["embeddings"]}
    assert all(i.startswith("dapper:Embedding.") for i in ids)
    for gene_set in graph["gene_sets"]:
        assert set(gene_set["has_embedding"]) <= ids
    assert {e["object"] for e in graph["has_embedding_edges"]} == ids
    first = deepcopy(graph)
    assign_ids(graph, sv)
    assert graph == first


def test_example_lints_clean_and_gene_sets_stay_terminal(graph, tmp_path, sv, context):
    assign_ids(graph, sv)
    report = lint(graph, tmp_path, sv, context)
    assert report.errors == [], [f.message for f in report.errors]
    assert report.profile == "geneset"
    vocab = context[0]
    doc = lp.Document.load(tmp_path / "embedding.yaml", vocab)
    terminals = lp._terminal_ids(doc, vocab.profiles["geneset"], vocab)
    # Embedding a collection does not make it upstream of anything.
    assert graph["gene_set_collections"][0]["id"] in terminals
    assert not any(t.startswith("dapper:Embedding.") for t in terminals)


@pytest.mark.parametrize("missing", ["has_vector_file", "vector_row"])
def test_vector_locator_requires_both_parts(missing, graph, tmp_path, sv, context):
    del graph["embeddings"][0][missing]
    assign_ids(graph, sv)
    assert any(f.check == "nodes" for f in lint(graph, tmp_path, sv, context).errors)


def test_backlink_must_agree_with_embedding_of(graph, tmp_path, sv, context):
    graph["gene_sets"][0]["has_embedding"] = ["dapper:Embedding.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    assign_ids(graph, sv)
    assert lint(graph, tmp_path, sv, context).errors
