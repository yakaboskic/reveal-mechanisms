import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from reveal_backend.box_mcp import Ledger
from reveal_backend.evidence_package import EvidenceBuildError
from reveal_backend.scientific_grounding import MODEL, ScientificReviewUnavailable, review_account, review_evidence, validate_review, review_paragraph


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.document = {"claims": [{"id": "claim:1"}, {"id": "claim:2"}]}
        self.package = {"selection": {"knowledge_gap_id": "gap:1"},
                        "pigean": {"factor": {"score": 0.1}},
                        "external_evidence": {"selected_graphs": ["prokn"]}}
        self.evidence = {"package": self.package, "graph_calls": []}
        verdict = {"verdict": "supported", "finding": "Limited association, explicitly uncertain.",
                   "source_refs": ["/package/pigean/factor"]}
        self.review = {"claims": [{**deepcopy(verdict), "claim_id": claim["id"]} for claim in self.document["claims"]],
                       "synthesis": deepcopy(verdict)}

    def test_complete_claim_coverage_and_resolving_source_refs(self):
        self.assertTrue(validate_review(self.review, self.document, self.evidence))
        for mutation in ("omit", "duplicate", "invent", "bad_ref", "self_cite", "no_ref"):
            changed = deepcopy(self.review)
            if mutation == "omit": changed["claims"].pop()
            if mutation == "duplicate": changed["claims"].append(changed["claims"][0])
            if mutation == "invent": changed["claims"][0]["claim_id"] = "claim:invented"
            if mutation == "bad_ref": changed["claims"][0]["source_refs"] = ["/package/missing"]
            if mutation == "self_cite": changed["claims"][0]["source_refs"] = ["/proposed_document/claims/0"]
            if mutation == "no_ref": changed["claims"][0]["source_refs"] = []
            with self.subTest(mutation=mutation), self.assertRaises(EvidenceBuildError):
                validate_review(changed, self.document, self.evidence)

    def test_overstatement_or_synthesis_failure_never_accepts(self):
        for target in (self.review["claims"][0], self.review["synthesis"]):
            target["verdict"] = "overstated"
            self.assertFalse(validate_review(self.review, self.document, self.evidence))
            target["verdict"] = "supported"

    def test_only_captured_graph_evidence_including_empty_and_failed_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), "job", 1)
            for tool, status in (("Read", "completed"), ("query_graph", "empty"), ("query_graph", "failed")):
                call = ledger.start(tool, {"contains": "example"}, "prokn")
                ledger.finish(call, {"row_count": 0}, status)
            ledger.freeze()
            path = Path(temp) / "manifest.json"
            result = review_evidence(self.package, path)
            self.assertEqual([c["status"] for c in result["graph_calls"]], ["empty", "failed"])
            artifact = Path(temp) / ledger.entries[1]["response"]["path"]
            artifact.write_text('{"row_count": 1}')
            with self.assertRaises(EvidenceBuildError): review_evidence(self.package, path)

    def test_bounded_reader_and_no_token_count_or_unknown_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(Path(temp), "job", 1); ledger.freeze()
            path = Path(temp) / "manifest.json"
            client = Mock()
            def response(content):
                result = Mock(status_code=200)
                result.json.return_value = {"stop_reason": "tool_use", "content": content,
                                             "usage": {"input_tokens": 1000, "output_tokens": 100}}
                return result
            read = response([{"type": "tool_use", "id": "r1", "name": "read_evidence", "input": {"pointer": "/package/pigean/factor"}}])
            final = response([{"type": "tool_use", "id": "f1", "name": "submit_review", "input": self.review}])
            client.post.side_effect = [read, final]
            report = review_account(self.document, self.package, path, model=MODEL, api_key="test-only", client=client)
            self.assertTrue(report["accepted"])
            self.assertNotIn("test-only", json.dumps(report))
            self.assertEqual(client.post.call_count, 2)
            self.assertTrue(all(call.args[0].endswith('/v1/messages') for call in client.post.call_args_list))
            client.reset_mock()
            with self.assertRaises(ScientificReviewUnavailable):
                review_account(self.document, self.package, path, model="unpriced-model", api_key="test-only", client=client)
            self.assertEqual(client.post.call_count, 0)

    def test_paragraph_review_rejects_semantic_reversal_and_incomplete_coverage(self):
        inputs = {"format": "reveal.paragraph-input/1", "account_id": "account:1",
                  "account_document": {"scientific_accounts": [{"id": "account:1", "component_claims": ["claim:1"]}],
                                       "claims": [{"id": "claim:1", "statement": "Causality remains unknown."}]},
                  "allowed_citations": [{"target_id": "claim:1", "citation_metadata_revision": 1}]}
        output = {"format": "reveal.paragraph-output/1", "segments": [
            {"text": "Causality is proven.", "citations": inputs["allowed_citations"]}]}
        count = Mock(status_code=200); count.json.return_value = {"input_tokens": 500}
        answer = Mock(status_code=200)
        client = Mock()
        for verdict in ({"segments": [{"segment_index": 0, "faithful": False, "finding": "Contradicts uncertainty in cited Claim."}]},
                        {"segments": []}):
            client.post.side_effect = [answer]
            answer.json.return_value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(verdict)}], "usage": {"input_tokens": 500, "output_tokens": 100}}
            if verdict["segments"]:
                self.assertFalse(review_paragraph(output, inputs, model=MODEL, api_key="test-only", client=client)["accepted"])
            else:
                with self.assertRaises(ScientificReviewUnavailable):
                    review_paragraph(output, inputs, model=MODEL, api_key="test-only", client=client)


if __name__ == "__main__": unittest.main()
