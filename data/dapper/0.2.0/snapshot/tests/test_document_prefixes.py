"""Declared namespaces, mixed URI/CURIE graphs, and identity-safe URI exports."""
from copy import deepcopy
import base64
import hashlib
import subprocess
import sys

import pytest
import yaml

from conftest import EXAMPLES, REPO_ROOT, SCHEMA
from dapper_identity import DAPPER_NAMESPACE, assign_ids, compact_identifier, digest_of, load_schema, verify
from document_prefixes import PrefixResolver, expand_document, transform_identifiers
import lint_provenance as lp


@pytest.fixture(scope="module")
def context(sv):
    return lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text())), lp.build_validator(SCHEMA)


@pytest.fixture
def graph(sv):
    doc = {
        "prefixes": {"local": "file:///work/", "catalog": "https://example.org/genes/"},
        "files": [{"id": "urn:input:raw", "filename": "input.tsv"},
                  {"id": "urn:output:gmt", "filename": "genesets.gmt", "md5": "a" * 32}],
        "activities": [{"id": "urn:run:convert", "name": "Generate a gene-set library",
                        "script_url": "local:convert.py", "command": "tool --arg unknown:literal"}],
        "gene_sets": [{"id": "urn:result:geneset", "name": "A readable name",
                       "alternate_identifier": ["unsigned_term_gene:opaque"],
                       "members": ["catalog:123"], "has_gmt_file": "urn:output:gmt"}],
        "used_edges": [{"subject": "urn:run:convert", "object": "urn:input:raw", "predicate": "prov:used"}],
        "was_generated_by_edges": [
            {"subject": "urn:result:geneset", "object": "urn:run:convert", "predicate": "prov:wasGeneratedBy"},
            {"subject": "urn:output:gmt", "object": "urn:run:convert", "predicate": "prov:wasGeneratedBy"}],
    }
    assign_ids(doc, sv)
    return doc


def report(doc, tmp_path, sv, context):
    path = tmp_path / "input.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    vocab, validator = context
    return lp.lint(path, vocab, sv, validator)


def test_document_prefixes_and_model_prefixes_are_both_accepted(graph, tmp_path, sv, context):
    assert not report(graph, tmp_path, sv, context).findings
    resolver = PrefixResolver(sv, graph)
    assert resolver.expand("local:file.tsv") == "file:///work/file.tsv"
    assert resolver.expand("HGNC:5") == "http://identifiers.org/hgnc/5"


@pytest.mark.parametrize("value", ["oops:value", "httpsx://example.org/file", "./relative", "/work/raw",
                                 "https:///no-host", "https://example.org/a b", "catalog:"])
def test_uri_fields_reject_unknown_or_incomplete_values(value, graph, tmp_path, sv, context):
    graph["activities"][0]["script_url"] = value
    assign_ids(graph, sv)
    errors = report(graph, tmp_path, sv, context).errors
    assert any(e.check == "prefixes" and e.where == "activities[0].script_url" for e in errors)


@pytest.mark.parametrize("declarations", [None, [], "bad", {"x": 42}, {"x:": "https://example.org/"},
    {"x": "relative/"}, {"x": "HGNC:"}, {"https": "https://example.org/"},
    {"prov": "https://example.org/wrong/"}, {"dapper": "https://example.org/records/"}])
def test_bad_or_conflicting_declarations_fail_even_unused(declarations, graph, tmp_path, sv, context):
    graph["prefixes"] = declarations
    assert any(e.check == "prefixes" and e.where.startswith("prefixes")
               for e in report(graph, tmp_path, sv, context).errors)
    with pytest.raises(ValueError):
        expand_document(graph, sv)


def test_unknown_prefixes_checked_in_ids_edges_and_relationship_lists(graph, tmp_path, sv, context):
    graph["files"][0]["id"] = "oops:input"
    graph["used_edges"][0]["object"] = "oops:input"
    graph["used_edges"][0]["predicate"] = "oops:used"
    graph["gene_sets"][0]["members"] = ["oops:gene"]
    errors = report(graph, tmp_path, sv, context).errors
    paths = {e.where for e in errors if e.check == "prefixes"}
    assert {"files[0].id", "used_edges[0].object", "used_edges[0].predicate", "gene_sets[0].members[0]"} <= paths


def test_uri_export_is_pure_idempotent_and_rewrites_changed_digests(graph, tmp_path, sv, context):
    before = deepcopy(graph)
    expanded = expand_document(graph, sv)
    assert graph == before
    assert "prefixes" not in expanded
    assert expanded["activities"][0]["script_url"] == "file:///work/convert.py"
    assert expanded["gene_sets"][0]["members"] == ["https://example.org/genes/123"]
    assert expanded["gene_sets"][0]["alternate_identifier"] == ["unsigned_term_gene:opaque"]
    assert expanded["activities"][0]["command"] == graph["activities"][0]["command"]
    assert compact_identifier(expanded["activities"][0]["id"]) != graph["activities"][0]["id"]
    assert expanded["gene_sets"][0]["has_gmt_file"] == expanded["files"][1]["id"]
    assert expanded["was_generated_by_edges"][0]["object"] == expanded["activities"][0]["id"]
    assert expanded == expand_document(expanded, sv)
    assert not verify(expanded, sv)
    assert not report(expanded, tmp_path, sv, context).findings


def test_mixed_full_and_compact_references_match_the_same_node(graph, tmp_path, sv, context):
    graph["used_edges"][0]["object"] = DAPPER_NAMESPACE + graph["files"][0]["id"].split(":", 1)[1]
    graph["prefixes"]["records"] = DAPPER_NAMESPACE
    graph["was_generated_by_edges"][0]["subject"] = graph["gene_sets"][0]["id"].replace("dapper:", "records:")
    assert not report(graph, tmp_path, sv, context).errors
    duplicate = deepcopy(graph["files"][0])
    duplicate["id"] = graph["used_edges"][0]["object"]
    graph["files"].append(duplicate)
    assert any(e.check == "duplicate-ids" for e in report(graph, tmp_path, sv, context).errors)
    with pytest.raises(ValueError, match="duplicate"):
        expand_document(graph, sv)


def test_expanded_ids_cannot_bypass_identity_and_dangling_checks(graph, tmp_path, sv, context):
    expanded = expand_document(graph, sv)
    expanded["files"][1]["md5"] = "b" * 32
    expanded["used_edges"][0]["object"] = DAPPER_NAMESPACE + "File." + "z" * 32
    checks = {e.check for e in report(expanded, tmp_path, sv, context).errors}
    assert {"identity", "refs"} <= checks


@pytest.mark.parametrize("value", ["a" * 32, "https://example.org/genesets.gmt", "urn:file:external",
                                 "dapper:File." + "z" * 32])
def test_gmt_requires_a_local_file_record_digest(value, graph, tmp_path, sv, context):
    graph["gene_sets"][0]["has_gmt_file"] = value
    assign_ids(graph, sv)
    assert any(e.check == "gmt-file" for e in report(graph, tmp_path, sv, context).errors)


def test_full_dapper_digest_substitution_preserves_existing_curie_behavior():
    digest = "a" * 32
    assert digest_of(DAPPER_NAMESPACE + "File." + digest) == digest_of("dapper:File." + digest) == digest
    assert digest_of("https://example.org/File." + digest) is None


def test_export_retains_external_record_identifiers(graph, tmp_path, sv, context):
    graph["files"][0]["id"] = "catalog:input"
    graph["used_edges"][0]["object"] = "https://example.org/genes/input"
    expanded = expand_document(graph, sv)
    assert expanded["files"][0]["id"] == "https://example.org/genes/input"
    assert not report(expanded, tmp_path, sv, context).errors


def test_nested_inlined_uri_slots_are_checked(sv):
    # Exercise an inlined schema class, not string searches across arbitrary JSON.
    from linkml_runtime.linkml_model.meta import ClassDefinition, SlotDefinition
    nested = load_schema(SCHEMA)
    nested.add_class(ClassDefinition(name="Wrapper", attributes={"inside": SlotDefinition(
        name="inside", range="Person", inlined=True)}))
    document = {"wrappers": [{"inside": {"orcid": "missing:123", "name": "label:untouched"}}]}
    _, errors = transform_identifiers(document, nested, {"wrappers": "Wrapper"})
    assert [path for path, _ in errors] == ["wrappers[0].inside.orcid"]


def test_claim_graph_export_preserves_dependent_reference_identity(sv, tmp_path, context):
    original = yaml.safe_load((EXAMPLES / "example_scientific_account.yaml").read_text())
    expanded = expand_document(original, sv)
    assert not verify(expanded, sv)
    assert not report(expanded, tmp_path, sv, context).errors


def test_cli_writes_only_a_valid_export_and_preserves_output_on_error(graph, tmp_path):
    source, output = tmp_path / "source.yaml", tmp_path / "expanded.yaml"
    source.write_text(yaml.safe_dump(graph))
    cmd = [sys.executable, str(REPO_ROOT / "schema/expand_prefixes.py"), str(source), "-o", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    before = output.read_bytes()
    del graph["prefixes"]
    source.write_text(yaml.safe_dump(graph))
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0 and "undeclared prefix" in result.stderr
    assert output.read_bytes() == before


def test_corrected_hubmap_example_and_export_are_clean(sv, tmp_path, context):
    original = yaml.safe_load((EXAMPLES / "example_geneset_hubmap.yaml").read_text())
    assert len(original["c2m2_files"]) == 1
    assert len(original["files"]) == 75
    assert all("persistent_id" not in n and "c2m2_uuid" not in n for n in original["files"])
    assert all("local_id" not in n for n in original["files"] + original["c2m2_files"])
    gene_set = original["gene_set_collections"][0]
    assert gene_set["name"] == "HuBMAP ASCT+B gene-set library (HZ1)"
    assert gene_set["alternate_identifier"] == ["unsigned_term_gene:2b85b11457a52cc7e0d6950a"]
    gmt = next(n for n in original["c2m2_files"] if n["filename"] == "genesets.gmt")
    content = (EXAMPLES / "data/hubmap_hz1.genesets.gmt").read_bytes()
    assert len(content) == gmt["size_in_bytes"]
    assert base64.b64encode(hashlib.md5(content).digest()).decode() == gmt["md5"]
    assert hashlib.sha256(content).hexdigest() == gmt["sha256"]
    rows = {r[0]: r[2:] for r in (line.split("\t") for line in content.decode().splitlines())}
    exported_gmt = next(n for n in original["files"] if n["filename"] == "genesets.dapper-ids.gmt")
    exported_content = (EXAMPLES / "data/hubmap_hz1.dapper-ids.gmt").read_bytes()
    assert len(exported_content) == exported_gmt["size_in_bytes"]
    assert base64.b64encode(hashlib.md5(exported_content).digest()).decode() == exported_gmt["md5"]
    assert hashlib.sha256(exported_content).hexdigest() == exported_gmt["sha256"]
    assert gene_set["has_gmt_file"] == exported_gmt["id"]
    exported_rows = {r[0]: r[2:] for r in (line.split("\t") for line in exported_content.decode().splitlines())}
    # Only first-column labels changed; retain every remaining byte, including line endings.
    assert [line.split(b"\t", 1)[1] for line in content.splitlines(keepends=True)] == [
        line.split(b"\t", 1)[1] for line in exported_content.splitlines(keepends=True)]
    assert len(original["gene_sets"]) == len(rows) == gene_set["n_sets"] == 358
    assert gene_set["n_genes"] == len({g for genes in rows.values() for g in genes}) == 964
    assert set(gene_set["members"]) == {n["id"] for n in original["gene_sets"]}
    assert set(exported_rows) == set(gene_set["members"])
    for row in original["gene_sets"]:
        assert row["gmt_entry"] == row["id"]
        source_genes = rows[row["alternate_identifier"][0]]
        assert exported_rows[row["gmt_entry"]] == source_genes
        assert row["members"] == ["HGNC.SYMBOL:" + g for g in source_genes]
        assert row["in_gene_set_collection"] == [gene_set["id"]]
        assert row["in_gmt_file"] == exported_gmt["id"]
        assert row["n_genes"] == len(set(row["members"]))
    assert not any("dcc_url" in node or "drc_url" in node
                   for group in ("files", "c2m2_files", "activities", "gene_set_collections", "gene_sets") for node in original[group])
    assert not report(original, tmp_path, sv, context).findings
    assert not verify(original, sv)
    assert all(a == b for a, b in assign_ids(deepcopy(original), sv).items())
    expanded = expand_document(original, sv)
    assert not report(expanded, tmp_path, sv, context).findings
    assert not verify(expanded, sv)
    assert all(n["location"].startswith("file:///humgen/")
               for n in expanded["files"] + expanded["c2m2_files"] if n["filename"] != "genesets.dapper-ids.gmt")
    expanded_gmt = next(n for n in expanded["files"] if n["filename"] == "genesets.dapper-ids.gmt")
    assert expanded_gmt["location"] == original["prefixes"]["export"] + "hubmap_hz1.dapper-ids.gmt"
    # A selector is the literal name in the physical file, even when URI expansion
    # changes the owning node's content-derived ID.
    for row, compact in zip(expanded["gene_sets"], original["gene_sets"]):
        assert row["gmt_entry"] == compact["gmt_entry"]
        assert row["in_gmt_file"] == expanded_gmt["id"]
        assert row["members"] == ["https://identifiers.org/hgnc.symbol:" + g
                                  for g in exported_rows[row["gmt_entry"]]]
    del original["prefixes"]["humgen"]
    assert sum(e.check == "prefixes" for e in report(original, tmp_path, sv, context).errors) == 75
