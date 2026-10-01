"""Read-only queries behind the portal's JSON API. Every function takes a connection from open_database()."""

import json
import sqlite3
from collections import Counter
from pathlib import Path

from . import SCHEMA_VERSION

FACTOR_AUDIT_COLUMNS = (
    "f.factor_id, f.trait, f.factor, f.factor_number, f.label, f.n_nonzero, f.loading_l1, f.loading_l2, "
    "f.n_top_rows, f.top_joint_loading, f.top_marginal_loading, f.jm_top_overlap, f.own_top_frac, f.hub_frac, "
    "f.sig_overlap_frac, f.median_fold_enrichment, f.dominant_library, f.dominant_library_frac, f.flags, "
    "tj.gene_set_id AS top_joint_gene_set_id, tj.gene_set_name AS top_joint_gene_set, "
    "tm.gene_set_id AS top_marginal_gene_set_id, tm.gene_set_name AS top_marginal_gene_set, "
    "t.kpn_trait_id, t.phenotype_name, t.trait_group")
FACTOR_AUDIT_FROM = ("factors f JOIN traits t ON t.trait = f.trait "
                     "LEFT JOIN gene_sets tj ON tj.gs_idx = f.top_joint_gs "
                     "LEFT JOIN gene_sets tm ON tm.gs_idx = f.top_marginal_gs")
GENE_SET_COLUMNS = ("gs.gene_set_id, gs.gene_set_name, gs.collection_id, gs.cfde_label, gs.library, gs.n_genes, "
                    "gs.n_universe, gs.n_top_joint_factors, gs.n_top_any_factors, gs.n_top_joint_traits")
CHUNK = 500


class NotFound(Exception):
    pass


def open_database(path):
    """Read-only, lock-free connection (the file is never written while served; safe on NFS)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("database not found: %s" % path)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    version = read_meta(conn).get("schema_version")
    if version != SCHEMA_VERSION:
        raise RuntimeError("%s has portal schema %s; this code reads %s (rebuild it)" % (path, version, SCHEMA_VERSION))
    return conn


def _rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def _one(conn, sql, params, what):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NotFound(what)
    return dict(row)


def _like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def read_meta(conn):
    return {r[0]: json.loads(r[1]) for r in conn.execute("SELECT key, value FROM meta")}


def _factor_idx(conn, factor_id):
    return _one(conn, "SELECT factor_idx, trait, loading_l1, loading_l2 FROM factors WHERE factor_id = ?",
                (factor_id,), "unknown factor %r" % factor_id)


def _gs_idx(conn, gene_set_id):
    return _one(conn, "SELECT gs.gs_idx, m.members FROM gene_sets gs JOIN gene_set_members m ON m.gs_idx = gs.gs_idx "
                      "WHERE gs.gene_set_id = ?", (gene_set_id,),
                "unknown gene set %r" % gene_set_id)


# --- overview ------------------------------------------------------------------------------------------------


def summary(conn):
    meta = read_meta(conn)
    libraries = _rows(conn.execute("SELECT * FROM libraries ORDER BY n_gene_sets DESC"))
    groups = _rows(conn.execute(
        "SELECT trait_group, COUNT(*) AS n_traits, SUM(n_factors) AS n_factors, SUM(n_flagged_factors) AS n_flagged "
        "FROM traits GROUP BY trait_group ORDER BY n_traits DESC"))
    return {"meta": meta, "libraries": libraries, "trait_groups": groups}


def hubs(conn, limit=100):
    return _rows(conn.execute(
        "SELECT %s FROM gene_sets gs WHERE gs.n_top_joint_factors > 0 "
        "ORDER BY gs.n_top_joint_factors DESC, gs.gene_set_name LIMIT ?" % GENE_SET_COLUMNS, (limit,)))


def search(conn, query, limit=8):
    q = (query or "").strip()
    if not q:
        return {"query": q, "traits": [], "factors": [], "gene_sets": [], "genes": []}
    like, prefix = _like(q), _like(q)[1:]

    def rank(*cols):
        return "MIN(%s)" % ", ".join(
            "CASE WHEN %s = :q COLLATE NOCASE THEN 0 WHEN %s LIKE :prefix ESCAPE '\\' THEN 1 ELSE 2 END" % (c, c)
            for c in cols) if len(cols) > 1 else (
            "CASE WHEN %s = :q COLLATE NOCASE THEN 0 WHEN %s LIKE :prefix ESCAPE '\\' THEN 1 ELSE 2 END" % (cols[0], cols[0]))

    params = {"q": q, "like": like, "prefix": prefix, "limit": limit}
    traits = _rows(conn.execute(
        "SELECT trait, kpn_trait_id, phenotype_name, trait_group, n_factors FROM traits "
        "WHERE trait LIKE :like ESCAPE '\\' OR kpn_trait_id LIKE :like ESCAPE '\\' OR phenotype_name LIKE :like ESCAPE '\\' "
        "ORDER BY %s, length(phenotype_name), trait LIMIT :limit" % rank("trait", "kpn_trait_id", "phenotype_name"), params))
    factors = _rows(conn.execute(
        "SELECT f.factor_id, f.trait, f.factor, f.label, f.flags, t.phenotype_name FROM factors f "
        "JOIN traits t ON t.trait = f.trait "
        "WHERE f.label LIKE :like ESCAPE '\\' OR f.factor_id LIKE :like ESCAPE '\\' "
        "ORDER BY %s, length(f.label), f.factor_id LIMIT :limit" % rank("f.label", "f.factor_id"), params))
    gene_sets = _rows(conn.execute(
        "SELECT gene_set_id, gene_set_name, library, n_universe, n_top_joint_factors FROM gene_sets "
        "WHERE gene_set_name LIKE :like ESCAPE '\\' OR gene_set_id = :q "
        "ORDER BY %s, n_top_joint_factors DESC, gene_set_name LIMIT :limit" % rank("gene_set_name", "gene_set_id"), params))
    genes = _rows(conn.execute(
        "SELECT gene, in_universe, n_factors, max_loading FROM genes WHERE gene LIKE :prefix ESCAPE '\\' "
        "ORDER BY %s, n_factors DESC, gene LIMIT :limit" % rank("gene"), params))
    return {"query": q, "traits": traits, "factors": factors, "gene_sets": gene_sets, "genes": genes}


# --- traits --------------------------------------------------------------------------------------------------


def list_traits(conn):
    return _rows(conn.execute(
        "SELECT trait, kpn_trait_id, phenotype_name, trait_group, legacy_trait_group, trait_type, gwas_source_category, "
        "n_factors, n_flagged_factors, qc_pass FROM traits ORDER BY phenotype_name COLLATE NOCASE, trait"))


def trait_detail(conn, trait):
    row = _one(conn, "SELECT * FROM traits WHERE trait = ?", (trait,), "unknown trait %r" % trait)
    row["qc"] = json.loads(row.pop("qc_json"))
    mappings = _rows(conn.execute(
        "SELECT target_id, target_label, target_ontology, predicate, confidence, justification, source "
        "FROM trait_mappings WHERE kpn_trait_id = ? ORDER BY target_ontology, target_id", (row["kpn_trait_id"],)))
    factors = list_factors(conn, trait=trait)
    for f in factors:
        f["top_genes"] = [r[0] for r in conn.execute(
            "SELECT g.gene FROM factor_genes fg JOIN genes g ON g.gene_idx = fg.gene_idx "
            "WHERE fg.factor_idx = (SELECT factor_idx FROM factors WHERE factor_id = ?) AND fg.rank <= 8 "
            "ORDER BY fg.rank", (f["factor_id"],))]
    return {"trait": row, "mappings": mappings, "factors": factors}


def trait_matrix(conn, trait, per_factor=5):
    """The trait's factors x the union of each factor's top joint gene sets (joint and marginal per cell)."""
    meta = read_meta(conn)
    factors = _rows(conn.execute(
        "SELECT factor_idx, factor_id, factor, label FROM factors WHERE trait = ? ORDER BY factor_number", (trait,)))
    if not factors:
        raise NotFound("unknown trait %r" % trait)
    order, seen = [], set()
    for f in factors:
        for r in conn.execute("SELECT gs_idx FROM factor_gene_sets WHERE factor_idx = ? AND joint_rank <= ? "
                              "ORDER BY joint_rank", (f["factor_idx"], per_factor)):
            if r[0] not in seen:
                seen.add(r[0])
                order.append((r[0], f["factor_id"]))
    table = "sibling_loadings" if meta.get("has_sibling_loadings") else "factor_gene_sets"
    rows = []
    for gs, source in order:
        info = dict(conn.execute("SELECT gene_set_id, gene_set_name, library, n_universe, n_top_joint_factors "
                                 "FROM gene_sets WHERE gs_idx = ?", (gs,)).fetchone())
        cells = {r[0]: {"joint": r[1], "marginal": r[2], "joint_rank": r[3], "marginal_rank": r[4]}
                 for r in conn.execute("SELECT factor_idx, joint, marginal, joint_rank, marginal_rank FROM %s "
                                       "WHERE gs_idx = ? AND factor_idx IN (%s)"
                                       % (table, ",".join(str(f["factor_idx"]) for f in factors)), (gs,))}
        info["selected_by"] = source
        info["cells"] = [cells.get(f["factor_idx"]) for f in factors]
        rows.append(info)
    for f in factors:
        del f["factor_idx"]
    return {"trait": trait, "per_factor": per_factor, "complete": table == "sibling_loadings",
            "factors": factors, "rows": rows}


# --- factors -------------------------------------------------------------------------------------------------


def list_factors(conn, trait=None, flag=None):
    where, params = [], []
    if trait:
        where.append("f.trait = ?")
        params.append(trait)
    if flag:
        where.append("(',' || f.flags || ',') LIKE ?")
        params.append("%," + flag + ",%")
    sql = "SELECT %s FROM %s" % (FACTOR_AUDIT_COLUMNS, FACTOR_AUDIT_FROM)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.trait, f.factor_number"
    return _rows(conn.execute(sql, params))


def factor_detail(conn, factor_id):
    rows = _rows(conn.execute("SELECT %s, f.loading_variant, f.gene_set_score, f.gene_score, f.eaggl_top_genes, "
                              "f.eaggl_top_gene_sets, f.metadata_json, f.library_counts_json FROM %s WHERE f.factor_id = ?"
                              % (FACTOR_AUDIT_COLUMNS, FACTOR_AUDIT_FROM), (factor_id,)))
    if not rows:
        raise NotFound("unknown factor %r" % factor_id)
    factor = rows[0]
    factor["metadata"] = json.loads(factor.pop("metadata_json"))
    factor["library_counts"] = json.loads(factor.pop("library_counts_json"))
    siblings = _rows(conn.execute(
        "SELECT factor_id, factor, factor_number, label, n_nonzero, flags FROM factors WHERE trait = ? "
        "ORDER BY factor_number", (factor["trait"],)))
    trait = dict(conn.execute("SELECT trait, kpn_trait_id, phenotype_name, trait_group, trait_type, n_factors, qc_pass "
                              "FROM traits WHERE trait = ?", (factor["trait"],)).fetchone())
    return {"factor": factor, "trait": trait, "siblings": siblings}


def _joint_top_members(conn, factor_idx, top_n):
    counts = Counter()
    for (members,) in conn.execute(
            "SELECT m.members FROM factor_gene_sets x JOIN gene_set_members m ON m.gs_idx = x.gs_idx "
            "WHERE x.factor_idx = ? AND x.joint_rank <= ?", (factor_idx, top_n)):
        counts.update(members.split())
    return counts


def factor_genes(conn, factor_id):
    """Every nonzero gene loading of the factor, ranked, with each gene's share of the loading mass, its
    contribution unit to marginal loadings (w / w'w) and how many of the factor's joint top-N sets contain it."""
    f = _factor_idx(conn, factor_id)
    top_n = read_meta(conn)["top_n"]
    in_top = _joint_top_members(conn, f["factor_idx"], top_n)
    l2sq = f["loading_l2"] ** 2
    rows = _rows(conn.execute(
        "SELECT fg.rank, g.gene, fg.loading, g.n_factors FROM factor_genes fg JOIN genes g ON g.gene_idx = fg.gene_idx "
        "WHERE fg.factor_idx = ? ORDER BY fg.rank", (f["factor_idx"],)))
    for r in rows:
        r["mass_share"] = r["loading"] / f["loading_l1"] if f["loading_l1"] else None
        r["marginal_unit"] = r["loading"] / l2sq if l2sq else None
        r["n_top_sets"] = in_top.get(r["gene"], 0)
    return {"factor_id": factor_id, "top_n": top_n, "genes": rows}


def factor_gene_sets(conn, factor_id):
    f = _factor_idx(conn, factor_id)
    rows = _rows(conn.execute(
        "SELECT %s, x.joint, x.marginal, x.joint_rank, x.marginal_rank, x.is_joint_top_factor, x.n_overlap, "
        "x.overlap_mass_frac, x.fold_enrichment, x.neg_log10_p, x.marginal_recomputed, x.top_overlap_genes "
        "FROM factor_gene_sets x JOIN gene_sets gs ON gs.gs_idx = x.gs_idx WHERE x.factor_idx = ? "
        "ORDER BY x.joint_rank" % GENE_SET_COLUMNS, (f["factor_idx"],)))
    for r in rows:
        r["top_overlap_genes"] = r["top_overlap_genes"].split()
    return {"factor_id": factor_id, "gene_sets": rows}


def _gene_flags(conn, names):
    found = {}
    names = list(names)
    for i in range(0, len(names), CHUNK):
        chunk = names[i:i + CHUNK]
        for r in conn.execute("SELECT gene, in_universe, n_factors FROM genes WHERE gene IN (%s)"
                              % ",".join("?" * len(chunk)), chunk):
            found[r[0]] = (r[1], r[2])
    return found


def factor_gene_set(conn, factor_id, gene_set_id):
    """Why a gene set scores on a factor: each member gene's loading and its exact contribution w_g / w'w to
    the marginal loading, the members without a loading, and the gene set on the trait's other factors."""
    f = _factor_idx(conn, factor_id)
    gs = _gs_idx(conn, gene_set_id)
    meta = read_meta(conn)
    info = dict(conn.execute("SELECT %s, c.label AS collection_label, c.partition, c.model, c.comparison, c.program "
                             "FROM gene_sets gs JOIN collections c ON c.collection_id = gs.collection_id "
                             "WHERE gs.gs_idx = ?" % GENE_SET_COLUMNS, (gs["gs_idx"],)).fetchone())
    row = conn.execute("SELECT joint, marginal, joint_rank, marginal_rank, is_joint_top_factor, n_overlap, "
                       "overlap_mass_frac, fold_enrichment, neg_log10_p, marginal_recomputed FROM factor_gene_sets "
                       "WHERE factor_idx = ? AND gs_idx = ?", (f["factor_idx"], gs["gs_idx"])).fetchone()
    loaded = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT g.gene, fg.loading, fg.rank FROM factor_genes fg JOIN genes g ON g.gene_idx = fg.gene_idx "
        "WHERE fg.factor_idx = ?", (f["factor_idx"],))}
    members = gs["members"].split()
    l2sq = f["loading_l2"] ** 2
    contributions, unloaded, outside = [], [], []
    flags = _gene_flags(conn, [m for m in members if m not in loaded])
    for gene in members:
        if gene in loaded:
            w, rank = loaded[gene]
            contributions.append({"gene": gene, "loading": w, "rank": rank, "contribution": w / l2sq if l2sq else None})
        elif flags.get(gene, (0, 0))[0]:
            unloaded.append({"gene": gene, "n_factors": flags[gene][1]})
        else:
            outside.append(gene)
    contributions.sort(key=lambda c: (-c["loading"], c["gene"]))
    total = 0.0
    for c in contributions:
        total += c["contribution"] or 0.0
        c["cumulative"] = total
    unloaded.sort(key=lambda u: (-u["n_factors"], u["gene"]))

    siblings = _rows(conn.execute(
        "SELECT f.factor_id, f.factor, f.factor_number, f.label, s.joint, s.marginal, s.joint_rank, s.marginal_rank, "
        "s.is_joint_top_factor FROM factors f LEFT JOIN %s s ON s.factor_idx = f.factor_idx AND s.gs_idx = ? "
        "WHERE f.trait = ? ORDER BY f.factor_number"
        % ("sibling_loadings" if meta.get("has_sibling_loadings") else "factor_gene_sets"),
        (gs["gs_idx"], f["trait"])))
    member_set = set(members)
    for s in siblings:  # the marginal loading is exact from the loadings, so it is always filled in
        sib = _factor_idx(conn, s["factor_id"])
        mass = sum(r[1] for r in conn.execute(
            "SELECT g.gene, fg.loading FROM factor_genes fg JOIN genes g ON g.gene_idx = fg.gene_idx "
            "WHERE fg.factor_idx = ?", (sib["factor_idx"],)) if r[0] in member_set)
        sq = sib["loading_l2"] ** 2
        s["marginal_recomputed"] = min(1.0, max(0.0, mass / sq)) if sq else 0.0
    return {"factor_id": factor_id, "gene_set": info, "projection": dict(row) if row else None,
            "loading_l2_squared": l2sq, "contributions": contributions, "unloaded": unloaded, "outside": outside,
            "siblings": siblings, "siblings_complete": bool(meta.get("has_sibling_loadings"))}


# --- gene sets and genes -------------------------------------------------------------------------------------


def gene_set_detail(conn, gene_set_id):
    gs = _gs_idx(conn, gene_set_id)
    info = dict(conn.execute("SELECT %s, c.label AS collection_label, c.partition, c.model, c.comparison, c.program, "
                             "c.s3_key FROM gene_sets gs JOIN collections c ON c.collection_id = gs.collection_id "
                             "WHERE gs.gs_idx = ?" % GENE_SET_COLUMNS, (gs["gs_idx"],)).fetchone())
    members = gs["members"].split()
    flags = _gene_flags(conn, members)
    factors = _rows(conn.execute(
        "SELECT f.factor_id, f.trait, f.factor, f.label, t.phenotype_name, x.joint, x.marginal, x.joint_rank, "
        "x.marginal_rank, x.is_joint_top_factor, x.n_overlap, x.fold_enrichment, x.neg_log10_p "
        "FROM factor_gene_sets x JOIN factors f ON f.factor_idx = x.factor_idx JOIN traits t ON t.trait = f.trait "
        "WHERE x.gs_idx = ? ORDER BY x.joint DESC, f.factor_id", (gs["gs_idx"],)))
    return {"gene_set": info,
            "members": [{"gene": m, "in_universe": flags.get(m, (0, 0))[0], "n_factors": flags.get(m, (0, 0))[1]}
                        for m in members],
            "factors": factors, "top_n": read_meta(conn)["top_n"]}


def gene_detail(conn, gene):
    info = _one(conn, "SELECT gene_idx, gene, in_universe, cfde_symbols, n_factors, max_loading FROM genes "
                      "WHERE gene = ?", (gene,), "unknown gene %r" % gene)
    factors = _rows(conn.execute(
        "SELECT f.factor_id, f.trait, f.factor, f.label, f.n_nonzero, t.phenotype_name, fg.loading, fg.rank "
        "FROM factor_genes fg JOIN factors f ON f.factor_idx = fg.factor_idx JOIN traits t ON t.trait = f.trait "
        "WHERE fg.gene_idx = ? ORDER BY fg.loading DESC, f.factor_id", (info.pop("gene_idx"),)))
    return {"gene": info, "factors": factors}
