"""Trait scope references a catalog without introducing local trait nodes."""
from copy import deepcopy
import importlib.util

import pytest
import yaml

from conftest import EXAMPLES, REPO_ROOT, SCHEMA
from dapper_identity import assign_ids, compute_id, verify
import lint_provenance as lp


AF = "KPN.TRAIT:0000096"
KPN_BASE = "https://broadinstitute.github.io/kpn-data-models/kpn.trait/"


@pytest.fixture(scope="module")
def trait_validator():
    return lp.build_validator(SCHEMA)


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
@pytest.mark.parametrize("trait", [None, AF, KPN_BASE + "0000096/", "EFO:0000275"])
def test_optional_catalog_trait_reference(class_name, trait, trait_validator):
    node = {"id": "urn:test:trait", "name": "Trait result"}
    if trait is not None:
        node["trait"] = trait
    assert not trait_validator.validate(node, class_name).results


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
@pytest.mark.parametrize("trait", ["AF", "atrial fibrillation", "KPN.TRAIT:", [AF], 96])
def test_trait_requires_one_identifier(class_name, trait, trait_validator):
    assert trait_validator.validate({"id": "urn:test:trait", "trait": trait}, class_name).results


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
def test_trait_scope_affects_identity(class_name, sv):
    base = {"name": "Trait result", "ancestry": "AA"}
    ids = {compute_id(base, class_name, sv),
           compute_id({**base, "trait": AF}, class_name, sv),
           compute_id({**base, "trait": "KPN.TRAIT:0000001"}, class_name, sv)}
    assert len(ids) == 3


def test_af_example_references_atrial_fibrillation_not_hearing_loss(sv, trait_validator):
    path = EXAMPLES / "example_bottom_line_af_aa.yaml"
    doc = yaml.safe_load(path.read_text())
    assert all(n["trait"] == AF for n in doc["datasets"] + doc["activities"])
    assert all("trait" not in n for n in doc["files"])
    assert sv.expand_curie(AF) == KPN_BASE + "0000096"
    assert verify(doc, sv) == []
    original = deepcopy(doc)
    assign_ids(doc, sv)
    assert doc == original
    # Catalog references stay external, without dangling-node errors.
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    report = lp.lint(path, vocab, sv, trait_validator)
    assert not report.findings, report.findings


def test_portal_inherits_trait_context_and_preserves_catalog_reference():
    spec = importlib.util.spec_from_file_location("trait_portal", REPO_ROOT / "portal/build.py")
    portal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal)
    schema = portal.load_schema()
    assert "trait" in schema["Dataset"]["attributes"]
    assert "trait" in schema["Activity"]["attributes"]
    assert "trait" not in schema["File"]["attributes"]
    graph = portal.build_graph(next(g for g in portal.GRAPH_DOCS if g["key"] == "bottom_line_af_aa"))
    result = next(n for n in graph["nodes"] if n["id"] == graph["start"])
    assert result["fields"]["trait"] == AF
