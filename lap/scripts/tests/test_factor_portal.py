"""Tests for the audit portal (scripts/factor_portal) and its LAP stage (portal_ commands).

A small, internally consistent project (2 traits, 3 factors, 5 gene sets in 2 collections) is written in the LAP
output formats, with joint/marginal loadings, ranks and top rows derived exactly as annotate-projection derives them.
Run with:
  /humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s lap/scripts/tests
"""

import argparse
import gzip
import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from projection_workflow import (FACTOR_INDEX_COLUMNS, GENE_SET_INDEX_COLUMNS, LONG_COLUMNS, QC_COLUMNS,  # noqa: E402
                                 TOP_COLUMNS, WorkflowError)
from factor_portal import build, queries, server  # noqa: E402
from factor_portal.__main__ import FACTOR_AUDIT_COLUMNS, GENE_SET_AUDIT_COLUMNS, build_parser, main  # noqa: E402
from factor_portal.assets import render_page  # noqa: E402
import test_reload_stage as lint  # noqa: E402

TOP_N = 2
UNIVERSE = ["G%02d" % i for i in range(1, 14)] + ["C1ORF1"]
TRAITS = {"TA": ("KPN.TRAIT:0000001", "Trait A"), "TB": ("KPN.TRAIT:0000002", "Trait B")}
FACTORS = [  # (factor_id, label, loadings)
    ("TA::Factor1", "Alpha program", {"G01": 1.0, "G02": 0.8, "G03": 0.5, "G04": 0.2}),
    ("TA::Factor2", "Beta program", {"G05": 1.0, "G06": 0.6, "G01": 0.1}),
    ("TB::Factor1", "Gamma program", {"G07": 0.9, "G08": 0.9, "G01": 0.3}),
]
GENE_SETS = [  # (id, name, collection label, members as written in the GMT)
    ("dapper:GeneSet." + "a" * 32, "set_one", "L1__c1", ["G01", "G02", "G03", "C1orf1"]),
    ("dapper:GeneSet." + "b" * 32, "set_two", "L1__c1", ["G05", "G06", "G09"]),
    ("dapper:GeneSet." + "c" * 32, "set_three", "L1__c1", ["G07", "G08", "ZZZ"]),
    ("dapper:GeneSet." + "d" * 32, "set_four", "L2__c2", ["G01", "G05", "G07", "G10", "G11"]),
    ("dapper:GeneSet." + "e" * 32, "set_five", "L2__c2", ["G12", "G13"]),
]
COLLECTIONS = {"L1__c1": ("dapper:GeneSetCollection." + "1" * 32, "L1"), "L2__c2": ("dapper:GeneSetCollection." + "2" * 32, "L2")}
CASE_MAP = {"C1orf1": "C1ORF1"}


def g4(x):
    return "%.4g" % x


def tsv(path, columns, rows):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as fh:
        fh.write("\t".join(columns) + "\n")
        for row in rows:
            fh.write("\t".join(str(row[c]) for c in columns) + "\n")
    return path


def mapped(members):
    return [CASE_MAP.get(m, m) for m in members]


def marginal(loadings, members):
    l2sq = sum(w * w for w in loadings.values())
    return min(1.0, max(0.0, sum(loadings.get(g, 0.0) for g in set(mapped(members))) / l2sq))


def projection_rows():
    """Long rows for every (factor, gene set): marginal is exact, joint = 0.8 x marginal except on K=1 traits."""
    by_trait = {}
    for factor_id, label, loadings in FACTORS:
        by_trait.setdefault(factor_id.split("::")[0], []).append((factor_id, label, loadings))
    rows = []
    for trait, factors in by_trait.items():
        k1 = len(factors) == 1
        values = {}
        for factor_id, _, loadings in factors:
            for gs_id, _, _, members in GENE_SETS:
                m = float(g4(marginal(loadings, members)))
                values[factor_id, gs_id] = (float(g4(m if k1 else 0.8 * m + 0.01 * int(factor_id[-1]))), m)
        best = {gs_id: max(factors, key=lambda f: values[f[0], gs_id][0])[0] for gs_id, _, _, _ in GENE_SETS}
        for factor_id, label, _ in factors:
            for kind in (0, 1):
                order = sorted(GENE_SETS, key=lambda g: (-values[factor_id, g[0]][kind], g[0]))
                for rank, g in enumerate(order, 1):
                    values[factor_id, g[0], kind] = rank
            for gs_id, name, coll, _ in GENE_SETS:
                joint, marg = values[factor_id, gs_id]
                rows.append({"trait": trait, "kpn_trait_id": TRAITS[trait][0], "factor_id": factor_id,
                             "factor": factor_id.split("::")[1], "factor_label": label, "gene_set_id": gs_id,
                             "gene_set_name": name, "collection_id": COLLECTIONS[coll][0], "cfde_label": coll,
                             "library": COLLECTIONS[coll][1], "joint_loading": g4(joint), "marginal_loading": g4(marg),
                             "joint_rank_in_factor": values[factor_id, gs_id, 0],
                             "marginal_rank_in_factor": values[factor_id, gs_id, 1],
                             "is_joint_top_factor": int(best[gs_id] == factor_id)})
    return rows


def write_project(root, perturb_marginal=False):
    p = lambda name: os.path.join(root, "proj." + name)  # noqa: E731
    n_factors = {t: sum(1 for f in FACTORS if f[0].startswith(t + "::")) for t in TRAITS}
    tsv(p("trait_kpn_map.tsv"), ["trait", "kpn_trait_id", "kpn_release", "kpn_release_commit", "gwas_source_category",
                                 "phenotype_name", "trait_group", "legacy_trait_group", "trait_type", "n_factors"],
        [{"trait": t, "kpn_trait_id": k, "kpn_release": "v0.0.2", "kpn_release_commit": "abc", "gwas_source_category": "KPN",
          "phenotype_name": name, "trait_group": "metabolic", "legacy_trait_group": "GLYCEMIC", "trait_type": "phenotype",
          "n_factors": n_factors[t]} for t, (k, name) in TRAITS.items()])
    flat_cols = ["portal_id", "gwas_source_category", "legacy_trait_group", "trait_group", "phenotype", "phenotype_name",
                 "description", "trait_type", "is_dichotomous", "is_complex", "pigean_id", "mapping_count", "target_id",
                 "target_label", "target_ontology", "mapping_predicate", "confidence", "mapping_justification", "source"]
    flat = [dict.fromkeys(flat_cols, "") for _ in range(3)]
    flat[0].update(portal_id="KPN.TRAIT:0000001", description="Trait A, measured", target_id="MONDO:0005148",
                   target_label="t2d", target_ontology="MONDO", mapping_predicate="skos:exactMatch")
    flat[1].update(portal_id="KPN.TRAIT:0000001", target_id="EFO:0001360", target_ontology="EFO")
    flat[2].update(portal_id="KPN.TRAIT:0000999", target_id="EFO:1")  # not in the project: ignored
    tsv(p("kpn_trait_flat.tsv"), flat_cols, flat)
    tsv(p("projection_manifest.tsv"), QC_COLUMNS,
        [dict(zip(QC_COLUMNS, [t, k, "v0.0.2", n_factors[t], 5, 5, 5, "True", 0, "NA", len(UNIVERSE), 1, "capped",
                               "c" * 40, "True"])) for t, (k, _) in TRAITS.items()])
    index_rows, meta_rows = [], []
    for i, (factor_id, label, loadings) in enumerate(FACTORS, 1):
        trait, factor = factor_id.split("::")
        index_rows.append(dict(zip(FACTOR_INDEX_COLUMNS, ["Factor%d" % i, factor_id, trait, TRAITS[trait][0], factor,
                                                         factor[6:], label, len(loadings),
                                                         math.sqrt(sum(w * w for w in loadings.values())), "capped"])))
        meta_rows.append({"factor_id": factor_id, "trait": trait, "factor": factor, "factor_number": factor[6:],
                          "label": label, "gene_set_score": "1.5", "gene_score": "0.4",
                          "top_genes": ",".join(sorted(loadings, key=lambda g: -loadings[g])[:2]),
                          "top_gene_sets": "GO_THING,KEGG_OTHER", "source_gc": "/x/gc.out"})
    tsv(p("factor_index.tsv"), FACTOR_INDEX_COLUMNS, index_rows)
    tsv(p("factor_metadata.tsv"), list(meta_rows[0]), meta_rows)
    with gzip.open(p("all_factors.factors_by_genes.tsv.gz"), "wt") as fh:
        fh.write("Factor\t" + "\t".join(UNIVERSE) + "\n")
        for factor_id, _, loadings in FACTORS:
            fh.write(factor_id + "\t" + "\t".join(str(loadings.get(g, 0.0)) for g in UNIVERSE) + "\n")
    tsv(p("cfde_index.tsv"), ["library", "partition", "model", "comparison", "program", "label", "s3_key", "collection_id",
                              "n_sets", "n_genes"],
        [{"library": lib, "partition": "p", "model": "m", "comparison": "c", "program": "", "label": label,
          "s3_key": label + "/genesets.gmt", "collection_id": cid,
          "n_sets": sum(1 for g in GENE_SETS if g[2] == label), "n_genes": 9} for label, (cid, lib) in COLLECTIONS.items()])
    tsv(p("gene_set_index.tsv.gz"), GENE_SET_INDEX_COLUMNS,
        [dict(zip(GENE_SET_INDEX_COLUMNS, [gs_id, name, COLLECTIONS[coll][0], coll, COLLECTIONS[coll][1], "p", "m", "c", "",
                                           i, len(members), len(set(mapped(members)) & set(UNIVERSE)), "2026-09-28"]))
         for i, (gs_id, name, coll, members) in enumerate(GENE_SETS, 1)])
    with gzip.open(p("cfde_annotations.gmt.gz"), "wt") as fh:
        for gs_id, _, _, members in GENE_SETS:
            fh.write(gs_id + "\t\t" + "\t".join(members) + "\n")
    with open(p("gene.map"), "w") as fh:
        fh.writelines("%s\t%s\n" % kv for kv in CASE_MAP.items())
    rows = projection_rows()
    top = [dict(r) for r in rows if r["joint_rank_in_factor"] <= TOP_N or r["marginal_rank_in_factor"] <= TOP_N]
    if perturb_marginal:
        top[0]["marginal_loading"] = g4(float(top[0]["marginal_loading"]) + 0.01)
    tsv(p("top.tsv.gz"), TOP_COLUMNS, top)
    long_files = [tsv(os.path.join(root, "%s.long.tsv.gz" % t), LONG_COLUMNS, [r for r in rows if r["trait"] == t])
                  for t in TRAITS]
    args = argparse.Namespace(
        project="proj", trait_kpn_map_file=p("trait_kpn_map.tsv"), kpn_trait_flat_file=p("kpn_trait_flat.tsv"),
        projection_manifest_file=p("projection_manifest.tsv"), factor_index_file=p("factor_index.tsv"),
        factor_metadata_file=p("factor_metadata.tsv"), all_factors_file=p("all_factors.factors_by_genes.tsv.gz"),
        cfde_index_file=p("cfde_index.tsv"), gene_set_index_file=p("gene_set_index.tsv.gz"),
        annotations_gmt_file=p("cfde_annotations.gmt.gz"), gene_map_file=p("gene.map"), top_gene_sets_file=p("top.tsv.gz"),
        long_file=long_files, top_n=TOP_N, output_db=os.path.join(root, "proj.portal.sqlite"))
    for key in build.DEFAULT_THRESHOLDS:
        setattr(args, "threshold_" + key, None)
    args.threshold_hub_min_factors = 2
    return args


def quiet_build(args):
    with redirect_stderr(io.StringIO()):
        build.build(args)


class HypergeometricTest(unittest.TestCase):
    def exact(self, universe, loaded, size, overlap):
        total = math.comb(universe, size)
        tail = sum(math.comb(loaded, i) * math.comb(universe - loaded, size - i)
                   for i in range(overlap, min(loaded, size) + 1))
        return -math.log10(tail / total)

    def test_matches_exact_tail(self):
        for case in [(18477, 293, 242, 12), (18477, 600, 250, 40), (1000, 50, 30, 6), (100, 10, 10, 10), (18477, 8752, 200, 150)]:
            with self.subTest(case=case):
                self.assertAlmostEqual(build.hypergeom_neg_log10_sf(*case), self.exact(*case), places=6)

    def test_no_enrichment_is_zero(self):
        self.assertEqual(build.hypergeom_neg_log10_sf(1000, 100, 100, 10), 0.0)  # overlap == expectation
        self.assertEqual(build.hypergeom_neg_log10_sf(1000, 100, 100, 0), 0.0)
        self.assertEqual(build.hypergeom_neg_log10_sf(1000, 0, 100, 0), 0.0)


class BuiltProject(unittest.TestCase):
    """One fixture project built once per test class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.args = write_project(cls.tmp.name)
        quiet_build(cls.args)
        cls.conn = queries.open_database(cls.args.output_db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()


class BuildTest(BuiltProject):
    def test_counts_and_meta(self):
        meta = queries.read_meta(self.conn)
        self.assertEqual((meta["n_traits"], meta["n_factors"], meta["n_gene_sets"], meta["n_collections"]), (2, 3, 5, 2))
        self.assertEqual(meta["n_top_rows"], sum(1 for r in projection_rows()
                                                 if r["joint_rank_in_factor"] <= TOP_N or r["marginal_rank_in_factor"] <= TOP_N))
        self.assertEqual(meta["has_sibling_loadings"], 1)
        self.assertEqual(meta["n_sibling_rows"], self.conn.execute("SELECT COUNT(*) FROM sibling_loadings").fetchone()[0])
        self.assertLessEqual(meta["marginal_check_max_absdiff"], meta["marginal_check_tolerance"])
        self.assertEqual(meta["n_factor_gene_loadings"], sum(len(f[2]) for f in FACTORS))
        self.assertEqual(meta["thresholds"]["hub_min_factors"], 2)

    def test_gene_loadings_are_ranked_and_case_mapping_applies(self):
        rows = self.conn.execute("SELECT fg.rank, g.gene, fg.loading FROM factor_genes fg JOIN genes g USING (gene_idx) "
                                 "WHERE fg.factor_idx = 1 ORDER BY fg.rank").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "G01", 1.0), (2, "G02", 0.8), (3, "G03", 0.5), (4, "G04", 0.2)])
        gene = self.conn.execute("SELECT in_universe, cfde_symbols, n_factors FROM genes WHERE gene = 'C1ORF1'").fetchone()
        self.assertEqual(tuple(gene), (1, "C1orf1", 0))
        self.assertEqual(self.conn.execute("SELECT in_universe FROM genes WHERE gene = 'ZZZ'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT n_factors FROM genes WHERE gene = 'G01'").fetchone()[0], 3)

    def test_overlap_statistics(self):
        row = self.conn.execute(
            "SELECT x.n_overlap, x.overlap_mass_frac, x.fold_enrichment, x.marginal_recomputed, x.top_overlap_genes "
            "FROM factor_gene_sets x JOIN gene_sets gs USING (gs_idx) WHERE x.factor_idx = 1 AND gs.gene_set_name = 'set_four'"
        ).fetchone()
        l2sq = 1 + 0.64 + 0.25 + 0.04
        self.assertEqual(row[0], 1)
        self.assertAlmostEqual(row[1], 1.0 / 2.5)
        self.assertAlmostEqual(row[2], 1 / (5 * 4 / len(UNIVERSE)))
        self.assertAlmostEqual(row[3], 1.0 / l2sq)
        self.assertEqual(row[4], "G01")

    def test_audit_columns_and_hubs(self):
        f = dict(self.conn.execute("SELECT * FROM factors WHERE factor_id = 'TB::Factor1'").fetchone())
        self.assertEqual((f["jm_top_overlap"], f["own_top_frac"]), (1.0, 1.0))  # K = 1: joint == marginal
        self.assertIn("few_genes", f["flags"].split(","))
        hubs = dict(self.conn.execute("SELECT gene_set_name, n_top_joint_factors FROM gene_sets").fetchall())
        joint_top = [r for r in projection_rows() if r["joint_rank_in_factor"] <= TOP_N]
        for name, n in hubs.items():
            self.assertEqual(n, sum(1 for r in joint_top if r["gene_set_name"] == name))
        libs = {r[0]: r[1] for r in self.conn.execute("SELECT library, n_top_joint_slots FROM libraries")}
        self.assertEqual(sum(libs.values()), TOP_N * len(FACTORS))

    def test_sibling_loadings_cover_every_factor_of_the_trait(self):
        rows = self.conn.execute(
            "SELECT f.factor_id, s.joint FROM sibling_loadings s JOIN factors f USING (factor_idx) JOIN gene_sets gs USING (gs_idx) "
            "WHERE gs.gene_set_name = 'set_one' AND f.trait = 'TA' ORDER BY f.factor_id").fetchall()
        expected = {(r["factor_id"]): float(r["joint_loading"]) for r in projection_rows()
                    if r["gene_set_name"] == "set_one" and r["trait"] == "TA"}
        self.assertEqual({r[0]: r[1] for r in rows}, expected)

    def test_refuses_a_marginal_that_the_loadings_do_not_reproduce(self):
        with tempfile.TemporaryDirectory() as root:
            args = write_project(root, perturb_marginal=True)
            with self.assertRaisesRegex(WorkflowError, "Recomputed marginal"):
                quiet_build(args)
            self.assertEqual(os.listdir(root).count("proj.portal.sqlite"), 0)
            self.assertFalse(os.path.exists(args.output_db + ".tmp"))

    def test_refuses_missing_long_files(self):
        with tempfile.TemporaryDirectory() as root:
            args = write_project(root)
            args.long_file = args.long_file[:1]
            with self.assertRaisesRegex(WorkflowError, "Long files cover 1 of 2 traits"):
                quiet_build(args)

    def test_builds_without_long_files(self):
        with tempfile.TemporaryDirectory() as root:
            args = write_project(root)
            args.long_file = []
            quiet_build(args)
            conn = queries.open_database(args.output_db)
            self.assertEqual(queries.read_meta(conn)["has_sibling_loadings"], 0)
            sheet = queries.factor_gene_set(conn, "TA::Factor1", GENE_SETS[0][0])
            self.assertFalse(sheet["siblings_complete"])
            self.assertTrue(all(s["marginal_recomputed"] is not None for s in sheet["siblings"]))
            self.assertFalse(queries.trait_matrix(conn, "TA")["complete"])
            conn.close()


class QueriesTest(BuiltProject):
    def test_search(self):
        res = queries.search(self.conn, "trait a")
        self.assertEqual([t["trait"] for t in res["traits"]], ["TA"])
        res = queries.search(self.conn, "G0", limit=50)
        self.assertEqual(res["genes"][0]["gene"], "G01")  # most factors first among prefix matches
        self.assertEqual([g["gene_set_name"] for g in queries.search(self.conn, GENE_SETS[2][0])["gene_sets"]], ["set_three"])
        self.assertEqual([f["factor_id"] for f in queries.search(self.conn, "beta")["factors"]], ["TA::Factor2"])
        self.assertEqual(queries.search(self.conn, "100%_")["genes"], [])  # LIKE wildcards are literal

    def test_trait_and_factor(self):
        trait = queries.trait_detail(self.conn, "TA")
        self.assertEqual(trait["trait"]["description"], "Trait A, measured")
        self.assertEqual({m["target_id"] for m in trait["mappings"]}, {"MONDO:0005148", "EFO:0001360"})
        self.assertEqual([f["factor_id"] for f in trait["factors"]], ["TA::Factor1", "TA::Factor2"])
        self.assertEqual(trait["factors"][0]["top_genes"], ["G01", "G02", "G03", "G04"])
        detail = queries.factor_detail(self.conn, "TA::Factor1")
        self.assertEqual(detail["factor"]["metadata"]["source_gc"], "/x/gc.out")
        self.assertEqual(len(detail["siblings"]), 2)
        with self.assertRaises(queries.NotFound):
            queries.factor_detail(self.conn, "TA::Factor9")

    def test_factor_genes_count_top_set_membership(self):
        genes = {g["gene"]: g for g in queries.factor_genes(self.conn, "TA::Factor1")["genes"]}
        joint_top = [r["gene_set_id"] for r in projection_rows()
                     if r["factor_id"] == "TA::Factor1" and r["joint_rank_in_factor"] <= TOP_N]
        members = {g[0]: mapped(g[3]) for g in GENE_SETS}
        self.assertEqual(genes["G01"]["n_top_sets"], sum("G01" in members[i] for i in joint_top))
        self.assertAlmostEqual(sum(g["mass_share"] for g in genes.values()), 1.0)

    def test_contributions_sum_to_the_marginal_loading(self):
        for factor_id, _, loadings in FACTORS:
            for gs_id, _, _, members in GENE_SETS:
                with self.subTest(factor=factor_id, gene_set=gs_id):
                    sheet = queries.factor_gene_set(self.conn, factor_id, gs_id)
                    total = sum(c["contribution"] for c in sheet["contributions"])
                    self.assertAlmostEqual(min(1.0, total), marginal(loadings, members))
                    self.assertEqual({c["gene"] for c in sheet["contributions"]} | {u["gene"] for u in sheet["unloaded"]}
                                     | set(sheet["outside"]), set(mapped(members)))
                    self.assertTrue(sheet["siblings_complete"])
                    trait = factor_id.split("::")[0]
                    self.assertEqual(len(sheet["siblings"]), sum(1 for f in FACTORS if f[0].startswith(trait + "::")))
        sheet = queries.factor_gene_set(self.conn, "TB::Factor1", GENE_SETS[2][0])
        self.assertEqual(sheet["outside"], ["ZZZ"])

    def test_gene_set_gene_and_matrix(self):
        gs = queries.gene_set_detail(self.conn, GENE_SETS[0][0])
        self.assertEqual([m["gene"] for m in gs["members"]], ["G01", "G02", "G03", "C1ORF1"])
        gene = queries.gene_detail(self.conn, "G01")
        self.assertEqual([f["factor_id"] for f in gene["factors"]], ["TA::Factor1", "TB::Factor1", "TA::Factor2"])
        matrix = queries.trait_matrix(self.conn, "TA", per_factor=1)
        self.assertTrue(matrix["complete"])
        self.assertTrue(all(all(c is not None for c in row["cells"]) for row in matrix["rows"]))
        self.assertEqual(len(queries.hubs(self.conn, limit=100)), sum(1 for g in GENE_SETS if any(
            r["gene_set_id"] == g[0] and r["joint_rank_in_factor"] <= TOP_N for r in projection_rows())))


class ServerAndCliTest(BuiltProject):
    def setUp(self):
        state = server.PortalState(self.args.output_db, "Audit test")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(state))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as res:
                return res.status, res.headers, res.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read().decode()

    def test_page_and_api(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Audit test</title>", body)
        self.assertIn('window.FACTOR_PORTAL_API_BASE = "";', body)
        status, headers, body = self.get("/api/summary")
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (200, "*"))
        self.assertEqual(json.loads(body)["meta"]["project"], "proj")
        status, _, body = self.get("/api/factor_gene_set?factor=TA%3A%3AFactor1&id=" + GENE_SETS[0][0])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["gene_set"]["gene_set_name"], "set_one")
        self.assertEqual(self.get("/api/factor?id=nope")[0], 404)
        self.assertEqual(self.get("/api/factor")[0], 400)
        self.assertEqual(self.get("/api/search?q=G&limit=x")[0], 400)
        self.assertEqual(self.get("/api/nothing")[0], 404)

    def test_html_and_export_audit(self):
        with tempfile.TemporaryDirectory() as root:
            page, factors, gene_sets = (os.path.join(root, n) for n in ("p.html", "f.tsv", "g.tsv"))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["html", "--db", self.args.output_db, "--api-url", "http://localhost:9", "--output-html", page]), 0)
                self.assertEqual(main(["export-audit", "--db", self.args.output_db, "--output-factor-audit-file", factors,
                                       "--output-gene-set-audit-file", gene_sets]), 0)
            with open(page) as fh:
                self.assertIn('window.FACTOR_PORTAL_API_BASE = "http://localhost:9";', fh.read())
            with open(factors) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[0].split("\t"), FACTOR_AUDIT_COLUMNS)
            self.assertEqual(len(lines), 1 + len(FACTORS))
            with open(gene_sets) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(lines[0].split("\t"), GENE_SET_AUDIT_COLUMNS)
            self.assertEqual(len(lines), 1 + len(GENE_SETS))

    def test_refuses_a_database_from_another_schema(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "old.sqlite")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '0')")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(RuntimeError, "rebuild"):
                queries.open_database(path)

    def test_page_escapes_the_api_base(self):
        self.assertIn('"<\\/script>"', render_page("t", api_base="</script>"))


class PortalCfgTest(unittest.TestCase):
    CMDS = {"portal_build_db_cmd": ("short", "build"), "portal_export_audit_cmd": ("local", "export-audit"),
            "portal_build_html_cmd": ("local", "html")}

    @classmethod
    def setUpClass(cls):
        cls.decl = lint.cfg_declarations()

    def test_commands_are_project_level_with_declared_files(self):
        import re
        for key, (kind, sub) in self.CMDS.items():
            with self.subTest(cmd=key):
                prefixes, value, postfix = self.decl[key]
                self.assertIn(kind, prefixes)
                self.assertTrue(postfix.startswith("class_level project"))
                self.assertTrue(value.startswith("$portal_cmd"))
                for file_key in re.findall(r"!\{(?:input|output):[^:}]*:([a-z_]+)\}", value):
                    self.assertIn("file", self.decl[file_key][0], file_key)

    def test_flags_are_accepted_by_the_cli(self):
        import re
        parser = build_parser()
        subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        for key, (_, sub) in self.CMDS.items():
            value = self.decl[key][1]
            flags = set(re.findall(r"!\{(?:input|output|prop):(--[a-z-]+):", value)) | set(re.findall(r"\s(--[a-z-]+)\s", value))
            known = set(subparsers[sub]._option_string_actions) | set(parser._option_string_actions)
            with self.subTest(cmd=key):
                self.assertTrue(flags)
                self.assertEqual(flags - known, set())

    def test_build_reads_every_trait_long_file_and_the_projected_loadings(self):
        value = self.decl["portal_build_db_cmd"][1]
        self.assertIn("!{input:--long-file:trait_projection_long_file}", value)
        self.assertIn("!{input:--all-factors-file:all_factors_file}", value)
        self.assertIn("--top-n $top_n_gene_sets", value)
        runner = self.decl["portal_cmd"][1]
        self.assertIn("PYTHONPATH=$base_dir/scripts", runner)
        self.assertTrue(runner.endswith("-m factor_portal"))


if __name__ == "__main__":
    unittest.main()
