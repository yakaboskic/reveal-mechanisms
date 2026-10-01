"""Build the portal SQLite from the LAP outputs (`python -m factor_portal build`).

Everything is read from files the projection pipeline already wrote and QC'd; nothing is re-projected.
The builder adds three things a reviewer needs and the TSVs do not hold in one place:

  * the EAGGL gene loadings of every factor (the nonzero entries of the factors-by-genes table that eaggl
    actually projected), ranked;
  * for every (factor, top gene set) row: the member genes that carry a loading on the factor, their share
    of the factor's loading mass, the hypergeometric overlap against the factor's loaded genes, and the
    marginal loading recomputed from the loadings (it must equal eaggl's value within %.4g rounding);
  * per-factor audit columns and flags, hub gene sets (in the joint top-N of many factors) and per-library
    representation in the top lists.

With the per-trait long files (`--long-file`, one per trait) it also stores, for every gene set in any of a
trait's top lists, its joint and marginal loading on each of that trait's factors, so the portal can show
which sibling factor took a gene set in the joint solve.

The database is written to <output>.tmp and renamed only when every check passed.
"""

import csv
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

from projection_workflow import (FACTOR_INDEX_COLUMNS, GENE_SET_INDEX_COLUMNS, LONG_COLUMNS,
                                 LOADING_ROUNDING_TOL, QC_COLUMNS, TOP_COLUMNS, WorkflowError, check, open_text,
                                 read_tsv, split_factor_id)

from . import SCHEMA_VERSION

LOG = logging.getLogger("factor_portal")
csv.field_size_limit(sys.maxsize)

# -log10 p at which a top gene set's overlap with the factor's loaded genes counts as beyond chance:
# Bonferroni 0.05 over the 44,399 gene sets is 1.1e-6.
SIG_NEG_LOG10_P = 6.0
# Calibrated on eaggl_capped__cfde_2026_09_28 (4,037 factors, top 50) so that each flag marks roughly the 1-10%
# tail of its distribution; they triage, they do not judge. Every value is stored in meta and shown in the UI.
# (The median joint top-50 row overlaps 12 of ~240 member genes, fold 3.4, p ~ 3e-4: a p < 1e-6 overlap is the
# exception, so sig_overlap_frac is reported but not flagged.)
DEFAULT_THRESHOLDS = {
    "hub_min_factors": 40,       # a gene set in the joint top-N of >= this many factors is a "hub" (522 sets)
    "few_genes": 25,             # factors with fewer nonzero gene loadings
    "weak_joint": 0.15,          # best joint loading below this
    "jm_overlap": 0.25,          # share of the joint top-N also in the marginal top-N
    "own_top": 0.5,              # share of the joint top-N for which this factor is the trait's top joint factor
    "hub_frac": 0.5,             # share of the joint top-N that are hub gene sets
    "median_fold": 2.0,          # median fold enrichment of the joint top-N's overlap with the loaded genes
}
FLAG_TEXT = [
    ("few_genes", "fewer than {few_genes} genes carry a nonzero loading"),
    ("weak_joint", "best joint loading is below {weak_joint}"),
    ("joint_marginal_disagree", "fewer than {jm_overlap_pct} of the joint top-{top_n} gene sets are also in the marginal top-{top_n}"),
    ("sibling_explained", "fewer than {own_top_pct} of the joint top-{top_n} gene sets load highest on this factor among the trait's factors"),
    ("hub_heavy", "at least {hub_frac_pct} of the joint top-{top_n} are hub gene sets (in the joint top-{top_n} of >= {hub_min_factors} factors)"),
    ("weak_overlap", "the joint top-{top_n} gene sets overlap the factor's loaded genes with a median fold enrichment below {median_fold}"),
]
TOP_OVERLAP_GENES = 8  # member genes with the largest loadings, kept per row for the table preview

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE traits (
    trait TEXT PRIMARY KEY, kpn_trait_id TEXT NOT NULL, phenotype_name TEXT, trait_group TEXT, legacy_trait_group TEXT,
    trait_type TEXT, gwas_source_category TEXT, description TEXT, is_dichotomous TEXT, kpn_release TEXT,
    n_factors INTEGER NOT NULL, n_flagged_factors INTEGER NOT NULL DEFAULT 0, qc_pass INTEGER NOT NULL,
    qc_json TEXT NOT NULL);
CREATE TABLE trait_mappings (
    kpn_trait_id TEXT NOT NULL, target_id TEXT NOT NULL, target_label TEXT, target_ontology TEXT, predicate TEXT,
    confidence TEXT, justification TEXT, source TEXT);
CREATE TABLE genes (
    gene_idx INTEGER PRIMARY KEY, gene TEXT NOT NULL UNIQUE, in_universe INTEGER NOT NULL, cfde_symbols TEXT,
    n_factors INTEGER NOT NULL DEFAULT 0, max_loading REAL);
CREATE TABLE factors (
    factor_idx INTEGER PRIMARY KEY, factor_id TEXT NOT NULL UNIQUE, trait TEXT NOT NULL, factor TEXT NOT NULL,
    factor_number INTEGER NOT NULL, label TEXT, loading_variant TEXT, n_nonzero INTEGER NOT NULL, loading_l1 REAL,
    loading_l2 REAL, gene_set_score REAL, gene_score REAL, eaggl_top_genes TEXT, eaggl_top_gene_sets TEXT,
    metadata_json TEXT NOT NULL, n_top_rows INTEGER NOT NULL DEFAULT 0, top_joint_gs INTEGER, top_joint_loading REAL,
    top_marginal_gs INTEGER, top_marginal_loading REAL, jm_top_overlap REAL, own_top_frac REAL, hub_frac REAL,
    sig_overlap_frac REAL, median_fold_enrichment REAL, dominant_library TEXT, dominant_library_frac REAL,
    library_counts_json TEXT, flags TEXT NOT NULL DEFAULT '');
CREATE TABLE factor_genes (
    factor_idx INTEGER NOT NULL, rank INTEGER NOT NULL, gene_idx INTEGER NOT NULL, loading REAL NOT NULL,
    PRIMARY KEY (factor_idx, rank)) WITHOUT ROWID;
CREATE TABLE collections (
    collection_id TEXT PRIMARY KEY, label TEXT NOT NULL, library TEXT, partition TEXT, model TEXT, comparison TEXT,
    program TEXT, n_sets INTEGER, n_genes INTEGER, s3_key TEXT);
CREATE TABLE gene_sets (
    gs_idx INTEGER PRIMARY KEY, gene_set_id TEXT NOT NULL UNIQUE, gene_set_name TEXT NOT NULL,
    collection_id TEXT NOT NULL, cfde_label TEXT, library TEXT, partition TEXT, model TEXT, comparison TEXT,
    program TEXT, n_genes INTEGER, n_universe INTEGER NOT NULL, n_top_joint_factors INTEGER NOT NULL DEFAULT 0,
    n_top_any_factors INTEGER NOT NULL DEFAULT 0, n_top_joint_traits INTEGER NOT NULL DEFAULT 0);
CREATE TABLE gene_set_members (gs_idx INTEGER PRIMARY KEY, members TEXT NOT NULL);
CREATE TABLE factor_gene_sets (
    factor_idx INTEGER NOT NULL, gs_idx INTEGER NOT NULL, joint REAL NOT NULL, marginal REAL NOT NULL,
    joint_rank INTEGER NOT NULL, marginal_rank INTEGER NOT NULL, is_joint_top_factor INTEGER NOT NULL,
    n_overlap INTEGER NOT NULL, overlap_mass_frac REAL, fold_enrichment REAL, neg_log10_p REAL,
    marginal_recomputed REAL NOT NULL, top_overlap_genes TEXT NOT NULL,
    PRIMARY KEY (factor_idx, gs_idx)) WITHOUT ROWID;
CREATE TABLE sibling_loadings (
    gs_idx INTEGER NOT NULL, factor_idx INTEGER NOT NULL, joint REAL NOT NULL, marginal REAL NOT NULL,
    joint_rank INTEGER NOT NULL, marginal_rank INTEGER NOT NULL, is_joint_top_factor INTEGER NOT NULL,
    PRIMARY KEY (gs_idx, factor_idx)) WITHOUT ROWID;
CREATE TABLE libraries (
    library TEXT PRIMARY KEY, n_collections INTEGER NOT NULL, n_gene_sets INTEGER NOT NULL, set_share REAL NOT NULL,
    n_top_joint_slots INTEGER NOT NULL, top_joint_share REAL NOT NULL, representation REAL,
    n_factors_with_top INTEGER NOT NULL, n_factors_dominant INTEGER NOT NULL);
"""
INDEXES = """
CREATE INDEX trait_mappings_id ON trait_mappings(kpn_trait_id);
CREATE INDEX factors_trait ON factors(trait, factor_number);
CREATE INDEX factor_genes_gene ON factor_genes(gene_idx, loading);
CREATE INDEX gene_sets_name ON gene_sets(gene_set_name COLLATE NOCASE);
CREATE INDEX gene_sets_hub ON gene_sets(n_top_joint_factors);
CREATE INDEX factor_gene_sets_gs ON factor_gene_sets(gs_idx, joint);
"""

LN10 = math.log(10.0)


def _float(value, where):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise WorkflowError("Non-numeric value %r in %s" % (value, where))
    check(math.isfinite(number), "Non-finite value %r in %s" % (value, where))
    return number


def _optional_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iter_tsv(path, columns):
    """Stream a TSV written by projection_workflow (never quoted), checking its header."""
    with open_text(path) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        check(header == columns, "Unexpected columns in %s: %s" % (path, header))
        for line in fh:
            yield line.rstrip("\n").split("\t")


def _log_comb(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def hypergeom_neg_log10_sf(universe, loaded, set_size, overlap):
    """-log10 P(X >= overlap) for X ~ Hypergeometric(universe, loaded, set_size); 0 when overlap <= expected."""
    if overlap <= 0 or set_size <= 0 or loaded <= 0 or overlap <= set_size * loaded / universe:
        return 0.0
    top = min(loaded, set_size)
    if overlap > top:
        return 0.0
    base = _log_comb(universe, set_size)
    peak, scaled = None, 0.0
    for i in range(overlap, top + 1):
        term = _log_comb(loaded, i) + _log_comb(universe - loaded, set_size - i) - base
        if peak is None or term > peak:
            scaled = (scaled * math.exp(peak - term) if peak is not None else 0.0) + 1.0
            peak = term
        else:
            scaled += math.exp(term - peak)
            if term < peak - 40.0:  # terms only shrink past the mode; the rest is below double precision
                break
    return max(0.0, -(peak + math.log(scaled)) / LN10)


def render_flag_text(thresholds, top_n):
    values = dict(thresholds, top_n=top_n)
    for key in ("jm_overlap", "own_top", "hub_frac"):
        values[key + "_pct"] = "%d%%" % round(100 * thresholds[key])
    return [{"flag": name, "text": text.format(**values)} for name, text in FLAG_TEXT]


def factor_flags(f, thresholds):
    flags = []
    if f["n_nonzero"] < thresholds["few_genes"]:
        flags.append("few_genes")
    if f["top_joint_loading"] is not None and f["top_joint_loading"] < thresholds["weak_joint"]:
        flags.append("weak_joint")
    if f["jm_top_overlap"] is not None and f["jm_top_overlap"] < thresholds["jm_overlap"]:
        flags.append("joint_marginal_disagree")
    if f["own_top_frac"] is not None and f["own_top_frac"] < thresholds["own_top"]:
        flags.append("sibling_explained")
    if f["hub_frac"] is not None and f["hub_frac"] >= thresholds["hub_frac"]:
        flags.append("hub_heavy")
    if f["median_fold_enrichment"] is not None and f["median_fold_enrichment"] < thresholds["median_fold"]:
        flags.append("weak_overlap")
    return flags


class Builder:
    def __init__(self, args):
        self.args = args
        self.top_n = args.top_n
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        for key in DEFAULT_THRESHOLDS:
            value = getattr(args, "threshold_" + key, None)
            if value is not None:
                self.thresholds[key] = value
        self.meta = {}

    # --- traits ------------------------------------------------------------------------------------------------
    def load_traits(self, conn):
        kpn_rows = read_tsv(self.args.trait_kpn_map_file)
        check(kpn_rows, "Empty trait KPN map %s" % self.args.trait_kpn_map_file)
        manifest = {}
        for row in _iter_tsv(self.args.projection_manifest_file, QC_COLUMNS):
            manifest[row[0]] = dict(zip(QC_COLUMNS, row))
        traits = {r["trait"]: r for r in kpn_rows}
        check(len(traits) == len(kpn_rows), "Duplicate traits in %s" % self.args.trait_kpn_map_file)
        check(set(traits) == set(manifest), "The projection manifest covers %d traits, the KPN map %d (differing: %s)"
              % (len(manifest), len(traits), sorted(set(traits) ^ set(manifest))[:10]))
        kpn_ids = {r["kpn_trait_id"] for r in kpn_rows}

        details, mappings = {}, []
        seen_mappings = set()
        for row in read_tsv(self.args.kpn_trait_flat_file):
            kpn_id = row.get("portal_id", "")
            if kpn_id not in kpn_ids:
                continue
            details.setdefault(kpn_id, row)
            target = row.get("target_id", "")
            key = (kpn_id, target, row.get("mapping_predicate", ""), row.get("source", ""))
            if target and key not in seen_mappings:
                seen_mappings.add(key)
                mappings.append((kpn_id, target, row.get("target_label"), row.get("target_ontology"),
                                 row.get("mapping_predicate"), row.get("confidence"),
                                 row.get("mapping_justification"), row.get("source")))
        for trait, row in sorted(traits.items()):
            qc = manifest[trait]
            check(qc["kpn_trait_id"] == row["kpn_trait_id"], "Trait %s has KPN id %s in the manifest and %s in the map"
                  % (trait, qc["kpn_trait_id"], row["kpn_trait_id"]))
            detail = details.get(row["kpn_trait_id"], {})
            conn.execute(
                "INSERT INTO traits (trait, kpn_trait_id, phenotype_name, trait_group, legacy_trait_group, trait_type,"
                " gwas_source_category, description, is_dichotomous, kpn_release, n_factors, qc_pass, qc_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trait, row["kpn_trait_id"], row["phenotype_name"], row["trait_group"], row["legacy_trait_group"],
                 row["trait_type"], row["gwas_source_category"], detail.get("description") or None,
                 detail.get("is_dichotomous") or None, row["kpn_release"], int(row["n_factors"]),
                 1 if qc["qc_pass"] == "True" else 0, json.dumps(qc, sort_keys=True)))
        conn.executemany("INSERT INTO trait_mappings VALUES (?,?,?,?,?,?,?,?)", mappings)
        self.traits = traits
        commits = sorted({r["pigean_commit"] for r in manifest.values()})
        self.meta.update({"n_traits": len(traits), "n_traits_qc_pass": sum(r["qc_pass"] == "True" for r in manifest.values()),
                          "kpn_release": ",".join(sorted({r["kpn_release"] for r in kpn_rows})),
                          "kpn_release_commit": ",".join(sorted({r["kpn_release_commit"] for r in kpn_rows})),
                          "pigean_commit": ",".join(commits), "n_trait_mappings": len(mappings)})
        LOG.info("traits: %d (%d ontology mappings)", len(traits), len(mappings))

    # --- genes and factors (header and index) -------------------------------------------------------------------
    def load_factor_index(self, conn):
        with open_text(self.args.all_factors_file) as fh:
            header = fh.readline().rstrip("\n").split("\t")
        check(header[0] == "Factor" and len(header) > 1, "%s is not an eaggl factors-by-genes table"
              % self.args.all_factors_file)
        self.universe = header[1:]
        check(len(set(self.universe)) == len(self.universe), "Duplicate genes in %s" % self.args.all_factors_file)
        self.gene_idx = {gene: i + 1 for i, gene in enumerate(self.universe)}
        self.gene_names = [None] + list(self.universe)
        self.gene_in_universe = [None] + [1] * len(self.universe)
        self.cfde_symbols = defaultdict(set)

        metadata = {r["factor_id"]: r for r in read_tsv(self.args.factor_metadata_file)}
        self.factors = []  # factor rows in global column order
        self.factor_by_id = {}
        for row in _iter_tsv(self.args.factor_index_file, FACTOR_INDEX_COLUMNS):
            r = dict(zip(FACTOR_INDEX_COLUMNS, row))
            column = r["global_eaggl_column"]
            check(column.startswith("Factor") and column[6:].isdigit(), "Bad global column %r" % column)
            trait, factor = split_factor_id(r["factor_id"])
            check(trait == r["trait"] and factor == r["factor"], "Factor index row %s is inconsistent" % r["factor_id"])
            check(trait in self.traits, "Factor %s belongs to an unknown trait" % r["factor_id"])
            check(r["factor_id"] in metadata, "Factor %s has no EAGGL metadata row" % r["factor_id"])
            r["factor_idx"] = int(column[6:])
            r["metadata"] = metadata[r["factor_id"]]
            self.factors.append(r)
            self.factor_by_id[r["factor_id"]] = r
        check([f["factor_idx"] for f in self.factors] == list(range(1, len(self.factors) + 1)),
              "Global factor columns are not Factor1..%d in order" % len(self.factors))
        per_trait = Counter(f["trait"] for f in self.factors)
        for trait, row in self.traits.items():
            check(per_trait.get(trait, 0) == int(row["n_factors"]), "Trait %s has %d factors in the index, %s in the map"
                  % (trait, per_trait.get(trait, 0), row["n_factors"]))
        variants = sorted({f["loading_variant"] for f in self.factors})
        self.meta.update({"n_factors": len(self.factors), "universe_size": len(self.universe),
                          "loading_variant": ",".join(variants)})
        LOG.info("factors: %d over %d genes", len(self.factors), len(self.universe))

    # --- collections and gene sets ------------------------------------------------------------------------------
    def load_gene_sets(self, conn):
        collections = read_tsv(self.args.cfde_index_file)
        for c in collections:
            conn.execute("INSERT INTO collections VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (c["collection_id"], c["label"], c["library"], c["partition"], c["model"], c["comparison"],
                          c["program"], int(c["n_sets"]), int(c["n_genes"]), c.get("s3_key")))
        case_map = {}
        with open(self.args.gene_map_file, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    source, target = line.rstrip("\n").split("\t")
                    case_map[source] = target

        index = []
        for row in _iter_tsv(self.args.gene_set_index_file, GENE_SET_INDEX_COLUMNS):
            index.append(dict(zip(GENE_SET_INDEX_COLUMNS, row)))
        self.gs_idx = {r["gene_set_id"]: i + 1 for i, r in enumerate(index)}
        check(len(self.gs_idx) == len(index), "Duplicate gene-set ids in %s" % self.args.gene_set_index_file)
        known_collections = {c["collection_id"] for c in collections}
        self.gs_library = [None] + [r["library"] for r in index]
        self.gs_name = [None] + [r["gene_set_name"] for r in index]

        # Members exactly as eaggl reads them (whitespace split after column 1), case-mapped like --gene-map-in.
        self.members = [None] * (len(index) + 1)
        self.n_universe = [0] * (len(index) + 1)
        seen = 0
        with open_text(self.args.annotations_gmt_file) as fh:
            for line in fh:
                fields = line.split()
                if not fields:
                    continue
                idx = self.gs_idx.get(fields[0])
                check(idx is not None, "Gene set %s in the GMT is not in the gene-set index" % fields[0])
                check(self.members[idx] is None, "Gene set %s appears twice in the GMT" % fields[0])
                genes, seen_genes = [], set()
                for symbol in fields[1:]:
                    gene = case_map.get(symbol, symbol)
                    if gene in seen_genes:
                        continue
                    seen_genes.add(gene)
                    gi = self.gene_idx.get(gene)
                    if gi is None:
                        gi = len(self.gene_names)
                        self.gene_idx[gene] = gi
                        self.gene_names.append(gene)
                        self.gene_in_universe.append(0)
                    if symbol != gene:
                        self.cfde_symbols[gi].add(symbol)
                    genes.append(gi)
                self.members[idx] = tuple(genes)
                self.n_universe[idx] = sum(1 for gi in genes if self.gene_in_universe[gi])
                seen += 1
        check(seen == len(index), "The GMT has %d gene sets, the index %d" % (seen, len(index)))
        for i, r in enumerate(index, 1):
            check(r["collection_id"] in known_collections, "Gene set %s has an unknown collection" % r["gene_set_id"])
            check(int(r["n_genes_in_eaggl_universe"]) == self.n_universe[i],
                  "Gene set %s: %d member genes in the EAGGL universe, the index says %s"
                  % (r["gene_set_id"], self.n_universe[i], r["n_genes_in_eaggl_universe"]))
        self.gs_rows = index
        snapshots = sorted({r["cfde_snapshot"] for r in index})
        self.meta.update({"n_gene_sets": len(index), "n_collections": len(collections), "cfde_snapshot": ",".join(snapshots),
                          "n_case_mapped_genes": len(case_map)})
        LOG.info("gene sets: %d in %d collections; %d gene symbols (%d in the EAGGL universe)", len(index),
                 len(collections), len(self.gene_names) - 1, len(self.universe))

    # --- top rows ---------------------------------------------------------------------------------------------
    def load_top_rows(self):
        self.top_rows = defaultdict(list)  # factor_idx -> [(gs_idx, joint, marginal, joint_rank, marginal_rank, is_top)]
        self.n_top_joint = Counter()
        self.n_top_any = Counter()
        top_joint_traits = defaultdict(set)
        n = 0
        col = {c: i for i, c in enumerate(TOP_COLUMNS)}
        for row in _iter_tsv(self.args.top_gene_sets_file, TOP_COLUMNS):
            where = "top row %d" % (n + 2)
            factor = self.factor_by_id.get(row[col["factor_id"]])
            check(factor is not None, "Unknown factor %s in %s" % (row[col["factor_id"]], where))
            check(row[col["trait"]] == factor["trait"] and row[col["kpn_trait_id"]] == factor["kpn_trait_id"],
                  "Trait or KPN id of %s disagrees with the factor index" % where)
            gs = self.gs_idx.get(row[col["gene_set_id"]])
            check(gs is not None, "Unknown gene set %s in %s" % (row[col["gene_set_id"]], where))
            check(row[col["library"]] == self.gs_library[gs], "Library of %s disagrees with the gene-set index" % where)
            joint, marginal = _float(row[col["joint_loading"]], where), _float(row[col["marginal_loading"]], where)
            jr, mr = int(row[col["joint_rank_in_factor"]]), int(row[col["marginal_rank_in_factor"]])
            check(jr <= self.top_n or mr <= self.top_n, "%s is in neither top-%d" % (where, self.top_n))
            is_top = int(row[col["is_joint_top_factor"]])
            self.top_rows[factor["factor_idx"]].append((gs, joint, marginal, jr, mr, is_top))
            self.n_top_any[gs] += 1
            if jr <= self.top_n:
                self.n_top_joint[gs] += 1
                top_joint_traits[gs].add(factor["trait"])
            n += 1
        missing = [f["factor_id"] for f in self.factors if f["factor_idx"] not in self.top_rows]
        check(not missing, "%d factors have no top gene sets (e.g. %s)" % (len(missing), missing[:5]))
        for factor_idx, rows in self.top_rows.items():
            gene_sets = [r[0] for r in rows]
            check(len(gene_sets) == len(set(gene_sets)), "Duplicate gene sets among the top rows of factor %d" % factor_idx)
            check(sum(1 for r in rows if r[3] <= self.top_n) == self.top_n
                  and sum(1 for r in rows if r[4] <= self.top_n) == self.top_n,
                  "Factor %d does not have exactly %d joint and %d marginal top rows" % (factor_idx, self.top_n, self.top_n))
        self.n_top_joint_traits = {gs: len(t) for gs, t in top_joint_traits.items()}
        self.hub = {gs for gs, count in self.n_top_joint.items() if count >= self.thresholds["hub_min_factors"]}
        self.meta.update({"n_top_rows": n, "n_hub_gene_sets": len(self.hub),
                          "n_gene_sets_in_any_joint_top": len(self.n_top_joint)})
        LOG.info("top rows: %d over %d factors; %d hub gene sets", n, len(self.top_rows), len(self.hub))

    # --- factor loadings, overlaps and audit ------------------------------------------------------------------
    def load_factors(self, conn):
        universe_size = len(self.universe)
        n_factors_by_gene = Counter()
        max_loading_by_gene = {}
        max_diff, n_checked, worst = 0.0, 0, None
        position = -1
        with open_text(self.args.all_factors_file) as fh:
            fh.readline()
            for position, line in enumerate(fh):
                fields = line.rstrip("\n").split("\t")
                check(position < len(self.factors), "%s has more rows than the factor index" % self.args.all_factors_file)
                f = self.factors[position]
                check(fields[0] == f["factor_id"], "Row %d of %s is %s; the factor index says %s"
                      % (position + 1, self.args.all_factors_file, fields[0], f["factor_id"]))
                check(len(fields) == universe_size + 1, "Row %s has %d values for %d genes"
                      % (fields[0], len(fields) - 1, universe_size))
                loadings = {}
                for gi, value in enumerate(fields[1:], 1):
                    if value in ("0", "0.0"):
                        continue
                    w = _float(value, fields[0])
                    if w != 0.0:
                        loadings[gi] = w
                l1 = sum(loadings.values())
                l2sq = sum(w * w for w in loadings.values())
                check(len(loadings) == int(f["n_nonzero_loadings"]), "Factor %s has %d nonzero loadings, the index says %s"
                      % (f["factor_id"], len(loadings), f["n_nonzero_loadings"]))
                ranked = sorted(loadings.items(), key=lambda kv: (-kv[1], self.gene_names[kv[0]]))
                conn.executemany("INSERT INTO factor_genes VALUES (?,?,?,?)",
                                 [(f["factor_idx"], rank, gi, w) for rank, (gi, w) in enumerate(ranked, 1)])
                for gi, w in loadings.items():
                    n_factors_by_gene[gi] += 1
                    if w > max_loading_by_gene.get(gi, -math.inf):
                        max_loading_by_gene[gi] = w

                rows_out = []
                for gs, joint, marginal, jr, mr, is_top in self.top_rows[f["factor_idx"]]:
                    hits = [(loadings[gi], gi) for gi in self.members[gs] if gi in loadings]
                    mass = sum(w for w, _ in hits)
                    recomputed = min(1.0, max(0.0, mass / l2sq)) if l2sq > 0 else 0.0
                    diff = abs(recomputed - marginal)
                    n_checked += 1
                    if diff > max_diff:
                        max_diff, worst = diff, (f["factor_id"], self.gs_rows[gs - 1]["gene_set_id"], marginal, recomputed)
                    set_size = self.n_universe[gs]
                    expected = set_size * len(loadings) / universe_size
                    hits.sort(key=lambda h: (-h[0], self.gene_names[h[1]]))
                    rows_out.append((f["factor_idx"], gs, joint, marginal, jr, mr, is_top, len(hits),
                                     mass / l1 if l1 > 0 else None,
                                     len(hits) / expected if expected > 0 else None,
                                     hypergeom_neg_log10_sf(universe_size, len(loadings), set_size, len(hits)),
                                     recomputed, " ".join(self.gene_names[gi] for _, gi in hits[:TOP_OVERLAP_GENES])))
                conn.executemany("INSERT INTO factor_gene_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_out)
                self._audit_factor(conn, f, loadings, l1, l2sq, rows_out)
        check(position + 1 == len(self.factors), "%s has %d rows, the factor index %d"
              % (self.args.all_factors_file, position + 1, len(self.factors)))
        check(max_diff <= LOADING_ROUNDING_TOL,
              "Recomputed marginal loading differs from eaggl's by %.3g (> %.1g) for %s: the loadings in %s are not the "
              "ones that were projected" % (max_diff, LOADING_ROUNDING_TOL, worst, self.args.all_factors_file))
        self.n_factors_by_gene = n_factors_by_gene
        self.max_loading_by_gene = max_loading_by_gene
        self.meta.update({"marginal_check_rows": n_checked, "marginal_check_max_absdiff": max_diff,
                          "marginal_check_tolerance": LOADING_ROUNDING_TOL,
                          "n_factor_gene_loadings": sum(n_factors_by_gene.values())})
        LOG.info("factor loadings: %d nonzero; marginal recomputed for %d rows (max |diff| %.3g)",
                 sum(n_factors_by_gene.values()), n_checked, max_diff)

    def _audit_factor(self, conn, f, loadings, l1, l2sq, rows):
        top_n = self.top_n
        joint_top = [r for r in rows if r[4] <= top_n]
        marginal_top = {r[1] for r in rows if r[5] <= top_n}
        best_joint = min(rows, key=lambda r: r[4])
        best_marginal = min(rows, key=lambda r: r[5])
        libraries = Counter(self.gs_library[r[1]] for r in joint_top)
        dominant, dominant_n = sorted(libraries.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        folds = [r[9] for r in joint_top if r[9] is not None]
        audit = {
            "n_nonzero": len(loadings),
            "top_joint_loading": best_joint[2],
            "jm_top_overlap": sum(1 for r in joint_top if r[1] in marginal_top) / len(joint_top),
            "own_top_frac": sum(r[6] for r in joint_top) / len(joint_top),
            "hub_frac": sum(1 for r in joint_top if r[1] in self.hub) / len(joint_top),
            "sig_overlap_frac": sum(1 for r in joint_top if r[10] >= SIG_NEG_LOG10_P) / len(joint_top),
            "median_fold_enrichment": statistics.median(folds) if folds else None,
        }
        flags = factor_flags(audit, self.thresholds)
        meta = f["metadata"]
        conn.execute(
            "INSERT INTO factors VALUES (%s)" % ",".join("?" * 29),
            (f["factor_idx"], f["factor_id"], f["trait"], f["factor"], int(f["factor_number"]), f["factor_label"],
             f["loading_variant"], len(loadings), l1, math.sqrt(l2sq), _optional_float(meta.get("gene_set_score")),
             _optional_float(meta.get("gene_score")), meta.get("top_genes"), meta.get("top_gene_sets"),
             json.dumps(meta, sort_keys=True), len(rows), best_joint[1], best_joint[2], best_marginal[1],
             best_marginal[3], audit["jm_top_overlap"], audit["own_top_frac"], audit["hub_frac"],
             audit["sig_overlap_frac"], audit["median_fold_enrichment"], dominant,
             dominant_n / len(joint_top), json.dumps(dict(sorted(libraries.items()))), ",".join(flags)))
        self.library_dominant[dominant] += 1
        for library in libraries:
            self.library_factors[library] += 1
        for library, count in libraries.items():
            self.library_slots[library] += count
        for flag in flags:
            self.flag_counts[flag] += 1
        if flags:
            self.flagged_by_trait[f["trait"]] += 1

    # --- sibling loadings from the per-trait long files ------------------------------------------------------
    def load_siblings(self, conn):
        if not self.args.long_file:
            self.meta["has_sibling_loadings"] = 0
            return
        needed_by_trait = defaultdict(set)
        factors_by_trait = defaultdict(set)
        for f in self.factors:
            factors_by_trait[f["trait"]].add(f["factor_id"])
            for row in self.top_rows[f["factor_idx"]]:
                needed_by_trait[f["trait"]].add(self.gs_rows[row[0] - 1]["gene_set_id"])
        top_lookup = {(factor_idx, row[0]): row for factor_idx, rows in self.top_rows.items() for row in rows}
        done = set()
        col = {c: i for i, c in enumerate(LONG_COLUMNS)}
        total = 0
        for path in sorted(self.args.long_file):
            with open_text(path) as fh:
                header = fh.readline().rstrip("\n").split("\t")
                check(header == LONG_COLUMNS, "Unexpected columns in %s" % path)
                trait, needed, rows = None, None, []
                for line in fh:
                    fields = line.split("\t", 6)
                    if trait is None:
                        trait = fields[col["trait"]]
                        check(trait in self.traits and trait not in done, "Long file %s: unknown or repeated trait %s"
                              % (path, trait))
                        needed = needed_by_trait[trait]
                    if fields[col["gene_set_id"]] not in needed:
                        continue
                    full = line.rstrip("\n").split("\t")
                    check(full[col["trait"]] == trait, "%s mixes traits" % path)
                    factor = self.factor_by_id.get(full[col["factor_id"]])
                    check(factor is not None and factor["trait"] == trait, "%s: unknown factor %s"
                          % (path, full[col["factor_id"]]))
                    rows.append((self.gs_idx[full[col["gene_set_id"]]], factor["factor_idx"],
                                 _float(full[col["joint_loading"]], path), _float(full[col["marginal_loading"]], path),
                                 int(full[col["joint_rank_in_factor"]]), int(full[col["marginal_rank_in_factor"]]),
                                 int(full[col["is_joint_top_factor"]])))
            check(trait is not None, "Empty long file %s" % path)
            expected = len(needed) * len(factors_by_trait[trait])
            check(len(rows) == expected, "%s: %d sibling rows, expected %d (%d gene sets x %d factors)"
                  % (path, len(rows), expected, len(needed), len(factors_by_trait[trait])))
            for gs, factor_idx, joint, marginal, jr, mr, is_top in rows:
                top = top_lookup.get((factor_idx, gs))
                check(top is None or top[1:] == (joint, marginal, jr, mr, is_top),
                      "%s disagrees with the top file for factor %d, gene set %d" % (path, factor_idx, gs))
            conn.executemany("INSERT INTO sibling_loadings VALUES (?,?,?,?,?,?,?)", rows)
            done.add(trait)
            total += len(rows)
            if len(done) % 50 == 0:
                LOG.info("sibling loadings: %d / %d traits", len(done), len(self.traits))
        check(done == set(self.traits), "Long files cover %d of %d traits (missing: %s)"
              % (len(done), len(self.traits), sorted(set(self.traits) - done)[:10]))
        self.meta.update({"has_sibling_loadings": 1, "n_sibling_rows": total})
        LOG.info("sibling loadings: %d rows over %d traits", total, len(done))

    # --- finish -----------------------------------------------------------------------------------------------
    def finish(self, conn):
        conn.executemany(
            "INSERT INTO genes VALUES (?,?,?,?,?,?)",
            [(gi, self.gene_names[gi], self.gene_in_universe[gi],
              " ".join(sorted(self.cfde_symbols[gi])) or None, self.n_factors_by_gene.get(gi, 0),
              self.max_loading_by_gene.get(gi)) for gi in range(1, len(self.gene_names))])
        conn.executemany(
            "INSERT INTO gene_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i, r["gene_set_id"], r["gene_set_name"], r["collection_id"], r["cfde_label"], r["library"], r["partition"],
              r["model"], r["comparison"], r["program"], int(r["n_genes"]), self.n_universe[i], self.n_top_joint.get(i, 0),
              self.n_top_any.get(i, 0), self.n_top_joint_traits.get(i, 0)) for i, r in enumerate(self.gs_rows, 1)])
        # Members (7.7M symbols, GMT order, EAGGL case) sit in their own table so name searches stay small.
        conn.executemany("INSERT INTO gene_set_members VALUES (?,?)",
                         [(i, " ".join(self.gene_names[gi] for gi in self.members[i])) for i in range(1, len(self.gs_rows) + 1)])
        conn.executemany("UPDATE traits SET n_flagged_factors = ? WHERE trait = ?",
                         [(n, trait) for trait, n in self.flagged_by_trait.items()])

        library_sets = Counter(r["library"] for r in self.gs_rows)
        library_collections = defaultdict(set)
        for r in self.gs_rows:
            library_collections[r["library"]].add(r["collection_id"])
        total_sets, total_slots = len(self.gs_rows), sum(self.library_slots.values())
        for library in sorted(library_sets):
            set_share = library_sets[library] / total_sets
            slot_share = self.library_slots.get(library, 0) / total_slots
            conn.execute("INSERT INTO libraries VALUES (?,?,?,?,?,?,?,?,?)",
                         (library, len(library_collections[library]), library_sets[library], set_share,
                          self.library_slots.get(library, 0), slot_share, slot_share / set_share,
                          self.library_factors.get(library, 0), self.library_dominant.get(library, 0)))

        self.meta.update({
            "schema_version": SCHEMA_VERSION, "project": self.args.project, "top_n": self.top_n,
            "thresholds": self.thresholds, "flags": render_flag_text(self.thresholds, self.top_n),
            "flag_counts": dict(sorted(self.flag_counts.items())),
            "n_flagged_factors": sum(self.flagged_by_trait.values()), "sig_neg_log10_p": SIG_NEG_LOG10_P,
            "n_genes": len(self.gene_names) - 1,
            "inputs": {key: os.path.basename(getattr(self.args, key)) for key in INPUT_KEYS},
            "n_long_files": len(self.args.long_file or []),
        })
        conn.executemany("INSERT INTO meta VALUES (?,?)",
                         [(key, json.dumps(value, sort_keys=True)) for key, value in sorted(self.meta.items())])
        conn.executescript(INDEXES)
        conn.execute("ANALYZE")

    def run(self):
        output = self.args.output_db
        tmp = output + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            # Index sorts stay in memory (a few hundred MB): SQLite's temp files would otherwise land in /tmp,
            # which is small on the submit host and on UGER nodes. Rows are only appended, so no VACUUM is needed.
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-262144")
            conn.executescript(SCHEMA)
            self.library_dominant, self.library_factors, self.library_slots = Counter(), Counter(), Counter()
            self.flag_counts, self.flagged_by_trait = Counter(), Counter()
            self.load_traits(conn)
            self.load_factor_index(conn)
            self.load_gene_sets(conn)
            self.load_top_rows()
            self.load_factors(conn)
            self.load_siblings(conn)
            self.finish(conn)
            conn.commit()
        except BaseException:
            conn.close()
            os.remove(tmp)
            raise
        conn.close()
        os.replace(tmp, output)
        LOG.info("wrote %s (%.1f MB): %d flagged factors %s", output, os.path.getsize(output) / 1e6,
                 self.meta["n_flagged_factors"], self.meta["flag_counts"])


INPUT_KEYS = ["trait_kpn_map_file", "kpn_trait_flat_file", "projection_manifest_file", "factor_index_file",
              "factor_metadata_file", "all_factors_file", "cfde_index_file", "gene_set_index_file",
              "annotations_gmt_file", "gene_map_file", "top_gene_sets_file"]


def add_arguments(p):
    p.add_argument("--project", required=True, help="LAP project instance name (shown in the UI)")
    p.add_argument("--trait-kpn-map-file", required=True)
    p.add_argument("--kpn-trait-flat-file", required=True, help="KPN ontology mappings (kpn_trait_flat.tsv)")
    p.add_argument("--projection-manifest-file", required=True)
    p.add_argument("--factor-index-file", required=True)
    p.add_argument("--factor-metadata-file", required=True, help="EAGGL factor_metadata.tsv")
    p.add_argument("--all-factors-file", required=True, help="The factors-by-genes table eaggl projected")
    p.add_argument("--cfde-index-file", required=True)
    p.add_argument("--gene-set-index-file", required=True)
    p.add_argument("--annotations-gmt-file", required=True)
    p.add_argument("--gene-map-file", required=True, help="Case-only CFDE -> EAGGL gene map (--gene-map-in)")
    p.add_argument("--top-gene-sets-file", required=True)
    p.add_argument("--long-file", action="append", default=[],
                   help="Per-trait long projection (repeat once per trait) for the sibling-factor loadings")
    p.add_argument("--top-n", type=int, default=50, help="Top-N used by annotate-projection")
    for key, value in DEFAULT_THRESHOLDS.items():
        p.add_argument("--threshold-" + key.replace("_", "-"), dest="threshold_" + key,
                       type=type(value), default=None, help="Audit threshold (default %s)" % value)
    p.add_argument("--output-db", required=True)


def build(args):
    Builder(args).run()
