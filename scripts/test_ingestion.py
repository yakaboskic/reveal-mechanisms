"""Offline regressions for source-boundary and completeness behavior."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import pull_cfde


class BioIndexPaginationTests(unittest.TestCase):
    def test_preserves_source_case_anomaly_and_query_key(self):
        row = {"phenotype": "gcat_trait_Orofacial_cleft", "gene_set_size": "cfde-inc-v2", "factor": "Factor3"}
        result = {"keys": ["gcat_trait_orofacial_cleft", "cfde-inc-v2"], "url": "https://example.org/query", "data": [row]}
        factors, anomalies = pull_cfde.normalize_factors([result], "cfde-inc-v2")
        self.assertEqual(factors[0]["raw"]["phenotype"], row["phenotype"])
        self.assertEqual(factors[0]["phenotype_key"], result["keys"][0])
        self.assertEqual(len(anomalies), 1)

    def test_wrong_model_is_rejected(self):
        result = {"keys": ["T2D", "cfde-inc-v2"], "url": "https://example.org/query", "data": [
            {"phenotype": "T2D", "gene_set_size": "cfde", "factor": "Factor1"}]}
        with self.assertRaisesRegex(ValueError, "model scope"):
            pull_cfde.normalize_factors([result], "cfde-inc-v2")

    def test_follows_continuation_and_encodes_token(self):
        pages = [
            {"data": [{"gene": "A"}], "continuation": "a+b/c=", "progress": {"bytes_read": 10, "bytes_total": 20}},
            {"data": [{"gene": "B"}], "continuation": None, "progress": {"bytes_read": 10, "bytes_total": 10}},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.object(pull_cfde, "fetch", side_effect=pages) as fetch:
            result = pull_cfde.query(Path(temp), "pigean-gene-phenotype", ["T2D", "cfde-inc-v2"])
            self.assertEqual(result["row_count"], 2)
            self.assertEqual(len(result["pages"]), 2)
            self.assertIn("token=a%2Bb%2Fc%3D", fetch.call_args_list[1].args[0])
            self.assertNotIn("limit=", fetch.call_args_list[0].args[0])

    def test_null_continuation_does_not_hide_truncation(self):
        page = {"data": [{"gene": "A"}], "continuation": None, "progress": {"bytes_read": 10, "bytes_total": 100}}
        with tempfile.TemporaryDirectory() as temp, patch.object(pull_cfde, "fetch", return_value=page):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                pull_cfde.query(Path(temp), "pigean-gene-factor", ["T2D", "cfde-inc-v2", "Factor1"])
            self.assertFalse(list(Path(temp).rglob("*.json")))

    def test_repeated_token_fails(self):
        page = {"data": [], "continuation": "same"}
        with tempfile.TemporaryDirectory() as temp, patch.object(pull_cfde, "fetch", return_value=page):
            with self.assertRaisesRegex(ValueError, "Repeated continuation"):
                pull_cfde.query(Path(temp), "pigean-factor", ["T2D", "cfde-inc-v2"])

    def test_cache_is_model_scoped(self):
        page = {"data": [], "continuation": None, "progress": {"bytes_read": 0, "bytes_total": 0}}
        with tempfile.TemporaryDirectory() as temp, patch.object(pull_cfde, "fetch", return_value=page) as fetch:
            root = Path(temp)
            for model in ["cfde-inc-v2", "cfde", "cfde-inc-v2"]:
                pull_cfde.query(root, "pigean-factor", ["T2D", model])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(len(list(root.rglob("*.json"))), 2)

    def test_legitimate_empty_results_are_complete(self):
        page = {"data": [], "continuation": None, "progress": {"bytes_read": 0, "bytes_total": 0}}
        with tempfile.TemporaryDirectory() as temp, patch.object(pull_cfde, "fetch", return_value=page):
            result = pull_cfde.query(Path(temp), "pigean-gene-set-genes", ["SET", "cfde-inc-v2"])
            self.assertTrue(result["complete"])
            self.assertEqual(result["row_count"], 0)


if __name__ == "__main__":
    unittest.main()
