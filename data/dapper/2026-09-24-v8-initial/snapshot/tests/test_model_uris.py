"""Published definition links must resolve without changing RDF semantics."""
import importlib.util
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import pytest

from conftest import REPO_ROOT


spec = importlib.util.spec_from_file_location("model_uris", REPO_ROOT / "tools/model_uris.py")
model_uris = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_uris)

BASE = "https://broadinstitute.github.io/dapper/model/reference/"


@pytest.fixture(scope="module")
def generator():
    return model_uris.ModelDocGenerator(
        str(REPO_ROOT / "schema/dapper.yaml"), preserve_names=True,
        subfolder_type_separation=True, render_imports=True,
    )


@pytest.mark.parametrize("name,curie,path", [
    ("Edge", "dapper_class:Edge", "classes/Edge"),
    ("File", "dapper_class:File", "classes/File"),
    ("C2M2File", "dapper_class:C2M2File", "classes/C2M2File"),
    ("subject", "dapper_slot:subject", "slots/subject"),
    ("drs_representation", "dapper_slot:drs_representation", "slots/drs_representation"),
    ("ResourceTypeEnum", "dapper_enum:ResourceTypeEnum", "enums/ResourceTypeEnum"),
    ("AncestryEnum", "dapper_enum:AncestryEnum", "enums/AncestryEnum"),
    ("ancestry", "dapper_slot:ancestry", "slots/ancestry"),
    ("AncestryContext", "dapper_class:AncestryContext", "classes/AncestryContext"),
    ("trait", "dapper_slot:trait", "slots/trait"),
    ("TraitContext", "dapper_class:TraitContext", "classes/TraitContext"),
    ("string", "dapper_type:string", "types/string"),
])
def test_documentation_uris_preserve_page_names(generator, name, curie, path):
    element = generator.schemaview.get_element(name)
    assert generator.uri(element, expand=False) == curie
    assert generator.uri(element) == BASE + path


def test_documentation_does_not_replace_semantic_mappings(generator):
    sv = generator.schemaview
    assert sv.get_mappings("Edge")["self"] == ["rdf:Statement"]
    assert sv.get_mappings("Edge")["native"] == ["dapper:Edge"]
    assert sv.get_uri(sv.get_slot("subject")) == "rdf:subject"
    assert sv.get_uri(sv.get_class("Activity")) == "prov:Activity"
    assert sv.get_uri(sv.get_type("string")) == "xsd:string"
    assert sv.expand_curie("dapper:File.example") == (
        "https://broadinstitute.github.io/dapper/ns#File.example"
    )


def test_enum_documentation_links_meanings_and_preserves_close_mappings(generator):
    element = generator.schemaview.get_enum("AncestryEnum")
    original = generator._get_template("enum").render(
        gen=generator, element=element, schemaview=generator.schemaview)
    markdown = generator.enum_mapping_documentation(original, element)
    assert "[HANCESTRO:0016](http://purl.obolibrary.org/obo/HANCESTRO_0016)" in markdown
    assert "| HS | close | [HANCESTRO:0014](http://purl.obolibrary.org/obo/HANCESTRO_0014) |" in markdown
    assert "HANCESTRO:0306" not in markdown
    assert "Multiple ancestry groups" in markdown
    assert "African American / Afro-Caribbean" in markdown
    assert "| Mixed | — |" in markdown
    assert "| HS | — |" in markdown


def test_existing_vocabulary_and_predicate_aliases_resolve(sv):
    routes = model_uris.namespace_routes(sv)
    assert routes["Edge"] == BASE + "classes/Edge/"
    assert routes["subject"] == BASE + "slots/subject/"
    assert routes["ResourceTypeEnum"] == BASE + "enums/ResourceTypeEnum/"
    assert routes["drsRepresentation"] == BASE + "slots/drs_representation/"
    assert routes["fundedBy"] == BASE + "classes/FundedBy/"
    assert "File.example" not in routes


def test_imported_type_native_links_resolve_after_documentation_merge(generator):
    # DocGenerator merges the LinkML types into its root schema. Its native
    # mappings then use dapper:, even though the semantic URI remains xsd:.
    routes = model_uris.namespace_routes(generator.schemaview)
    assert routes["string"] == BASE + "types/string/"


def test_namespace_rejects_missing_documentation(sv, tmp_path):
    with pytest.raises(ValueError, match="was not built"):
        model_uris.write_namespace(sv, tmp_path)


def test_namespace_supports_local_preview_and_no_javascript(sv, tmp_path):
    routes = model_uris.namespace_routes(sv)
    for target in routes.values():
        relative = PurePosixPath(urlsplit(target).path).relative_to("/dapper")
        page = tmp_path / relative / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.touch()
    model_uris.write_namespace(sv, tmp_path)
    html = (tmp_path / "ns/index.html").read_text()
    data = re.search(r'<script id="namespace-routes" type="application/json">(.*?)</script>', html)
    redirects = json.loads(data.group(1))
    assert redirects["Edge"] == "../model/reference/classes/Edge/"
    assert '<li id="Edge"><a href="../model/reference/classes/Edge/">dapper:Edge</a></li>' in html
    assert len(redirects) == len(routes)


def test_namespace_rejects_ambiguous_native_names(sv):
    # A future same-name class and slot must not silently redirect one to the other.
    from copy import deepcopy
    from linkml_runtime.linkml_model.meta import SlotDefinition

    modified = deepcopy(sv)
    modified.add_slot(SlotDefinition(name="Edge"))
    # Native slot casing differs, so use an explicit alias to the existing class.
    modified.schema.slots["Edge"].slot_uri = "dapper:Edge"
    with pytest.raises(ValueError, match="Ambiguous DAPPER term"):
        model_uris.namespace_routes(modified)
