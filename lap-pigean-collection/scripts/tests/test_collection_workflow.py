"""Unit tests of the collection helpers on tiny fixtures (no S3, no pigean run).

Run with: python3 -B -m unittest discover -s scripts/tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import collection_workflow as cw  # noqa: E402

COMMIT = "c" * 40
G1, G2, G3 = ("dapper:GeneSet." + c * 8 for c in "abc")
COLLECTION = "dapper:GeneSetCollection.zzzzzzzz"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def ndjson(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def run_helper(self, *argv):
        self.assertEqual(cw.main(list(argv)), 0)


class GenesetsBuild(Base):
    def build(self):
        folder = self.path("cfde", "LibA", "genesets", "part", "models", "M1", "extractor")
        write(os.path.join(folder, cw.GMT_NAME),
              "%s\tfree text here\tTP53\tLSM\xa06.00\tC10orf71\n%s\tmore text\tINS\n" % (G1, G2))
        write(os.path.join(folder, cw.DOCUMENT_NAME),
              "files:\n- id: dapper:File.x\n  name: not a gene set\ngene_sets:\n- id: %s\n  name: 'Set: one'\n"
              "  members:\n  - HGNC.SYMBOL:TP53\n- id: %s\n  name: a long\n    folded name\n"
              "gene_set_collections:\n- id: %s\n  name: the collection\n" % (G1, G2, COLLECTION))
        # The same gene set again in a second GMT of the library: written once.
        twin = self.path("cfde", "LibA", "genesets", "part", "models", "M1", "extractor", "cmp")
        write(os.path.join(twin, cw.GMT_NAME), "%s\tx\tINS\n" % G2)
        write(os.path.join(twin, cw.DOCUMENT_NAME),
              "gene_sets:\n- id: %s\n  name: a long folded name\ngene_set_collections:\n- id: %s\n" % (G2, COLLECTION))
        self.run_helper("genesets-build", "--libraries", "LibA", "--cfde-dir", self.path("cfde"),
                        "--output-dir", self.path("gmts"), "--output-index-file", self.path("index.tsv"),
                        "--output-file", self.path("gmts.tsv"))

    def test_gmt_blanks_descriptions_and_drops_unusable_tokens(self):
        self.build()
        with open(self.path("gmts", "LibA.gmt")) as fh:
            self.assertEqual(fh.read(), "%s\t\tTP53\tC10orf71\n%s\t\tINS\n" % (G1, G2))

    def test_index_names_and_labels(self):
        self.build()
        rows = cw.read_columns(self.path("index.tsv"), cw.GENE_SET_INDEX_COLUMNS)
        self.assertEqual([(r["gene_set_name"], r["cfde_label"], r["collection_id"], r["n_genes"]) for r in rows],
                         [("Set: one", "LibA__part__M1", COLLECTION, "2"),
                          ("a long folded name", "LibA__part__M1", COLLECTION, "1")])
        table = cw.read_columns(self.path("gmts.tsv"), cw.LIBRARY_GMT_COLUMNS)
        self.assertEqual(table[0]["n_gene_sets"], "2")
        self.assertEqual(table[0]["sha256"], cw.sha256_file(self.path("gmts", "LibA.gmt")))


class FormatTrait(Base):
    TRAIT = "My_trait_(x)"

    def setUp(self):
        super().setUp()
        instance = cw.instance_name(self.TRAIT)
        self.assertEqual(instance, "My_trait__x_")
        self.instance = instance
        row = {"instance": instance, "trait": self.TRAIT}
        row.update({c: "0" for c in cw.TRAIT_COLUMNS[2:]})
        cw.write_tsv(self.path("traits.tsv"), cw.TRAIT_COLUMNS, [row])
        src = self.path("src", self.TRAIT, "msig")
        write(os.path.join(src, "gs.out"), "Gene\tprior\tcombined\tlog_bf\textra\n"
              "TP53\t1.5\t6.7\t5.2\tz\nC10ORF71\t-0.2\t0.1\t0.3\tz\nINS\tNA\t1\t1\tz\n")
        write(os.path.join(src, "gss.out"), "Gene_Set\tlabel\tfilter_reason\tN\tbeta\tbeta_uncorrected\n"
              "set_a\tsrc1\tkept\t10\t0.5\t0.9\nset_zero\tsrc1\tkept\t4\t0\t0\nset_cut\tsrc1\tprefilter_p_value\t4\tNA\tNA\n")
        write(os.path.join(src, "ggss.out"), "Gene\tgene_set\tbeta\nTP53\tset_a\t0.5\nTP53\tset_zero\t0\nXIST\tset_a\tNone\n")
        write(self.path("gmts", "LibA.gmt"), "%s\t\tTP53\tC10orf71\tNOT_A_GENE\n%s\t\tINS\n%s\t\tTP53\n" % (G1, G2, G3))
        cw.write_tsv(self.path("gmts.tsv"), cw.LIBRARY_GMT_COLUMNS, [
            {"library": name, "file": self.path("gmts", name + ".gmt"), "n_gene_sets": 3, "sha256": "x"}
            for name in ("LibA", "LibB")])
        write(self.path("gmts", "LibB.gmt"), "")
        cw.write_tsv(self.path("index.tsv"), cw.GENE_SET_INDEX_COLUMNS, [
            {"gene_set_id": g, "gene_set_name": "name of " + g[-8:], "collection_id": COLLECTION,
             "cfde_label": "LibA__p__M", "library": "LibA", "n_genes": 1} for g in (G1, G2, G3)])
        stats = {}
        for library, body in (("LibA", "%s\tLibA\tkept\t3\t0.2\t0.4\n%s\tLibA\tkept\t1\t0\t0\n%s\tLibA\tprefilter_p_value\t1\tNA\tNA\n"
                               % (G1, G2, G3)), ("LibB", "")):
            stats[library] = self.path("betas", library + ".gene_set_stats.tsv")
            write(stats[library], "Gene_Set\tlabel\tfilter_reason\tN\tbeta\tbeta_uncorrected\n" + body)
        cw.write_tsv(self.path("runs.tsv"), cw.BETAS_RUN_COLUMNS, [
            {"instance": instance, "trait": self.TRAIT, "library": library, "status": "fitted",
             "pigean_commit": COMMIT, "gene_set_stats_file": stats[library]} for library in ("LibA", "LibB")])
        self.run_helper("format-trait", "--expected-pigean-commit", COMMIT, "--instance", instance,
                        "--traits-file", self.path("traits.tsv"), "--source-dir", self.path("src"),
                        "--source-library", "msig", "--trait-group", "portal", "--runs-file", self.path("runs.tsv"),
                        "--gene-set-index-file", self.path("index.tsv"), "--library-gmts-file", self.path("gmts.tsv"),
                        "--output-dir", self.path("data"), "--output-file", self.path("files.tsv"))

    def data(self, library, kind):
        return ndjson(self.path("data", library, self.TRAIT, kind + ".json"))

    def test_source_library_files(self):
        genes = self.data("msig", "combined_gene_stats")
        self.assertEqual(genes, [
            {"gene": "TP53", "prior": 1.5, "combined": 6.7, "log_bf": 5.2, "trait_group": "portal",
             "phenotype": self.TRAIT, "gene_set_size": "msig"},
            {"gene": "C10ORF71", "prior": -0.2, "combined": 0.1, "log_bf": 0.3, "trait_group": "portal",
             "phenotype": self.TRAIT, "gene_set_size": "msig"}])  # INS has a non-finite prior
        self.assertEqual(self.data("msig", "combined_gene_set_stats"), [
            {"gene_set": "set_a", "source": "src1", "beta": 0.5, "beta_uncorrected": 0.9, "n": 10,
             "trait_group": "portal", "phenotype": self.TRAIT, "gene_set_size": "msig"}])
        self.assertEqual(self.data("msig", "gene_gene_set_stats"), [
            {"gene": g, "gene_set": "set_a", "phenotype": self.TRAIT, "gene_set_size": "msig"} for g in ("TP53", "XIST")])

    def test_cfde_library_has_gene_sets_only(self):
        self.assertEqual(os.listdir(self.path("data", "LibA", self.TRAIT)), ["cfde_gene_set_stats.json"])
        self.assertEqual(self.data("LibA", "cfde_gene_set_stats"), [
            {"gene_set": G1, "gene_set_name": "name of aaaaaaaa", "source": "LibA__p__M", "collection_id": COLLECTION,
             "beta": 0.2, "beta_uncorrected": 0.4, "n": 3, "trait_group": "portal", "phenotype": self.TRAIT,
             "gene_set_size": "LibA"}])

    def test_library_without_rows_has_no_folder_and_manifest_matches_disk(self):
        self.assertFalse(os.path.exists(self.path("data", "LibB")))
        files = cw.read_columns(self.path("files.tsv"), cw.FILES_COLUMNS)
        self.assertEqual(sorted((f["library"], f["role"], f["rows"]) for f in files), [
            ("LibA", "cfde_gene_set_stats", "1"), ("msig", "gene_set_stats", "1"),
            ("msig", "gene_stats", "2"), ("msig", "membership", "2")])
        for f in files:
            path = self.path("data", f["relative_path"])
            self.assertEqual((str(os.path.getsize(path)), cw.sha256_file(path)), (f["size"], f["sha256"]))


if __name__ == "__main__":
    unittest.main()
