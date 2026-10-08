#!/usr/bin/env python3
"""Helper steps for the CFDE -> EAGGL supplied-factor projection LAP pipeline.

Every subcommand is a single LAP step (see ../config/cfde_projection.cfg). The helper only
prepares inputs for, and relabels outputs of, `python -m eaggl factor`; it never computes a
projection itself. Identifiers are carried verbatim end to end:

  * gene sets:   GMT column 1 (`dapper:GeneSet.*`) == eaggl `Gene_Set`
  * collections: `dapper:GeneSetCollection.*` from the CFDE index, attached by join
  * factors:     EAGGL `trait::FactorN`; eaggl names factors by position (Factor1..K), so the
                 row order of every factor file we write is recorded in a factor index
  * traits:      EAGGL trait == KPN `legacy_phenotype_id` -> `KPN.TRAIT:*`

Any violated invariant raises WorkflowError, which exits non-zero so LAP marks the step failed.
Standard library only; runs on Python 3.9.
"""

import argparse
from array import array
import csv
import gzip
import hashlib
import io
import math
import os
import re
import subprocess
import sys
from collections import Counter, OrderedDict, defaultdict

GENE_SET_ID_RE = re.compile(r"^dapper:GeneSet\.[A-Za-z0-9_-]{32}$")
COLLECTION_ID_PREFIX = "dapper:GeneSetCollection."
KPN_TRAIT_ID_RE = re.compile(r"^KPN\.TRAIT:\d{7}$")
FACTOR_ID_SEP = "::"
FACTOR_COL_RE = re.compile(r"^Factor(\d+)$")
NA = "NA"
# eaggl writes loadings with %.4g, so two runs that agree numerically can still differ by one unit
# in the fourth significant digit (<= 1e-4 for values in [0, 1]).
LOADING_ROUNDING_TOL = 1.5e-4

csv.field_size_limit(sys.maxsize)


class WorkflowError(Exception):
    pass


def check(condition, message):
    if not condition:
        raise WorkflowError(message)


# -------------------------------------------------------------------------------------------------
# I/O helpers


class DeterministicGzipWriter(gzip.GzipFile):
    """gzip writer whose bytes depend only on the content (no mtime or file name in the header).

    LAP re-runs a command when an input's md5 changes, so an upstream step rebuilt with identical
    content must not re-trigger all 711 projections.
    """

    def __init__(self, path):
        self._raw = open(path, "wb")
        super().__init__(filename="", mode="wb", fileobj=self._raw, mtime=0)

    def close(self):
        try:
            super().close()
        finally:
            self._raw.close()


def open_text(path, mode="r"):
    if path.endswith(".gz"):
        if "w" in mode:
            return io.TextIOWrapper(DeterministicGzipWriter(path), encoding="utf-8", newline="")
        return gzip.open(path, mode + "t", encoding="utf-8", newline="")
    return open(path, mode, encoding="utf-8", newline="")


def read_tsv(path):
    with open_text(path) as fh:
        return list(csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE))


def tsv_line(values):
    """One TSV line, written verbatim: ids and labels are never quoted or escaped."""
    fields = [str(v) for v in values]
    check(not any(("\t" in f or "\n" in f) for f in fields), "Tab or newline inside a TSV value: %s" % fields)
    return "\t".join(fields) + "\n"


def write_tsv(path, columns, rows):
    with open_text(path, "w") as fh:
        fh.write(tsv_line(columns))
        for row in rows:
            fh.write(tsv_line(row[c] for c in columns))


def split_factor_id(factor_id):
    trait, sep, factor = factor_id.partition(FACTOR_ID_SEP)
    check(sep and trait and FACTOR_COL_RE.match(factor) and FACTOR_ID_SEP not in factor,
          "Malformed EAGGL factor id %r (expected <trait>::FactorN)" % factor_id)
    return trait, factor


def read_factor_ids(path):
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == ["factor_id"], "%s must have the single column factor_id" % path)
    ids = [r["factor_id"] for r in rows]
    duplicates = [f for f, n in Counter(ids).items() if n > 1]
    check(not duplicates, "Duplicate factor ids in %s: %s" % (path, duplicates[:5]))
    for factor_id in ids:
        split_factor_id(factor_id)
    return ids


def traits_in_order(factor_ids):
    """Distinct traits in first-appearance order, with their factor counts."""
    counts = OrderedDict()
    for factor_id in factor_ids:
        trait = split_factor_id(factor_id)[0]
        counts[trait] = counts.get(trait, 0) + 1
    return counts


def git_output(repo_dir, *args):
    return subprocess.run(["git", "-C", repo_dir] + list(args), check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True).stdout.strip()


def gmt_fields(line):
    """Tab fields of a GMT line: [name, description, gene, ...]."""
    return line.rstrip("\n").rstrip("\r").split("\t")


def eaggl_genes(line):
    """The genes eaggl reads from a GMT line (whitespace split, everything after column 1)."""
    return set(line.split()[1:])


def parse_loading(value, where):
    try:
        number = float(value)
    except ValueError:
        raise WorkflowError("Non-numeric loading %r in %s" % (value, where))
    check(math.isfinite(number) and 0.0 <= number <= 1.0, "Loading %r outside [0, 1] in %s" % (value, where))
    return number


# -------------------------------------------------------------------------------------------------
# KPN trait ids


def load_kpn_registry(path):
    rows = read_tsv(path)
    required = {"portal_id", "gwas_source_category", "legacy_phenotype_id", "phenotype_name",
                "legacy_trait_group", "trait_group", "trait_type"}
    check(rows and required <= set(rows[0].keys()), "%s is missing KPN registry columns %s"
          % (path, sorted(required - set(rows[0].keys() if rows else []))))
    return rows


def map_traits_to_kpn(traits, registry_rows):
    """Exact, unique legacy_phenotype_id match for every trait, else WorkflowError."""
    by_legacy = defaultdict(list)
    for row in registry_rows:
        by_legacy[row["legacy_phenotype_id"]].append(row)
    mapping, unmatched, ambiguous, bad_ids = OrderedDict(), [], [], []
    for trait in traits:
        hits = by_legacy.get(trait, [])
        if not hits:
            unmatched.append(trait)
        elif len(hits) > 1:
            ambiguous.append("%s -> %s" % (trait, ",".join(r["portal_id"] for r in hits)))
        elif not KPN_TRAIT_ID_RE.match(hits[0]["portal_id"]):
            bad_ids.append("%s -> %s" % (trait, hits[0]["portal_id"]))
        else:
            mapping[trait] = hits[0]
    problems = []
    if unmatched:
        problems.append("%d traits have no KPN legacy_phenotype_id match: %s" % (len(unmatched), unmatched[:20]))
    if ambiguous:
        problems.append("%d traits match several KPN rows: %s" % (len(ambiguous), ambiguous[:20]))
    if bad_ids:
        problems.append("%d traits map to malformed KPN ids: %s" % (len(bad_ids), bad_ids[:20]))
    check(not problems, "; ".join(problems))
    kpn_ids = [row["portal_id"] for row in mapping.values()]
    check(len(set(kpn_ids)) == len(kpn_ids), "Several traits map to the same KPN.TRAIT id")
    return mapping


TRAIT_KPN_COLUMNS = ["trait", "kpn_trait_id", "kpn_release", "kpn_release_commit", "gwas_source_category",
                     "phenotype_name", "trait_group", "legacy_trait_group", "trait_type", "n_factors"]


def cmd_trait_kpn_map(args):
    factor_ids = read_factor_ids(args.factor_ids_file)
    trait_counts = traits_in_order(factor_ids)
    mapping = map_traits_to_kpn(list(trait_counts), load_kpn_registry(args.registry_file))

    # Pin provenance: the registry bytes on disk must be the blob committed at the release tag
    # (hash-object reads the file itself, so git's stat cache or skip-worktree bits cannot hide an edit).
    registry_path = os.path.realpath(args.registry_file)
    repo_dir = git_output(os.path.dirname(registry_path), "rev-parse", "--show-toplevel")
    release_commit = git_output(repo_dir, "rev-list", "-n", "1", args.kpn_release)
    relative = os.path.relpath(registry_path, os.path.realpath(repo_dir))
    check(("/%s/" % args.kpn_release) in "/" + relative, "%s is not inside the %s release directory"
          % (relative, args.kpn_release))
    try:
        tag_blob = git_output(repo_dir, "rev-parse", "--verify", "%s^{commit}:%s" % (args.kpn_release, relative))
    except subprocess.CalledProcessError:
        raise WorkflowError("%s is not part of KPN release %s" % (relative, args.kpn_release))
    disk_blob = git_output(repo_dir, "hash-object", "--", relative)
    check(disk_blob == tag_blob, "%s differs from KPN release %s (%s != %s)"
          % (registry_path, args.kpn_release, disk_blob, tag_blob))

    rows = []
    for trait, n_factors in trait_counts.items():
        kpn = mapping[trait]
        rows.append({"trait": trait, "kpn_trait_id": kpn["portal_id"], "kpn_release": args.kpn_release,
                     "kpn_release_commit": release_commit, "gwas_source_category": kpn["gwas_source_category"],
                     "phenotype_name": kpn["phenotype_name"], "trait_group": kpn["trait_group"],
                     "legacy_trait_group": kpn["legacy_trait_group"], "trait_type": kpn["trait_type"],
                     "n_factors": n_factors})
    write_tsv(args.output_file, TRAIT_KPN_COLUMNS, rows)
    categories = Counter(r["gwas_source_category"] for r in rows)
    print("Mapped %d/%d traits to KPN.TRAIT ids (%s) at %s %s"
          % (len(rows), len(trait_counts), dict(categories), args.kpn_release, release_commit))


def read_trait_kpn_map(path):
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == TRAIT_KPN_COLUMNS, "Unexpected columns in %s" % path)
    return OrderedDict((r["trait"], r) for r in rows)


# -------------------------------------------------------------------------------------------------
# CFDE gene-set collections

COLLECTION_GENE_SET_COLUMNS = ["gene_set_id", "gene_set_name", "collection_id", "cfde_label", "library",
                               "gmt_row", "n_genes"]


def cmd_cfde_index(args):
    """The release's rows of the snapshot's CFDE index: the collections of --libraries, in snapshot order."""
    wanted = [library for library in args.libraries.split(",") if library]
    check(wanted and len(set(wanted)) == len(wanted), "--libraries must list distinct libraries: %r" % args.libraries)
    with open_text(args.index_file) as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        columns, rows = list(reader.fieldnames or []), list(reader)
    check("library" in columns and "label" in columns, "%s is not a CFDE index" % args.index_file)
    missing = sorted(set(wanted) - {row["library"] for row in rows})
    check(not missing, "Libraries %s are not in %s" % (missing, args.index_file))
    kept = [row for row in rows if row["library"] in set(wanted)]
    write_tsv(args.output_file, columns, kept)
    print("%d of %d collections kept from %d libraries" % (len(kept), len(rows), len(wanted)))


def cmd_collection_index(args):
    check(args.collection_id.startswith(COLLECTION_ID_PREFIX), "Malformed collection id %r" % args.collection_id)
    expected_yaml = "GeneSetCollection.%s.yaml" % args.collection_id[len(COLLECTION_ID_PREFIX):]
    actual_yaml = os.path.basename(os.path.realpath(args.collection_yaml_file))
    check(actual_yaml == expected_yaml, "Collection YAML %s does not match collection id %s (expected %s)"
          % (actual_yaml, args.collection_id, expected_yaml))

    with open_text(args.gmt_file) as id_fh, open_text(args.names_gmt_file) as name_fh:
        id_lines, name_lines = id_fh.readlines(), name_fh.readlines()
    check(len(id_lines) == len(name_lines), "%s has %d rows but %s has %d"
          % (args.gmt_file, len(id_lines), args.names_gmt_file, len(name_lines)))
    check(id_lines, "%s is empty" % args.gmt_file)

    rows, seen = [], set()
    for row_number, (id_line, name_line) in enumerate(zip(id_lines, name_lines), 1):
        id_fields, name_fields = gmt_fields(id_line), gmt_fields(name_line)
        gene_set_id = id_fields[0]
        where = "%s row %d" % (args.gmt_file, row_number)
        check(GENE_SET_ID_RE.match(gene_set_id), "Malformed gene-set id %r in %s" % (gene_set_id, where))
        check(gene_set_id not in seen, "Duplicate gene-set id %s in %s" % (gene_set_id, where))
        seen.add(gene_set_id)
        check(len(id_fields) >= 3 and id_fields[1] == "", "%s must have an empty description column" % where)
        check(id_fields[1:] == name_fields[1:], "Gene columns differ between the id and name GMTs at %s" % where)
        genes = eaggl_genes(id_line)
        check(genes, "%s has no genes" % where)
        rows.append({"gene_set_id": gene_set_id, "gene_set_name": name_fields[0], "collection_id": args.collection_id,
                     "cfde_label": args.label, "library": args.library, "gmt_row": row_number,
                     "n_genes": len(genes)})
    write_tsv(args.output_file, COLLECTION_GENE_SET_COLUMNS, rows)
    print("%s: %d gene sets in %s" % (args.label, len(rows), args.collection_id))


GENE_SET_INDEX_COLUMNS = ["gene_set_id", "gene_set_name", "collection_id", "cfde_label", "library", "partition",
                          "model", "comparison", "program", "gmt_row", "n_genes", "n_genes_in_eaggl_universe",
                          "cfde_snapshot"]


def read_gene_list(path):
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == ["gene"], "%s must have the single column gene" % path)
    genes = [r["gene"] for r in rows]
    check(len(set(genes)) == len(genes), "Duplicate genes in %s" % path)
    return genes


def cmd_build_annotations(args):
    """Concatenate the collections' id GMTs into eaggl's X input and index every gene set. Two streaming passes over
    the GMTs (case-only gene map, then index rows), so memory holds one row per gene set, never their genes."""
    index_rows = OrderedDict((r["label"], r) for r in read_tsv(args.cfde_index_file))
    universe = set(read_gene_list(args.eaggl_genes_file))

    gene_sets = {}  # gene_set_id -> (label, gene_set_name, collection_id, gmt_row, n_genes)
    for path in args.collection_gene_sets_file:
        for row in read_tsv(path):
            check(row["gene_set_id"] not in gene_sets, "Gene-set id %s appears in more than one collection"
                  % row["gene_set_id"])
            gene_sets[row["gene_set_id"]] = (row["cfde_label"], row["gene_set_name"], row["collection_id"], row["gmt_row"],
                                             row["n_genes"])
    labels = {value[0] for value in gene_sets.values()}
    check(labels == set(index_rows), "Collections present (%d) differ from the CFDE index (%d); missing: %s"
          % (len(labels), len(index_rows), sorted(set(index_rows) - labels)[:10]))

    # Assign each GMT to its collection by content, then emit collections in sorted label order.
    gmt_by_label = {}
    for path in args.gmt_file:
        with open_text(path) as fh:
            ids = [line.split(None, 1)[0] for line in fh if line.strip()]
        check(ids and ids[0] in gene_sets, "%s does not match any collection index" % path)
        label = gene_sets[ids[0]][0]
        check(label not in gmt_by_label, "Two GMTs for collection %s" % label)
        check(all(gene_sets.get(i, ("",))[0] == label for i in ids), "%s mixes gene sets from several collections" % path)
        check(len(ids) == int(index_rows[label]["n_sets"]), "%s has %d gene sets; the CFDE index says %s"
              % (label, len(ids), index_rows[label]["n_sets"]))
        gmt_by_label[label] = (path, len(ids))
    check(set(gmt_by_label) == set(index_rows), "Missing GMTs for %s" % sorted(set(index_rows) - set(gmt_by_label))[:10])
    check(sum(n for _, n in gmt_by_label.values()) == len(gene_sets), "GMT gene-set count differs from the collection indexes")

    def memberships():
        for label in sorted(gmt_by_label):
            with open_text(gmt_by_label[label][0]) as fh:
                for line in fh:
                    if line.strip():
                        yield label, line.split(None, 1)[0], eaggl_genes(line)

    # Pass 1, case-only gene map: a CFDE symbol absent from EAGGL whose upper-case form is an EAGGL gene.
    upper_to_eaggl = {}
    for gene in universe:
        upper_to_eaggl.setdefault(gene.upper(), set()).add(gene)
    case_map, outside = {}, set()
    for _, _, genes in memberships():
        for gene in genes:
            if gene not in universe and gene not in case_map and gene not in outside:
                targets = upper_to_eaggl.get(gene.upper(), set())
                if len(targets) == 1:
                    case_map[gene] = next(iter(targets))
                else:
                    outside.add(gene)

    # Pass 2, the gene-set index and the per-library overlap statistics.
    library_stats = defaultdict(lambda: {"collections": set(), "sets": 0, "genes": set(), "entries": 0,
                                         "entries_in_universe": 0, "universe_sizes": []})
    written = 0
    with open_text(args.output_gene_set_index_file, "w") as index_fh:
        index_fh.write(tsv_line(GENE_SET_INDEX_COLUMNS))
        for label, gene_set_id, genes in memberships():
            mapped = {case_map.get(g, g) for g in genes}
            in_universe = len(mapped & universe)
            check(in_universe >= 1, "%s (%s) shares no gene with the EAGGL factors and would be dropped by eaggl"
                  % (gene_set_id, label))
            _, name, collection_id, gmt_row, n_genes = gene_sets[gene_set_id]
            cfde = index_rows[label]
            check(collection_id == cfde["collection_id"], "Collection id for %s differs from the CFDE index" % label)
            index_fh.write(tsv_line([gene_set_id, name, collection_id, label, cfde["library"], cfde["partition"],
                                     cfde["model"], cfde["comparison"], cfde["program"], gmt_row, n_genes, in_universe,
                                     args.cfde_snapshot]))
            written += 1
            entries_in_universe = sum(1 for g in genes if case_map.get(g, g) in universe)
            for key in (cfde["library"], "ALL"):
                stats = library_stats[key]
                stats["collections"].add(label)
                stats["sets"] += 1
                stats["genes"] |= genes
                stats["entries"] += len(genes)
                stats["entries_in_universe"] += entries_in_universe
                stats["universe_sizes"].append(in_universe)

    with DeterministicGzipWriter(args.output_gmt_file) as out:
        for label in sorted(gmt_by_label):
            with open(gmt_by_label[label][0], "rb") as fh:
                data = fh.read()
            out.write(data if data.endswith(b"\n") else data + b"\n")
    with open(args.output_gene_map_file, "w") as fh:
        for gene in sorted(case_map):
            fh.write("%s\t%s\n" % (gene, case_map[gene]))

    report = []
    for key in sorted(library_stats, key=lambda k: (k == "ALL", k)):
        stats = library_stats[key]
        sizes = sorted(stats["universe_sizes"])
        report.append({"library": key, "n_collections": len(stats["collections"]), "n_gene_sets": stats["sets"],
                       "n_distinct_genes": len(stats["genes"]),
                       "n_genes_in_universe_exact": len(stats["genes"] & universe),
                       "n_genes_case_mapped": len([g for g in stats["genes"] if g in case_map]),
                       "n_genes_outside_universe": len([g for g in stats["genes"]
                                                        if case_map.get(g, g) not in universe]),
                       "membership_entries": stats["entries"],
                       "fraction_entries_in_universe": "%.4f" % (stats["entries_in_universe"] / stats["entries"]),
                       "min_set_genes_in_universe": sizes[0], "median_set_genes_in_universe": sizes[len(sizes) // 2]})
    write_tsv(args.output_overlap_report_file, list(report[0].keys()), report)
    print("Wrote %d gene sets from %d collections; %d case-only gene map entries"
          % (written, len(gmt_by_label), len(case_map)))


def read_gene_set_index(path):
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == GENE_SET_INDEX_COLUMNS, "Unexpected columns in %s" % path)
    return OrderedDict((r["gene_set_id"], r) for r in rows)


# -------------------------------------------------------------------------------------------------
# EAGGL factors

FACTOR_INDEX_COLUMNS = ["global_eaggl_column", "factor_id", "trait", "kpn_trait_id", "factor", "factor_number",
                        "factor_label", "n_nonzero_loadings", "loading_l2", "loading_variant"]
TRAIT_FACTOR_INDEX_COLUMNS = ["local_eaggl_column", "factor_id", "trait", "kpn_trait_id", "factor", "factor_number",
                              "factor_label", "global_eaggl_column"]


def cmd_assemble_factors(args):
    factor_ids = read_factor_ids(args.factor_ids_file)
    genes = read_gene_list(args.genes_file)
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    metadata = {r["factor_id"]: r for r in read_tsv(args.factor_metadata_file)}
    check(set(metadata) == set(factor_ids), "factor_metadata.tsv and factor_ids.tsv list different factors")

    uncapped = defaultdict(dict)  # factor_id -> gene -> original (pre-cap) loading string
    for row in read_tsv(args.capped_entries_file):
        check(float(row["original_loading"]) > 1.0 and float(row["capped_loading"]) == 1.0,
              "Unexpected capped entry %s" % row)
        uncapped[row["factor_id"]][row["gene"]] = row["original_loading"]
    gene_col = {gene: i for i, gene in enumerate(genes)}

    index_rows = []
    with open_text(args.loadings_file) as fh, open_text(args.output_file, "w") as out:
        header = fh.readline().rstrip("\n").split("\t")
        check(header[0] == "factor_id", "%s must start with factor_id" % args.loadings_file)
        check(header[1:] == genes, "Gene columns of %s differ from genes.tsv" % args.loadings_file)
        out.write("\t".join(["Factor"] + genes) + "\n")
        for row_number, line in enumerate(fh):
            fields = line.rstrip("\n").split("\t")
            check(row_number < len(factor_ids), "More loading rows than factor ids")
            factor_id = fields[0]
            check(factor_id == factor_ids[row_number], "Loading row %d is %s, expected %s"
                  % (row_number + 1, factor_id, factor_ids[row_number]))
            check(len(fields) == len(header), "Loading row for %s has %d fields" % (factor_id, len(fields)))
            values = fields[1:]
            if args.loading_variant == "uncapped":
                for gene, original in uncapped.get(factor_id, {}).items():
                    check(float(values[gene_col[gene]]) == 1.0, "Capped cell %s/%s is not 1" % (factor_id, gene))
                    values[gene_col[gene]] = original
            numbers = [float(v) for v in values]
            check(all(math.isfinite(x) and x >= 0.0 for x in numbers), "Invalid loading for %s" % factor_id)
            check(args.loading_variant == "uncapped" or max(numbers) <= 1.0, "Capped loading above 1 for %s" % factor_id)
            out.write("\t".join([factor_id] + values) + "\n")
            trait, factor = split_factor_id(factor_id)
            meta = metadata[factor_id]
            check(meta["trait"] == trait and meta["factor"] == factor, "Metadata disagrees with id %s" % factor_id)
            index_rows.append({"global_eaggl_column": "Factor%d" % (row_number + 1), "factor_id": factor_id,
                               "trait": trait, "kpn_trait_id": kpn[trait]["kpn_trait_id"], "factor": factor,
                               "factor_number": meta["factor_number"], "factor_label": meta["label"],
                               "n_nonzero_loadings": sum(1 for x in numbers if x > 0.0),
                               "loading_l2": "%.6g" % math.sqrt(sum(x * x for x in numbers)),
                               "loading_variant": args.loading_variant})
    check(len(index_rows) == len(factor_ids), "%d loading rows for %d factor ids" % (len(index_rows), len(factor_ids)))
    check(set(r["trait"] for r in index_rows) == set(kpn), "Traits in the loadings differ from the KPN map")
    write_tsv(args.output_factor_index_file, FACTOR_INDEX_COLUMNS, index_rows)
    print("Assembled %d %s factors x %d genes" % (len(index_rows), args.loading_variant, len(genes)))


def read_factor_index(path, columns=FACTOR_INDEX_COLUMNS):
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == columns, "Unexpected columns in %s" % path)
    return rows


def cmd_trait_factors(args):
    wanted = [r for r in read_factor_index(args.factor_index_file) if r["trait"] == args.trait]
    check(wanted, "No factors for trait %s" % args.trait)
    wanted_ids = {r["factor_id"]: r for r in wanted}
    kept = []
    with open_text(args.all_factors_file) as fh, open_text(args.output_file, "w") as out:
        header = fh.readline()
        check(header.startswith("Factor\t"), "%s must start with Factor" % args.all_factors_file)
        out.write(header)  # every gene column is kept so eaggl never drops a gene set
        for line in fh:
            factor_id = line.split("\t", 1)[0]
            if factor_id in wanted_ids:
                out.write(line)
                kept.append(factor_id)
    check(kept == [r["factor_id"] for r in wanted], "Factor rows for %s are missing or out of order" % args.trait)
    rows = []
    for k, factor_id in enumerate(kept, 1):
        g = wanted_ids[factor_id]
        rows.append({"local_eaggl_column": "Factor%d" % k, "factor_id": factor_id, "trait": g["trait"],
                     "kpn_trait_id": g["kpn_trait_id"], "factor": g["factor"], "factor_number": g["factor_number"],
                     "factor_label": g["factor_label"], "global_eaggl_column": g["global_eaggl_column"]})
    write_tsv(args.output_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS, rows)
    print("%s: %d factors" % (args.trait, len(rows)))


# -------------------------------------------------------------------------------------------------
# eaggl outputs

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def cmd_record_pigean_commit(args):
    """Run right before eaggl: the pinned pigean checkout must be at the expected commit with a clean src/."""
    head = git_output(args.repo_dir, "rev-parse", "HEAD")
    check(head == args.expected_commit, "pigean checkout %s is at %s, expected %s"
          % (args.repo_dir, head, args.expected_commit))
    status = subprocess.run(["git", "--no-optional-locks", "-C", args.repo_dir, "status", "--porcelain",
                             "--untracked-files=all", "--", "src"], check=True, stdout=subprocess.PIPE,
                            universal_newlines=True).stdout
    check(not status.strip(), "pigean checkout %s has local changes under src/:\n%s" % (args.repo_dir, status))
    with open(args.output_file, "w") as fh:
        fh.write(head + "\n")


def read_pigean_commit(path, expected):
    with open(path) as fh:
        commit = fh.read().strip()
    check(COMMIT_RE.match(commit), "Malformed pigean commit %r in %s" % (commit, path))
    check(commit == expected, "eaggl ran at pigean %s, expected %s (%s)" % (commit, expected, path))
    return commit


def read_params(path):
    params = defaultdict(list)
    for row in read_tsv(path):
        params[row["Parameter"]].append(row["Value"])
    return params


def read_projection(path, column_to_factor_id, gene_set_ids):
    """Parse one eaggl --gene-set-clusters(-marginal)-out file.

    Returns {gene_set_id: (cluster_factor_id or None, [loading strings in factor-column order])} and
    the number of label mismatches. Raises unless the ids match the index and every loading is in [0, 1].
    """
    columns = list(column_to_factor_id)
    with open_text(path) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        position = {name: i for i, name in enumerate(header)}
        factor_cols = [h for h in header if FACTOR_COL_RE.match(h)]
        check(factor_cols == columns, "%s has factor columns %s..., expected %s..."
              % (path, factor_cols[:3], columns[:3]))
        for name in ("Gene_Set", "cluster", "label"):
            check(name in position, "%s has no %s column" % (path, name))
        value_positions = [position[c] for c in columns]
        result, label_failures = {}, 0
        for line in fh:
            fields = line.rstrip("\n").split("\t")
            gene_set_id = fields[position["Gene_Set"]]
            check(gene_set_id not in result, "Duplicate gene set %s in %s" % (gene_set_id, path))
            values = [fields[i] for i in value_positions]
            for value in values:
                parse_loading(value, "%s (%s)" % (path, gene_set_id))
            cluster, label = fields[position["cluster"]], fields[position["label"]]
            cluster_id = None
            if cluster != NA:
                check(cluster in column_to_factor_id, "Unknown cluster %s in %s" % (cluster, path))
                cluster_id = column_to_factor_id[cluster]
                if label != cluster_id:
                    label_failures += 1
            result[gene_set_id] = (cluster_id, values)
    check(set(result) == set(gene_set_ids), "%s has %d gene sets; %d are missing from it and %d are unexpected"
          % (path, len(result), len(set(gene_set_ids) - set(result)), len(set(result) - set(gene_set_ids))))
    return result, label_failures


# Ranks are 1-based ordinal (highest loading first, ties by gene-set id): within the factor over every gene set, and
# within the gene set's library (the same order restricted to the library).
LONG_COLUMNS = ["trait", "kpn_trait_id", "factor_id", "factor", "factor_label", "gene_set_id", "collection_id",
                "cfde_label", "library", "joint_loading", "marginal_loading", "joint_rank_in_factor",
                "marginal_rank_in_factor", "is_joint_top_factor", "joint_rank_in_library", "marginal_rank_in_library"]
TOP_COLUMNS = LONG_COLUMNS[:6] + ["gene_set_name"] + LONG_COLUMNS[6:]
QC_COLUMNS = ["trait", "kpn_trait_id", "kpn_release", "n_factors", "n_gene_sets_index", "n_gene_sets_joint",
              "n_gene_sets_marginal", "ids_equal_index", "label_check_failures", "k1_joint_marginal_max_absdiff",
              "aligned_genes", "seed", "loading_variant", "pigean_commit", "qc_pass"]


CHUNK_COLUMNS = ["chunk", "file", "first_row", "n_gene_sets", "first_gene_set_id", "last_gene_set_id", "sha256"]
CHUNK_FILE_RE = re.compile(r"^chunk_\d{5}\.gmt\.gz$")
CHUNK_OUTPUTS = (".joint.tsv.gz", ".marginal.tsv.gz", ".params.tsv", ".warnings.txt", ".log")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def gene_set_index_rows(path):
    """Gene-set index rows in index order, streamed (the index can hold millions of gene sets)."""
    with open_text(path) as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        check(next(reader, None) == GENE_SET_INDEX_COLUMNS, "Unexpected columns in %s" % path)
        for row in reader:
            check(len(row) == len(GENE_SET_INDEX_COLUMNS), "Malformed row in %s" % path)
            yield dict(zip(GENE_SET_INDEX_COLUMNS, row))


def cmd_chunk_annotations(args):
    """Split eaggl's X input into GMTs of at most --chunk-size gene sets, in gene-set index order, and list them.

    eaggl densifies its genes x gene-sets matrix, so a chunk bounds its memory. Each gene set's joint and marginal
    loadings depend only on its own column and the factors, so projecting chunks gives the same loadings.
    """
    check(args.chunk_size >= 1, "--chunk-size must be positive")
    ids = [row["gene_set_id"] for row in gene_set_index_rows(args.gene_set_index_file)]
    os.makedirs(args.output_dir, exist_ok=True)
    for name in os.listdir(args.output_dir):
        if CHUNK_FILE_RE.match(name):
            os.unlink(os.path.join(args.output_dir, name))
    rows, lines, position = [], [], 0

    def flush():
        path = os.path.join(args.output_dir, "chunk_%05d.gmt.gz" % (len(rows) + 1))
        with DeterministicGzipWriter(path) as out:
            out.writelines(lines)
        first = position - len(lines)
        rows.append({"chunk": "chunk_%05d" % (len(rows) + 1), "file": os.path.abspath(path), "first_row": first,
                     "n_gene_sets": len(lines), "first_gene_set_id": ids[first], "last_gene_set_id": ids[position - 1],
                     "sha256": sha256_file(path)})
        del lines[:]

    with gzip.open(args.annotations_gmt_file, "rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            gene_set_id = line.split(None, 1)[0].decode("utf-8")
            check(position < len(ids) and gene_set_id == ids[position],
                  "The annotations GMT and the gene-set index disagree at gene set %d (%s)" % (position + 1, gene_set_id))
            lines.append(line if line.endswith(b"\n") else line + b"\n")
            position += 1
            if len(lines) == args.chunk_size:
                flush()
    if lines:
        flush()
    check(position == len(ids), "The annotations GMT has %d gene sets; the index has %d" % (position, len(ids)))
    write_tsv(args.output_file, CHUNK_COLUMNS, rows)
    print("%d gene sets in %d chunks of at most %d" % (position, len(rows), args.chunk_size))


def run_eaggl_chunk(args, chunk_file, prefix):
    """The eaggl factor flags of the per-trait projection, on one chunk; outputs are renamed into place on success."""
    tmp = prefix + ".tmp"
    env = dict(os.environ, PYTHONPATH=args.pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    command = [args.python, "-B", "-m", "eaggl", "factor", "--factor-gene-clusters-in", args.trait_factors_file,
               "--factor-gene-clusters-layout", "factors-by-genes", "--X-in", chunk_file, "--gene-map-in", args.gene_map_file,
               "--gene-set-projection-mode", "both", "--gene-set-clusters-out", tmp + ".joint.tsv.gz",
               "--gene-set-clusters-marginal-out", tmp + ".marginal.tsv.gz", "--factor-output-scope", "all",
               "--cluster-row-min-max-loading", "0", "--seed", str(args.seed), "--hide-progress", "--hide-opts",
               "--params-out", tmp + ".params.tsv", "--warnings-file", tmp + ".warnings.txt", "--log-file", tmp + ".log"]
    proc = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check(proc.returncode == 0, "eaggl failed on %s:\n%s" % (chunk_file, proc.stdout[-3000:]))
    for suffix in CHUNK_OUTPUTS:
        if os.path.exists(tmp + suffix):
            os.replace(tmp + suffix, prefix + suffix)
        else:
            check(suffix == ".warnings.txt", "eaggl wrote no %s for %s" % (suffix, chunk_file))
            open(prefix + suffix, "w").close()


def library_ranks(values, ids, libraries, n_libraries):
    """(rank in factor, rank in library) arrays: ordinal, highest value first, ties by gene-set id."""
    order = sorted(range(len(values)), key=lambda i: (-values[i], ids[i]))
    in_factor, in_library, counts = array("i", bytes(4 * len(values))), array("i", bytes(4 * len(values))), [0] * n_libraries
    for rank, i in enumerate(order, 1):
        in_factor[i] = rank
        counts[libraries[i]] += 1
        in_library[i] = counts[libraries[i]]
    return in_factor, in_library


def cmd_project_trait(args):
    """Project every CFDE gene set onto one trait's factors, chunk by chunk, and merge the chunks.

    Chunks are resumable: a chunk whose outputs and fingerprint (chunk, factors and gene map checksums, seed, pigean
    commit) are in --work-dir is not rerun. After the merge, every factor gets ordinal ranks over all gene sets and
    within each library. The long file then holds every factor's row for each gene set that is among the --top-n of
    some factor in its library (joint or marginal); the top file holds the rows within that factor's own top --top-n.
    The work directory is removed once the outputs are written.
    """
    head = git_output(args.repo_dir, "rev-parse", "HEAD")
    check(head == args.expected_pigean_commit, "pigean checkout %s is at %s, expected %s" % (args.repo_dir, head, args.expected_pigean_commit))
    status = subprocess.run(["git", "--no-optional-locks", "-C", args.repo_dir, "status", "--porcelain", "--untracked-files=all",
                             "--", "src"], check=True, stdout=subprocess.PIPE, universal_newlines=True).stdout
    check(not status.strip(), "pigean checkout %s has local changes under src/:\n%s" % (args.repo_dir, status))
    factors = read_factor_index(args.trait_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS)
    check(all(f["trait"] == args.trait for f in factors), "Factor index is not for trait %s" % args.trait)
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn, "Trait %s is not in the KPN map" % args.trait)
    check(kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id == factors[0]["kpn_trait_id"],
          "KPN id for %s disagrees: meta %s, map %s, factor index %s"
          % (args.trait, args.kpn_trait_id, kpn[args.trait]["kpn_trait_id"], factors[0]["kpn_trait_id"]))
    with open_text(args.trait_factors_file) as fh:
        n_genes = len(fh.readline().rstrip("\n").split("\t")) - 1
    column_to_factor_id = OrderedDict((f["local_eaggl_column"], f["factor_id"]) for f in factors)
    factor_position = {f["factor_id"]: k for k, f in enumerate(factors)}
    K = len(factors)

    ids, libraries, collections, labels, names, library_names = [], array("i"), [], [], [], {}
    for row in gene_set_index_rows(args.gene_set_index_file):
        ids.append(row["gene_set_id"])
        libraries.append(library_names.setdefault(row["library"], len(library_names)))
        collections.append(row["collection_id"]); labels.append(row["cfde_label"]); names.append(row["gene_set_name"])
    library_of = {v: k for k, v in library_names.items()}
    n = len(ids)
    joint = [array("d", bytes(8 * n)) for _ in range(K)]
    marginal = [array("d", bytes(8 * n)) for _ in range(K)]
    top_factor, seen = array("i", [-1]) * n, bytearray(n)

    chunks = read_tsv(args.chunks_file)
    check(chunks and list(chunks[0].keys()) == CHUNK_COLUMNS, "Unexpected columns in %s" % args.chunks_file)
    check(sum(int(c["n_gene_sets"]) for c in chunks) == n, "The chunks do not cover the gene-set index")
    os.makedirs(args.work_dir, exist_ok=True)
    fingerprint_base = "%s %s %s %s" % (sha256_file(args.trait_factors_file), sha256_file(args.gene_map_file), args.seed, head)
    label_failures, k1_diff, rerun, params, warnings, logs = 0, 0.0, 0, None, [], []
    for chunk in chunks:
        first, size = int(chunk["first_row"]), int(chunk["n_gene_sets"])
        chunk_ids = ids[first:first + size]
        check(chunk_ids and (chunk_ids[0], chunk_ids[-1]) == (chunk["first_gene_set_id"], chunk["last_gene_set_id"]),
              "%s does not match the gene-set index" % chunk["chunk"])
        prefix = os.path.join(args.work_dir, chunk["chunk"])
        fingerprint = "%s %s\n" % (chunk["sha256"], fingerprint_base)
        done = prefix + ".done"
        if not (os.path.exists(done) and open(done).read() == fingerprint
                and all(os.path.exists(prefix + suffix) for suffix in CHUNK_OUTPUTS)):
            check(sha256_file(chunk["file"]) == chunk["sha256"], "%s changed since it was listed" % chunk["file"])
            run_eaggl_chunk(args, chunk["file"], prefix)
            with open(done, "w") as fh:
                fh.write(fingerprint)
            rerun += 1
        joint_rows, joint_failures = read_projection(prefix + ".joint.tsv.gz", column_to_factor_id, chunk_ids)
        marginal_rows, marginal_failures = read_projection(prefix + ".marginal.tsv.gz", column_to_factor_id, chunk_ids)
        label_failures += joint_failures + marginal_failures
        chunk_params = read_params(prefix + ".params.tsv")
        aligned = chunk_params.get("factor_projection_only_gene_set_aligned_genes", [NA])[-1]
        check(aligned == str(n_genes), "eaggl aligned %s genes in %s; the factor file has %d" % (aligned, chunk["chunk"], n_genes))
        check(chunk_params.get("gene_set_projection_mode", [NA])[-1] == "both", "eaggl did not run in both mode")
        params = params or prefix + ".params.tsv"
        positions = {gene_set_id: first + j for j, gene_set_id in enumerate(chunk_ids)}
        for rows_, target in ((joint_rows, joint), (marginal_rows, marginal)):
            for gene_set_id, (cluster_id, values) in rows_.items():
                i = positions[gene_set_id]
                for k, value in enumerate(values):
                    number = float(value)
                    check("%.4g" % number == value, "Loading %r of %s is not a %%.4g string" % (value, gene_set_id))
                    target[k][i] = number
                if target is joint:
                    top_factor[i] = factor_position[cluster_id] if cluster_id else -1
                    seen[i] = 1
        if K == 1:
            k1_diff = max([k1_diff] + [abs(joint[0][i] - marginal[0][i]) for i in range(first, first + size)])
        with open(prefix + ".warnings.txt") as fh:
            warnings.extend(line for line in fh if line.strip())
        logs.append(prefix + ".log")
    check(all(seen), "%d gene sets have no projection" % (n - sum(seen)))
    check(label_failures == 0, "%d eaggl labels do not match the factor order for %s" % (label_failures, args.trait))
    check(K != 1 or k1_diff <= LOADING_ROUNDING_TOL, "K=1 joint and marginal loadings differ by %.3g for %s" % (k1_diff, args.trait))

    ranked, union = [], set()
    for k in range(K):
        joint_factor, joint_library = library_ranks(joint[k], ids, libraries, len(library_names))
        marginal_factor, marginal_library = library_ranks(marginal[k], ids, libraries, len(library_names))
        union.update(i for i in range(n) if joint_library[i] <= args.top_n or marginal_library[i] <= args.top_n)
        ranked.append((joint_factor, joint_library, marginal_factor, marginal_library))
    with open_text(args.output_long_file, "w") as long_fh, open_text(args.output_top_file, "w") as top_fh:
        long_fh.write(tsv_line(LONG_COLUMNS))
        top_fh.write(tsv_line(TOP_COLUMNS))
        for k, factor in enumerate(factors):
            joint_factor, joint_library, marginal_factor, marginal_library = ranked[k]
            for i in sorted(union, key=lambda i: (library_of[libraries[i]], joint_library[i])):
                row = {"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "factor_id": factor["factor_id"],
                       "factor": factor["factor"], "factor_label": factor["factor_label"], "gene_set_id": ids[i],
                       "gene_set_name": names[i], "collection_id": collections[i], "cfde_label": labels[i],
                       "library": library_of[libraries[i]], "joint_loading": "%.4g" % joint[k][i],
                       "marginal_loading": "%.4g" % marginal[k][i], "joint_rank_in_factor": joint_factor[i],
                       "marginal_rank_in_factor": marginal_factor[i], "is_joint_top_factor": int(top_factor[i] == k),
                       "joint_rank_in_library": joint_library[i], "marginal_rank_in_library": marginal_library[i]}
                long_fh.write(tsv_line(row[c] for c in LONG_COLUMNS))
                if joint_library[i] <= args.top_n or marginal_library[i] <= args.top_n:
                    top_fh.write(tsv_line(row[c] for c in TOP_COLUMNS))
    qc = {"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "kpn_release": kpn[args.trait]["kpn_release"],
          "n_factors": K, "n_gene_sets_index": n, "n_gene_sets_joint": sum(seen), "n_gene_sets_marginal": sum(seen),
          "ids_equal_index": True, "label_check_failures": label_failures,
          "k1_joint_marginal_max_absdiff": "%.3g" % k1_diff if K == 1 else NA, "aligned_genes": n_genes, "seed": args.seed,
          "loading_variant": args.loading_variant, "pigean_commit": head, "qc_pass": True}
    write_tsv(args.output_qc_file, QC_COLUMNS, [qc])
    with open(args.output_commit_file, "w") as fh:
        fh.write(head + "\n")
    with open(params) as src, open(args.output_params_file, "w") as dst:
        dst.write(src.read())
    with open(args.output_warnings_file, "w") as fh:
        fh.writelines(sorted(set(warnings)))
    with open(args.output_log_file, "w") as out:
        for path in logs:
            out.write("== %s\n" % os.path.basename(path)[:-len(".log")])
            with open(path) as fh:
                out.write(fh.read())
    if not args.keep_work_dir:
        for chunk in chunks:
            for suffix in CHUNK_OUTPUTS + (".done",):
                path = os.path.join(args.work_dir, chunk["chunk"] + suffix)
                if os.path.exists(path):
                    os.unlink(path)
        if not os.listdir(args.work_dir):
            os.rmdir(args.work_dir)
    print("%s: %d factors x %d gene sets in %d chunks (%d run now); %d gene sets in the long file"
          % (args.trait, K, n, len(chunks), rerun, len(union)))


def cmd_relabel_global(args):
    factors = read_factor_index(args.factor_index_file)
    gene_sets = read_gene_set_index(args.gene_set_index_file)
    column_to_factor_id = OrderedDict((f["global_eaggl_column"], f["factor_id"]) for f in factors)
    with open_text(args.all_factors_file) as fh:
        n_genes = len(fh.readline().rstrip("\n").split("\t")) - 1
    params = read_params(args.params_file)
    aligned = params.get("factor_projection_only_gene_set_aligned_genes", [NA])[-1]
    check(aligned == str(n_genes), "eaggl aligned %s genes; the factor file has %d" % (aligned, n_genes))
    check(params.get("gene_set_projection_mode", [NA])[-1] == "both", "eaggl did not run in both mode")
    pigean_commit = read_pigean_commit(args.pigean_commit_file, args.expected_pigean_commit)

    qc_rows = []
    for mode, raw, output in (("joint", args.joint_file, args.output_joint_file),
                              ("marginal", args.marginal_file, args.output_marginal_file)):
        seen, label_failures = set(), 0
        with open_text(raw) as fh, open_text(output, "w") as out:
            header = fh.readline().rstrip("\n").split("\t")
            position = {name: i for i, name in enumerate(header)}
            factor_cols = [h for h in header if FACTOR_COL_RE.match(h)]
            check(factor_cols == list(column_to_factor_id), "%s factor columns differ from the factor index" % raw)
            value_positions = [position[c] for c in factor_cols]
            out.write("\t".join(["gene_set_id", "collection_id", "cfde_label", "library", "top_factor_id"]
                                + [column_to_factor_id[c] for c in factor_cols]) + "\n")
            for line in fh:
                fields = line.rstrip("\n").split("\t")
                gene_set_id = fields[position["Gene_Set"]]
                check(gene_set_id in gene_sets and gene_set_id not in seen, "Unexpected gene set %s in %s"
                      % (gene_set_id, raw))
                seen.add(gene_set_id)
                values = [fields[i] for i in value_positions]
                for value in values:
                    parse_loading(value, "%s (%s)" % (raw, gene_set_id))
                cluster, label = fields[position["cluster"]], fields[position["label"]]
                top = column_to_factor_id[cluster] if cluster != NA else NA
                label_failures += int(top != label)
                g = gene_sets[gene_set_id]
                out.write("\t".join([gene_set_id, g["collection_id"], g["cfde_label"], g["library"], top] + values) + "\n")
        check(seen == set(gene_sets), "%s is missing %d gene sets" % (raw, len(set(gene_sets) - seen)))
        check(label_failures == 0, "%d eaggl labels in %s do not match the factor order" % (label_failures, raw))
        qc_rows.append({"mode": mode, "n_gene_sets": len(seen), "n_factors": len(factor_cols),
                        "ids_equal_index": True, "label_check_failures": label_failures, "aligned_genes": aligned,
                        "loading_variant": factors[0]["loading_variant"], "pigean_commit": pigean_commit})
    write_tsv(args.output_qc_file, list(qc_rows[0].keys()), qc_rows)
    print("Relabelled all-factor projections: %d gene sets x %d factors" % (len(gene_sets), len(column_to_factor_id)))


def cmd_collect(args):
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    qc_rows = []
    for path in args.qc_file:
        rows = read_tsv(path)
        check(len(rows) == 1 and list(rows[0].keys()) == QC_COLUMNS, "Unexpected QC file %s" % path)
        qc_rows.append(rows[0])
    traits = [r["trait"] for r in qc_rows]
    check(len(set(traits)) == len(traits), "Duplicate traits among QC files")
    check(set(traits) == set(kpn), "QC files cover %d traits; the project has %d (missing: %s)"
          % (len(traits), len(kpn), sorted(set(kpn) - set(traits))[:10]))
    check(all(r["qc_pass"] == "True" for r in qc_rows), "Some traits failed QC")
    commits = {r["pigean_commit"] for r in qc_rows}
    check(commits == {args.expected_pigean_commit}, "Traits ran on pigean commits %s, expected only %s"
          % (sorted(commits), args.expected_pigean_commit))
    qc_rows.sort(key=lambda r: r["trait"])
    write_tsv(args.output_manifest_file, QC_COLUMNS, qc_rows)

    n_rows = 0
    with open_text(args.output_top_file, "w") as out:
        out.write("\t".join(TOP_COLUMNS) + "\n")
        for path in sorted(args.top_file):
            with open_text(path) as fh:
                check(fh.readline().rstrip("\n").split("\t") == TOP_COLUMNS, "Unexpected columns in %s" % path)
                for line in fh:
                    out.write(line)
                    n_rows += 1
    print("Collected %d traits (%d factors); %d top gene-set rows"
          % (len(qc_rows), sum(int(r["n_factors"]) for r in qc_rows), n_rows))


def cmd_compare_global(args):
    """Manual check: per-trait vs all-factor projections for a sample of traits."""
    wanted = defaultdict(dict)  # factor_id -> gene_set_id -> (joint, marginal) from per-trait runs
    for path in args.trait_long_file:
        with open_text(path) as fh:
            for row in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                wanted[row["factor_id"]][row["gene_set_id"]] = (float(row["joint_loading"]),
                                                                float(row["marginal_loading"]))
    global_values = {}
    for mode, path in (("joint", args.global_joint_file), ("marginal", args.global_marginal_file)):
        with open_text(path) as fh:
            header = fh.readline().rstrip("\n").split("\t")
            positions = {f: header.index(f) for f in wanted}
            for line in fh:
                fields = line.rstrip("\n").split("\t")
                for factor_id, i in positions.items():
                    global_values[(mode, factor_id, fields[0])] = float(fields[i])
    by_trait = defaultdict(list)
    for factor_id in wanted:
        by_trait[split_factor_id(factor_id)[0]].append(factor_id)
    rows = []
    for trait in sorted(by_trait):
        keys = [(f, g) for f in by_trait[trait] for g in wanted[f]]
        joint_pairs = [(wanted[f][g][0], global_values[("joint", f, g)]) for f, g in keys]
        marginal_diff = max(abs(wanted[f][g][1] - global_values[("marginal", f, g)]) for f, g in keys)
        rows.append({"trait": trait, "n_factors": len(by_trait[trait]), "n_values": len(keys),
                     "marginal_max_absdiff": "%.3g" % marginal_diff,
                     "marginal_equal": marginal_diff <= LOADING_ROUNDING_TOL,
                     "joint_pearson": "%.4f" % pearson(joint_pairs)})
    write_tsv(args.output_file, list(rows[0].keys()), rows)
    check(all(r["marginal_equal"] for r in rows), "Per-trait and all-factor marginal loadings differ")
    print("Compared %d traits" % len(rows))


def pearson(pairs):
    n = len(pairs)
    mx, my = sum(x for x, _ in pairs) / n, sum(y for _, y in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx, syy = sum((x - mx) ** 2 for x, _ in pairs), sum((y - my) ** 2 for _, y in pairs)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else float("nan")


# -------------------------------------------------------------------------------------------------
# Trait -> CFDE gene-set betas: `python -m pigean betas` (no outer Gibbs) on each trait's existing PIGEAN
# gene stats. Those come from one all-trait export, indexed once and sliced per trait by byte range.

GENE_STATS_INDEX_COLUMNS = ["trait", "byte_offset", "byte_length", "rows", "source_size", "source_mtime_ns"]
PIGEAN_GENE_SET_STATS_COLUMNS = {"Gene_Set", "filter_reason", "N", "beta", "beta_uncorrected", "avg_postp"}
GENE_SET_STATS_COLUMNS = ["trait", "kpn_trait_id", "gene_set_id", "collection_id", "cfde_label", "library", "n_genes",
                          "beta_uncorrected", "beta", "avg_postp", "library_rank", "response", "p", "sigma2"]


def source_identity(path):
    stat = os.stat(path)
    return str(stat.st_size), str(stat.st_mtime_ns)


def cmd_gene_stats_index(args):
    """Where each pipeline trait's rows are in the all-trait gene stats (one pass; a trait's rows must be contiguous)."""
    wanted = set(read_trait_kpn_map(args.trait_kpn_map_file))
    identity = source_identity(args.gene_stats_file)
    found, runs = {}, Counter()
    def close(phenotype, start, end, rows):
        name = phenotype.decode("utf-8")
        runs[name] += 1
        if name in wanted:
            check(runs[name] == 1, "The rows of %s in %s are not contiguous" % (name, args.gene_stats_file))
            found[name] = (start, end - start, rows)
    with open(args.gene_stats_file, "rb") as fh:
        header = fh.readline().decode("utf-8").rstrip("\n").split("\t")
        check(header[:2] == ["phenotype", "gene"] and "log_bf" in header,
              "%s must start with phenotype, gene and have log_bf: %s" % (args.gene_stats_file, header))
        offset = start = fh.tell()
        phenotype, prefix, rows = None, None, 0
        for line in fh:
            if prefix is None or not line.startswith(prefix):
                if phenotype is not None:
                    close(phenotype, start, offset, rows)
                phenotype = line[:line.find(b"\t")]
                prefix, start, rows = phenotype + b"\t", offset, 0
            rows += 1
            offset += len(line)
        if phenotype is not None:
            close(phenotype, start, offset, rows)
    check(source_identity(args.gene_stats_file) == identity, "%s changed while it was indexed" % args.gene_stats_file)
    missing = sorted(wanted - set(found))
    check(not missing, "%d traits have no gene stats in %s: %s" % (len(missing), args.gene_stats_file, missing[:20]))
    write_tsv(args.output_file, GENE_STATS_INDEX_COLUMNS, [
        {"trait": trait, "byte_offset": found[trait][0], "byte_length": found[trait][1], "rows": found[trait][2],
         "source_size": identity[0], "source_mtime_ns": identity[1]} for trait in sorted(found)])
    print("%d traits indexed of %d phenotypes in %s" % (len(found), len(runs), args.gene_stats_file))


def cmd_trait_gene_stats(args):
    """One trait's rows of the all-trait gene stats, read by byte range (the export must be unchanged since indexing)."""
    entries = [r for r in read_tsv(args.index_file) if r["trait"] == args.trait]
    check(len(entries) == 1, "Trait %s is not in %s" % (args.trait, args.index_file))
    entry = entries[0]
    check(source_identity(args.gene_stats_file) == (entry["source_size"], entry["source_mtime_ns"]),
          "%s changed since it was indexed: rebuild %s" % (args.gene_stats_file, args.index_file))
    with open(args.gene_stats_file, "rb") as fh:
        header = fh.readline()
        fh.seek(int(entry["byte_offset"]))
        lines = fh.read(int(entry["byte_length"])).splitlines(keepends=True)
    prefix = (args.trait + "\t").encode("utf-8")
    check(len(lines) == int(entry["rows"]) and all(line.startswith(prefix) for line in lines),
          "The indexed rows of %s do not match %s" % (args.trait, args.gene_stats_file))
    genes = [line.split(b"\t", 2)[1] for line in lines]
    check(len(set(genes)) == len(genes), "Duplicate genes in the gene stats of %s" % args.trait)
    if lines and not lines[-1].endswith(b"\n"):
        lines[-1] += b"\n"
    with DeterministicGzipWriter(args.output_file) as out:
        out.write(header)
        out.writelines(lines)
    print("%s: %d genes" % (args.trait, len(lines)))


def cmd_annotate_gene_set_stats(args):
    """Validate one trait's `pigean betas` gene-set stats and keep the gene sets PIGEAN analyzed (filter_reason kept;
    the prefiltered ones carry no beta), ranked by beta_uncorrected within their library."""
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn and kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id,
          "KPN id for %s disagrees: meta %s, map %s" % (args.trait, args.kpn_trait_id, kpn.get(args.trait, {}).get("kpn_trait_id")))
    gene_sets = read_gene_set_index(args.gene_set_index_file)
    params = read_params(args.params_file)
    used = params.get("option_gene_stats_log_bf_col", [NA])[-1]
    check(used == args.response, "pigean regressed on %s, expected %s" % (used, args.response))
    learned = {name: params.get(name, [NA])[-1] for name in ("p", "sigma2")}
    read_pigean_commit(args.pigean_commit_file, args.expected_pigean_commit)
    rows, seen, reasons = [], set(), Counter()
    with open_text(args.stats_file) as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        missing = PIGEAN_GENE_SET_STATS_COLUMNS - set(reader.fieldnames or [])
        check(not missing, "%s lacks the pigean columns %s" % (args.stats_file, sorted(missing)))
        for r in reader:
            gene_set_id = r["Gene_Set"]
            check(gene_set_id in gene_sets, "Unknown gene set %s in %s" % (gene_set_id, args.stats_file))
            check(gene_set_id not in seen, "Duplicate gene set %s in %s" % (gene_set_id, args.stats_file))
            seen.add(gene_set_id)
            reasons[r["filter_reason"]] += 1
            if r["filter_reason"] != "kept":
                continue
            for name in ("beta_uncorrected", "beta", "avg_postp"):
                check(math.isfinite(float(r[name])), "Non-finite %s for %s" % (name, gene_set_id))
            gene_set = gene_sets[gene_set_id]
            rows.append({"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "gene_set_id": gene_set_id,
                         "collection_id": gene_set["collection_id"], "cfde_label": gene_set["cfde_label"],
                         "library": gene_set["library"], "n_genes": r["N"], "beta_uncorrected": r["beta_uncorrected"],
                         "beta": r["beta"], "avg_postp": r["avg_postp"], "response": args.response, **learned})
    by_library = defaultdict(list)
    for row in rows:
        by_library[row["library"]].append(row)
    for items in by_library.values():
        items.sort(key=lambda row: (-float(row["beta_uncorrected"]), row["gene_set_id"]))
        for rank, row in enumerate(items, 1):
            row["library_rank"] = rank
    rows.sort(key=lambda row: (row["library"], row["library_rank"]))
    write_tsv(args.output_file, GENE_SET_STATS_COLUMNS, rows)
    print("%s: %d of %d gene sets analyzed by PIGEAN (%s)" % (args.trait, len(rows), len(seen), dict(sorted(reasons.items()))))


# -------------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("trait-kpn-map", help="Map EAGGL traits to KPN.TRAIT ids")
    p.add_argument("--factor-ids-file", required=True)
    p.add_argument("--registry-file", required=True)
    p.add_argument("--kpn-release", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_trait_kpn_map)

    p = sub.add_parser("cfde-index", help="Keep the release's libraries of the snapshot's CFDE index")
    for name in ("libraries", "index-file", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_cfde_index)

    p = sub.add_parser("collection-index", help="Validate one CFDE collection and index its gene sets")
    for name in ("label", "collection-id", "library", "gmt-file", "names-gmt-file", "collection-yaml-file",
                 "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_collection_index)

    p = sub.add_parser("build-annotations", help="Concatenate all CFDE GMTs and build the gene-set index")
    p.add_argument("--gmt-file", action="append", required=True)
    p.add_argument("--collection-gene-sets-file", action="append", required=True)
    for name in ("cfde-index-file", "eaggl-genes-file", "cfde-snapshot", "output-gmt-file",
                 "output-gene-set-index-file", "output-gene-map-file", "output-overlap-report-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_build_annotations)

    p = sub.add_parser("assemble-factors", help="Write the eaggl factors-by-genes table for all factors")
    for name in ("loadings-file", "factor-ids-file", "factor-metadata-file", "genes-file", "capped-entries-file",
                 "trait-kpn-map-file", "output-file", "output-factor-index-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--loading-variant", choices=["capped", "uncapped"], required=True)
    p.set_defaults(func=cmd_assemble_factors)

    p = sub.add_parser("trait-factors", help="Write one trait's factors (all gene columns)")
    for name in ("all-factors-file", "factor-index-file", "trait", "output-file", "output-factor-index-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_trait_factors)

    p = sub.add_parser("record-pigean-commit", help="Check the pinned pigean checkout and record its commit")
    for name in ("repo-dir", "expected-commit", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_record_pigean_commit)

    p = sub.add_parser("chunk-annotations", help="Split eaggl's gene-set input into chunks of at most --chunk-size")
    for name in ("annotations-gmt-file", "gene-set-index-file", "output-dir", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--chunk-size", type=int, required=True)
    p.set_defaults(func=cmd_chunk_annotations)

    p = sub.add_parser("project-trait", help="Project one trait chunk by chunk with eaggl and rank the merged loadings")
    for name in ("python", "pigean-src", "repo-dir", "expected-pigean-commit", "chunks-file", "trait-factors-file",
                 "trait-factor-index-file", "gene-map-file", "gene-set-index-file", "trait-kpn-map-file", "trait",
                 "kpn-trait-id", "loading-variant", "work-dir", "output-long-file", "output-top-file", "output-qc-file",
                 "output-commit-file", "output-params-file", "output-warnings-file", "output-log-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--top-n", type=int, default=50)
    p.add_argument("--keep-work-dir", action="store_true", help="Keep the per-chunk eaggl outputs")
    p.set_defaults(func=cmd_project_trait)

    p = sub.add_parser("relabel-global", help="Relabel the all-factor eaggl projections with factor ids")
    for name in ("joint-file", "marginal-file", "all-factors-file", "factor-index-file", "gene-set-index-file",
                 "params-file", "pigean-commit-file", "expected-pigean-commit", "output-joint-file",
                 "output-marginal-file", "output-qc-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_relabel_global)

    p = sub.add_parser("collect", help="Build the per-trait manifest and the combined top gene sets")
    p.add_argument("--qc-file", action="append", required=True)
    p.add_argument("--top-file", action="append", required=True)
    for name in ("trait-kpn-map-file", "expected-pigean-commit", "output-manifest-file", "output-top-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("gene-stats-index", help="Index each trait's rows in the all-trait PIGEAN gene stats")
    for name in ("gene-stats-file", "trait-kpn-map-file", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_gene_stats_index)

    p = sub.add_parser("trait-gene-stats", help="Write one trait's PIGEAN gene stats from the indexed export")
    for name in ("gene-stats-file", "index-file", "trait", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_trait_gene_stats)

    p = sub.add_parser("annotate-gene-set-stats", help="Validate and rank one trait's pigean betas gene-set stats")
    for name in ("stats-file", "params-file", "gene-set-index-file", "trait-kpn-map-file", "trait", "kpn-trait-id",
                 "response", "pigean-commit-file", "expected-pigean-commit", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_annotate_gene_set_stats)

    p = sub.add_parser("compare-global", help="Compare per-trait and all-factor projections (manual check)")
    p.add_argument("--trait-long-file", action="append", required=True)
    for name in ("global-joint-file", "global-marginal-file", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_compare_global)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except WorkflowError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
