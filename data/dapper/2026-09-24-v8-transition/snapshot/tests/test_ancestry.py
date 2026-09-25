"""Ancestry scope, ontology mapping strength, and DIG export consistency."""
from copy import deepcopy

import pytest
import yaml

from conftest import EXAMPLES, SCHEMA
from dapper_identity import assign_ids, compute_id, verify
import lint_provenance as lp
import lint_dig_ancestry as dig


CODES = ("AA", "AF", "EA", "EU", "GME", "HS", "Mixed", "SA", "SSAF")


@pytest.fixture(scope="module")
def ancestry_validator():
    return lp.build_validator(SCHEMA)


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
@pytest.mark.parametrize("code", [None, *CODES])
def test_ancestry_accepts_known_codes_or_omission(class_name, code, ancestry_validator):
    node = {"id": "urn:test:ancestry", "name": "Genetic analysis"}
    if code is not None:
        node["ancestry"] = code
    assert not ancestry_validator.validate(node, class_name).results


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
@pytest.mark.parametrize("code", ["aa", "mixed", "AFR", "unknown", ["AA", "EU"], 42])
def test_ancestry_rejects_unrecognized_values(class_name, code, ancestry_validator):
    assert ancestry_validator.validate(
        {"id": "urn:test:ancestry", "ancestry": code}, class_name).results


@pytest.mark.parametrize("class_name", ["Dataset", "Activity"])
def test_ancestry_constitutes_identity(class_name, sv):
    node = {"name": "Genetic analysis"}
    ids = {compute_id(node, class_name, sv)}
    ids.update(compute_id({**node, "ancestry": code}, class_name, sv) for code in CODES)
    assert len(ids) == 1 + len(CODES)


def test_mapping_strength_does_not_equate_ethnicity_or_admixture(sv):
    values = sv.get_enum("AncestryEnum").permissible_values
    assert set(values) == set(CODES)
    assert values["AA"].meaning == "HANCESTRO:0016"
    assert values["AF"].meaning == "HANCESTRO:0010"
    assert values["HS"].meaning is None
    assert values["HS"].close_mappings == ["HANCESTRO:0014"]
    assert values["Mixed"].meaning is None
    assert not values["Mixed"].exact_mappings
    assert "ancestry" not in sv.class_slots("File")
    assert "ancestry" not in sv.class_slots("DrsObject")


def test_example_annotates_only_explicitly_scoped_records(sv):
    doc = yaml.safe_load((EXAMPLES / "example_bottom_line_af_aa.yaml").read_text())
    assert doc["datasets"][0]["ancestry"] == "AA"
    assert sum("ancestry" in n for n in doc["datasets"]) == 7
    assert sum("ancestry" in n for n in doc["activities"]) == 5
    for node in doc["datasets"][1:] + doc["activities"]:
        assert (node.get("ancestry") == "AA") == ("ancestry=AA" in node["description"])
    assert all("ancestry" not in f for f in doc["files"])
    assert not verify(doc, sv)
    original = deepcopy(doc)
    assign_ids(doc, sv)
    assert doc == original


@pytest.mark.parametrize("location,expected", [
    ("s3://dig-open-bottom-line-analysis/bottom-line/AA/AF.sumstats.tsv.gz", "AA"),
    ("s3://dig-open-bottom-line-analysis-stg/bottom-line/AF/AA.sumstats.tsv.gz", "AF"),
    ("s3://dig-open-bottom-line-analysis/bottom-line/Mixed/", "Mixed"),
    ("s3://dig-open-bottom-line-analysis/bottom-line/unknown/AF.sumstats.tsv.gz", "unknown"),
    ("s3://other-bucket/bottom-line/AA/AF.sumstats.tsv.gz", None),
    ("s3://dig-open-bottom-line-analysis/input-sumstats/study_AA/", None),
    ("s3://dig-open-bottom-line-analysis/bottom-line/AA/nested/AF.sumstats.tsv.gz", None),
    ("s3://dig-open-bottom-line-analysis/bottom-line/AA/metadata.json", None),
    ("https://example.org/bottom-line/AA/AF.sumstats.tsv.gz", None),
])
def test_dig_parser_reads_only_the_documented_path_segment(location, expected):
    assert dig.export_ancestry(location) == expected


@pytest.mark.parametrize("reified", [False, True])
@pytest.mark.parametrize("declared,folder,errors", [
    ("AA", "AA", 0), ("EU", "AA", 1), ("Mixed", "Mixed", 0),
    (None, "AA", 0), ("AA", "unknown", 1), ("AA", "aa", 1),
])
def test_dig_distribution_check(declared, folder, errors, reified, tmp_path, sv, ancestry_validator):
    raw = yaml.safe_load((EXAMPLES / "example_bottom_line_af_aa.yaml").read_text())
    dataset = raw["datasets"][0]
    if declared is None:
        dataset.pop("ancestry")
    else:
        dataset["ancestry"] = declared
    raw["files"][0]["location"] = (
        f"s3://dig-open-bottom-line-analysis/bottom-line/{folder}/AF.sumstats.tsv.gz")
    if reified:
        raw["has_file_edges"] = [{"subject": dataset["id"],
                                  "predicate": sv.expand_curie("dapper:hasFile"),
                                  "object": dataset.pop("has_file")[0]}]
    assign_ids(raw, sv)
    original = deepcopy(raw)
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump(raw))
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    # S3 conventions must not affect generic DAPPER validation.
    report = lp.lint(path, vocab, sv, ancestry_validator)
    assert not report.errors, report.findings
    doc = lp.Document.load(path, vocab)
    dig.check_ancestry(doc, vocab, sv, report)
    assert len(report.errors) == errors, report.findings
    assert doc.raw == original


def test_dig_checks_dataset_prefix_without_distribution(tmp_path, sv):
    path = tmp_path / "candidate.yaml"
    path.write_text(yaml.safe_dump({"datasets": [{"id": "urn:example:data", "ancestry": "AA",
        "location": "s3://dig-open-bottom-line-analysis/bottom-line/EU/"}]}))
    vocab = lp.Vocabulary.build(sv, yaml.safe_load(lp.PROFILES_PATH.read_text()))
    report = lp.Report(path=path)
    dig.check_ancestry(lp.Document.load(path, vocab), vocab, sv, report)
    assert len(report.errors) == 1
    assert report.errors[0].check == "dig-ancestry"
