"""Exact citation targets, metadata isolation, text anchors, and gap context."""
from copy import deepcopy
import json

import pytest
import yaml
from jsonschema import Draft202012Validator
from linkml.generators.jsonschemagen import JsonSchemaGenerator

from conftest import EXAMPLES, REPO_ROOT, SCHEMA
from citation_metadata import SCHEMA_PATH, check_citation_metadata, check_citation_registry_links
from dapper_identity import DOC_GROUPS, assign_ids, verify
from document_prefixes import expand_document, transform_identifiers
from scientific_claims import assemble_cited_text, check_scientific_content, index_document
import lint_provenance as lp


@pytest.fixture
def document():
    return yaml.safe_load((EXAMPLES / "example_scientific_account.yaml").read_text())


@pytest.fixture
def record():
    return json.loads((REPO_ROOT / "schema/citations/example-record.json").read_text())


@pytest.fixture(scope="module")
def schema():
    return json.loads(JsonSchemaGenerator(str(SCHEMA)).serialize())


def errors(schema, cls, node):
    return list(Draft202012Validator({**schema, "$ref": f"#/$defs/{cls}"}).iter_errors(node))


def test_gap_context_is_inherited_optional_and_affects_scientific_identity(document, sv, schema):
    gap = document["knowledge_gaps"][0]
    assert not errors(schema, "KnowledgeGap", gap)
    question = {k: v for k, v in gap.items() if k not in {"gap_description", "gap_kind"}}
    assert not errors(schema, "Question", question)
    question.pop("about_entities")
    assert not errors(schema, "Question", question)
    assert errors(schema, "KnowledgeGap", {**gap, "gap_kind": "RESOLVED"})
    before = deepcopy(document)
    gap["gap_kind"] = "HUMAN_MODEL_MISMATCH"
    gap["about_entities"].append("urn:example:disease:Y")
    assign_ids(document, sv)
    assert document["knowledge_gaps"][0]["id"] != before["knowledge_gaps"][0]["id"]
    assert document["claims"] == before["claims"]
    assert document["paragraphs"][0]["citations"][0]["target_id"] == gap["id"]
    assert not verify(document, sv)
    assert not check_scientific_content(index_document(document))


@pytest.mark.parametrize("field,value", [("target_id", ""), ("citation_metadata_revision", 0),
    ("start", -1), ("end", 0), ("start", 1.5), ("citation_metadata_revision", True)])
def test_occurrence_shape_rejects_invalid_fields(document, schema, field, value):
    paragraph = document["paragraphs"][0]
    paragraph["citations"][0][field] = value
    assert errors(schema, "Paragraph", paragraph)


@pytest.mark.parametrize("field", ["target_id", "citation_metadata_revision", "start", "end"])
def test_occurrence_requires_target_revision_and_span(document, schema, field):
    paragraph = document["paragraphs"][0]
    paragraph["citations"][0].pop(field)
    assert errors(schema, "Paragraph", paragraph)


@pytest.mark.parametrize("mutation,expected", [("proposition", "must resolve"), ("missing", "no local record"),
    ("backwards", "citation span"), ("past_end", "citation span"), ("quote", "exact_text"), ("duplicate", "duplicate")])
def test_cross_record_citation_errors(document, mutation, expected):
    paragraph = document["paragraphs"][0]
    citation = paragraph["citations"][0]
    if mutation == "proposition": citation["target_id"] = document["propositions"][0]["id"]
    if mutation == "missing": citation["target_id"] = "dapper:Claim." + "a" * 32
    if mutation == "backwards": citation["end"] = citation["start"]
    if mutation == "past_end": citation["end"] = len(paragraph["text"]) + 1
    if mutation == "quote": citation["exact_text"] = "A different claim."
    if mutation == "duplicate": paragraph["citations"].append(deepcopy(citation))
    problems = check_scientific_content(index_document(document))
    assert any(expected in message for _, message in problems), problems


def test_unicode_offsets_repeated_targets_and_coincident_citations(document):
    gap = document["knowledge_gaps"][0]["id"]
    claim = document["claims"][0]["id"]
    pin = lambda target: {"target_id": target, "citation_metadata_revision": 1}
    assembled = assemble_cited_text([
        {"text": "🧬 β-cell question?", "citations": [pin(gap)]},
        {"text": "A result.", "citations": [pin(claim)]},
        {"text": "A synthesis.", "citations": [pin(claim), pin(gap)]},
    ])
    document["paragraphs"][0].update(assembled)
    assert assembled["citations"][1]["start"] == len("🧬 β-cell question? ")
    assert not check_scientific_content(index_document(document))
    for citation in assembled["citations"]:
        assert assembled["text"][citation["start"]:citation["end"]] == citation["exact_text"]


def test_citation_revision_changes_only_paragraph_identity(document, sv):
    before = deepcopy(document)
    document["paragraphs"][0]["citations"][0]["citation_metadata_revision"] = 2
    assign_ids(document, sv)
    assert document["paragraphs"][0]["id"] != before["paragraphs"][0]["id"]
    for group in ("claims", "propositions", "knowledge_gaps", "scientific_accounts"):
        assert document[group] == before[group]
    assert not verify(document, sv)


def test_prefix_export_handles_inline_citations_and_linter_checks_them(document, sv, tmp_path):
    expanded = expand_document(document, sv)
    normalized, problems = transform_identifiers(expanded, sv, DOC_GROUPS, compact_dapper=True)
    assert not problems
    assert not check_scientific_content(index_document(normalized))
    assert not verify(expanded, sv)
    path = tmp_path / "account.yaml"
    expanded["paragraphs"][0]["citations"][0]["end"] = 100000
    assign_ids(expanded, sv, expanded=True, preserve_external=True)
    path.write_text(yaml.safe_dump(expanded))
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    report = lp.lint(path, vocab, sv, lp.build_validator(SCHEMA))
    assert any(e.check == "scientific-content" and "citation span" in e.message for e in report.errors)


def test_registry_schema_and_example_are_valid_and_separate(document, record, sv):
    Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text()))
    assert not check_citation_metadata(record)
    assert record["target_id"] == document["knowledge_gaps"][0]["id"]
    before = deepcopy(document)
    record.update(metadata_revision=2, title="An editorially shortened inquiry")
    assert not check_citation_metadata(record)
    assert not verify(document, sv)
    assert document == before
    assert "CitationOccurrence" not in DOC_GROUPS.values()


def test_citations_connect_prior_questions_without_changing_account_membership(document, sv, tmp_path):
    prior = {"id": "urn:example:prior-question", "text": "Which other processes might be involved?"}
    document["questions"] = [prior]
    document["paragraphs"][0]["citations"].append({
        **document["paragraphs"][0]["citations"][0], "target_id": prior["id"]})
    before = deepcopy(document["scientific_accounts"])
    assign_ids(document, sv)
    assert document["scientific_accounts"] == before
    path = tmp_path / "prior-question.yaml"
    path.write_text(yaml.safe_dump(document))
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    report = lp.lint(path, vocab, sv, lp.build_validator(SCHEMA))
    assert not report.errors


def test_native_citation_date_uses_observed_mint_and_retains_publication_date(record):
    source = {"source_ref": "urn:example:registry-event"}
    record.update(first_minted_at="2026-09-23T23:50:00Z", published_at="2026-09-24T10:00:00Z",
                  issued_date="2026-09-23", issued_basis="first_minted_at", publication_state="published",
                  date_provenance={k: source for k in ["first_minted_at", "published_at"]})
    assert not check_citation_metadata(record)
    record["issued_date"] = "2026-09-24"
    assert any("matching UTC mint date" in e for e in check_citation_metadata(record))
    record.update(issued_date=None, issued_basis="unknown")
    assert any("native objects" in e for e in check_citation_metadata(record))


@pytest.mark.parametrize("change", [
    {"target_class": "Question"}, {"target_id": "dapper:KnowledgeGap.EXAMPLE_DIGEST"},
    {"metadata_revision": 0}, {"doi": "dapper:KnowledgeGap.digest"},
    {"first_minted_at": "2026-09-24T10:00:00+02:00"}, {"issued_date": "2026-02-30"},
    {"issued_date": "2026-09-24"}, {"published_at": "2026-09-24T10:00:00Z"},
])
def test_registry_rejects_inconsistent_or_invented_metadata(record, change):
    record.update(change)
    assert check_citation_metadata(record)


def test_dates_retain_original_issue_and_require_provenance(record):
    source = {"source_ref": "https://example.org/source/revision/123"}
    record.update(origin="imported", original_issued_date="2020-03-04", imported_at="2026-09-24T10:00:00Z",
                  first_minted_at="2026-09-24T10:00:00Z", issued_date="2020-03-04", issued_basis="original_issued_date",
                  date_provenance={k: source for k in ["original_issued_date", "imported_at", "first_minted_at"]})
    assert not check_citation_metadata(record)
    record.update(issued_basis="first_minted_at", issued_date="2026-09-24")
    assert any("original issue date" in e for e in check_citation_metadata(record))
    record.update(issued_basis="original_issued_date", issued_date="2020-03-04")
    record["date_provenance"].pop("original_issued_date")
    assert any("source provenance" in e for e in check_citation_metadata(record))


def test_contributors_preserve_roles_order_and_orcid_source(record):
    source = {"source_ref": "urn:example:identity-observation:1"}
    person = {"agent_id": "urn:example:person", "kind": "person", "given_name": "Example", "family_name": "Researcher",
              "roles": ["agent_operator", "publisher"], "source": source,
              "orcid": {"id": "https://orcid.org/0000-0002-1825-0097", "status": "supplied", "source": source}}
    software = {"agent_id": "urn:example:software", "kind": "software", "display_name": "Illustrative agent",
                "roles": ["ai_generator"], "model_version": None, "source": source}
    record["byline"] = [person, software]
    before = deepcopy(record)
    assert not check_citation_metadata(record)
    assert record == before
    software["orcid"] = person["orcid"]
    assert check_citation_metadata(record)
    software.pop("orcid")
    person["orcid"].pop("source")
    assert check_citation_metadata(record)


def test_renderer_cannot_silently_use_latest_metadata_revision(document, record):
    paragraph = document["paragraphs"][0]
    records = []
    for citation in paragraph["citations"]:
        records.append({**record, "target_id": citation["target_id"],
                        "target_class": citation["target_id"].split(":")[1].split(".")[0]})
    assert not check_citation_registry_links(paragraph, records)
    records[0]["metadata_revision"] = 2
    assert len(check_citation_registry_links(paragraph, records)) == 1
    records[0]["metadata_revision"] = 1
    records.append(deepcopy(records[0]))
    assert any("duplicate" in e for e in check_citation_registry_links(paragraph, records))
