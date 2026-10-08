#!/usr/bin/env python3
"""Helper steps for the CFDE -> EAGGL supplied-factor projection LAP pipeline.

Every subcommand is a single LAP step (see ../config/cfde_projection.cfg). The CFDE gene sets are read
once into a packed binary matrix (pack-annotations), and each trait's projection is eaggl's supplied-factor
projection computed from it by projection_kernel.py (project-trait). check-projection compares that kernel
against the pinned `python -m eaggl factor` before any trait runs. Identifiers are carried verbatim end to end:

  * gene sets:   GMT column 1 (`dapper:GeneSet.*`) == eaggl `Gene_Set`
  * collections: `dapper:GeneSetCollection.*` from the CFDE index, attached by join
  * factors:     EAGGL `trait::FactorN`; eaggl names factors by position (Factor1..K), so the
                 row order of every factor file we write is recorded in a factor index
  * traits:      EAGGL trait == KPN `legacy_phenotype_id` -> `KPN.TRAIT:*`

Any violated invariant raises WorkflowError, which exits non-zero so LAP marks the step failed.
Standard library only, except the projection subcommands (numpy and scipy, imported when they run); Python 3.9.
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
import time
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


def gmt_genes(fields):
    """The member genes of a GMT row's tab fields: columns 3 on (empty fields skipped).

    DAPPER 0.2.0 GMTs fill the description column (2) with free text. eaggl and pigean split GMT lines on any
    whitespace and take every token after the id as a gene (a `gene:weight` token as a weighted gene), so the
    annotations GMT they read is written with that column blank."""
    return [gene for gene in fields[2:] if gene]


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
        check(len(id_fields) >= 3, "%s has no gene columns" % where)
        check(id_fields[2:] == name_fields[2:], "Gene columns differ between the id and name GMTs at %s" % where)
        genes = gmt_genes(id_fields)
        check(genes, "%s has no genes" % where)
        odd = [gene for gene in genes if ":" in gene or gene != "".join(gene.split())]
        check(not odd, "%s has gene tokens eaggl and pigean would misread (whitespace or ':'): %s" % (where, odd[:5]))
        rows.append({"gene_set_id": gene_set_id, "gene_set_name": name_fields[0], "collection_id": args.collection_id,
                     "cfde_label": args.label, "library": args.library, "gmt_row": row_number,
                     "n_genes": len(set(genes))})
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
    the GMTs (case-only gene map, then index rows), so memory holds one row per gene set, never their genes.

    Each output line is the gene set's id, a blank description and its genes in GMT order: the collections' GMTs carry
    free-text descriptions that eaggl and pigean would otherwise read as genes."""
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
                        fields = gmt_fields(line)
                        yield label, fields[0], gmt_genes(fields)

    # Pass 1, case-only gene map: a CFDE symbol absent from EAGGL whose upper-case form is an EAGGL gene.
    upper_to_eaggl = {}
    for gene in universe:
        upper_to_eaggl.setdefault(gene.upper(), set()).add(gene)
    case_map, outside = {}, set()
    for _, _, genes in memberships():
        for gene in set(genes):
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
    with open_text(args.output_gene_set_index_file, "w") as index_fh, open_text(args.output_gmt_file, "w") as gmt_fh:
        index_fh.write(tsv_line(GENE_SET_INDEX_COLUMNS))
        for label, gene_set_id, member_list in memberships():
            gmt_fh.write("\t".join([gene_set_id, ""] + member_list) + "\n")
            genes = set(member_list)
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


def write_trait_factors(all_factors_file, factor_index_rows, trait, output_file):
    """One trait's rows of the all-factors table (every gene column); returns its trait factor index rows."""
    wanted = [r for r in factor_index_rows if r["trait"] == trait]
    check(wanted, "No factors for trait %s" % trait)
    wanted_ids = {r["factor_id"]: r for r in wanted}
    kept = []
    with open_text(all_factors_file) as fh, open_text(output_file, "w") as out:
        header = fh.readline()
        check(header.startswith("Factor\t"), "%s must start with Factor" % all_factors_file)
        out.write(header)  # every gene column is kept so eaggl never drops a gene set
        for line in fh:
            factor_id = line.split("\t", 1)[0]
            if factor_id in wanted_ids:
                out.write(line)
                kept.append(factor_id)
    check(kept == [r["factor_id"] for r in wanted], "Factor rows for %s are missing or out of order" % trait)
    rows = []
    for k, factor_id in enumerate(kept, 1):
        g = wanted_ids[factor_id]
        rows.append({"local_eaggl_column": "Factor%d" % k, "factor_id": factor_id, "trait": g["trait"],
                     "kpn_trait_id": g["kpn_trait_id"], "factor": g["factor"], "factor_number": g["factor_number"],
                     "factor_label": g["factor_label"], "global_eaggl_column": g["global_eaggl_column"]})
    return rows


def cmd_trait_factors(args):
    rows = write_trait_factors(args.all_factors_file, read_factor_index(args.factor_index_file), args.trait,
                               args.output_file)
    write_tsv(args.output_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS, rows)
    print("%s: %d factors" % (args.trait, len(rows)))


def read_factor_matrix(path, genes, factor_ids):
    """A factors-by-genes file as the genes x K float64 matrix eaggl loads (rows must be exactly factor_ids)."""
    import numpy as np
    with open_text(path) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        check(header == ["Factor"] + genes, "The gene columns of %s differ from the packed genes" % path)
        names, rows = [], []
        for line in fh:
            fields = line.rstrip("\n").split("\t")
            names.append(fields[0])
            rows.append([float(value) for value in fields[1:]])
    check(names == factor_ids, "%s holds factors %s..., expected %s..." % (path, names[:3], factor_ids[:3]))
    matrix = np.array(rows, dtype=float).T
    check(np.all(np.isfinite(matrix)) and np.all(matrix >= 0), "Invalid factor loadings in %s" % path)
    return matrix


# -------------------------------------------------------------------------------------------------
# eaggl outputs

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def check_pigean_clone(repo_dir, expected_commit):
    """The pinned pigean checkout must be at the expected commit with a clean src/; returns the commit."""
    head = git_output(repo_dir, "rev-parse", "HEAD")
    check(head == expected_commit, "pigean checkout %s is at %s, expected %s" % (repo_dir, head, expected_commit))
    status = subprocess.run(["git", "--no-optional-locks", "-C", repo_dir, "status", "--porcelain",
                             "--untracked-files=all", "--", "src"], check=True, stdout=subprocess.PIPE,
                            universal_newlines=True).stdout
    check(not status.strip(), "pigean checkout %s has local changes under src/:\n%s" % (repo_dir, status))
    return head


def cmd_record_pigean_commit(args):
    """Run right before pigean: the pinned pigean checkout must be at the expected commit with a clean src/."""
    head = check_pigean_clone(args.repo_dir, args.expected_commit)
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


CHUNK_COLUMNS = ["chunk", "first_row", "n_gene_sets", "first_gene_set_id", "last_gene_set_id", "n_entries"]
CHECK_COLUMNS = ["trait", "n_factors", "n_gene_sets", "n_case_mapped_gene_sets", "n_values", "joint_mismatches",
                 "marginal_mismatches", "top_factor_mismatches", "kernel_updates", "eaggl_seconds", "kernel_seconds",
                 "pigean_commit", "check_pass"]


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


def read_gene_map(path):
    """eaggl's --gene-map-in as a dict (build-annotations writes the case-only CFDE -> EAGGL map)."""
    mapping = {}
    with open_text(path) as fh:
        for line in fh:
            if line.strip():
                fields = line.rstrip("\n").split("\t")
                check(len(fields) == 2 and fields[0] not in mapping, "Malformed gene map line in %s: %r" % (path, line))
                mapping[fields[0]] = fields[1]
    return mapping


def gmt_gene_rows(line, gene_row, gene_map):
    """The sorted EAGGL gene rows eaggl reads from a GMT line: each whitespace token after column 1, through the gene
    map, that is an EAGGL gene (a repeated gene counts once)."""
    return sorted({gene_row[g] for g in (gene_map.get(t, t) for t in line.split()[1:]) if g in gene_row})


def cmd_pack_annotations(args):
    """Read the annotations GMT once into the 0/1 genes x gene-sets matrix every trait's projection reads, and list
    the projection chunks.

    The matrix is two .npy arrays in CSC layout over the EAGGL genes in the factor tables' column order: gene set j
    (row j of the gene-set index) holds the gene rows indices[indptr[j]:indptr[j + 1]]. A chunk is a row range of at
    most --chunk-size gene sets in index order, and is projected as one eaggl run on it would be (its own random
    start and stopping rule).
    """
    import numpy as np
    check(args.chunk_size >= 1, "--chunk-size must be positive")
    genes = read_gene_list(args.genes_file)
    gene_row = {gene: i for i, gene in enumerate(genes)}
    gene_map = read_gene_map(args.gene_map_file)
    ids = [row["gene_set_id"] for row in gene_set_index_rows(args.gene_set_index_file)]
    indptr, indices, position = array("q", [0]), array("i"), 0
    check(indptr.itemsize == 8 and indices.itemsize == 4, "Unexpected C integer sizes")
    with gzip.open(args.annotations_gmt_file, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            fields = gmt_fields(line)
            gene_set_id = fields[0]
            check(position < len(ids) and gene_set_id == ids[position],
                  "The annotations GMT and the gene-set index disagree at gene set %d (%s)" % (position + 1, gene_set_id))
            check(len(fields) >= 3 and fields[1] == "" and line.split()[1:] == fields[2:],
                  "%s: the annotations GMT must have a blank description and whitespace-free genes" % gene_set_id)
            rows = gmt_gene_rows(line, gene_row, gene_map)
            check(rows, "%s shares no gene with the EAGGL factors" % gene_set_id)
            indices.extend(rows)
            indptr.append(len(indices))
            position += 1
    check(position == len(ids), "The annotations GMT has %d gene sets; the index has %d" % (position, len(ids)))
    for path, values, dtype in ((args.output_indptr_file, indptr, np.int64), (args.output_indices_file, indices, np.int32)):
        with open(path, "wb") as fh:  # a file object, because np.save appends .npy to other names
            np.save(fh, np.frombuffer(values, dtype=dtype))
    chunks = []
    for first in range(0, len(ids), args.chunk_size):
        last = min(first + args.chunk_size, len(ids)) - 1
        chunks.append({"chunk": "chunk_%05d" % (len(chunks) + 1), "first_row": first, "n_gene_sets": last - first + 1,
                       "first_gene_set_id": ids[first], "last_gene_set_id": ids[last],
                       "n_entries": indptr[last + 1] - indptr[first]})
    write_tsv(args.output_file, CHUNK_COLUMNS, chunks)
    print("Packed %d gene sets x %d genes (%d memberships) in %d chunks of at most %d"
          % (len(ids), len(genes), len(indices), len(chunks), args.chunk_size))


def read_chunks(path, ids, indptr):
    """[(chunk, first row, size)], checked to be consecutive row ranges covering the gene-set index and the pack."""
    chunks = read_tsv(path)
    check(chunks and list(chunks[0].keys()) == CHUNK_COLUMNS, "Unexpected columns in %s" % path)
    check(len(indptr) == len(ids) + 1, "The pack holds %d gene sets; the index has %d" % (len(indptr) - 1, len(ids)))
    result, end = [], 0
    for chunk in chunks:
        first, size = int(chunk["first_row"]), int(chunk["n_gene_sets"])
        check(first == end and size >= 1 and first + size <= len(ids)
              and (ids[first], ids[first + size - 1]) == (chunk["first_gene_set_id"], chunk["last_gene_set_id"])
              and int(indptr[first + size]) - int(indptr[first]) == int(chunk["n_entries"]),
              "%s does not match the gene-set index and the pack" % chunk["chunk"])
        result.append((chunk["chunk"], first, size))
        end = first + size
    check(end == len(ids), "The chunks cover %d of %d gene sets" % (end, len(ids)))
    return result


def run_eaggl(python, pigean_src, factors_file, gmt_file, gene_map_file, prefix, seed):
    """`python -m eaggl factor` projecting a factors-by-genes file onto a GMT (joint and marginal); returns its params."""
    env = dict(os.environ, PYTHONPATH=pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    command = [python, "-B", "-m", "eaggl", "factor", "--factor-gene-clusters-in", factors_file,
               "--factor-gene-clusters-layout", "factors-by-genes", "--X-in", gmt_file, "--gene-map-in", gene_map_file,
               "--gene-set-projection-mode", "both", "--gene-set-clusters-out", prefix + ".joint.tsv.gz",
               "--gene-set-clusters-marginal-out", prefix + ".marginal.tsv.gz", "--factor-output-scope", "all",
               "--cluster-row-min-max-loading", "0", "--seed", str(seed), "--hide-progress", "--hide-opts",
               "--params-out", prefix + ".params.tsv", "--warnings-file", prefix + ".warnings.txt",
               "--log-file", prefix + ".log"]
    proc = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check(proc.returncode == 0, "eaggl failed on %s:\n%s" % (gmt_file, proc.stdout[-3000:]))
    params = read_params(prefix + ".params.tsv")
    check(params.get("gene_set_projection_mode", [NA])[-1] == "both", "eaggl did not run in both mode")
    return params


def cmd_check_projection(args):
    """Compare projection_kernel with the pinned eaggl on a sample of gene sets, before any trait is projected.

    The sample is --sample-per-library evenly spaced gene sets of every library, plus as many more holding a
    case-mapped gene, written as a GMT from the annotations GMT. For each of --traits, eaggl (the clone at
    --expected-pigean-commit with a clean src/) projects that GMT with the flags of one per-trait run, and the kernel
    projects the same gene sets from the pack. Every joint and marginal %.4g loading and every top factor must be
    identical. The report goes to --output-file only then (otherwise to <output-file>.failed.tsv).
    """
    import projection_kernel as pk
    head = check_pigean_clone(args.repo_dir, args.expected_pigean_commit)
    traits = [t for t in args.traits.split(",") if t]
    check(traits and len(set(traits)) == len(traits), "--traits must list distinct traits")
    check(args.sample_per_library >= 1, "--sample-per-library must be positive")
    genes = read_gene_list(args.genes_file)
    gene_map = read_gene_map(args.gene_map_file)
    factor_index = read_factor_index(args.factor_index_file)
    ids, library_rows = [], defaultdict(list)
    for row in gene_set_index_rows(args.gene_set_index_file):
        library_rows[row["library"]].append(len(ids))
        ids.append(row["gene_set_id"])
    selected = set()
    for rows in library_rows.values():
        selected.update(rows[::max(1, len(rows) // args.sample_per_library)][:args.sample_per_library])

    os.makedirs(args.work_dir, exist_ok=True)
    sample_gmt = os.path.join(args.work_dir, "sample.gmt.gz")
    position, extra, n_mapped = 0, 0, 0
    with gzip.open(args.annotations_gmt_file, "rt", encoding="utf-8") as fh, open_text(sample_gmt, "w") as out:
        for line in fh:
            if not line.strip():
                continue
            has_mapped = any(token in gene_map for token in line.split()[1:])
            if has_mapped and position not in selected and extra < args.sample_per_library:
                selected.add(position)
                extra += 1
            if position in selected:
                out.write(line if line.endswith("\n") else line + "\n")
                n_mapped += has_mapped
            position += 1
    check(position == len(ids), "The annotations GMT has %d gene sets; the index has %d" % (position, len(ids)))
    columns = sorted(selected)
    sample_ids = [ids[j] for j in columns]
    indptr, indices = pk.load_pool(args.indptr_file, args.indices_file)
    check(len(indptr) == len(ids) + 1, "The pack holds %d gene sets; the index has %d" % (len(indptr) - 1, len(ids)))
    V = pk.gene_set_matrix(indptr, indices, columns, len(genes))

    report = []
    for trait in traits:
        prefix = os.path.join(args.work_dir, trait)
        factors_file = prefix + ".factors_by_genes.tsv.gz"
        local = write_trait_factors(args.all_factors_file, factor_index, trait, factors_file)
        factor_ids = [r["factor_id"] for r in local]
        W = read_factor_matrix(factors_file, genes, factor_ids)
        started = time.time()
        params = run_eaggl(args.python, args.pigean_src, factors_file, sample_gmt, args.gene_map_file, prefix + ".eaggl",
                           args.seed)
        eaggl_seconds = time.time() - started
        aligned = params.get("factor_projection_only_gene_set_aligned_genes", [NA])[-1]
        check(aligned == str(len(genes)), "eaggl aligned %s genes for %s; the factor file has %d" % (aligned, trait, len(genes)))
        started = time.time()
        joint, marginal, updates, _ = pk.project(W, V, args.seed)
        kernel_seconds = time.time() - started
        top = pk.top_factors(joint)
        column_to_factor_id = OrderedDict((r["local_eaggl_column"], r["factor_id"]) for r in local)
        eaggl_joint, label_failures = read_projection(prefix + ".eaggl.joint.tsv.gz", column_to_factor_id, sample_ids)
        eaggl_marginal, _ = read_projection(prefix + ".eaggl.marginal.tsv.gz", column_to_factor_id, sample_ids)
        check(label_failures == 0, "%d eaggl labels do not match the factor order for %s" % (label_failures, trait))
        position_of = {factor_id: k for k, factor_id in enumerate(factor_ids)}
        mismatches = Counter()
        for j, gene_set_id in enumerate(sample_ids):
            cluster_id, values = eaggl_joint[gene_set_id]
            mismatches["joint"] += sum(v != "%.4g" % x for v, x in zip(values, joint[j]))
            mismatches["marginal"] += sum(v != "%.4g" % x for v, x in zip(eaggl_marginal[gene_set_id][1], marginal[j]))
            mismatches["top"] += int((position_of[cluster_id] if cluster_id else -1) != top[j])
        report.append({"trait": trait, "n_factors": len(factor_ids), "n_gene_sets": len(sample_ids),
                       "n_case_mapped_gene_sets": n_mapped, "n_values": 2 * len(sample_ids) * len(factor_ids),
                       "joint_mismatches": mismatches["joint"], "marginal_mismatches": mismatches["marginal"],
                       "top_factor_mismatches": mismatches["top"], "kernel_updates": updates,
                       "eaggl_seconds": "%.1f" % eaggl_seconds, "kernel_seconds": "%.3f" % kernel_seconds,
                       "pigean_commit": head, "check_pass": not any(mismatches.values())})
    passed = all(r["check_pass"] for r in report)
    write_tsv(args.output_file if passed else args.output_file + ".failed.tsv", CHECK_COLUMNS, report)
    check(passed, "The projection kernel disagrees with eaggl %s: %s" % (head, "; ".join(
        "%s %s joint, %s marginal, %s top-factor mismatches" % (r["trait"], r["joint_mismatches"], r["marginal_mismatches"],
                                                                r["top_factor_mismatches"]) for r in report if not r["check_pass"])))
    print("The projection kernel equals eaggl %s on %d gene sets (%d with case-mapped genes) for %s"
          % (head[:7], len(sample_ids), n_mapped, ", ".join(traits)))


def read_projection_check(path):
    """The commit of the eaggl that check-projection compared the kernel against (every trait must have passed)."""
    rows = read_tsv(path)
    check(rows and list(rows[0].keys()) == CHECK_COLUMNS, "Unexpected columns in %s" % path)
    check(all(r["check_pass"] == "True" for r in rows), "%s records a failed check" % path)
    commits = {r["pigean_commit"] for r in rows}
    check(len(commits) == 1 and COMMIT_RE.match(next(iter(commits))), "Malformed commits in %s" % path)
    return commits.pop()


def round_loadings(values):
    """The loadings as eaggl writes them (%.4g), as floats: the long files store these and the ranks order them."""
    import numpy as np
    return np.array([float("%.4g" % x) for x in values.tolist()], dtype=float)


def library_ranks(values, id_order, libraries, n_libraries):
    """(rank in factor, rank in library) arrays: ordinal, highest value first, ties by gene-set id (id_order)."""
    import numpy as np
    order = np.lexsort((id_order, -values))
    in_factor = np.empty(len(values), dtype=np.int64)
    in_factor[order] = np.arange(1, len(values) + 1)
    in_library = np.empty(len(values), dtype=np.int64)
    ordered_libraries = libraries[order]
    for code in range(n_libraries):
        members = order[ordered_libraries == code]
        in_library[members] = np.arange(1, len(members) + 1)
    return in_factor, in_library


def cmd_project_trait(args):
    """Project every CFDE gene set onto one trait's factors from the pack, chunk by chunk, and rank the loadings.

    Each chunk is projected as one eaggl run on it would be (projection_kernel.py; check-projection compared the
    kernel with that eaggl). Every factor then gets ordinal ranks over all gene sets and within each library. The
    long file holds every factor's row for each gene set that is among the --top-n of some factor in its library
    (joint or marginal); the top file holds the rows within that factor's own top --top-n.
    """
    import numpy as np
    import projection_kernel as pk
    commit = read_projection_check(args.check_file)
    factors = read_factor_index(args.trait_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS)
    check(all(f["trait"] == args.trait for f in factors), "Factor index is not for trait %s" % args.trait)
    check([f["local_eaggl_column"] for f in factors] == ["Factor%d" % k for k in range(1, len(factors) + 1)],
          "The factor index of %s is not in Factor1..K order" % args.trait)
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn, "Trait %s is not in the KPN map" % args.trait)
    check(kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id == factors[0]["kpn_trait_id"],
          "KPN id for %s disagrees: meta %s, map %s, factor index %s"
          % (args.trait, args.kpn_trait_id, kpn[args.trait]["kpn_trait_id"], factors[0]["kpn_trait_id"]))
    genes = read_gene_list(args.genes_file)
    W = read_factor_matrix(args.trait_factors_file, genes, [f["factor_id"] for f in factors])
    K = len(factors)

    ids, libraries, collections, labels, names, library_names = [], array("i"), [], [], [], {}
    for row in gene_set_index_rows(args.gene_set_index_file):
        ids.append(row["gene_set_id"])
        libraries.append(library_names.setdefault(row["library"], len(library_names)))
        collections.append(row["collection_id"]); labels.append(row["cfde_label"]); names.append(row["gene_set_name"])
    library_of = {v: k for k, v in library_names.items()}
    n = len(ids)
    indptr, indices = pk.load_pool(args.indptr_file, args.indices_file)
    chunks = read_chunks(args.chunks_file, ids, indptr)

    joint, marginal = np.empty((K, n)), np.empty((K, n))
    top_factor = np.empty(n, dtype=np.int64)
    log_lines, warnings, most_updates = [], [], 0
    for name, first, size in chunks:
        started = time.time()
        V = pk.gene_set_matrix(indptr, indices, range(first, first + size), len(genes))
        chunk_joint, chunk_marginal, updates, change = pk.project(W, V, args.seed)
        joint[:, first:first + size] = chunk_joint.T
        marginal[:, first:first + size] = chunk_marginal.T
        top_factor[first:first + size] = pk.top_factors(chunk_joint)
        most_updates = max(most_updates, updates)
        log_lines.append("%s rows %d-%d: %d updates, last relative change %.3g, %.2f s\n"
                         % (name, first, first + size - 1, updates, change, time.time() - started))
        if change >= pk.TOL:
            warnings.append("%s stopped after %d updates at relative change %.3g (tolerance %g), as eaggl does\n"
                            % (name, updates, change, pk.TOL))
    check(np.all(np.isfinite(joint)) and np.all(np.isfinite(marginal)), "Non-finite loadings for %s" % args.trait)
    for k in range(K):
        joint[k], marginal[k] = round_loadings(joint[k]), round_loadings(marginal[k])
    k1_diff = float(np.max(np.abs(joint[0] - marginal[0]))) if K == 1 else 0.0
    check(K != 1 or k1_diff <= LOADING_ROUNDING_TOL, "K=1 joint and marginal loadings differ by %.3g for %s" % (k1_diff, args.trait))

    id_order = np.empty(n, dtype=np.int64)
    id_order[sorted(range(n), key=ids.__getitem__)] = np.arange(n)
    library_codes = np.frombuffer(libraries, dtype=np.int32)
    ranked, union = [], np.zeros(n, dtype=bool)
    for k in range(K):
        joint_factor, joint_library = library_ranks(joint[k], id_order, library_codes, len(library_names))
        marginal_factor, marginal_library = library_ranks(marginal[k], id_order, library_codes, len(library_names))
        union |= (joint_library <= args.top_n) | (marginal_library <= args.top_n)
        ranked.append((joint_factor, joint_library, marginal_factor, marginal_library))
    union_rows = np.nonzero(union)[0].tolist()
    with open_text(args.output_long_file, "w") as long_fh, open_text(args.output_top_file, "w") as top_fh:
        long_fh.write(tsv_line(LONG_COLUMNS))
        top_fh.write(tsv_line(TOP_COLUMNS))
        for k, factor in enumerate(factors):
            joint_factor, joint_library, marginal_factor, marginal_library = ranked[k]
            for i in sorted(union_rows, key=lambda i: (library_of[libraries[i]], joint_library[i])):
                row = {"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "factor_id": factor["factor_id"],
                       "factor": factor["factor"], "factor_label": factor["factor_label"], "gene_set_id": ids[i],
                       "gene_set_name": names[i], "collection_id": collections[i], "cfde_label": labels[i],
                       "library": library_of[libraries[i]], "joint_loading": "%.4g" % joint[k, i],
                       "marginal_loading": "%.4g" % marginal[k, i], "joint_rank_in_factor": int(joint_factor[i]),
                       "marginal_rank_in_factor": int(marginal_factor[i]), "is_joint_top_factor": int(top_factor[i] == k),
                       "joint_rank_in_library": int(joint_library[i]), "marginal_rank_in_library": int(marginal_library[i])}
                long_fh.write(tsv_line(row[c] for c in LONG_COLUMNS))
                if joint_library[i] <= args.top_n or marginal_library[i] <= args.top_n:
                    top_fh.write(tsv_line(row[c] for c in TOP_COLUMNS))
    qc = {"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "kpn_release": kpn[args.trait]["kpn_release"],
          "n_factors": K, "n_gene_sets_index": n, "n_gene_sets_joint": n, "n_gene_sets_marginal": n,
          "ids_equal_index": True, "label_check_failures": 0,
          "k1_joint_marginal_max_absdiff": "%.3g" % k1_diff if K == 1 else NA, "aligned_genes": len(genes),
          "seed": args.seed, "loading_variant": args.loading_variant, "pigean_commit": commit, "qc_pass": True}
    write_tsv(args.output_qc_file, QC_COLUMNS, [qc])
    with open(args.output_commit_file, "w") as fh:
        fh.write(commit + "\n")
    params = [("projection", "eaggl supplied-factor projection, projection_kernel.py"), ("eaggl_commit_checked", commit),
              ("gene_set_projection_mode", "both"), ("seed", args.seed), ("tolerance", pk.TOL),
              ("max_updates", pk.MAX_ITER), ("n_genes", len(genes)), ("n_factors", K), ("n_gene_sets", n),
              ("n_chunks", len(chunks)), ("most_updates", most_updates), ("chunks_at_max_updates", len(warnings))]
    write_tsv(args.output_params_file, ["parameter", "value"], [{"parameter": p, "value": v} for p, v in params])
    with open(args.output_warnings_file, "w") as fh:
        fh.writelines(warnings)
    with open(args.output_log_file, "w") as fh:
        fh.writelines(log_lines)
    print("%s: %d factors x %d gene sets in %d chunks (at most %d updates); %d gene sets in the long file"
          % (args.trait, K, n, len(chunks), most_updates, len(union_rows)))


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
# gene stats, fitted per library. The gene stats come from one all-trait export, indexed once and sliced per trait by
# byte range; the library GMTs are split once from the annotations GMT.

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


LIBRARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LIBRARY_GMT_COLUMNS = ["library", "file", "n_gene_sets", "sha256"]
BETAS_RUN_COLUMNS = ["library", "status", "n_gene_sets", "gene_set_stats_file", "params_file", "log_file", "warnings_file",
                     "response", "pigean_commit", "seconds"]
NO_GENE_SETS = "No gene sets survived the input filters"


def cmd_library_gmts(args):
    """Split the annotations GMT, read once, into one GMT per library (gene-set index order), and list them.

    pigean betas fits each library on its own: its own prior p, prefilter and 5,000-gene-set cap, so a library's betas
    do not depend on how many gene sets the other libraries hold.
    """
    library_of = OrderedDict((row["gene_set_id"], row["library"]) for row in gene_set_index_rows(args.gene_set_index_file))
    libraries = sorted(set(library_of.values()))
    for library in libraries:
        check(LIBRARY_RE.match(library), "Library name %r cannot name a file" % library)
    os.makedirs(args.output_dir, exist_ok=True)
    for name in os.listdir(args.output_dir):  # a previous split's GMTs
        if name.endswith(".gmt.gz"):
            os.unlink(os.path.join(args.output_dir, name))
    paths = {library: os.path.join(args.output_dir, library + ".gmt.gz") for library in libraries}
    writers = {library: DeterministicGzipWriter(paths[library]) for library in libraries}
    counts, ids = Counter(), iter(library_of)
    try:
        with gzip.open(args.annotations_gmt_file, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                gene_set_id = line.split(None, 1)[0].decode("utf-8")
                check(gene_set_id == next(ids, None), "The annotations GMT and the gene-set index disagree at %s" % gene_set_id)
                writers[library_of[gene_set_id]].write(line if line.endswith(b"\n") else line + b"\n")
                counts[library_of[gene_set_id]] += 1
    finally:
        for writer in writers.values():
            writer.close()
    check(next(ids, None) is None, "The annotations GMT has fewer gene sets than the index")
    write_tsv(args.output_file, LIBRARY_GMT_COLUMNS, [
        {"library": library, "file": os.path.abspath(paths[library]), "n_gene_sets": counts[library],
         "sha256": sha256_file(paths[library])} for library in libraries])
    print("Split %d gene sets into %d library GMTs: %s" % (len(library_of), len(libraries), dict(sorted(counts.items()))))


def pigean_betas_command(python, profile, gmt_file, gene_stats_file, response, seed, prefix):
    """One library's `pigean betas` run: the GWAS profile on the trait's gene stats (no outer Gibbs)."""
    return [python, "-B", "-m", "pigean", "betas", "--config", profile, "--X-in", gmt_file,
            "--gene-stats-in", gene_stats_file, "--gene-stats-id-col", "gene", "--gene-stats-log-bf-col", response,
            "--retain-all-beta-uncorrected", "--deterministic", "--seed", str(seed), "--hide-progress", "--hide-opts",
            "--gene-set-stats-out", prefix + ".gene_set_stats.tsv.gz", "--params-out", prefix + ".params.tsv",
            "--warnings-file", prefix + ".warnings.txt", "--log-file", prefix + ".log"]


def read_text(path):
    with open(path) as fh:
        return fh.read()


def read_columns(path, columns):
    """A TSV's rows, refusing any other header (a header-only file has no rows)."""
    with open_text(path) as fh:
        check(fh.readline().rstrip("\n").split("\t") == columns, "Unexpected columns in %s" % path)
    return read_tsv(path)


def cmd_betas_trait(args):
    """One trait's `pigean betas` fits, one per library on that library's GMT alone, and the table of runs.

    Checks the pinned clone first (like record-pigean-commit). A library whose outputs and fingerprint (gene stats,
    GMT, profile, response, seed and pigean commit) are in --work-dir is not refitted. pigean exits without outputs
    when no gene set of a library survives its filters; that library is recorded with status no_gene_sets.
    """
    head = check_pigean_clone(args.repo_dir, args.expected_pigean_commit)
    table = {r["library"]: r for r in read_columns(args.library_gmts_file, LIBRARY_GMT_COLUMNS)}
    excluded = {library for library in args.exclude_libraries.split(",") if library}
    libraries = [library for library in args.libraries.split(",") if library and library not in excluded]
    check(len(set(libraries)) == len(libraries), "Repeated library in --libraries")
    missing = [library for library in libraries if library not in table]
    check(not missing, "No GMT for libraries %s in %s" % (missing, args.library_gmts_file))
    os.makedirs(args.work_dir, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=args.pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    base = "%s %s %s %s %s" % (sha256_file(args.gene_stats_file), sha256_file(args.profile), args.response, args.seed, head)
    rows = []
    for library in libraries:
        entry, prefix = table[library], os.path.join(args.work_dir, library)
        files = {"gene_set_stats_file": prefix + ".gene_set_stats.tsv.gz", "params_file": prefix + ".params.tsv",
                 "log_file": prefix + ".log", "warnings_file": prefix + ".warnings.txt"}
        fingerprint, done, seconds = "%s %s\n" % (entry["sha256"], base), prefix + ".done", "reused"
        if not (os.path.exists(done) and read_text(done) == fingerprint and all(map(os.path.exists, files.values()))):
            for path in list(files.values()) + [done]:
                if os.path.exists(path):
                    os.unlink(path)
            check(sha256_file(entry["file"]) == entry["sha256"], "%s changed since it was split" % entry["file"])
            started = time.time()
            proc = subprocess.run(pigean_betas_command(args.python, args.profile, entry["file"], args.gene_stats_file,
                                                       args.response, args.seed, prefix),
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            check(proc.returncode == 0, "pigean betas failed on %s:\n%s" % (library, proc.stdout[-3000:]))
            if not os.path.exists(files["gene_set_stats_file"]):
                logged = proc.stdout + (read_text(files["log_file"]) if os.path.exists(files["log_file"]) else "")
                check(NO_GENE_SETS in logged, "pigean betas wrote no gene-set stats for %s:\n%s" % (library, proc.stdout[-3000:]))
                write_tsv(files["gene_set_stats_file"], sorted(PIGEAN_GENE_SET_STATS_COLUMNS), [])
                if os.path.exists(files["params_file"]):
                    os.unlink(files["params_file"])
            for path in (files["params_file"], files["log_file"], files["warnings_file"]):
                if not os.path.exists(path):
                    open(path, "w").close()
            seconds = "%.0f" % (time.time() - started)
            with open(done, "w") as fh:
                fh.write(fingerprint)
        status = "fitted" if os.path.getsize(files["params_file"]) else "no_gene_sets"
        rows.append(dict(files, library=library, status=status, n_gene_sets=entry["n_gene_sets"], response=args.response,
                         pigean_commit=head, seconds=seconds))
    write_tsv(args.output_file, BETAS_RUN_COLUMNS, rows)
    print("%d libraries: %s" % (len(rows), ", ".join("%s %s (%s s)" % (r["library"], r["status"], r["seconds"]) for r in rows)))


def cmd_annotate_gene_set_stats(args):
    """Validate one trait's per-library `pigean betas` fits and keep the gene sets PIGEAN analyzed (filter_reason
    kept; the prefiltered ones carry no beta), ranked by beta_uncorrected within their library.

    Every library of --libraries must have exactly one run among the --runs-file tables, at the expected pigean commit
    and regressed on --response; each run's gene sets must belong to its library. p and sigma2 are that library's.
    """
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn and kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id,
          "KPN id for %s disagrees: meta %s, map %s" % (args.trait, args.kpn_trait_id, kpn.get(args.trait, {}).get("kpn_trait_id")))
    runs = [run for path in args.runs_file for run in read_columns(path, BETAS_RUN_COLUMNS)]
    libraries = [library for library in args.libraries.split(",") if library]
    ran = Counter(run["library"] for run in runs)
    check(sorted(ran.elements()) == sorted(libraries), "The runs cover libraries %s; expected each of %s once"
          % (sorted(ran.elements()), sorted(libraries)))
    for run in runs:
        check(run["pigean_commit"] == args.expected_pigean_commit, "pigean ran %s at %s, expected %s"
              % (run["library"], run["pigean_commit"], args.expected_pigean_commit))
        check(run["response"] == args.response and run["status"] in ("fitted", "no_gene_sets"),
              "Unexpected run of %s: %s, %s" % (run["library"], run["response"], run["status"]))
    gene_sets = {}
    for row in gene_set_index_rows(args.gene_set_index_file):
        gene_sets[row["gene_set_id"]] = (row["library"], row["collection_id"], row["cfde_label"])
    rows, seen, reasons = [], set(), Counter()
    for run in runs:
        learned = {"p": NA, "sigma2": NA}
        if run["status"] == "fitted":
            params = read_params(run["params_file"])
            used = params.get("option_gene_stats_log_bf_col", [NA])[-1]
            check(used == args.response, "pigean regressed %s on %s, expected %s" % (run["library"], used, args.response))
            learned = {name: params.get(name, [NA])[-1] for name in ("p", "sigma2")}
        library_rows = []
        with open_text(run["gene_set_stats_file"]) as fh:
            reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
            missing = PIGEAN_GENE_SET_STATS_COLUMNS - set(reader.fieldnames or [])
            check(not missing, "%s lacks the pigean columns %s" % (run["gene_set_stats_file"], sorted(missing)))
            for r in reader:
                gene_set_id = r["Gene_Set"]
                check(gene_set_id in gene_sets, "Unknown gene set %s in %s" % (gene_set_id, run["gene_set_stats_file"]))
                check(gene_set_id not in seen, "Duplicate gene set %s in %s" % (gene_set_id, run["gene_set_stats_file"]))
                library, collection_id, label = gene_sets[gene_set_id]
                check(library == run["library"], "%s is in %s, not %s" % (gene_set_id, library, run["library"]))
                seen.add(gene_set_id)
                reasons[r["filter_reason"]] += 1
                if r["filter_reason"] != "kept":
                    continue
                for name in ("beta_uncorrected", "beta", "avg_postp"):
                    check(math.isfinite(float(r[name])), "Non-finite %s for %s" % (name, gene_set_id))
                library_rows.append({"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "gene_set_id": gene_set_id,
                                     "collection_id": collection_id, "cfde_label": label, "library": library,
                                     "n_genes": r["N"], "beta_uncorrected": r["beta_uncorrected"], "beta": r["beta"],
                                     "avg_postp": r["avg_postp"], "response": args.response, **learned})
        library_rows.sort(key=lambda row: (-float(row["beta_uncorrected"]), row["gene_set_id"]))
        for rank, row in enumerate(library_rows, 1):
            row["library_rank"] = rank
        rows.extend(library_rows)
    rows.sort(key=lambda row: (row["library"], row["library_rank"]))
    write_tsv(args.output_file, GENE_SET_STATS_COLUMNS, rows)
    print("%s: %d of %d gene sets analyzed by PIGEAN in %d libraries (%s)"
          % (args.trait, len(rows), len(seen), len(runs), dict(sorted(reasons.items()))))


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

    p = sub.add_parser("pack-annotations", help="Read the CFDE gene sets once into the packed matrix; list the chunks")
    for name in ("annotations-gmt-file", "gene-set-index-file", "gene-map-file", "genes-file", "output-indptr-file",
                 "output-indices-file", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--chunk-size", type=int, required=True)
    p.set_defaults(func=cmd_pack_annotations)

    p = sub.add_parser("check-projection", help="Compare the projection kernel with the pinned eaggl on sample gene sets")
    for name in ("python", "pigean-src", "repo-dir", "expected-pigean-commit", "annotations-gmt-file",
                 "gene-set-index-file", "gene-map-file", "genes-file", "indptr-file", "indices-file", "all-factors-file",
                 "factor-index-file", "traits", "work-dir", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--sample-per-library", type=int, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(func=cmd_check_projection)

    p = sub.add_parser("project-trait", help="Project one trait onto every packed gene set and rank the loadings")
    for name in ("check-file", "chunks-file", "indptr-file", "indices-file", "genes-file", "trait-factors-file",
                 "trait-factor-index-file", "gene-set-index-file", "trait-kpn-map-file", "trait", "kpn-trait-id",
                 "loading-variant", "output-long-file", "output-top-file", "output-qc-file", "output-commit-file",
                 "output-params-file", "output-warnings-file", "output-log-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--top-n", type=int, default=50)
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

    p = sub.add_parser("library-gmts", help="Split the annotations GMT into one GMT per library")
    for name in ("annotations-gmt-file", "gene-set-index-file", "output-dir", "output-file"):
        p.add_argument("--" + name, required=True)
    p.set_defaults(func=cmd_library_gmts)

    p = sub.add_parser("betas-trait", help="Run pigean betas for one trait on each library's GMT")
    for name in ("python", "pigean-src", "repo-dir", "expected-pigean-commit", "profile", "library-gmts-file",
                 "libraries", "gene-stats-file", "response", "work-dir", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--exclude-libraries", default="")
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(func=cmd_betas_trait)

    p = sub.add_parser("annotate-gene-set-stats", help="Validate and rank one trait's per-library pigean betas fits")
    p.add_argument("--runs-file", action="append", required=True)
    for name in ("libraries", "gene-set-index-file", "trait-kpn-map-file", "trait", "kpn-trait-id", "response",
                 "expected-pigean-commit", "output-file"):
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
