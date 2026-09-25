"""Scientific content, hypothesis roles, evidence use, and end-to-end provenance."""
from copy import deepcopy
import importlib.util
import json

import pytest
import yaml
from jsonschema import Draft202012Validator
from linkml.generators.jsonschemagen import JsonSchemaGenerator

from conftest import REPO_ROOT
from dapper_identity import DOC_GROUPS, assign_ids, compute_id, verify
from scientific_claims import check_scientific_content, index_document, render_account


@pytest.fixture(scope="module")
def claim_schema():
    return json.loads(JsonSchemaGenerator(str(REPO_ROOT / "schema/dapper.yaml")).serialize())


@pytest.fixture
def claim_example():
    return yaml.safe_load((REPO_ROOT / "schema/examples/example_pigean_claims.yaml").read_text())


def errors(schema, cls, node):
    return list(Draft202012Validator({**schema, "$ref": f"#/$defs/{cls}"}).iter_errors(node))


def test_all_worked_records_validate_against_imported_schema(claim_schema, claim_example):
    checked = 0
    for group, cls in DOC_GROUPS.items():
        for node in claim_example.get(group, []):
            assert not errors(claim_schema, cls, node), (cls, errors(claim_schema, cls, node))
            checked += 1
    assert checked == 24


@pytest.mark.parametrize("kind", ["PROBABILITY", "POSTERIOR_PROBABILITY"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_probabilities_reject_out_of_range_values(claim_schema, claim_example, kind, value):
    score = claim_example["claim_scores"][0]
    score.update(score_kind=kind, value=value)
    assert errors(claim_schema, "ClaimScore", score)


@pytest.mark.parametrize("kind,value", [("PROBABILITY", 0.0), ("POSTERIOR_PROBABILITY", 1.0),
                                        ("EFFECT_ESTIMATE", -2.5), ("LOADING", 8.4), ("SCORE", 12.0)])
def test_scores_keep_their_own_scales(claim_schema, claim_example, kind, value):
    score = claim_example["claim_scores"][0]
    score.update(score_kind=kind, value=value)
    assert not errors(claim_schema, "ClaimScore", score)


@pytest.mark.parametrize("field", ["proposition", "was_generated_by", "was_attributed_to"])
def test_claim_cannot_lose_its_target_or_provenance(claim_schema, claim_example, field):
    claim = claim_example["claims"][0]
    del claim[field]
    assert errors(claim_schema, "Claim", claim)


@pytest.mark.parametrize("field", ["subject_entity", "relation", "object_entity"])
def test_partial_structured_proposition_is_rejected(claim_schema, claim_example, field):
    proposition = claim_example["propositions"][0]
    del proposition[field]
    assert errors(claim_schema, "Proposition", proposition)


@pytest.mark.parametrize("change", [{"component_claims": []}, {"context": ""},
                                    {"proposition": "urn:unwanted"}, {"has_score": []}])
def test_accounts_require_claims_and_context_but_are_not_claims(claim_schema, claim_example, change):
    account = claim_example["scientific_accounts"][0]
    account.update(change)
    assert errors(claim_schema, "ScientificAccount", account)


def test_one_claim_and_no_conclusion_is_a_valid_account(claim_schema, claim_example):
    account = claim_example["scientific_accounts"][0]
    account["component_claims"] = account["component_claims"][:1]
    assert not errors(claim_schema, "ScientificAccount", account)


def test_changed_assessment_preserves_propositions_but_changes_dependent_claims(sv, claim_example):
    before = deepcopy(claim_example)
    claim_example["claim_scores"][0]["value"] = 0.5
    assign_ids(claim_example, sv)
    assert claim_example["propositions"] == before["propositions"]
    assert claim_example["claims"][0]["id"] != before["claims"][0]["id"]
    assert claim_example["claims"][1:] == before["claims"][1:]
    assert claim_example["scientific_accounts"][0]["id"] != before["scientific_accounts"][0]["id"]
    assert verify(claim_example, sv) == []
    reminted = deepcopy(claim_example)
    assign_ids(claim_example, sv)
    assert claim_example == reminted


def test_publication_back_reference_does_not_change_claim_identity(sv, claim_example):
    claim = claim_example["claims"][0]
    original = compute_id(claim, "Claim", sv)
    claim["asserted_in"] = ["urn:example:publication"]
    assert compute_id(claim, "Claim", sv) == original


def test_pigean_account_reaches_both_raw_inputs_without_combining_scores(sv, claim_example):
    nodes = {n["id"]: (cls, n) for g, cls in DOC_GROUPS.items() for n in claim_example.get(g, [])}
    composite = claim_example["scientific_accounts"][0]
    assert len(set(composite["component_claims"])) == 3
    assert "has_score" not in composite
    for claim_id in composite["component_claims"]:
        assert nodes[claim_id][0] == "Claim"
        claim = nodes[claim_id][1]
        assert nodes[claim["proposition"]][0] == "Proposition"
        assert nodes[claim["has_score"][0]][0] == "ClaimScore"

    deps = {nid: set() for nid in nodes}
    for nid, (cls, node) in nodes.items():
        for slot in sv.class_induced_slots(cls):
            if slot.is_a != "relationship":
                continue
            raw = node.get(slot.name)
            for ref in raw if isinstance(raw, list) else [raw]:
                if ref in nodes:
                    deps[nid].add(ref)
                elif isinstance(ref, str) and ref.startswith("dapper:"):
                    pytest.fail(f"Dangling reference: {ref}")
    for group in ("used_edges", "was_generated_by_edges"):
        for edge in claim_example[group]:
            assert edge["subject"] in nodes and edge["object"] in nodes
            deps[edge["subject"]].add(edge["object"])
    reached, pending = set(), [composite["id"]]
    while pending:
        nid = pending.pop()
        if nid not in reached:
            reached.add(nid)
            pending.extend(deps[nid])
    filenames = {nodes[nid][1].get("filename") for nid in reached}
    assert {"expression.tsv", "raw-gwas.tsv", "processed-gwas.tsv", "gene-set-b.gmt"} <= filenames


def test_portal_loads_accounts_and_connects_claims():
    spec = importlib.util.spec_from_file_location("claims_portal", REPO_ROOT / "portal/build.py")
    portal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal)
    schema = portal.load_schema()
    assert {"hypothesis", "component_claims", "context", "was_generated_by"} <= set(schema["ScientificAccount"]["attributes"])
    example = next(g for g in portal.GRAPH_DOCS if g["key"] == "pigean_claims")
    graph = portal.build_graph(example)
    assert example["start"] in {n["id"] for n in graph["nodes"]}
    assert sum(e["predicate"] == "dapper:component_claims" for e in graph["edges"]) == 3


@pytest.fixture
def account_example():
    return yaml.safe_load((REPO_ROOT / "schema/examples/example_scientific_account.yaml").read_text())


@pytest.mark.parametrize("filename", ["example_scientific_account.yaml", "example_claim_provenance_trace.yaml"])
def test_migrated_scientific_documents_validate(claim_schema, filename):
    document = yaml.safe_load((REPO_ROOT / "schema/examples" / filename).read_text())
    for cls, node in index_document(document).values():
        assert not errors(claim_schema, cls, node), (cls, errors(claim_schema, cls, node))
    assert check_scientific_content(index_document(document)) == []


def test_retired_classes_are_replaced_and_models_do_not_assess(sv):
    assert not {"Hypothesis", "CompositeClaim"} & set(sv.all_classes())
    assert "Claim" not in sv.class_ancestors("ScientificAccount")
    model_slots = {s.name for s in sv.class_induced_slots("MechanisticModel")}
    assert not {"confidence", "status", "has_score", "has_evidence"} & model_slots


@pytest.mark.parametrize("framing", ["question", "hypothesis"])
def test_any_framing_role_is_sufficient(claim_schema, account_example, framing):
    account = account_example["scientific_accounts"][0]
    values = {"question": account["question"], "hypothesis": account["hypothesis"]}
    for key in values:
        account.pop(key, None)
    account[framing] = values[framing]
    assert not errors(claim_schema, "ScientificAccount", account)


def test_missing_and_blank_framing_is_rejected(claim_schema, account_example):
    account = account_example["scientific_accounts"][0]
    account.pop("question")
    account.pop("hypothesis")
    assert errors(claim_schema, "ScientificAccount", account)
    account["question"] = "  "
    assert errors(claim_schema, "ScientificAccount", account)


def test_hypothesis_role_reuses_the_assessed_proposition(account_example, sv):
    account = account_example["scientific_accounts"][0]
    target = account_example["claims"][1]["proposition"]
    assert account["hypothesis"] == target
    before = deepcopy(account_example["propositions"])
    account.pop("hypothesis")
    assign_ids(account_example, sv)
    assert account_example["propositions"] == before


@pytest.mark.parametrize("field", ["target_proposition", "direction", "explanation", "context"])
def test_source_claim_evidence_needs_an_explicit_interpretation(claim_schema, account_example, field):
    evidence = account_example["evidence_items"][0]
    evidence.pop(field)
    assert errors(claim_schema, "EvidenceItem", evidence)


@pytest.mark.parametrize("mutation, expected", [
    ("conclusion", "subset"), ("duplicate", "distinct"),
    ("hypothesis", "must resolve to Proposition"),
    ("target", "targets a different proposition"),
    ("cycle", "circular evidential support"),
    ("all_conclusions", "at least one claim"),
])
def test_cross_record_constraints(account_example, mutation, expected):
    account = account_example["scientific_accounts"][0]
    result, conclusion = account_example["claims"]
    evidence = account_example["evidence_items"][0]
    if mutation == "conclusion": account["conclusion_claims"] = ["urn:absent"]
    if mutation == "duplicate": account["component_claims"].append(result["id"])
    if mutation == "hypothesis": account["hypothesis"] = result["id"]
    if mutation == "target": evidence["target_proposition"] = result["proposition"]
    if mutation == "cycle": evidence["source_claims"] = [conclusion["id"]]
    if mutation == "all_conclusions": account["conclusion_claims"] = account["component_claims"][:]
    problems = check_scientific_content(index_document(account_example))
    assert any(expected in message for _, message in problems), problems


def test_paragraph_edits_do_not_change_account_or_propositions(account_example, sv):
    before = deepcopy(account_example)
    account_example["paragraphs"][0]["text"] += " This is another expression."
    assign_ids(account_example, sv)
    assert account_example["scientific_accounts"] == before["scientific_accounts"]
    assert account_example["propositions"] == before["propositions"]
    assert account_example["paragraphs"][0]["id"] != before["paragraphs"][0]["id"]


def test_renderer_preserves_hypothesis_claim_interpretation_and_assumptions(account_example):
    segments = render_account(account_example)
    roles = [s["role"] for s in segments]
    assert roles.index("hypothesis") < roles.index("claim") < roles.index("conclusion")
    assert {"interpretation", "evidence_context", "assumption"} <= set(roles)
    assert all(s["references"] for s in segments)
    assert " ".join(s["text"] for s in segments) == account_example["paragraphs"][0]["text"]


def test_renderer_does_not_assert_a_disputed_target(account_example):
    claim = account_example["claims"][1]
    claim.pop("statement")
    claim["direction"] = "DISPUTES"
    segments = render_account(account_example)
    conclusion = next(s for s in segments if s["role"] == "conclusion")
    assert conclusion["text"].startswith("Assessment of the proposition:")
    assert any(s["text"] == "Assessment direction: disputes." for s in segments)


def test_renderer_keeps_each_pigean_score_separate(claim_example):
    segments = render_account(claim_example)
    scores = [s for s in segments if s["role"] == "score"]
    assert len(scores) == 3
    assert all(s["references"][0] in {c["id"] for c in claim_example["claims"]} for s in scores)


def test_knowledge_gap_is_a_question_and_works_as_account_framing(sv, claim_schema, account_example):
    gap = account_example["knowledge_gaps"][0]
    assert "Question" in sv.class_ancestors("KnowledgeGap")
    assert account_example["scientific_accounts"][0]["question"] == gap["id"]
    assert not errors(claim_schema, "KnowledgeGap", gap)
    assert check_scientific_content(index_document(account_example)) == []
    without_inherited_text = {k: v for k, v in gap.items() if k != "text"}
    assert errors(claim_schema, "KnowledgeGap", without_inherited_text)
    without_gap = {k: v for k, v in gap.items() if k != "gap_description"}
    assert errors(claim_schema, "KnowledgeGap", without_gap)
    assert errors(claim_schema, "KnowledgeGap", {**gap, "confidence": 0.9})


def test_question_and_knowledge_gap_both_resolve_through_question_field(claim_schema, account_example):
    gap = account_example.pop("knowledge_gaps")[0]
    question = {k: v for k, v in gap.items() if k not in {"gap_description", "gap_kind"}}
    question["id"] = "urn:test:plain-question"
    account_example["questions"] = [question]
    account_example["scientific_accounts"][0]["question"] = question["id"]
    account_example["paragraphs"][0]["citations"][0]["target_id"] = question["id"]
    assert not errors(claim_schema, "Question", question)
    assert check_scientific_content(index_document(account_example)) == []
    segments = render_account(account_example)
    assert any(s["role"] == "framing" and s["text"] == question["text"] for s in segments)
    assert not any(s["role"] == "knowledge_gap" for s in segments)
