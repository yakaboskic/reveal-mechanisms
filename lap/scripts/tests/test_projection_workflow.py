"""Tests for lap/scripts/projection_workflow.py on tiny fixtures, including real eaggl runs.

Run with the interpreter the pipeline uses (it has numpy/scipy for eaggl):
  /humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s lap/scripts/tests
"""

import contextlib
import csv
import gzip
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import projection_workflow as pw  # noqa: E402

try:
    import numpy as np
    import projection_kernel as pk
except ImportError:  # the stdlib helpers are still tested
    np = pk = None

PIGEAN_SRC = os.environ.get("PIGEAN_SRC", "/humgen/diabetes2/users/chase/packages/pigean/src")

GENES = ["A1", "B2", "C10ORF71", "D4", "E5", "F6"]
# T-one has two factors, Solo one; F6 has no loading anywhere; one T-one cell was capped from 2.5.
LOADINGS = [
    ("T-one::Factor1", [1.0, 0.5, 0.0, 0.0, 0.0, 0.0]),
    ("T-one::Factor2", [0.0, 0.25, 0.8, 0.4, 0.0, 0.0]),
    ("Solo::Factor1", [0.0, 0.0, 0.3, 0.9, 0.6, 0.0]),
]
LABELS = {"T-one::Factor1": "Alpha program", "T-one::Factor2": "Beta program", "Solo::Factor1": "Solo program"}
KPN_IDS = {"T-one": "KPN.TRAIT:0000001", "Solo": "KPN.TRAIT:0000002"}
FAKE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
ID_A1 = "dapper:GeneSet." + "a" * 31 + "1"
ID_A2 = "dapper:GeneSet." + "a" * 31 + "2"
ID_B1 = "dapper:GeneSet." + "b" * 31 + "1"
ID_B2 = "dapper:GeneSet." + "b" * 30 + "_2"
# (label, library, collection id, [(gene-set id, name, genes)])
COLLECTIONS = [
    ("LIB__a", "LIBA", "dapper:GeneSetCollection." + "A" * 32, [
        (ID_A1, "set a one", ["A1", "B2", "NOTAGENE"]),
        (ID_A2, "set a two", ["C10orf71", "D4"]),  # C10orf71 only matches EAGGL's C10ORF71 by case
    ]),
    ("LIB__b", "LIBB", "dapper:GeneSetCollection." + "B" * 30 + "-_", [
        (ID_B1, "set b one", ["D4", "E5", "B2"]),
        (ID_B2, "set b zero", ["F6"]),  # every factor has loading 0 on F6
    ]),
]


DESCRIPTION = "%s signature, E5 and F6 up"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


def tsv(rows):
    return "".join("\t".join(str(v) for v in row) + "\n" for row in rows)


def read_rows(path):
    with pw.open_text(path) as fh:
        return list(csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE))


def run(argv):
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = pw.main(argv)
    return code, err.getvalue()


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Fixture:
    def __init__(self, root):
        self.root = root
        self.p = lambda *parts: os.path.join(root, *parts)
        os.makedirs(self.p("out"))  # LAP creates output directories in real runs
        write(self.p("eaggl", "genes.tsv"), tsv([["gene"]] + [[g] for g in GENES]))
        write(self.p("eaggl", "factor_ids.tsv"), tsv([["factor_id"]] + [[f] for f, _ in LOADINGS]))
        with gzip.open(self.p("eaggl", "loadings.tsv.gz"), "wt") as fh:
            fh.write(tsv([["factor_id"] + GENES] + [[f] + [repr(v) for v in vals] for f, vals in LOADINGS]))
        meta = [["factor_id", "trait", "factor", "factor_number", "label"]]
        for factor_id, _ in LOADINGS:
            trait, factor = factor_id.split("::")
            meta.append([factor_id, trait, factor, factor[len("Factor"):], LABELS[factor_id]])
        write(self.p("eaggl", "factor_metadata.tsv"), tsv(meta))
        with gzip.open(self.p("eaggl", "capped_entries.tsv.gz"), "wt") as fh:
            fh.write(tsv([["factor_id", "gene", "original_loading", "capped_loading"], ["T-one::Factor1", "A1", "2.5", "1.0"]]))

        # KPN release repo: registry committed and tagged v0.0.2.
        self.kpn_repo = self.p("kpn")
        self.registry = self.p("kpn", "versions", "trait", "v0.0.2", "kpn_trait_registry.tsv")
        self.write_registry([("T-one", KPN_IDS["T-one"]), ("Solo", KPN_IDS["Solo"]), ("Other", "KPN.TRAIT:0000009")])
        subprocess.run(["git", "init", "-q", self.kpn_repo], check=True)
        git(self.kpn_repo, "add", "-A")
        git(self.kpn_repo, "commit", "-q", "-m", "release")
        git(self.kpn_repo, "tag", "v0.0.2")

        index = [["library", "partition", "model", "comparison", "program", "label", "collection_id", "n_sets"]]
        for label, library, collection_id, sets in COLLECTIONS:
            index.append([library, "tissue", "M1", "", "", label, collection_id, len(sets)])
            d = self.p("cfde", label)
            # DAPPER 0.2.0 fills the description column with free text; these name EAGGL genes, which must not
            # become members.
            write(os.path.join(d, "genesets.dapper-ids.gmt"),
                  tsv([[i, DESCRIPTION % library] + genes for i, _, genes in sets]))
            write(os.path.join(d, "genesets.gmt"), tsv([[name, "Gene set %s: A1 knockdown" % name] + genes for _, name, genes in sets]))
            write(os.path.join(d, "GeneSetCollection.%s.yaml" % collection_id.split(".", 1)[1]), "id: x\n")
        write(self.p("cfde", "index.tsv"), tsv(index))

    def write_registry(self, pairs):
        header = ["portal_id", "gwas_source_category", "legacy_phenotype_id", "phenotype_name", "legacy_trait_group",
                  "trait_group", "trait_type", "pigean_id"]
        write(self.registry, tsv([header] + [[kpn, "KPN", trait, trait + " name", "G", "g", "phenotype", ""]
                                              for trait, kpn in pairs]))

    def collection_args(self, label, library, collection_id):
        d = self.p("cfde", label)
        return ["collection-index", "--label", label, "--collection-id", collection_id, "--library", library,
                "--gmt-file", os.path.join(d, "genesets.dapper-ids.gmt"),
                "--names-gmt-file", os.path.join(d, "genesets.gmt"),
                "--collection-yaml-file", os.path.join(d, "GeneSetCollection.%s.yaml" % collection_id.split(".", 1)[1]),
                "--output-file", self.p("out", label + ".gene_sets.tsv")]

    def kpn_args(self):
        return ["trait-kpn-map", "--factor-ids-file", self.p("eaggl", "factor_ids.tsv"), "--registry-file", self.registry,
                "--kpn-release", "v0.0.2", "--output-file", self.p("out", "trait_kpn_map.tsv")]


class TraitKpnMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_maps_every_trait_and_pins_the_release(self):
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 0, err)
        rows = read_rows(self.fx.p("out", "trait_kpn_map.tsv"))
        self.assertEqual([(r["trait"], r["kpn_trait_id"], r["n_factors"]) for r in rows],
                         [("T-one", KPN_IDS["T-one"], "2"), ("Solo", KPN_IDS["Solo"], "1")])
        tag_commit = subprocess.run(["git", "-C", self.fx.kpn_repo, "rev-list", "-n", "1", "v0.0.2"],
                                    stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        self.assertEqual({r["kpn_release_commit"] for r in rows}, {tag_commit})

    def test_unmatched_trait_fails(self):
        self.fx.write_registry([("T-one", KPN_IDS["T-one"])])
        git(self.fx.kpn_repo, "commit", "-qam", "drop Solo")
        git(self.fx.kpn_repo, "tag", "-f", "v0.0.2")
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 1)
        self.assertIn("no KPN legacy_phenotype_id match", err)

    def test_duplicate_legacy_id_fails(self):
        self.fx.write_registry([("T-one", KPN_IDS["T-one"]), ("Solo", KPN_IDS["Solo"]), ("Solo", "KPN.TRAIT:0000003")])
        git(self.fx.kpn_repo, "commit", "-qam", "duplicate Solo")
        git(self.fx.kpn_repo, "tag", "-f", "v0.0.2")
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 1)
        self.assertIn("match several KPN rows", err)

    def test_registry_differing_from_release_tag_fails(self):
        self.fx.write_registry([("T-one", KPN_IDS["T-one"]), ("Solo", KPN_IDS["Solo"])])  # uncommitted edit
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 1)
        self.assertIn("differs from KPN release", err)


    def test_registry_untracked_at_release_tag_fails(self):
        git(self.fx.kpn_repo, "rm", "-q", "--cached", self.fx.registry)
        git(self.fx.kpn_repo, "commit", "-qm", "untrack registry")
        git(self.fx.kpn_repo, "tag", "-f", "v0.0.2")
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 1)
        self.assertIn("is not part of KPN release", err)

    def test_registry_edit_hidden_by_skip_worktree_fails(self):
        git(self.fx.kpn_repo, "update-index", "--skip-worktree", self.fx.registry)
        self.fx.write_registry([("T-one", KPN_IDS["T-one"]), ("Solo", KPN_IDS["Solo"])])
        code, err = run(self.fx.kpn_args())
        self.assertEqual(code, 1)
        self.assertIn("differs from KPN release", err)


class PigeanCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "pigean")
        write(os.path.join(self.repo, "src", "eaggl", "x.py"), "x = 1\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c")
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                   universal_newlines=True).stdout.strip()
        self.out = os.path.join(self.tmp.name, "commit.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, expected):
        return run(["record-pigean-commit", "--repo-dir", self.repo, "--expected-commit", expected,
                    "--output-file", self.out])

    def test_records_clean_expected_commit(self):
        self.assertEqual(self.record(self.head)[0], 0)
        self.assertEqual(pw.read_pigean_commit(self.out, self.head), self.head)

    def test_wrong_commit_fails(self):
        code, err = self.record(FAKE_COMMIT)
        self.assertEqual(code, 1)
        self.assertIn("expected " + FAKE_COMMIT, err)

    def test_dirty_src_fails(self):
        write(os.path.join(self.repo, "src", "eaggl", "new.py"), "y = 2\n")
        code, err = self.record(self.head)
        self.assertEqual(code, 1)
        self.assertIn("local changes under src/", err)


class CollectionIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_indexes_ids_names_and_collection(self):
        label, library, collection_id, sets = COLLECTIONS[0]
        code, err = run(self.fx.collection_args(label, library, collection_id))
        self.assertEqual(code, 0, err)
        rows = read_rows(self.fx.p("out", label + ".gene_sets.tsv"))
        self.assertEqual([(r["gene_set_id"], r["gene_set_name"], r["collection_id"], r["n_genes"]) for r in rows],
                         [(i, name, collection_id, str(len(genes))) for i, name, genes in sets])

    def test_descriptions_are_not_genes_and_may_differ_between_the_gmts(self):
        label, library, collection_id, sets = COLLECTIONS[1]
        self.assertEqual(run(self.fx.collection_args(label, library, collection_id))[0], 0)
        rows = read_rows(self.fx.p("out", label + ".gene_sets.tsv"))
        self.assertEqual([r["n_genes"] for r in rows], [str(len(genes)) for _, _, genes in sets])  # ID_B2 has only F6

    def test_gene_tokens_eaggl_would_misread_fail(self):
        label, library, collection_id, sets = COLLECTIONS[0]
        d = self.fx.p("cfde", label)
        for genes in (["A1", "B2:0.5"], ["A1", "B2 C"]):
            write(os.path.join(d, "genesets.dapper-ids.gmt"), tsv([[sets[0][0], "x"] + genes, [sets[1][0], "x", "D4"]]))
            write(os.path.join(d, "genesets.gmt"), tsv([[sets[0][1], "y"] + genes, [sets[1][1], "y", "D4"]]))
            code, err = run(self.fx.collection_args(label, library, collection_id))
            self.assertEqual(code, 1)
            self.assertIn("would misread", err)

    def test_gene_mismatch_between_gmts_fails(self):
        label, library, collection_id, _ = COLLECTIONS[0]
        write(self.fx.p("cfde", label, "genesets.gmt"), tsv([["set a one", "", "A1"], ["set a two", "", "D4"]]))
        code, err = run(self.fx.collection_args(label, library, collection_id))
        self.assertEqual(code, 1)
        self.assertIn("Gene columns differ", err)

    def test_wrong_collection_yaml_fails(self):
        label, library, _, _ = COLLECTIONS[0]
        other = COLLECTIONS[1][2]
        args = self.fx.collection_args(label, library, other)
        args[args.index("--collection-yaml-file") + 1] = self.fx.p(
            "cfde", label, "GeneSetCollection.%s.yaml" % COLLECTIONS[0][2].split(".", 1)[1])
        code, err = run(args)
        self.assertEqual(code, 1)
        self.assertIn("does not match collection id", err)


@unittest.skipUnless(os.path.isdir(os.path.join(PIGEAN_SRC, "eaggl")), "pigean eaggl source not available")
class EndToEndTest(unittest.TestCase):
    """Every pipeline step in cfg order, with real eaggl projections (per trait and all factors)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fx = cls.fx = Fixture(cls.tmp.name)
        p = fx.p
        cls.check_run(fx.kpn_args())
        for label, library, collection_id, _ in COLLECTIONS:
            cls.check_run(fx.collection_args(label, library, collection_id))
        cls.check_run(["build-annotations"]
                      + sum((["--gmt-file", p("cfde", c[0], "genesets.dapper-ids.gmt")] for c in COLLECTIONS), [])
                      + sum((["--collection-gene-sets-file", p("out", c[0] + ".gene_sets.tsv")] for c in COLLECTIONS), [])
                      + ["--cfde-index-file", p("cfde", "index.tsv"), "--eaggl-genes-file", p("eaggl", "genes.tsv"),
                         "--cfde-snapshot", "2026-09-28", "--output-gmt-file", p("out", "annotations.gmt"),
                         "--output-gene-set-index-file", p("out", "gene_set_index.tsv"),
                         "--output-gene-map-file", p("out", "case.gene.map"),
                         "--output-overlap-report-file", p("out", "overlap.tsv")])
        cls.check_run(["assemble-factors", "--loadings-file", p("eaggl", "loadings.tsv.gz"),
                       "--factor-ids-file", p("eaggl", "factor_ids.tsv"),
                       "--factor-metadata-file", p("eaggl", "factor_metadata.tsv"),
                       "--genes-file", p("eaggl", "genes.tsv"), "--capped-entries-file", p("eaggl", "capped_entries.tsv.gz"),
                       "--trait-kpn-map-file", p("out", "trait_kpn_map.tsv"), "--loading-variant", "capped",
                       "--output-file", p("out", "all_factors.tsv"),
                       "--output-factor-index-file", p("out", "factor_index.tsv")])
        # A clean checkout standing in for the pinned pigean clone (check-projection checks it like record-pigean-commit).
        cls.repo = p("pigean")
        write(os.path.join(cls.repo, "src", "eaggl", "x.py"), "x = 1\n")
        subprocess.run(["git", "init", "-q", cls.repo], check=True)
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-qm", "c")
        cls.head = subprocess.run(["git", "-C", cls.repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.strip()
        # Chunks of 3: the 4 gene sets are projected as 3 + 1.
        cls.check_run(cls.pack_args(p("out", "pack"), 3))
        cls.check_run(cls.check_args(p("out", "pack"), p("out", "check")))
        for trait in KPN_IDS:
            d = lambda name: p("out", trait, name)
            os.makedirs(p("out", trait))
            cls.check_run(["trait-factors", "--all-factors-file", p("out", "all_factors.tsv"),
                           "--factor-index-file", p("out", "factor_index.tsv"), "--trait", trait,
                           "--output-file", d("factors.tsv"), "--output-factor-index-file", d("factor_index.tsv")])
            cls.check_run(cls.project_args(trait, p("out", "pack"), d))
        os.makedirs(p("out", "global"))
        cls.run_eaggl(p("out", "all_factors.tsv"), p("out", "global", ""))
        g = lambda name: p("out", "global", name)
        cls.check_run(["relabel-global", "--joint-file", g("joint.tsv.gz"), "--marginal-file", g("marginal.tsv.gz"),
                       "--all-factors-file", p("out", "all_factors.tsv"), "--factor-index-file", p("out", "factor_index.tsv"),
                       "--gene-set-index-file", p("out", "gene_set_index.tsv"), "--params-file", g("params.tsv"),
                       "--pigean-commit-file", g("commit.txt"), "--expected-pigean-commit", FAKE_COMMIT,
                       "--output-joint-file", g("joint.by_factor_id.tsv.gz"),
                       "--output-marginal-file", g("marginal.by_factor_id.tsv.gz"), "--output-qc-file", g("qc.tsv")])
        cls.check_run(["collect"] + sum((["--qc-file", p("out", t, "qc.tsv"), "--top-file", p("out", t, "top.tsv.gz")]
                                         for t in KPN_IDS), [])
                      + ["--trait-kpn-map-file", p("out", "trait_kpn_map.tsv"), "--expected-pigean-commit", cls.head,
                         "--output-manifest-file", p("out", "manifest.tsv"), "--output-top-file", p("out", "top.tsv.gz")])
        cls.check_run(["compare-global"] + sum((["--trait-long-file", p("out", t, "long.tsv.gz")] for t in KPN_IDS), [])
                      + ["--global-joint-file", g("joint.by_factor_id.tsv.gz"),
                         "--global-marginal-file", g("marginal.by_factor_id.tsv.gz"), "--output-file", g("compare.tsv")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def pack_args(cls, pack, chunk_size, gene_map=None):
        """The flags of prep_pack_annotations_cmd in cfde_projection.cfg (outputs <pack>.indptr.npy etc.)."""
        p = cls.fx.p
        return ["pack-annotations", "--annotations-gmt-file", p("out", "annotations.gmt"),
                "--gene-set-index-file", p("out", "gene_set_index.tsv"), "--gene-map-file", gene_map or p("out", "case.gene.map"),
                "--genes-file", p("eaggl", "genes.tsv"), "--chunk-size", str(chunk_size),
                "--output-indptr-file", pack + ".indptr.npy", "--output-indices-file", pack + ".indices.npy",
                "--output-file", pack + ".chunks.tsv"]

    @classmethod
    def check_args(cls, pack, check):
        """The flags of prep_check_projection_cmd in cfde_projection.cfg (report <check>.tsv, work dir <check>/)."""
        p = cls.fx.p
        return ["check-projection", "--python", sys.executable, "--pigean-src", PIGEAN_SRC, "--repo-dir", cls.repo,
                "--expected-pigean-commit", cls.head, "--annotations-gmt-file", p("out", "annotations.gmt"),
                "--gene-set-index-file", p("out", "gene_set_index.tsv"), "--gene-map-file", p("out", "case.gene.map"),
                "--genes-file", p("eaggl", "genes.tsv"), "--indptr-file", pack + ".indptr.npy",
                "--indices-file", pack + ".indices.npy", "--all-factors-file", p("out", "all_factors.tsv"),
                "--factor-index-file", p("out", "factor_index.tsv"), "--traits", ",".join(KPN_IDS),
                "--sample-per-library", "2", "--seed", "1", "--work-dir", check, "--output-file", check + ".tsv"]

    @classmethod
    def project_args(cls, trait, pack, d, kpn_trait_id=None, check=None):
        """The flags of trait_project_cmd in cfde_projection.cfg."""
        p = cls.fx.p
        return ["project-trait", "--check-file", check or p("out", "check.tsv"), "--chunks-file", pack + ".chunks.tsv",
                "--indptr-file", pack + ".indptr.npy", "--indices-file", pack + ".indices.npy",
                "--genes-file", p("eaggl", "genes.tsv"), "--trait-factors-file", d("factors.tsv"),
                "--trait-factor-index-file", d("factor_index.tsv"), "--gene-set-index-file", p("out", "gene_set_index.tsv"),
                "--trait-kpn-map-file", p("out", "trait_kpn_map.tsv"), "--trait", trait,
                "--kpn-trait-id", kpn_trait_id or KPN_IDS[trait], "--loading-variant", "capped", "--seed", "1",
                "--top-n", "2", "--output-long-file", d("long.tsv.gz"), "--output-top-file", d("top.tsv.gz"),
                "--output-qc-file", d("qc.tsv"), "--output-log-file", d("projection.log")]

    @staticmethod
    def check_run(argv):
        code, err = run(argv)
        if code != 0:
            raise AssertionError("%s failed: %s" % (argv[0], err))

    @classmethod
    def run_eaggl(cls, factors, prefix):
        """The flags of trait_project_cmd / global_project_cmd in cfde_projection.cfg."""
        p = cls.fx.p
        with open(prefix + "commit.txt", "w") as fh:
            fh.write(FAKE_COMMIT + "\n")
        env = dict(os.environ, PYTHONPATH=PIGEAN_SRC, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
                   OPENBLAS_NUM_THREADS="1")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "eaggl", "factor", "--factor-gene-clusters-in", factors,
             "--factor-gene-clusters-layout", "factors-by-genes", "--X-in", p("out", "annotations.gmt"),
             "--gene-map-in", p("out", "case.gene.map"), "--gene-set-projection-mode", "both",
             "--gene-set-clusters-out", prefix + "joint.tsv.gz", "--gene-set-clusters-marginal-out", prefix + "marginal.tsv.gz",
             "--factor-output-scope", "all", "--cluster-row-min-max-loading", "0", "--seed", "1", "--hide-progress",
             "--hide-opts", "--params-out", prefix + "params.tsv", "--warnings-file", prefix + "warnings.txt",
             "--log-file", prefix + "eaggl.log"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if proc.returncode != 0:
            raise AssertionError("eaggl failed:\n" + proc.stdout[-3000:])

    def long_rows(self, trait):
        return read_rows(self.fx.p("out", trait, "long.tsv.gz"))

    def test_annotations_concatenate_the_id_gmts_and_fix_case_only(self):
        with open(self.fx.p("out", "annotations.gmt")) as fh:
            rows = [line.rstrip("\n").split("\t") for line in fh]
        # Each gene set's id, a blank description (the source GMTs' text is dropped) and its genes in GMT order.
        self.assertEqual(rows, [[i, ""] + genes for c in COLLECTIONS for i, _, genes in c[3]])
        with open(self.fx.p("out", "case.gene.map")) as fh:
            self.assertEqual(fh.read(), "C10orf71\tC10ORF71\n")
        index = read_rows(self.fx.p("out", "gene_set_index.tsv"))
        self.assertEqual({r["gene_set_id"]: (r["collection_id"], r["n_genes_in_eaggl_universe"]) for r in index},
                         {ID_A1: (COLLECTIONS[0][2], "2"), ID_A2: (COLLECTIONS[0][2], "2"),
                          ID_B1: (COLLECTIONS[1][2], "3"), ID_B2: (COLLECTIONS[1][2], "1")})

    def test_gzip_outputs_are_byte_deterministic(self):
        p = self.fx.p
        paths = [p("out", "again.%d.tsv.gz" % i) for i in (1, 2)]
        for i, path in enumerate(paths):
            if i:
                time.sleep(1.1)  # gzip headers store a whole-second mtime
            self.check_run(["trait-factors", "--all-factors-file", p("out", "all_factors.tsv"),
                            "--factor-index-file", p("out", "factor_index.tsv"), "--trait", "Solo",
                            "--output-file", path, "--output-factor-index-file", path + ".index.tsv"])
        with open(paths[0], "rb") as a, open(paths[1], "rb") as b:
            self.assertEqual(a.read(), b.read())
        with gzip.open(paths[0], "rb") as a, open(p("out", "Solo", "factors.tsv"), "rb") as c:
            self.assertEqual(a.read(), c.read())  # the pipeline's plain file holds the same bytes

    def test_factor_index_keeps_ids_order_and_kpn(self):
        rows = read_rows(self.fx.p("out", "factor_index.tsv"))
        self.assertEqual([(r["global_eaggl_column"], r["factor_id"], r["kpn_trait_id"]) for r in rows],
                         [("Factor1", "T-one::Factor1", KPN_IDS["T-one"]), ("Factor2", "T-one::Factor2", KPN_IDS["T-one"]),
                          ("Factor3", "Solo::Factor1", KPN_IDS["Solo"])])
        with open(self.fx.p("out", "all_factors.tsv")) as fh:
            self.assertEqual(fh.readline().split("\t")[0], "Factor")

    def test_long_files_carry_every_id_verbatim(self):
        for trait in KPN_IDS:
            rows = self.long_rows(trait)
            factor_ids = [f for f, _ in LOADINGS if f.startswith(trait + "::")]
            self.assertEqual(len(rows), 4 * len(factor_ids))
            self.assertEqual({r["factor_id"] for r in rows}, set(factor_ids))
            self.assertEqual({r["gene_set_id"] for r in rows}, {ID_A1, ID_A2, ID_B1, ID_B2})
            self.assertEqual({r["kpn_trait_id"] for r in rows}, {KPN_IDS[trait]})
            self.assertEqual({r["factor_label"] for r in rows}, {LABELS[f] for f in factor_ids})

    def test_marginal_matches_independent_computation(self):
        genes = {i: set(g) for c in COLLECTIONS for i, _, g in c[3]}
        genes[ID_A2] = {"C10ORF71", "D4"}  # after the case map
        loadings = dict(LOADINGS)
        for trait in KPN_IDS:
            for row in self.long_rows(trait):
                w = dict(zip(GENES, loadings[row["factor_id"]]))
                expected = min(1.0, sum(w.get(g, 0.0) for g in genes[row["gene_set_id"]]) / sum(v * v for v in w.values()))
                self.assertAlmostEqual(float(row["marginal_loading"]), expected, delta=5e-4 * max(expected, 1e-3))

    def test_single_factor_joint_equals_marginal(self):
        for row in self.long_rows("Solo"):
            self.assertAlmostEqual(float(row["joint_loading"]), float(row["marginal_loading"]), delta=pw.LOADING_ROUNDING_TOL)
        qc = read_rows(self.fx.p("out", "Solo", "qc.tsv"))[0]
        self.assertNotEqual(qc["k1_joint_marginal_max_absdiff"], "NA")

    def test_zero_loading_set_is_kept_with_no_top_factor(self):
        for trait in KPN_IDS:
            rows = [r for r in self.long_rows(trait) if r["gene_set_id"] == ID_B2]
            self.assertTrue(rows)
            self.assertTrue(all(float(r["joint_loading"]) == 0 and r["is_joint_top_factor"] == "0" for r in rows))

    def test_qc_manifest_and_top_file(self):
        manifest = read_rows(self.fx.p("out", "manifest.tsv"))
        self.assertEqual([(r["trait"], r["kpn_trait_id"], r["aligned_genes"], r["qc_pass"]) for r in manifest],
                         [("Solo", KPN_IDS["Solo"], str(len(GENES)), "True"), ("T-one", KPN_IDS["T-one"], str(len(GENES)), "True")])
        top = read_rows(self.fx.p("out", "top.tsv.gz"))
        self.assertTrue(all(int(r["joint_rank_in_library"]) <= 2 or int(r["marginal_rank_in_library"]) <= 2 for r in top))
        self.assertIn("set a one", {r["gene_set_name"] for r in top})

    def test_global_projection_relabelled_and_marginal_matches_per_trait(self):
        with gzip.open(self.fx.p("out", "global", "joint.by_factor_id.tsv.gz"), "rt") as fh:
            header = fh.readline().rstrip("\n").split("\t")
        self.assertEqual(header, ["gene_set_id", "collection_id", "cfde_label", "library", "top_factor_id"]
                         + [f for f, _ in LOADINGS])
        compare = read_rows(self.fx.p("out", "global", "compare.tsv"))
        self.assertEqual({r["trait"]: r["marginal_equal"] for r in compare}, {"T-one": "True", "Solo": "True"})

    def test_project_trait_rejects_a_wrong_kpn_id(self):
        p = self.fx.p
        d = lambda name: p("out", "Solo", name) if name in ("factors.tsv", "factor_index.tsv") else p("out", "wrong-kpn", name)
        code, err = run(self.project_args("Solo", p("out", "pack"), d, KPN_IDS["T-one"]))
        self.assertEqual(code, 1)
        self.assertIn("KPN id for Solo disagrees", err)
        self.assertFalse(os.path.exists(p("out", "wrong-kpn")))  # refused before anything was written

    def test_project_trait_needs_a_passed_check(self):
        p = self.fx.p
        rows = read_rows(p("out", "check.tsv"))
        rows[0]["check_pass"] = "False"
        failed = write(p("out", "failed-check", "check.tsv"), tsv([pw.CHECK_COLUMNS] + [[r[c] for c in pw.CHECK_COLUMNS] for r in rows]))
        d = lambda name: p("out", "Solo", name) if name in ("factors.tsv", "factor_index.tsv") else p("out", "failed-check", name)
        code, err = run(self.project_args("Solo", p("out", "pack"), d, check=failed))
        self.assertEqual(code, 1)
        self.assertIn("records a failed check", err)

    def test_pack_and_chunks_cover_the_index_in_order(self):
        import numpy as np
        rows = read_rows(self.fx.p("out", "pack.chunks.tsv"))
        self.assertEqual([(r["chunk"], r["first_row"], r["n_gene_sets"], r["n_entries"]) for r in rows],
                         [("chunk_00001", "0", "3", "7"), ("chunk_00002", "3", "1", "1")])
        self.assertEqual([r["first_gene_set_id"] for r in rows], [ID_A1, ID_B2])
        indptr, indices = np.load(self.fx.p("out", "pack.indptr.npy")), np.load(self.fx.p("out", "pack.indices.npy"))
        self.assertEqual((indptr.dtype, indices.dtype), (np.dtype(np.int64), np.dtype(np.int32)))
        columns = [[GENES[g] for g in indices[indptr[j]:indptr[j + 1]]] for j in range(4)]
        # NOTAGENE is not an EAGGL gene; C10orf71 reaches C10ORF71 through the case map; rows are sorted.
        self.assertEqual(columns, [["A1", "B2"], ["C10ORF71", "D4"], ["B2", "D4", "E5"], ["F6"]])

    def test_pack_refuses_a_gmt_with_a_description(self):
        p = self.fx.p
        with open(p("out", "annotations.gmt")) as fh:
            lines = fh.readlines()
        first = lines[0].split("\t")
        lines[0] = "\t".join([first[0], "a description"] + first[2:])
        os.makedirs(p("out", "described"))
        with open(p("out", "described", "annotations.gmt"), "w") as fh:
            fh.writelines(lines)
        args = self.pack_args(p("out", "described", "pack"), 3)
        args[args.index("--annotations-gmt-file") + 1] = p("out", "described", "annotations.gmt")
        code, err = run(args)
        self.assertEqual(code, 1)
        self.assertIn("must have a blank description", err)

    def test_check_projection_compares_every_value_with_eaggl(self):
        rows = read_rows(self.fx.p("out", "check.tsv"))
        self.assertEqual([(r["trait"], r["n_gene_sets"], r["n_case_mapped_gene_sets"], r["n_values"], r["check_pass"])
                          for r in rows], [("T-one", "4", "1", "16", "True"), ("Solo", "4", "1", "8", "True")])
        self.assertEqual({(r["joint_mismatches"], r["marginal_mismatches"], r["top_factor_mismatches"]) for r in rows},
                         {("0", "0", "0")})
        self.assertEqual({r["pigean_commit"] for r in rows}, {self.head})
        qc = read_rows(self.fx.p("out", "Solo", "qc.tsv"))[0]
        self.assertEqual(qc["pigean_commit"], self.head)

    def test_check_projection_refuses_a_pack_that_disagrees_with_eaggl(self):
        p = self.fx.p
        empty_map = write(p("out", "no-case-map", "empty.gene.map"), "")  # drops C10orf71 -> C10ORF71 from the pack only
        self.check_run(self.pack_args(p("out", "no-case-map", "pack"), 3, gene_map=empty_map))
        code, err = run(self.check_args(p("out", "no-case-map", "pack"), p("out", "no-case-map", "check")))
        self.assertEqual(code, 1)
        self.assertIn("The projection kernel disagrees with eaggl", err)
        self.assertFalse(os.path.exists(p("out", "no-case-map", "check.tsv")))
        failed = read_rows(p("out", "no-case-map", "check.tsv.failed.tsv"))
        self.assertEqual({r["check_pass"] for r in failed}, {"False"})

    def test_kernel_equals_eaggl_on_every_gene_set(self):
        """One chunk of all 4 gene sets is one eaggl run on the annotations GMT: every string must be identical."""
        p = self.fx.p
        for trait in KPN_IDS:
            os.makedirs(p("out", "single", trait))  # LAP creates output directories in real runs
        self.check_run(self.pack_args(p("out", "single", "pack"), 100))
        for trait in KPN_IDS:
            d = lambda name: p("out", trait, name) if name in ("factors.tsv", "factor_index.tsv") else p("out", "single", trait, name)
            self.check_run(self.project_args(trait, p("out", "single", "pack"), d))
            genes, factors, _ = pw.trait_factors(p("out", "all_factors.tsv"), pw.read_factor_index(p("out", "factor_index.tsv")), trait)
            pw.write_factors_wide(p("out", "single", trait, "factors_by_genes.tsv"), genes, factors)
            self.run_eaggl(p("out", "single", trait, "factors_by_genes.tsv"), p("out", "single", trait, "eaggl."))
            columns = OrderedDict((r["local_eaggl_column"], r["factor_id"]) for r in read_rows(p("out", trait, "factor_index.tsv")))
            ids = [i for c in COLLECTIONS for i, _, _ in c[3]]
            joint, _ = pw.read_projection(p("out", "single", trait, "eaggl.joint.tsv.gz"), columns, ids)
            marginal, _ = pw.read_projection(p("out", "single", trait, "eaggl.marginal.tsv.gz"), columns, ids)
            rows = read_rows(d("long.tsv.gz"))
            self.assertEqual(len(rows), 4 * len(columns))
            factor_ids = list(columns.values())
            for row in rows:
                k = factor_ids.index(row["factor_id"])
                cluster, values = joint[row["gene_set_id"]]
                self.assertEqual((row["joint_loading"], row["marginal_loading"], row["is_joint_top_factor"]),
                                 (values[k], marginal[row["gene_set_id"]][1][k], str(int(cluster == row["factor_id"]))))

    def test_chunked_projection_equals_one_chunk(self):
        p = self.fx.p
        os.makedirs(p("out", "single-chunk", "T-one"))
        self.check_run(self.pack_args(p("out", "single-chunk", "pack"), 100))
        d = lambda name: p("out", "single-chunk", "T-one", name) if name not in ("factors.tsv", "factor_index.tsv") else p("out", "T-one", name)
        self.check_run(self.project_args("T-one", p("out", "single-chunk", "pack"), d))
        chunked = {(r["factor_id"], r["gene_set_id"]): r for r in self.long_rows("T-one")}
        single = {(r["factor_id"], r["gene_set_id"]): r for r in read_rows(d("long.tsv.gz"))}
        self.assertEqual(set(chunked), set(single))
        for key, row in single.items():
            self.assertEqual(row["marginal_loading"], chunked[key]["marginal_loading"])
            self.assertAlmostEqual(float(row["joint_loading"]), float(chunked[key]["joint_loading"]), delta=1e-3)
        with open(d("projection.log")) as fh:
            self.assertEqual(sum(line.startswith("chunk_") for line in fh), 1)
        with open(p("out", "T-one", "projection.log")) as fh:
            self.assertEqual(sum(line.startswith("chunk_") for line in fh), 2)

    def test_library_ranks_restrict_the_factor_order_to_the_library(self):
        for row in self.long_rows("T-one"):
            library = [r for r in self.long_rows("T-one") if r["factor_id"] == row["factor_id"] and r["library"] == row["library"]]
            expected = 1 + sum(1 for r in library if int(r["joint_rank_in_factor"]) < int(row["joint_rank_in_factor"]))
            self.assertEqual(int(row["joint_rank_in_library"]), expected)


def eaggl_dense_projection(W, V, seed):
    """eaggl state.py _project_H_with_fixed_W as the gene-set projection calls it (no weights, phi=0, tol=1e-4,
    cap_genes), on dense arrays: the reference projection_kernel.project must reproduce."""
    np.random.seed(seed)
    V = V.toarray()
    S = np.ones_like(V)
    H = np.random.random((W.shape[1], V.shape[1])) * np.max(V)
    lam = np.ones(W.shape[1])
    V_ap = W @ H
    for it in range(100):
        numerator = W.T @ (V * S)
        denominator = W.T @ (V_ap * S) + 0.0 * H * (1 / lam)[:, np.newaxis] + 1e-10
        update = np.clip(np.maximum(H * (numerator / denominator), 0), 0, 1)
        diff = np.linalg.norm(update - H, "fro") / (np.linalg.norm(H, "fro") + 1e-10)
        H = update
        V_ap = W @ H
        if diff < 1e-4:
            break
    return H.T, it + 1


@unittest.skipIf(pk is None, "numpy/scipy not available")
class ProjectionKernelTest(unittest.TestCase):
    def setUp(self):
        from scipy import sparse
        rng = np.random.RandomState(7)
        self.W = rng.random_sample((300, 6)) * (rng.random_sample((300, 6)) < 0.2)
        self.V = sparse.csc_matrix((rng.random_sample((300, 400)) < 0.05).astype(float))
        self.indptr, self.indices = self.V.indptr.astype(np.int64), self.V.indices.astype(np.int32)

    def test_matches_eaggl_dense_updates(self):
        joint, marginal, updates, change = pk.project(self.W, self.V, 1)
        reference, reference_updates = eaggl_dense_projection(self.W, self.V, 1)
        self.assertEqual(updates, reference_updates)
        self.assertLess(np.max(np.abs(joint - reference)), 1e-12)
        expected = np.clip((self.V.T @ self.W) / np.sum(self.W * self.W, axis=0), 0, 1)
        self.assertLess(np.max(np.abs(marginal - expected)), 1e-15)
        self.assertEqual(joint.shape, (400, 6))

    def test_the_seed_sets_the_start_and_an_empty_chunk_stays_zero(self):
        self.assertTrue(np.array_equal(pk.project(self.W, self.V, 1)[0], pk.project(self.W, self.V, 1)[0]))
        self.assertFalse(np.array_equal(pk.project(self.W, self.V, 1)[0], pk.project(self.W, self.V, 2)[0]))
        empty = pk.gene_set_matrix(np.zeros(3, dtype=np.int64), np.zeros(0, dtype=np.int32), [0, 1], 300)
        joint, marginal, updates, change = pk.project(self.W, empty, 1)
        self.assertEqual((float(joint.max()), float(marginal.max()), updates), (0.0, 0.0, 1))

    def test_gene_set_matrix_takes_ranges_and_lists(self):
        for columns in (range(10, 60), [3, 1, 250, 251, 399]):
            got = pk.gene_set_matrix(self.indptr, self.indices, columns, 300)
            self.assertTrue(np.array_equal(got.toarray(), self.V[:, list(columns)].toarray()))

    def test_top_factor_is_the_first_highest_and_none_without_a_positive_loading(self):
        joint = np.array([[0.1, 0.3, 0.3], [0.0, 0.0, 0.0], [0.5, 0.2, 0.0]])
        self.assertEqual(pk.top_factors(joint).tolist(), [1, -1, 0])

    def test_ranks_are_ordinal_with_ties_by_gene_set_id(self):
        values = np.array([0.5, 0.9, 0.5, 0.1, 0.9])
        ids = ["e", "d", "c", "b", "a"]
        id_order = np.empty(5, dtype=np.int64)
        id_order[sorted(range(5), key=ids.__getitem__)] = np.arange(5)
        libraries = np.array([0, 1, 0, 1, 0], dtype=np.int32)
        in_factor, in_library = pw.library_ranks(values, id_order, libraries, 2)
        self.assertEqual(in_factor.tolist(), [4, 2, 3, 5, 1])  # a(0.9), d(0.9), c(0.5), e(0.5), b(0.1)
        self.assertEqual(in_library.tolist(), [3, 1, 2, 2, 1])

    def test_rounded_loadings_are_the_written_strings(self):
        values = np.array([0.123456, 1e-40 / 3, 1.0, 0.0, 0.99995])
        rounded = pw.round_loadings(values)
        self.assertEqual(["%.4g" % x for x in rounded], ["%.4g" % x for x in values])
        self.assertEqual(rounded.tolist(), [float("%.4g" % x) for x in values])


class GeneSetBetasTest(unittest.TestCase):
    """betas_ stage helpers: index the all-trait PIGEAN gene stats, slice one trait, annotate `pigean betas` output."""

    PIGEAN_COLUMNS = ["Gene_Set", "label", "filter_reason", "N", "scale", "beta", "beta_uncorrected", "avg_postp", "p_used",
                      "sigma2_used"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = lambda *parts: os.path.join(self.tmp.name, *parts)
        # Other is not a pipeline trait; it may appear anywhere, even split.
        self.stats = write(self.p("gene_stats.tsv"), tsv(
            [("phenotype", "gene", "combined", "log_bf", "prior"), ("Other", "A1", "1", "1", "0"),
             ("T-one", "A1", "2.5", "2", "0.5"), ("T-one", "B2", "1.2", "1", "0.2"), ("Solo", "D4", "0.4", "0.3", "0.1"),
             ("Other", "B2", "1", "1", "0")]))
        self.kpn_map = write(self.p("trait_kpn_map.tsv"), tsv(
            [pw.TRAIT_KPN_COLUMNS] + [(t, k, "v0.0.2", "c", "x", t, "g", "lg", "tt", "1") for t, k in KPN_IDS.items()]))
        self.index_rows = [(i, "set %s" % i[-1], c[2], c[0], c[1], "", "", "", "", "1", "3", "2", "2026-09-28")
                           for c in COLLECTIONS for i, _, _ in c[3]]
        self.gene_set_index = write(self.p("gene_set_index.tsv"), tsv([pw.GENE_SET_INDEX_COLUMNS] + self.index_rows))
        self.commit = write(self.p("commit.txt"), FAKE_COMMIT + "\n")

    def index(self, *extra):
        return run(["gene-stats-index", "--gene-stats-file", self.stats, "--trait-kpn-map-file", self.kpn_map,
                    "--output-file", self.p("index.tsv")] + list(extra))

    def test_index_and_slice_give_each_trait_its_own_rows(self):
        self.assertEqual(self.index()[0], 0)
        self.assertEqual([(r["trait"], r["rows"]) for r in read_rows(self.p("index.tsv"))], [("Solo", "1"), ("T-one", "2")])
        code, err = run(["trait-gene-stats", "--gene-stats-file", self.stats, "--index-file", self.p("index.tsv"),
                         "--trait", "T-one", "--output-file", self.p("T-one.tsv.gz")])
        self.assertEqual((code, err), (0, ""))
        with gzip.open(self.p("T-one.tsv.gz"), "rt") as fh:
            self.assertEqual(fh.read(), "phenotype\tgene\tcombined\tlog_bf\tprior\nT-one\tA1\t2.5\t2\t0.5\nT-one\tB2\t1.2\t1\t0.2\n")

    def test_index_refuses_split_or_missing_traits(self):
        write(self.stats, tsv([("phenotype", "gene", "log_bf"), ("T-one", "A1", "2"), ("Solo", "D4", "1"), ("T-one", "B2", "1")]))
        code, err = self.index()
        self.assertEqual(code, 1)
        self.assertIn("The rows of T-one", err)
        write(self.stats, tsv([("phenotype", "gene", "log_bf"), ("T-one", "A1", "2")]))
        code, err = self.index()
        self.assertEqual(code, 1)
        self.assertIn("1 traits have no gene stats", err)

    def test_slice_refuses_an_export_changed_since_indexing(self):
        self.assertEqual(self.index()[0], 0)
        with open(self.stats, "a") as fh:
            fh.write("Late\tA1\t1\t1\t0\n")
        code, err = run(["trait-gene-stats", "--gene-stats-file", self.stats, "--index-file", self.p("index.tsv"),
                         "--trait", "Solo", "--output-file", self.p("Solo.tsv.gz")])
        self.assertEqual(code, 1)
        self.assertIn("changed since it was indexed", err)

    def runs(self, specs):
        """betas-trait runs of T-one: specs are (library, status, [pigean rows], used response, p)."""
        out = []
        for library, status, rows, used, p in specs:
            prefix = self.p("%s.%d" % (library, len(os.listdir(self.tmp.name))))
            write(prefix + ".gene_set_stats.tsv", tsv([self.PIGEAN_COLUMNS] + rows))
            write(prefix + ".params.tsv", "" if status == "no_gene_sets" else tsv(
                [("Parameter", "Version", "Value"), ("option_gene_stats_log_bf_col", "1", used), ("p", "1", p),
                 ("sigma2", "1", "7e-09")]))
            out.append({"trait": "T-one", "kpn_trait_id": KPN_IDS["T-one"], "library": library, "status": status,
                        "n_gene_sets": "2", "response": "log_bf", "pigean_commit": FAKE_COMMIT, "seconds": "1",
                        "gene_set_stats_file": prefix + ".gene_set_stats.tsv", "params_file": prefix + ".params.tsv",
                        "log_file": prefix + ".log", "warnings_file": prefix + ".warnings.txt"})
        return out

    def annotate(self, runs, response="log_bf"):
        args = SimpleNamespace(trait="T-one", kpn_trait_id=KPN_IDS["T-one"], response=response,
                               expected_pigean_commit=FAKE_COMMIT)
        gene_sets = {row[0]: (row[4], row[2], row[3]) for row in self.index_rows}  # id -> library, collection id, label
        return pw.annotate_runs(args, runs, gene_sets)

    @staticmethod
    def row(gene_set_id, reason, uncorrected):
        return (gene_set_id, "x.gmt", reason, "3", "0.1", "0.05", uncorrected, "0.5", "0.0004", "7e-07")

    def test_annotate_ranks_each_library_by_its_own_fit(self):
        row = self.row
        runs = self.runs([("LIBA", "fitted", [row(ID_A1, "kept", "0.2"), row(ID_A2, "kept", "0.9")], "log_bf", "0.004"),
                          ("LIBB", "fitted", [row(ID_B1, "kept", "0.1"), row(ID_B2, "prefilter_p_value", "0")], "log_bf", "1e-05")])
        rows, reasons, columns = self.annotate(runs)
        self.assertEqual([(r["gene_set_id"], r["library"], r["library_rank"], r["beta_uncorrected"], r["p"]) for r in rows],
                         [(ID_A2, "LIBA", 1, "0.9", "0.004"), (ID_A1, "LIBA", 2, "0.2", "0.004"),
                          (ID_B1, "LIBB", 1, "0.1", "1e-05")])
        self.assertEqual({(r["kpn_trait_id"], r["collection_id"], r["response"], r["sigma2"]) for r in rows if r["library"] == "LIBB"},
                         {(KPN_IDS["T-one"], COLLECTIONS[1][2], "log_bf", "7e-09")})
        self.assertEqual([(r["n_kept"], r["n_nonzero_beta_uncorrected"], r["p"]) for r in runs], [(2, 2, "0.004"), (1, 1, "1e-05")])
        self.assertEqual((reasons, columns), ({"kept": 3, "prefilter_p_value": 1}, self.PIGEAN_COLUMNS))

    def test_annotate_reports_the_prior_pigean_used_when_it_learned_none(self):
        runs = self.runs([("LIBA", "fitted", [self.row(ID_A1, "kept", "0.2"), self.row(ID_A2, "prefilter_p_value", "0")],
                           "log_bf", "0.004")])
        write(runs[0]["params_file"], tsv([("Parameter", "Version", "Value"), ("option_gene_stats_log_bf_col", "1", "log_bf")]))
        rows, _, _ = self.annotate(runs)
        self.assertEqual((runs[0]["p"], runs[0]["sigma2"], rows[0]["p"]), ("0.0004", "7e-07", "0.0004"))  # p_used, sigma2_used

    def test_annotate_records_a_library_without_gene_sets(self):
        runs = self.runs([("LIBA", "fitted", [self.row(ID_A1, "kept", "0.2")], "log_bf", "0.004"),
                          ("LIBB", "no_gene_sets", [], "log_bf", pw.NA)])
        rows, _, _ = self.annotate(runs)
        self.assertEqual([r["library"] for r in rows], ["LIBA"])
        self.assertEqual((runs[1]["n_kept"], runs[1]["p"]), (0, pw.NA))

    def test_annotate_refuses_another_response_commit_or_library_and_unknown_gene_sets(self):
        row = self.row
        cases = [([("LIBB", "fitted", [row(ID_B1, "kept", "0.1")], "combined", "0.004")], "pigean regressed LIBB on combined"),
                 ([("LIBB", "fitted", [row(ID_A2, "kept", "0.1")], "log_bf", "0.004")], "is in LIBA, not LIBB"),
                 ([("LIBB", "fitted", [row("dapper:GeneSet." + "z" * 32, "kept", "0.1")], "log_bf", "0.004")], "Unknown gene set")]
        for specs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(pw.WorkflowError, message):
                    self.annotate(self.runs(specs))
        runs = self.runs([("LIBA", "fitted", [row(ID_A1, "kept", "0.2")], "log_bf", "0.004")])
        runs[0]["pigean_commit"] = "f" * 40
        with self.assertRaisesRegex(pw.WorkflowError, "pigean ran LIBA at"):
            self.annotate(runs)


FAKE_PIGEAN = '''"""Stands in for `python -m pigean betas`: every gene set of --X-in is kept, beta_uncorrected falls with row order."""
import gzip, os, sys
args = sys.argv[2:]
value = lambda flag: args[args.index(flag) + 1]
opener = lambda path, mode: (gzip.open if path.endswith(".gz") else open)(path, mode)
with open(os.environ["FAKE_PIGEAN_CALLS"], "a") as fh:
    fh.write(os.path.basename(value("--X-in")) + "\\n")
with open(os.environ["FAKE_PIGEAN_CALLS"] + ".args", "a") as fh:
    fh.write(" ".join(args) + "\\n")
with open(value("--log-file"), "w") as log:
    if os.path.basename(value("--X-in")).startswith(os.environ.get("FAKE_PIGEAN_EMPTY", "-")):
        log.write("No gene sets survived the input filters; stopping\\n")
        sys.exit(0)
    if os.path.basename(value("--X-in")).startswith(os.environ.get("FAKE_PIGEAN_NONE_LEFT", "-")):
        log.write("Ignoring 1 gene sets due to too few genes (kept 0)\\n")
        sys.stderr.write("Error: No gene sets are left!\\n")
        sys.exit(1)
    if os.path.basename(value("--X-in")).startswith(os.environ.get("FAKE_PIGEAN_CRASH", "-")):
        sys.stderr.write("Killed\\n")
        sys.exit(137)
    log.write("ok\\n")
with opener(value("--X-in"), "rt") as fh:
    ids = [line.split()[0] for line in fh if line.strip()]
with opener(value("--gene-set-stats-out"), "wt") as out:
    out.write("Gene_Set\\tlabel\\tfilter_reason\\tN\\tbeta\\tbeta_uncorrected\\tavg_postp\\n")
    for k, gene_set in enumerate(ids):
        out.write("%s\\tx\\tkept\\t3\\t0.01\\t%g\\t0.5\\n" % (gene_set, 1.0 / (k + 1)))
with open(value("--params-out"), "w") as out:
    out.write("Parameter\\tVersion\\tValue\\noption_gene_stats_log_bf_col\\t1\\t%s\\np\\t1\\t0.001\\nsigma2\\t1\\t1e-08\\n"
              % value("--gene-stats-log-bf-col"))
'''


class BetasPerLibraryTest(unittest.TestCase):
    """library-gmts, betas-trait (one `pigean betas` fit per library, with a fake pigean; resumable) and betas-collect."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = self.p = lambda *parts: os.path.join(self.tmp.name, *parts)
        write(p("annotations.gmt"), tsv([[i, ""] + genes for c in COLLECTIONS for i, _, genes in c[3]]))
        index = [(i, "set", c[2], c[0], c[1], "", "", "", "", "1", "3", "2", "2026-10-05") for c in COLLECTIONS for i, _, _ in c[3]]
        write(p("gene_set_index.tsv"), tsv([pw.GENE_SET_INDEX_COLUMNS] + index))
        write(p("trait_kpn_map.tsv"), tsv([pw.TRAIT_KPN_COLUMNS] + [(t, k, "v0.0.2", "c", "x", t, "g", "lg", "tt", "1")
                                                                    for t, k in KPN_IDS.items()]))
        write(p("src", "pigean", "__init__.py"), "")
        write(p("src", "pigean", "__main__.py"), FAKE_PIGEAN)
        self.repo = p("clone")
        write(os.path.join(self.repo, "src", "pigean", "x.py"), "x = 1\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c")
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                   universal_newlines=True).stdout.strip()
        write(p("profile.json"), "{}\n")
        write(p("gene_stats.tsv"), tsv([("phenotype", "gene", "log_bf"), ("T-one", "A1", "2")]))
        os.environ["FAKE_PIGEAN_CALLS"] = p("calls.txt")
        self.addCleanup(os.environ.pop, "FAKE_PIGEAN_CALLS", None)
        self.addCleanup(os.environ.pop, "FAKE_PIGEAN_EMPTY", None)
        self.addCleanup(os.environ.pop, "FAKE_PIGEAN_NONE_LEFT", None)
        self.addCleanup(os.environ.pop, "FAKE_PIGEAN_CRASH", None)
        self.assertEqual(run(["library-gmts", "--annotations-gmt-file", p("annotations.gmt"), "--gene-set-index-file",
                              p("gene_set_index.tsv"), "--output-dir", p("libraries"), "--output-file", p("libraries.tsv")]),
                         (0, ""))

    def betas(self, libraries, name, exclude="", cap="20000", trait="T-one"):
        """betas_trait_cmd's flags; outputs <name>.runs.tsv, <name>.all.tsv and <name>.tsv."""
        p = self.p
        return run(["betas-trait", "--python", sys.executable, "--pigean-src", p("src"), "--repo-dir", self.repo,
                    "--expected-pigean-commit", self.head, "--profile", p("profile.json"), "--library-gmts-file",
                    p("libraries.tsv"), "--libraries", libraries, "--exclude-libraries", exclude, "--gene-stats-file",
                    p("gene_stats.tsv"), "--gene-set-index-file", p("gene_set_index.tsv"), "--trait-kpn-map-file",
                    p("trait_kpn_map.tsv"), "--trait", trait, "--kpn-trait-id", KPN_IDS[trait], "--response", "log_bf",
                    "--seed", "1", "--max-num-gene-sets-initial", cap, "--work-dir", p("work", trait),
                    "--output-runs-file", p(name + ".runs.tsv"), "--output-all-file", p(name + ".all.tsv"),
                    "--output-file", p(name + ".tsv")])

    def calls(self):
        with open(self.p("calls.txt")) as fh:
            return fh.read().split()

    def test_library_gmts_split_the_annotations_in_index_order(self):
        rows = read_rows(self.p("libraries.tsv"))
        self.assertEqual([(r["library"], r["n_gene_sets"]) for r in rows], [("LIBA", "2"), ("LIBB", "2")])
        with open(rows[1]["file"]) as fh:
            self.assertEqual([line.split("\t")[0] for line in fh], [ID_B1, ID_B2])
        self.assertEqual(rows[1]["sha256"], pw.sha256_file(rows[1]["file"]))

    def test_each_library_is_fitted_alone_then_ranked(self):
        self.assertEqual(self.betas("LIBA,LIBB", "small", exclude="LIBB")[0], 0)
        self.assertEqual(self.betas("LIBB", "large")[0], 0)
        self.assertEqual(self.calls(), ["LIBA.gmt", "LIBB.gmt"])
        runs = read_rows(self.p("small.runs.tsv")) + read_rows(self.p("large.runs.tsv"))
        self.assertEqual([(r["trait"], r["library"], r["status"], r["n_kept"], r["p"], r["pigean_commit"]) for r in runs],
                         [("T-one", "LIBA", "fitted", "2", "0.001", self.head), ("T-one", "LIBB", "fitted", "2", "0.001", self.head)])
        self.assertEqual([(r["gene_set_id"], r["library_rank"]) for r in read_rows(self.p("small.tsv"))], [(ID_A1, "1"), (ID_A2, "2")])
        rows = read_rows(self.p("large.all.tsv"))
        self.assertEqual([(r["library"], r["Gene_Set"], r["filter_reason"]) for r in rows], [("LIBB", ID_B1, "kept"), ("LIBB", ID_B2, "kept")])
        # A rerun reuses both fits; changed gene stats refit.
        self.assertEqual(self.betas("LIBA,LIBB", "again")[0], 0)
        self.assertEqual(self.calls(), ["LIBA.gmt", "LIBB.gmt"])
        self.assertEqual({r["seconds"] for r in read_rows(self.p("again.runs.tsv"))}, {"reused"})
        write(self.p("gene_stats.tsv"), tsv([("phenotype", "gene", "log_bf"), ("T-one", "A1", "3")]))
        self.assertEqual(self.betas("LIBA", "refit")[0], 0)
        self.assertEqual(self.calls()[-1], "LIBA.gmt")

    def test_a_library_without_surviving_gene_sets_is_recorded(self):
        for variable in ("FAKE_PIGEAN_EMPTY", "FAKE_PIGEAN_NONE_LEFT"):  # a clean stop, or pigean's bail from the betas
            with self.subTest(variable=variable):
                os.environ[variable] = "LIBB"
                self.assertEqual(self.betas("LIBA,LIBB", variable, cap="100")[0], 0)
                runs = {r["library"]: r for r in read_rows(self.p(variable + ".runs.tsv"))}
                self.assertEqual((runs["LIBA"]["status"], runs["LIBB"]["status"], runs["LIBB"]["n_kept"]),
                                 ("fitted", "no_gene_sets", "0"))
                self.assertEqual({r["library"] for r in read_rows(self.p(variable + ".tsv"))}, {"LIBA"})
                os.environ.pop(variable)
                shutil.rmtree(self.p("work"))  # the next variant refits from scratch

    def test_a_killed_pigean_fails_the_library(self):
        os.environ["FAKE_PIGEAN_CRASH"] = "LIBB"
        code, err = self.betas("LIBA,LIBB", "runs")
        self.assertEqual(code, 1)
        self.assertIn("pigean betas failed on LIBB (exit 137)", err)
        self.assertFalse(os.path.exists(self.p("work", "T-one", "LIBB.done")))

    def test_the_initial_cap_reaches_pigean_and_its_fingerprint(self):
        self.assertEqual(self.betas("LIBA", "runs")[0], 0)
        with open(self.p("calls.txt.args")) as fh:
            self.assertIn("--max-num-gene-sets-initial 20000 ", fh.read())
        self.assertEqual(self.betas("LIBA", "again", cap="5000")[0], 0)  # another cap refits
        self.assertEqual(self.calls(), ["LIBA.gmt", "LIBA.gmt"])

    def test_refuses_an_unknown_library_a_wrong_kpn_id_or_a_dirty_clone(self):
        code, err = self.betas("LIBA,LIBZ", "runs")
        self.assertEqual(code, 1)
        self.assertIn("No GMT for libraries ['LIBZ']", err)
        write(self.p("trait_kpn_map.tsv"), tsv([pw.TRAIT_KPN_COLUMNS] + [("T-one", KPN_IDS["Solo"], "v0.0.2", "c", "x", "T-one", "g",
                                                                          "lg", "tt", "1")]))
        code, err = self.betas("LIBA", "runs")
        self.assertEqual(code, 1)
        self.assertIn("KPN id for T-one disagrees", err)
        write(self.p("trait_kpn_map.tsv"), tsv([pw.TRAIT_KPN_COLUMNS] + [(t, k, "v0.0.2", "c", "x", t, "g", "lg", "tt", "1")
                                                                         for t, k in KPN_IDS.items()]))
        write(os.path.join(self.repo, "src", "pigean", "y.py"), "y = 2\n")
        code, err = self.betas("LIBA", "runs")
        self.assertEqual(code, 1)
        self.assertIn("has local changes under src/", err)

    def test_collect_needs_every_library_of_every_trait(self):
        for trait in KPN_IDS:
            self.assertEqual(self.betas("LIBA,LIBB,LINCS", trait, exclude="LINCS", trait=trait)[0], 0)
        collect = lambda *files: run(["betas-collect"] + sum((["--runs-file", self.p(f + ".runs.tsv")] for f in files), [])
                                     + ["--trait-kpn-map-file", self.p("trait_kpn_map.tsv"), "--libraries", "LIBA,LIBB,LINCS",
                                        "--exclude-libraries", "LINCS", "--expected-pigean-commit", self.head,
                                        "--output-file", self.p("manifest.tsv")])
        self.assertEqual(collect("T-one", "Solo")[0], 0)
        rows = read_rows(self.p("manifest.tsv"))
        self.assertEqual([(r["trait"], r["library"], r["status"]) for r in rows],
                         [("Solo", "LIBA", "fitted"), ("Solo", "LIBB", "fitted"), ("T-one", "LIBA", "fitted"), ("T-one", "LIBB", "fitted")])
        code, err = collect("T-one")
        self.assertEqual(code, 1)
        self.assertIn("Runs cover 1 traits; the project has 2", err)

if __name__ == "__main__":
    unittest.main()
