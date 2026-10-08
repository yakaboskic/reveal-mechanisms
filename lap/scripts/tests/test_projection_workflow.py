"""Tests for lap/scripts/projection_workflow.py on tiny fixtures, including real eaggl runs.

Run with the interpreter the pipeline uses (it has numpy/scipy for eaggl):
  /humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s lap/scripts/tests
"""

import contextlib
import csv
import gzip
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import projection_workflow as pw  # noqa: E402

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
            write(os.path.join(d, "genesets.dapper-ids.gmt"), tsv([[i, ""] + genes for i, _, genes in sets]))
            write(os.path.join(d, "genesets.gmt"), tsv([[name, ""] + genes for _, name, genes in sets]))
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
                         "--cfde-snapshot", "2026-09-28", "--output-gmt-file", p("out", "annotations.gmt.gz"),
                         "--output-gene-set-index-file", p("out", "gene_set_index.tsv.gz"),
                         "--output-gene-map-file", p("out", "case.gene.map"),
                         "--output-overlap-report-file", p("out", "overlap.tsv")])
        cls.check_run(["assemble-factors", "--loadings-file", p("eaggl", "loadings.tsv.gz"),
                       "--factor-ids-file", p("eaggl", "factor_ids.tsv"),
                       "--factor-metadata-file", p("eaggl", "factor_metadata.tsv"),
                       "--genes-file", p("eaggl", "genes.tsv"), "--capped-entries-file", p("eaggl", "capped_entries.tsv.gz"),
                       "--trait-kpn-map-file", p("out", "trait_kpn_map.tsv"), "--loading-variant", "capped",
                       "--output-file", p("out", "all_factors.tsv.gz"),
                       "--output-factor-index-file", p("out", "factor_index.tsv")])
        # A clean checkout standing in for the pinned pigean clone (project-trait checks it like record-pigean-commit).
        cls.repo = p("pigean")
        write(os.path.join(cls.repo, "src", "eaggl", "x.py"), "x = 1\n")
        subprocess.run(["git", "init", "-q", cls.repo], check=True)
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-qm", "c")
        cls.head = subprocess.run(["git", "-C", cls.repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                  universal_newlines=True).stdout.strip()
        # Chunks of 3: the 4 gene sets are projected as 3 + 1.
        cls.check_run(["chunk-annotations", "--annotations-gmt-file", p("out", "annotations.gmt.gz"),
                       "--gene-set-index-file", p("out", "gene_set_index.tsv.gz"), "--chunk-size", "3",
                       "--output-dir", p("out", "chunks"), "--output-file", p("out", "chunks.tsv")])
        for trait in KPN_IDS:
            d = lambda name: p("out", trait, name)
            os.makedirs(p("out", trait))
            cls.check_run(["trait-factors", "--all-factors-file", p("out", "all_factors.tsv.gz"),
                           "--factor-index-file", p("out", "factor_index.tsv"), "--trait", trait,
                           "--output-file", d("factors.tsv.gz"), "--output-factor-index-file", d("factor_index.tsv")])
            cls.check_run(cls.project_args(trait, p("out", "chunks.tsv"), d))
        os.makedirs(p("out", "global"))
        cls.run_eaggl(p("out", "all_factors.tsv.gz"), p("out", "global", ""))
        g = lambda name: p("out", "global", name)
        cls.check_run(["relabel-global", "--joint-file", g("joint.tsv.gz"), "--marginal-file", g("marginal.tsv.gz"),
                       "--all-factors-file", p("out", "all_factors.tsv.gz"), "--factor-index-file", p("out", "factor_index.tsv"),
                       "--gene-set-index-file", p("out", "gene_set_index.tsv.gz"), "--params-file", g("params.tsv"),
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
    def project_args(cls, trait, chunks_file, d, kpn_trait_id=None):
        """The flags of trait_project_cmd in cfde_projection.cfg."""
        p = cls.fx.p
        return ["project-trait", "--python", sys.executable, "--pigean-src", PIGEAN_SRC, "--repo-dir", cls.repo,
                "--expected-pigean-commit", cls.head, "--chunks-file", chunks_file, "--trait-factors-file", d("factors.tsv.gz"),
                "--trait-factor-index-file", d("factor_index.tsv"), "--gene-map-file", p("out", "case.gene.map"),
                "--gene-set-index-file", p("out", "gene_set_index.tsv.gz"), "--trait-kpn-map-file", p("out", "trait_kpn_map.tsv"),
                "--trait", trait, "--kpn-trait-id", kpn_trait_id or KPN_IDS[trait], "--loading-variant", "capped",
                "--seed", "1", "--top-n", "2", "--work-dir", d("eaggl_chunks"), "--output-long-file", d("long.tsv.gz"),
                "--output-top-file", d("top.tsv.gz"), "--output-qc-file", d("qc.tsv"), "--output-commit-file", d("commit.txt"),
                "--output-params-file", d("params.tsv"), "--output-warnings-file", d("warnings.txt"),
                "--output-log-file", d("eaggl.log")]

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
             "--factor-gene-clusters-layout", "factors-by-genes", "--X-in", p("out", "annotations.gmt.gz"),
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
        with gzip.open(self.fx.p("out", "annotations.gmt.gz"), "rt") as fh:
            ids = [line.split("\t")[0] for line in fh]
        self.assertEqual(ids, [i for c in COLLECTIONS for i, _, _ in c[3]])
        with open(self.fx.p("out", "case.gene.map")) as fh:
            self.assertEqual(fh.read(), "C10orf71\tC10ORF71\n")
        index = read_rows(self.fx.p("out", "gene_set_index.tsv.gz"))
        self.assertEqual({r["gene_set_id"]: (r["collection_id"], r["n_genes_in_eaggl_universe"]) for r in index},
                         {ID_A1: (COLLECTIONS[0][2], "2"), ID_A2: (COLLECTIONS[0][2], "2"),
                          ID_B1: (COLLECTIONS[1][2], "3"), ID_B2: (COLLECTIONS[1][2], "1")})

    def test_gzip_outputs_are_byte_deterministic(self):
        p = self.fx.p
        paths = [p("out", "again.%d.tsv.gz" % i) for i in (1, 2)]
        for i, path in enumerate(paths):
            if i:
                time.sleep(1.1)  # gzip headers store a whole-second mtime
            self.check_run(["trait-factors", "--all-factors-file", p("out", "all_factors.tsv.gz"),
                            "--factor-index-file", p("out", "factor_index.tsv"), "--trait", "Solo",
                            "--output-file", path, "--output-factor-index-file", path + ".index.tsv"])
        with open(paths[0], "rb") as a, open(paths[1], "rb") as b, open(p("out", "Solo", "factors.tsv.gz"), "rb") as c:
            first = a.read()
            self.assertEqual(first, b.read())
            self.assertEqual(first, c.read())

    def test_factor_index_keeps_ids_order_and_kpn(self):
        rows = read_rows(self.fx.p("out", "factor_index.tsv"))
        self.assertEqual([(r["global_eaggl_column"], r["factor_id"], r["kpn_trait_id"]) for r in rows],
                         [("Factor1", "T-one::Factor1", KPN_IDS["T-one"]), ("Factor2", "T-one::Factor2", KPN_IDS["T-one"]),
                          ("Factor3", "Solo::Factor1", KPN_IDS["Solo"])])
        with gzip.open(self.fx.p("out", "all_factors.tsv.gz"), "rt") as fh:
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
        d = lambda name: p("out", "Solo", name) if name in ("factors.tsv.gz", "factor_index.tsv") else p("out", "wrong-kpn", name)
        code, err = run(self.project_args("Solo", p("out", "chunks.tsv"), d, KPN_IDS["T-one"]))
        self.assertEqual(code, 1)
        self.assertIn("KPN id for Solo disagrees", err)
        self.assertFalse(os.path.exists(p("out", "wrong-kpn")))  # refused before any chunk ran

    def test_chunks_cover_the_index_in_order(self):
        rows = read_rows(self.fx.p("out", "chunks.tsv"))
        self.assertEqual([(r["chunk"], r["first_row"], r["n_gene_sets"]) for r in rows], [("chunk_00001", "0", "3"), ("chunk_00002", "3", "1")])
        self.assertEqual([r["first_gene_set_id"] for r in rows], [ID_A1, ID_B2])

    def test_chunked_projection_equals_one_chunk_and_resumes(self):
        p = self.fx.p
        os.makedirs(p("out", "single", "T-one"))
        self.check_run(["chunk-annotations", "--annotations-gmt-file", p("out", "annotations.gmt.gz"),
                        "--gene-set-index-file", p("out", "gene_set_index.tsv.gz"), "--chunk-size", "100",
                        "--output-dir", p("out", "single", "chunks"), "--output-file", p("out", "single", "chunks.tsv")])
        d = lambda name: p("out", "single", "T-one", name) if name not in ("factors.tsv.gz", "factor_index.tsv") else p("out", "T-one", name)
        args = self.project_args("T-one", p("out", "single", "chunks.tsv"), d) + ["--keep-work-dir"]
        self.check_run(args)
        chunked = {(r["factor_id"], r["gene_set_id"]): r for r in self.long_rows("T-one")}
        single = {(r["factor_id"], r["gene_set_id"]): r for r in read_rows(d("long.tsv.gz"))}
        self.assertEqual(set(chunked), set(single))
        for key, row in single.items():
            self.assertEqual(row["marginal_loading"], chunked[key]["marginal_loading"])
            self.assertAlmostEqual(float(row["joint_loading"]), float(chunked[key]["joint_loading"]), delta=1e-3)
        # The work directory was kept: a rerun reuses the finished chunk.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pw.main(args), 0)
        self.assertIn("(0 run now)", out.getvalue())

    def test_library_ranks_restrict_the_factor_order_to_the_library(self):
        for row in self.long_rows("T-one"):
            library = [r for r in self.long_rows("T-one") if r["factor_id"] == row["factor_id"] and r["library"] == row["library"]]
            expected = 1 + sum(1 for r in library if int(r["joint_rank_in_factor"]) < int(row["joint_rank_in_factor"]))
            self.assertEqual(int(row["joint_rank_in_library"]), expected)


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

    def annotate(self, rows, response="log_bf", used="log_bf", trait="T-one"):
        write(self.p("stats.tsv"), tsv([self.PIGEAN_COLUMNS] + rows))
        write(self.p("params.tsv"), tsv([("Parameter", "Version", "Value"), ("option_gene_stats_log_bf_col", "1", used),
                                         ("p", "1", "0.0004"), ("sigma2", "1", "7e-09")]))
        return run(["annotate-gene-set-stats", "--stats-file", self.p("stats.tsv"), "--params-file", self.p("params.tsv"),
                    "--gene-set-index-file", self.gene_set_index, "--trait-kpn-map-file", self.kpn_map, "--trait", trait,
                    "--kpn-trait-id", KPN_IDS[trait], "--response", response, "--pigean-commit-file", self.commit,
                    "--expected-pigean-commit", FAKE_COMMIT, "--output-file", self.p("out.tsv.gz")])

    def test_annotate_keeps_analyzed_gene_sets_ranked_within_their_library(self):
        row = lambda i, reason, uncorrected: (i, "x.gmt", reason, "3", "0.1", "0.05", uncorrected, "0.5", "0.0004", "7e-07")
        code, err = self.annotate([row(ID_A1, "kept", "0.2"), row(ID_A2, "kept", "0.9"), row(ID_B1, "kept", "0.1"),
                                   row(ID_B2, "prefilter_p_value", "0")])
        self.assertEqual((code, err), (0, ""))
        rows = read_rows(self.p("out.tsv.gz"))
        self.assertEqual([(r["gene_set_id"], r["library"], r["library_rank"], r["beta_uncorrected"]) for r in rows],
                         [(ID_A2, "LIBA", "1", "0.9"), (ID_A1, "LIBA", "2", "0.2"), (ID_B1, "LIBB", "1", "0.1")])
        self.assertEqual({(r["kpn_trait_id"], r["collection_id"], r["response"], r["p"], r["sigma2"]) for r in rows if r["library"] == "LIBB"},
                         {(KPN_IDS["T-one"], COLLECTIONS[1][2], "log_bf", "0.0004", "7e-09")})

    def test_annotate_refuses_another_response_or_an_unknown_gene_set(self):
        row = (ID_A1, "x.gmt", "kept", "3", "0.1", "0.05", "0.2", "0.5", "0.0004", "7e-07")
        code, err = self.annotate([row], response="log_bf", used="combined")
        self.assertEqual(code, 1)
        self.assertIn("pigean regressed on combined, expected log_bf", err)
        code, err = self.annotate([("dapper:GeneSet." + "z" * 32,) + row[1:]])
        self.assertEqual(code, 1)
        self.assertIn("Unknown gene set", err)


if __name__ == "__main__":
    unittest.main()
