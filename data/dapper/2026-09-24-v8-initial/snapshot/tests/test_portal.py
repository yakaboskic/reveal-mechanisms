"""Portal metadata must link to the same definitions as the model navigator."""
import importlib.util
import pytest
import yaml

from conftest import REPO_ROOT
from test_model_uris import model_uris


spec = importlib.util.spec_from_file_location("portal", REPO_ROOT / "portal/build.py")
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)


def test_portal_vocabulary_agrees_with_namespace(sv):
    routes = model_uris.namespace_routes(sv)
    for term, url in portal.vocabulary_links(portal.read_schema()).items():
        assert routes[term] == "https://broadinstitute.github.io/dapper/" + url
    assert "File.example" not in routes


def test_inherited_enum_links_preserve_mapping_strength():
    classes = portal.load_schema()
    ancestry = classes["Dataset"]["attributes"]["ancestry"]
    assert ancestry["url"] == "model/reference/slots/ancestry/"
    assert ancestry["enum"]["url"] == "model/reference/enums/AncestryEnum/"
    values = ancestry["enum"]["values"]
    assert values["AA"]["meaning"] == "HANCESTRO:0016"
    assert "meaning" not in values["HS"]
    assert values["HS"]["mappings"] == [{"relation": "close", "target": "HANCESTRO:0014"}]
    assert "meaning" not in values["Mixed"]
    assert not values["Mixed"]["mappings"]
    assert classes["Activity"]["attributes"]["ancestry"] == ancestry


def test_embedded_data_cannot_close_script_element():
    html = portal.render({"description": "</script><script>alert('example')</script>"}, "")
    assert "</script><script>alert" not in html
    assert r"\u003c/script>" in html


@pytest.mark.parametrize("spec", portal.GRAPH_DOCS, ids=lambda spec: spec["key"])
def test_portal_preserves_original_statements_independently_of_layout(spec):
    raw = yaml.safe_load((portal.EXAMPLES / spec["file"]).read_text())
    graph = portal.build_graph(spec)
    triples = {(e["subject"], e["predicate"], e["object"]) for e in graph["edges"]}
    ids = {n["id"] for n in graph["nodes"]}
    defaults = portal.edge_predicates(portal.read_schema())
    for group, (cls, _) in portal.EDGE_GROUPS.items():
        for e in raw.get(group) or []:
            if e["subject"] in ids and e["object"] in ids:
                assert (e["subject"], e.get("predicate") or defaults[cls], e["object"]) in triples
    for n in graph["nodes"]:
        for field, (predicate, _) in portal.INLINE_LINKS.items():
            value = n["fields"].get(field)
            for target in value if isinstance(value, list) else [value]:
                if isinstance(target, str) and target in ids and target != n["id"]:
                    assert (n["id"], predicate, target) in triples


def test_inline_predicate_labels_agree_with_schema(sv):
    for name, (predicate, _) in portal.INLINE_LINKS.items():
        assert sv.get_uri(sv.get_slot(name)) == predicate, name
