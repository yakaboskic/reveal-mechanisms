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


def open_bytes(path, mode="rb"):
    """A binary handle; gzip (deterministic when writing) only for a name ending in .gz."""
    if path.endswith(".gz"):
        return DeterministicGzipWriter(path) if "w" in mode else gzip.open(path, "rb")
    return open(path, mode)


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


TRAIT_FACTOR_COLUMNS = ["factor_id", "gene", "loading"]


def trait_factors(all_factors_file, factor_index_rows, trait):
    """(genes, [(factor_id, [loading text per gene])], trait factor index rows): one trait's rows of the all-factors
    table, in factor-index order (eaggl's Factor1..K)."""
    wanted = [r for r in factor_index_rows if r["trait"] == trait]
    check(wanted, "No factors for trait %s" % trait)
    wanted_ids = {r["factor_id"]: r for r in wanted}
    factors = []
    with open_text(all_factors_file) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        check(header[0] == "Factor", "%s must start with Factor" % all_factors_file)
        for line in fh:
            factor_id = line.split("\t", 1)[0]
            if factor_id in wanted_ids:
                values = line.rstrip("\n").split("\t")[1:]
                check(len(values) == len(header) - 1, "Row %s of %s has %d values" % (factor_id, all_factors_file, len(values)))
                factors.append((factor_id, values))
    check([f for f, _ in factors] == [r["factor_id"] for r in wanted], "Factor rows for %s are missing or out of order" % trait)
    rows = []
    for k, (factor_id, _) in enumerate(factors, 1):
        g = wanted_ids[factor_id]
        rows.append({"local_eaggl_column": "Factor%d" % k, "factor_id": factor_id, "trait": g["trait"],
                     "kpn_trait_id": g["kpn_trait_id"], "factor": g["factor"], "factor_number": g["factor_number"],
                     "factor_label": g["factor_label"], "global_eaggl_column": g["global_eaggl_column"]})
    return header[1:], factors, rows


def write_factors_long(path, genes, factors):
    """The trait's factors as factor_id, gene, loading: every nonzero loading, its text unchanged."""
    with open_text(path, "w") as out:
        out.write(tsv_line(TRAIT_FACTOR_COLUMNS))
        for factor_id, values in factors:
            for gene, value in zip(genes, values):
                if float(value) != 0.0:
                    out.write(tsv_line([factor_id, gene, value]))


def write_factors_wide(path, genes, factors):
    """eaggl's factors-by-genes layout (header cell Factor, every gene column, so eaggl never drops a gene set)."""
    with open_text(path, "w") as out:
        out.write("\t".join(["Factor"] + genes) + "\n")
        for factor_id, values in factors:
            out.write("\t".join([factor_id] + values) + "\n")


def cmd_trait_factors(args):
    genes, factors, rows = trait_factors(args.all_factors_file, read_factor_index(args.factor_index_file), args.trait)
    write_factors_long(args.output_file, genes, factors)
    write_tsv(args.output_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS, rows)
    print("%s: %d factors" % (args.trait, len(rows)))


def read_factor_matrix(path, genes, factor_ids):
    """A trait's factors (factor_id, gene, loading) as the genes x K float64 matrix eaggl loads: the genes in the factor
    tables' column order, the factors in factor-index order, zero where no loading is listed."""
    import numpy as np
    gene_row = {gene: i for i, gene in enumerate(genes)}
    column = {factor_id: k for k, factor_id in enumerate(factor_ids)}
    matrix, seen = np.zeros((len(genes), len(factor_ids))), set()
    with open_text(path) as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        check(next(reader, None) == TRAIT_FACTOR_COLUMNS, "Unexpected columns in %s" % path)
        for factor_id, gene, value in reader:
            check(factor_id in column and gene in gene_row, "Unknown factor %s or gene %s in %s" % (factor_id, gene, path))
            check((factor_id, gene) not in seen, "Repeated loading %s/%s in %s" % (factor_id, gene, path))
            seen.add((factor_id, gene))
            matrix[gene_row[gene], column[factor_id]] = float(value)
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
              "aligned_genes", "seed", "loading_variant", "pigean_commit", "n_chunks", "most_updates",
              "chunks_at_max_updates", "qc_pass"]


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
    with open_text(args.annotations_gmt_file) as fh:
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
    sample_gmt = os.path.join(args.work_dir, "sample.gmt")
    position, extra, n_mapped = 0, 0, 0
    with open_text(args.annotations_gmt_file) as fh, open_text(sample_gmt, "w") as out:
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
        factors_file = prefix + ".factors_by_genes.tsv"  # eaggl's input
        trait_genes, factors, local = trait_factors(args.all_factors_file, factor_index, trait)
        check(trait_genes == genes, "The gene columns of %s differ from %s" % (args.all_factors_file, args.genes_file))
        write_factors_wide(factors_file, trait_genes, factors)
        write_factors_long(prefix + ".factors.tsv", trait_genes, factors)  # what factors_trait_cmd writes and the kernel reads
        factor_ids = [r["factor_id"] for r in local]
        W = read_factor_matrix(prefix + ".factors.tsv", genes, factor_ids)
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
          "seed": args.seed, "loading_variant": args.loading_variant, "pigean_commit": commit, "n_chunks": len(chunks),
          "most_updates": most_updates, "chunks_at_max_updates": len(warnings), "qc_pass": True}
    write_tsv(args.output_qc_file, QC_COLUMNS, [qc])
    with open(args.output_log_file, "w") as fh:
        fh.write("eaggl supplied-factor projection (projection_kernel.py), checked against eaggl %s; seed %d, tolerance %g, "
                 "at most %d updates per chunk\n" % (commit, args.seed, pk.TOL, pk.MAX_ITER))
        fh.writelines(log_lines)
        fh.writelines("WARNING: " + line for line in warnings)
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
    with open_bytes(args.output_file, "wb") as out:
        out.write(header)
        out.writelines(lines)
    print("%s: %d genes" % (args.trait, len(lines)))


LIBRARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LIBRARY_GMT_COLUMNS = ["library", "file", "n_gene_sets", "sha256"]
BETAS_RUN_COLUMNS = ["trait", "kpn_trait_id", "library", "status", "n_gene_sets", "n_kept", "n_nonzero_beta_uncorrected",
                     "p", "sigma2", "response", "pigean_commit", "seconds", "gene_set_stats_file", "params_file", "log_file",
                     "warnings_file"]
# pigean's two ways of saying that none of a library's gene sets survived its filters for the trait: a clean exit before
# the betas, or a bail from the betas once the post-read filters (gene-set size, beta adjustment) left none.
NO_GENE_SETS = ("No gene sets survived the input filters", "No gene sets are left!")


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
        if name.endswith((".gmt", ".gmt.gz")):
            os.unlink(os.path.join(args.output_dir, name))
    paths = {library: os.path.join(args.output_dir, library + ".gmt") for library in libraries}
    writers = {library: open_bytes(paths[library], "wb") for library in libraries}
    counts, ids = Counter(), iter(library_of)
    try:
        with open_bytes(args.annotations_gmt_file) as fh:
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


def pigean_betas_command(python, profile, gmt_file, gene_stats_file, response, seed, prefix, max_initial):
    """One library's `pigean betas` run: the GWAS profile on the trait's gene stats (no outer Gibbs). With max_initial,
    pigean keeps at most that many of the library's gene sets (the most significant by nominal p) for pruning, the
    hyperparameters and the betas."""
    cap = [] if max_initial is None else ["--max-num-gene-sets-initial", str(max_initial)]
    return [python, "-B", "-m", "pigean", "betas", "--config", profile, "--X-in", gmt_file] + cap + [
            "--gene-stats-in", gene_stats_file, "--gene-stats-id-col", "gene", "--gene-stats-log-bf-col", response,
            "--retain-all-beta-uncorrected", "--deterministic", "--seed", str(seed), "--hide-progress", "--hide-opts",
            "--gene-set-stats-out", prefix + ".gene_set_stats.tsv", "--params-out", prefix + ".params.tsv",
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
    """One trait's gene-set betas: a `pigean betas` fit per library on that library's GMT alone, then the gene sets
    PIGEAN analyzed (filter_reason kept; the prefiltered ones carry no beta), ranked by beta_uncorrected within their
    library.

    Checks the KPN id and the pinned clone first. A library whose outputs and fingerprint (gene stats, GMT, profile,
    response, seed, initial cap and pigean commit) are in --work-dir is not refitted. When none of a library's gene sets
    survives pigean's filters for the trait, pigean stops without outputs and the library is recorded as no_gene_sets.
    Writes the runs (one row per library), every row pigean wrote (--output-all-file; with --retain-all-beta-uncorrected
    the filtered gene sets too, with their filter_reason) and the ranked betas.
    """
    check(args.max_num_gene_sets_initial is None or args.max_num_gene_sets_initial >= 1,
          "--max-num-gene-sets-initial must be positive")
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn and kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id,
          "KPN id for %s disagrees: meta %s, map %s" % (args.trait, args.kpn_trait_id, kpn.get(args.trait, {}).get("kpn_trait_id")))
    head = check_pigean_clone(args.repo_dir, args.expected_pigean_commit)
    table = {r["library"]: r for r in read_columns(args.library_gmts_file, LIBRARY_GMT_COLUMNS)}
    excluded = {library for library in args.exclude_libraries.split(",") if library}
    libraries = [library for library in args.libraries.split(",") if library and library not in excluded]
    check(libraries and len(set(libraries)) == len(libraries), "--libraries must list distinct libraries")
    missing = [library for library in libraries if library not in table]
    check(not missing, "No GMT for libraries %s in %s" % (missing, args.library_gmts_file))
    os.makedirs(args.work_dir, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=args.pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    base = "%s %s %s %s %s %s" % (sha256_file(args.gene_stats_file), sha256_file(args.profile), args.response, args.seed,
                                  args.max_num_gene_sets_initial, head)
    runs = []
    for library in libraries:
        entry, prefix = table[library], os.path.join(args.work_dir, library)
        files = {"gene_set_stats_file": prefix + ".gene_set_stats.tsv", "params_file": prefix + ".params.tsv",
                 "log_file": prefix + ".log", "warnings_file": prefix + ".warnings.txt"}
        fingerprint, done, seconds = "%s %s\n" % (entry["sha256"], base), prefix + ".done", "reused"
        if not (os.path.exists(done) and read_text(done) == fingerprint and all(map(os.path.exists, files.values()))):
            for path in list(files.values()) + [done]:
                if os.path.exists(path):
                    os.unlink(path)
            check(sha256_file(entry["file"]) == entry["sha256"], "%s changed since it was split" % entry["file"])
            started = time.time()
            proc = subprocess.run(pigean_betas_command(args.python, args.profile, entry["file"], args.gene_stats_file,
                                                       args.response, args.seed, prefix, args.max_num_gene_sets_initial),
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            logged = proc.stdout + (read_text(files["log_file"]) if os.path.exists(files["log_file"]) else "")
            none_left = any(message in logged for message in NO_GENE_SETS)
            check(proc.returncode == 0 or none_left, "pigean betas failed on %s (exit %d):\n%s"
                  % (library, proc.returncode, proc.stdout[-3000:]))
            if proc.returncode != 0 or not os.path.exists(files["gene_set_stats_file"]):
                check(none_left, "pigean betas wrote no gene-set stats for %s:\n%s" % (library, proc.stdout[-3000:]))
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
        runs.append(dict(files, trait=args.trait, kpn_trait_id=args.kpn_trait_id, library=library, status=status,
                         n_gene_sets=entry["n_gene_sets"], response=args.response, pigean_commit=head, seconds=seconds))

    wanted = set(libraries)
    gene_sets = {}
    for row in gene_set_index_rows(args.gene_set_index_file):
        if row["library"] in wanted:
            gene_sets[row["gene_set_id"]] = (row["library"], row["collection_id"], row["cfde_label"])
    rows, reasons, all_columns = annotate_runs(args, runs, gene_sets)
    write_tsv(args.output_runs_file, BETAS_RUN_COLUMNS, runs)
    write_tsv(args.output_file, GENE_SET_STATS_COLUMNS, rows)
    if args.output_all_file:
        with open_text(args.output_all_file, "w") as out:
            out.write(tsv_line(["library"] + all_columns))
            for run in runs:
                with open_text(run["gene_set_stats_file"]) as fh:
                    header = fh.readline().rstrip("\n").split("\t")
                    if run["status"] == "fitted":
                        check(header == all_columns, "pigean wrote other columns for %s: %s" % (run["library"], header))
                        for line in fh:
                            out.write(run["library"] + "\t" + line)
    print("%s: %d gene sets analyzed in %d libraries (%s); %s" % (
        args.trait, len(rows), len(runs), ", ".join("%s %s" % (r["library"], r["status"] if r["status"] != "fitted" else
                                                             "%s kept" % r["n_kept"]) for r in runs), dict(sorted(reasons.items()))))


def annotate_runs(args, runs, gene_sets):
    """The kept gene sets of every run with their KPN and collection ids, ranked by beta_uncorrected within the library;
    fills each run's n_kept, n_nonzero_beta_uncorrected, p and sigma2. Refuses another response or commit, an unknown or
    repeated gene set and a gene set outside its run's library. Returns (rows, filter_reason counts, pigean columns)."""
    rows, seen, reasons, all_columns = [], set(), Counter(), None
    for run in runs:
        check(run["pigean_commit"] == args.expected_pigean_commit, "pigean ran %s at %s, expected %s"
              % (run["library"], run["pigean_commit"], args.expected_pigean_commit))
        learned = {"p": NA, "sigma2": NA}
        if run["status"] == "fitted":
            params = read_params(run["params_file"])
            used = params.get("option_gene_stats_log_bf_col", [NA])[-1]
            check(used == args.response, "pigean regressed %s on %s, expected %s" % (run["library"], used, args.response))
            learned = {name: params.get(name, [NA])[-1] for name in ("p", "sigma2")}
        kept = []
        with open_text(run["gene_set_stats_file"]) as fh:
            reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
            missing = PIGEAN_GENE_SET_STATS_COLUMNS - set(reader.fieldnames or [])
            check(not missing, "%s lacks the pigean columns %s" % (run["gene_set_stats_file"], sorted(missing)))
            if run["status"] == "fitted":
                all_columns = all_columns or list(reader.fieldnames)
            for r in reader:
                gene_set_id = r["Gene_Set"]
                check(gene_set_id not in seen, "Duplicate gene set %s in %s" % (gene_set_id, run["gene_set_stats_file"]))
                check(gene_set_id in gene_sets, "Unknown gene set %s in %s" % (gene_set_id, run["gene_set_stats_file"]))
                check(gene_sets[gene_set_id][0] == run["library"], "%s is in %s, not %s"
                      % (gene_set_id, gene_sets[gene_set_id][0], run["library"]))
                seen.add(gene_set_id)
                reasons[r["filter_reason"]] += 1
                if r["filter_reason"] == "kept":
                    for name in ("beta_uncorrected", "beta", "avg_postp"):
                        check(math.isfinite(float(r[name])), "Non-finite %s for %s" % (name, gene_set_id))
                    kept.append(r)
        # Below --update-hyper-min-gene-sets pigean keeps its default prior and records none: report the one it used.
        for name in ("p", "sigma2"):
            used = {r.get(name + "_used") for r in kept}
            if learned[name] == NA and len(used) == 1 and None not in used:
                learned[name] = used.pop()
        library_rows = []
        for r in kept:
            library, collection_id, label = gene_sets[r["Gene_Set"]]
            library_rows.append({"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "gene_set_id": r["Gene_Set"],
                                 "collection_id": collection_id, "cfde_label": label, "library": library,
                                 "n_genes": r["N"], "beta_uncorrected": r["beta_uncorrected"], "beta": r["beta"],
                                 "avg_postp": r["avg_postp"], "response": args.response, **learned})
        library_rows.sort(key=lambda row: (-float(row["beta_uncorrected"]), row["gene_set_id"]))
        for rank, row in enumerate(library_rows, 1):
            row["library_rank"] = rank
        run.update(learned, n_kept=len(library_rows),
                   n_nonzero_beta_uncorrected=sum(1 for row in library_rows if float(row["beta_uncorrected"]) != 0))
        rows.extend(library_rows)
    rows.sort(key=lambda row: (row["library"], row["library_rank"]))
    return rows, reasons, all_columns or sorted(PIGEAN_GENE_SET_STATS_COLUMNS)


BETAS_MANIFEST_COLUMNS = ["trait", "kpn_trait_id", "library", "status", "n_gene_sets", "n_kept",
                          "n_nonzero_beta_uncorrected", "p", "sigma2", "seconds"]


def cmd_betas_collect(args):
    """Every trait's betas runs in one table: each trait of the KPN map must have one run per library of --libraries
    (less --exclude-libraries), all at the expected pigean commit."""
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    excluded = {library for library in args.exclude_libraries.split(",") if library}
    libraries = sorted(library for library in args.libraries.split(",") if library and library not in excluded)
    by_trait = defaultdict(list)
    for path in args.runs_file:
        for run in read_columns(path, BETAS_RUN_COLUMNS):
            by_trait[run["trait"]].append(run)
    check(set(by_trait) == set(kpn), "Runs cover %d traits; the project has %d (missing: %s)"
          % (len(by_trait), len(kpn), sorted(set(kpn) - set(by_trait))[:10]))
    rows = []
    for trait in sorted(by_trait):
        runs = by_trait[trait]
        check(sorted(r["library"] for r in runs) == libraries, "%s has runs for %s, expected %s"
              % (trait, sorted(r["library"] for r in runs), libraries))
        for run in sorted(runs, key=lambda r: r["library"]):
            check(run["pigean_commit"] == args.expected_pigean_commit, "%s/%s ran at pigean %s, expected %s"
                  % (trait, run["library"], run["pigean_commit"], args.expected_pigean_commit))
            check(run["kpn_trait_id"] == kpn[trait]["kpn_trait_id"], "KPN id of %s disagrees with the map" % trait)
            rows.append(run)
    write_tsv(args.output_file, BETAS_MANIFEST_COLUMNS, rows)
    summary = []
    for library in libraries:
        library_runs = [r for r in rows if r["library"] == library]
        kept = sorted(int(r["n_kept"]) for r in library_runs)
        summary.append("%s %d/%d fitted, median %d kept" % (library, sum(r["status"] == "fitted" for r in library_runs),
                                                            len(library_runs), kept[len(kept) // 2]))
    print("%d traits x %d libraries: %s" % (len(by_trait), len(libraries), "; ".join(summary)))


# -------------------------------------------------------------------------------------------------
# Factor-trait links: eaggl's trait linkage and factor-PheWAS of every factor against every PIGEAN phenotype

LINKAGE_PHENOTYPE_COLUMNS = ["phenotype", "kpn_trait_id", "kpn_match", "gwas_source_category", "phenotype_name",
                             "is_anchor", "n_genes", "n_genes_kept"]


def kpn_phenotype_matcher(registry_rows):
    """A function phenotype -> (KPN registry row or None, the registry column it matched or "none"): legacy_phenotype_id
    (the anchors' rule), else pigean_id. A phenotype matching several rows of that column is refused."""
    check("pigean_id" in registry_rows[0], "The KPN registry has no pigean_id column")
    hits = {column: defaultdict(list) for column in ("legacy_phenotype_id", "pigean_id")}
    for row in registry_rows:
        for column, by_name in hits.items():
            if row[column]:
                by_name[row[column]].append(row)

    def match(phenotype):
        for column, by_name in hits.items():
            rows = by_name.get(phenotype, [])
            check(len(rows) <= 1, "%s matches several KPN %s rows: %s" % (phenotype, column, [r["portal_id"] for r in rows]))
            if rows:
                return rows[0], column
        return None, "none"
    return match


def case_target(universe):
    """A memoized function symbol -> the EAGGL gene it names: itself if it is one, else the one EAGGL gene with its
    upper-case form (e.g. C10orf71 -> C10ORF71), else None."""
    upper = defaultdict(set)
    for gene in universe:
        upper[gene.upper()].add(gene)
    memo = {}

    def target(symbol):
        if symbol not in memo:
            hits = upper.get(symbol.upper(), ())
            memo[symbol] = symbol if symbol in universe else next(iter(hits)) if len(hits) == 1 else None
        return memo[symbol]
    target.memo = memo
    return target


def one_row_per_gene(rows, target):
    """[(symbol, row)] of one phenotype -> the rows to keep: one per EAGGL gene, its exact spelling when listed, else the
    first case variant (PIGEAN lists some genes under both spellings, e.g. a rare disease's C9ORF72 with its direct
    support and C9orf72 with a prior only). Rows naming no EAGGL gene are kept. Returns (kept rows, dropped count)."""
    exact = {symbol for symbol, _ in rows if target(symbol) == symbol}
    kept, taken, dropped = [], set(), 0
    for symbol, row in rows:
        gene = target(symbol)
        if gene is not None and gene != symbol:
            if gene in exact or gene in taken:
                dropped += 1
                continue
            taken.add(gene)
        kept.append(row)
    return kept, dropped


def cmd_linkage_phenotype_stats(args):
    """The rows of the all-trait PIGEAN gene stats that eaggl's trait linkage reads, in one pass: combined above
    --min-combined (its --trait-linkage-threshold) and combined, log_bf and prior all numbers (eaggl's reader skips a
    row with any other value, e.g. the export's prior-only genes with log_bf NA). Also every phenotype with its KPN id
    and the case-only map of the export's gene symbols onto the EAGGL genes (eaggl's --gene-map-in).

    eaggl reads only these rows of the export (0.5% of it), and with nothing left to filter its factor-PheWAS reuses them
    instead of re-reading the whole file once per 300 phenotypes. The factor-PheWAS hits are the rows above the same
    cutoff, so both statistics are what eaggl computes from the whole export, except that its re-read would count the
    rows its first read skipped (log_bf NA) as hits. A phenotype keeps one row per EAGGL gene (one_row_per_gene), so the
    map never gives eaggl two values for a gene.
    """
    check(math.isfinite(args.min_combined), "--min-combined must be finite")
    anchors = read_trait_kpn_map(args.trait_kpn_map_file)
    match_kpn = kpn_phenotype_matcher(load_kpn_registry(args.registry_file))
    universe = set(read_gene_list(args.genes_file))
    target = case_target(universe)
    identity = source_identity(args.gene_stats_file)
    phenotypes, kept_rows, total, dropped, unreadable = OrderedDict(), 0, 0, 0, 0
    with open(args.gene_stats_file, "rb") as fh, open(args.output_file, "wb") as out:
        header_line = fh.readline()
        header = header_line.decode("utf-8").rstrip("\n").split("\t")
        check(header[:2] == ["phenotype", "gene"] and {"combined", "log_bf", "prior"} <= set(header),
              "%s must start with phenotype, gene and have combined, log_bf and prior: %s" % (args.gene_stats_file, header))
        n_tabs = len(header) - 1
        value_cols = [header.index(name) for name in ("combined", "log_bf", "prior")]
        combined_col, split_at = value_cols[0], max(value_cols) + 1
        out.write(header_line)
        current, counts, buffered = None, None, []

        def flush():
            nonlocal kept_rows, dropped
            kept, n_dropped = one_row_per_gene(buffered, target)
            out.writelines(kept)
            counts[1] += len(kept)
            kept_rows += len(kept)
            dropped += n_dropped
            buffered.clear()
        for line in fh:
            check(line.count(b"\t") == n_tabs and line.endswith(b"\n"), "Malformed row %d of %s: %r"
                  % (total + 2, args.gene_stats_file, line[:200]))
            total += 1
            fields = line.split(b"\t", split_at)
            symbol = fields[1].decode("utf-8")
            target(symbol)  # every symbol of the export, for the gene map
            if fields[0] != current:
                if current is not None:
                    flush()
                current = fields[0]
                name = current.decode("utf-8")
                check(name not in phenotypes, "The rows of %s in %s are not contiguous" % (name, args.gene_stats_file))
                counts = phenotypes[name] = [0, 0]
            counts[0] += 1
            try:
                combined = float(fields[combined_col])
            except ValueError:
                continue
            if combined > args.min_combined:
                try:
                    for col in value_cols[1:]:
                        float(fields[col])
                except ValueError:
                    unreadable += 1
                    continue
                buffered.append((symbol, line))
        if current is not None:
            flush()
    check(source_identity(args.gene_stats_file) == identity, "%s changed while it was read" % args.gene_stats_file)
    missing = sorted(set(anchors) - set(phenotypes))
    check(not missing, "%d traits have no gene stats in %s: %s" % (len(missing), args.gene_stats_file, missing[:20]))
    rows = []
    for name, (n_genes, n_kept) in phenotypes.items():
        registry_row, column = match_kpn(name)
        if name in anchors:
            check(column == "legacy_phenotype_id" and registry_row["portal_id"] == anchors[name]["kpn_trait_id"],
                  "The KPN id of %s disagrees with the trait map" % name)
        rows.append({"phenotype": name, "kpn_trait_id": registry_row["portal_id"] if registry_row else NA,
                     "kpn_match": column, "gwas_source_category": registry_row["gwas_source_category"] if registry_row else NA,
                     "phenotype_name": registry_row["phenotype_name"] if registry_row else NA,
                     "is_anchor": name in anchors, "n_genes": n_genes, "n_genes_kept": n_kept})
    write_tsv(args.output_phenotypes_file, LINKAGE_PHENOTYPE_COLUMNS, rows)
    gene_map = {symbol: gene for symbol, gene in target.memo.items() if gene is not None and gene != symbol}
    with open(args.output_gene_map_file, "w") as fh:  # eaggl's format: two columns, no header
        for symbol in sorted(gene_map):
            fh.write("%s\t%s\n" % (symbol, gene_map[symbol]))
    exact = sum(1 for symbol, gene in target.memo.items() if gene == symbol)
    matched = Counter(r["kpn_match"] for r in rows)
    print("Kept %d of %d rows (combined > %g; %d more skipped as eaggl does, log_bf or prior not a number) for %d "
          "phenotypes (%d anchors; KPN ids: %s); %d of %d EAGGL genes in the export, %d symbols mapped by case (%d rows "
          "dropped for a gene's other spelling)" % (kept_rows, total, args.min_combined, unreadable, len(rows), len(anchors),
                                                    dict(sorted(matched.items())), exact, len(universe), len(gene_map),
                                                    dropped))


def read_linkage_phenotypes(path):
    rows = read_columns(path, LINKAGE_PHENOTYPE_COLUMNS)
    check(rows, "%s lists no phenotypes" % path)
    return OrderedDict((r["phenotype"], r) for r in rows)


# eaggl's trait-factor links (--trait-factor-links-out) and factor-PheWAS (--factor-phewas-stats-out) columns we keep
EAGGL_LINK_COLUMNS = ["trait", "factor", "is_anchor", "nnls_loading", "cosine_loading"]
EAGGL_PHEWAS_COLUMNS = ["Factor", "Pheno", "mode", "anchor_covariate", "threshold_cutoff", "se_type", "beta", "P",
                        "P_onesided", "Z", "SE"]
LINKAGE_COLUMNS = ["trait", "kpn_trait_id", "factor_id", "factor_label", "phenotype", "phenotype_kpn_trait_id",
                   "phenotype_name", "is_own_trait", "is_atlas_trait", "nnls_loading", "cosine_loading", "phewas_beta",
                   "phewas_se", "phewas_z", "phewas_p", "phewas_p_onesided"]
LINKAGE_QC_COLUMNS = ["trait", "kpn_trait_id", "n_factors", "n_phenotypes", "n_rows", "n_genes", "n_genes_with_stats",
                      "min_combined", "phewas_mode", "anchor_covariate", "se_type", "pigean_commit", "eaggl_seconds"]


def write_factors_wide_from_long(path, genes, factor_ids, long_file):
    """eaggl's factors-by-genes layout from a trait's long factor file: its loading text where listed, 0 elsewhere."""
    column = {factor_id: k for k, factor_id in enumerate(factor_ids)}
    gene_row = {gene: i for i, gene in enumerate(genes)}
    values = [["0"] * len(genes) for _ in factor_ids]
    with open_text(long_file) as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        check(next(reader, None) == TRAIT_FACTOR_COLUMNS, "Unexpected columns in %s" % long_file)
        for factor_id, gene, value in reader:
            check(factor_id in column and gene in gene_row, "Unknown factor %s or gene %s in %s" % (factor_id, gene, long_file))
            values[column[factor_id]][gene_row[gene]] = value
    write_factors_wide(path, genes, list(zip(factor_ids, values)))


def eaggl_linkage_command(python, factors_file, phewas_file, gene_stats_file, gene_map_file, min_combined, seed, prefix):
    """`python -m eaggl factor` in projection-only mode: the trait linkage of every phenotype of phewas_file onto the
    supplied factors, and the factor-PheWAS adjusted for the anchor's direct support (log_bf of gene_stats_file).
    Phenotype rows count as hits, and enter the linkage, above min_combined. gene_map_file renames the stats' gene
    symbols onto the factors' (both inputs)."""
    stats_columns = ["--gene-stats-id-col", "gene", "--gene-stats-log-bf-col", "log_bf", "--gene-stats-combined-col",
                     "combined", "--gene-stats-prior-col", "prior"]
    return [python, "-B", "-m", "eaggl", "factor", "--factor-gene-clusters-in", factors_file,
            "--factor-gene-clusters-layout", "factors-by-genes", "--gene-map-in", gene_map_file,
            "--gene-phewas-stats-in", phewas_file] + [x.replace("--gene-stats", "--gene-phewas-stats") for x in stats_columns] + [
            "--gene-phewas-stats-pheno-col", "phenotype", "--trait-linkage-threshold", repr(min_combined),
            "--gene-stats-in", gene_stats_file] + stats_columns + [
            "--trait-factor-links-out", prefix + ".trait_factor_links.tsv",
            "--run-factor-phewas", "--factor-phewas-thresholded-combined-cutoff", repr(min_combined),
            "--factor-phewas-stats-out", prefix + ".factor_phewas_stats.tsv",
            "--seed", str(seed), "--hide-progress", "--hide-opts", "--params-out", prefix + ".params.tsv",
            "--warnings-file", prefix + ".warnings.txt", "--log-file", prefix + ".log"]


def read_eaggl_table(path, columns, key_columns, factor_columns):
    """{(phenotype, FactorN): {column: text}} of an eaggl output, refusing missing columns, unknown factors and repeats."""
    check(os.path.exists(path), "eaggl wrote no %s" % path)
    result = {}
    with open_text(path) as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        missing = set(columns) - set(reader.fieldnames or [])
        check(not missing, "%s lacks the columns %s" % (path, sorted(missing)))
        for row in reader:
            pheno, factor = row[key_columns[0]], row[key_columns[1]]
            check(factor in factor_columns, "Unknown factor %s in %s" % (factor, path))
            check((pheno, factor) not in result, "Repeated %s/%s in %s" % (pheno, factor, path))
            result[(pheno, factor)] = row
    return result


def p_value_key(text):
    value = float(text)
    return value if math.isfinite(value) else 1.0


def cmd_linkage_trait(args):
    """Every factor of one trait linked to every PIGEAN phenotype, by the pinned eaggl (projection-only).

    The trait linkage projects each phenotype's support (combined, as a probability) onto the trait's factors with the
    fixed-W NNLS of the gene-set projection (nnls_loading; descriptive). The factor-PheWAS regresses each phenotype's
    hits (combined > --min-combined) on one factor's gene loadings plus the anchor trait's direct support (log_bf), with
    robust (HC3) standard errors: is the phenotype enriched in the factor beyond the anchor's own genes? eaggl's files
    stay in --work-dir; the output joins them per factor and phenotype with the factor ids and the phenotypes' KPN ids.
    """
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    check(args.trait in kpn and kpn[args.trait]["kpn_trait_id"] == args.kpn_trait_id,
          "KPN id for %s disagrees: meta %s, map %s" % (args.trait, args.kpn_trait_id, kpn.get(args.trait, {}).get("kpn_trait_id")))
    head = check_pigean_clone(args.repo_dir, args.expected_pigean_commit)
    phenotypes = read_linkage_phenotypes(args.phenotypes_file)
    check(args.trait in phenotypes, "%s is not a phenotype of %s" % (args.trait, args.phenotypes_file))
    local = read_factor_index(args.trait_factor_index_file, TRAIT_FACTOR_INDEX_COLUMNS)
    check(all(r["trait"] == args.trait for r in local), "%s lists another trait's factors" % args.trait_factor_index_file)
    check([r["local_eaggl_column"] for r in local] == ["Factor%d" % k for k in range(1, len(local) + 1)],
          "%s is not in Factor1..K order" % args.trait_factor_index_file)
    by_column = OrderedDict((r["local_eaggl_column"], r) for r in local)

    os.makedirs(args.work_dir, exist_ok=True)
    prefix = os.path.join(args.work_dir, args.trait)
    factors_file = prefix + ".factors_by_genes.tsv"
    genes = read_gene_list(args.genes_file)
    write_factors_wide_from_long(factors_file, genes, [r["factor_id"] for r in local], args.trait_factors_file)
    # The anchor's gene stats on the factor genes only (one row each, as linkage-phenotype-stats keeps them): eaggl adds
    # any other gene of --gene-stats-in to its genes, and its trait linkage then refuses the factor basis. A factor gene
    # without stats gets eaggl's fill (the mean log_bf).
    gene_map, universe = read_gene_map(args.gene_map_file), set(genes)
    target = lambda symbol: symbol if symbol in universe else gene_map.get(symbol)
    stats_file, listed = prefix + ".gene_stats.tsv", []
    with open_text(args.gene_stats_file) as fh:
        header = fh.readline()
        check(header.rstrip("\n").split("\t")[:2] == ["phenotype", "gene"], "Unexpected columns in %s" % args.gene_stats_file)
        for line in fh:
            fields = line.split("\t", 2)
            check(fields[0] == args.trait, "%s holds rows of %s" % (args.gene_stats_file, fields[0]))
            if target(fields[1]) is not None:
                listed.append((fields[1], line))
    kept, _ = one_row_per_gene(listed, target)
    covered = [target(line.split("\t", 2)[1]) for line in kept]
    check(len(set(covered)) == len(covered), "%s lists a gene twice" % args.gene_stats_file)
    with open_text(stats_file, "w") as out:
        out.write(header)
        out.writelines(kept)
    outputs = [prefix + suffix for suffix in (".trait_factor_links.tsv", ".factor_phewas_stats.tsv", ".params.tsv")]
    for path in outputs:
        if os.path.exists(path):
            os.unlink(path)
    env = dict(os.environ, PYTHONPATH=args.pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    started = time.time()
    proc = subprocess.run(eaggl_linkage_command(args.python, factors_file, args.phewas_stats_file, stats_file,
                                                args.gene_map_file, args.min_combined, args.seed, prefix),
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    seconds = time.time() - started
    check(proc.returncode == 0, "eaggl failed for %s:\n%s" % (args.trait, proc.stdout[-3000:]))

    links = read_eaggl_table(outputs[0], EAGGL_LINK_COLUMNS, ("trait", "factor"), by_column)
    phewas = read_eaggl_table(outputs[1], EAGGL_PHEWAS_COLUMNS, ("Pheno", "Factor"), by_column)
    check(set(links) == set(phewas), "eaggl's trait links and factor-PheWAS cover different phenotypes for %s (%d vs %d rows)"
          % (args.trait, len(links), len(phewas)))
    names = sorted({pheno for pheno, _ in links})
    unknown = [pheno for pheno in names if pheno not in phenotypes]
    check(not unknown, "eaggl linked phenotypes missing from %s: %s" % (args.phenotypes_file, unknown[:10]))
    check(len(links) == len(names) * len(local), "eaggl linked %d rows, expected %d phenotypes x %d factors"
          % (len(links), len(names), len(local)))
    settings = {(r["mode"], r["anchor_covariate"], r["se_type"], float(r["threshold_cutoff"])) for r in phewas.values()}
    check(len(settings) == 1, "Several factor-PheWAS models in %s: %s" % (outputs[1], sorted(settings)))
    mode, anchor_covariate, se_type, cutoff = settings.pop()
    check(cutoff == float("%.3g" % args.min_combined), "eaggl used the hit cutoff %g, expected %g" % (cutoff, args.min_combined))

    rows = []
    for column, factor in by_column.items():
        factor_rows = []
        for pheno in names:
            link, stats, info = links[(pheno, column)], phewas[(pheno, column)], phenotypes[pheno]
            factor_rows.append({"trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "factor_id": factor["factor_id"],
                                "factor_label": factor["factor_label"], "phenotype": pheno,
                                "phenotype_kpn_trait_id": info["kpn_trait_id"], "phenotype_name": info["phenotype_name"],
                                "is_own_trait": pheno == args.trait, "is_atlas_trait": info["is_anchor"] == "True",
                                "nnls_loading": link["nnls_loading"], "cosine_loading": link["cosine_loading"],
                                "phewas_beta": stats["beta"], "phewas_se": stats["SE"], "phewas_z": stats["Z"],
                                "phewas_p": stats["P"], "phewas_p_onesided": stats["P_onesided"]})
        factor_rows.sort(key=lambda r: (p_value_key(r["phewas_p_onesided"]), r["phenotype"]))
        rows.extend(factor_rows)
    write_tsv(args.output_file, LINKAGE_COLUMNS, rows)
    write_tsv(args.output_qc_file, LINKAGE_QC_COLUMNS, [{
        "trait": args.trait, "kpn_trait_id": args.kpn_trait_id, "n_factors": len(local), "n_phenotypes": len(names),
        "n_rows": len(rows), "n_genes": len(genes), "n_genes_with_stats": len(covered),
        "min_combined": "%g" % args.min_combined, "phewas_mode": mode,
        "anchor_covariate": anchor_covariate, "se_type": se_type, "pigean_commit": head, "eaggl_seconds": "%.0f" % seconds}])
    print("%s: %d factors x %d phenotypes linked in %.0f s (%s, anchor covariate %s)"
          % (args.trait, len(local), len(names), seconds, mode, anchor_covariate))


FACTOR_LINK_COLUMNS = ["trait", "kpn_trait_id", "factor_id", "factor_label", "phenotype", "phenotype_kpn_trait_id",
                       "phenotype_name", "is_own_trait", "is_atlas_trait", "nnls_loading", "phewas_beta",
                       "phewas_p_onesided", "phewas_q"]
FACTOR_LINK_SUMMARY_COLUMNS = ["trait", "kpn_trait_id", "factor_id", "factor_label", "n_phenotypes", "n_linked",
                               "n_linked_kpn_traits", "n_linked_atlas_traits", "own_trait_q", "top_linked"]
TOP_LINKED = 5


def bh_q_values(p_values):
    """Benjamini-Hochberg q-values (a non-finite p counts as 1)."""
    import numpy as np
    p = np.where(np.isfinite(p_values), p_values, 1.0)
    order = np.argsort(p, kind="mergesort")
    ranked = p[order] * len(p) / np.arange(1, len(p) + 1)
    q = np.empty(len(p))
    q[order] = np.minimum(np.minimum.accumulate(ranked[::-1])[::-1], 1.0)
    return q


def cmd_linkage_collect(args):
    """Every trait's factor-phenotype links in one table: the factor-PheWAS one-sided p-values of all traits get
    Benjamini-Hochberg q-values together, and a factor is linked to the phenotypes with q <= --max-q. Also one summary
    row per factor and the per-trait QC. Every trait of the KPN map must be present, at the pinned commit, with one
    factor-PheWAS model."""
    import numpy as np
    check(0 < args.max_q <= 1, "--max-q must be in (0, 1]")
    kpn = read_trait_kpn_map(args.trait_kpn_map_file)
    qc = OrderedDict()
    for path in args.qc_file:
        for row in read_columns(path, LINKAGE_QC_COLUMNS):
            check(row["trait"] not in qc, "Two QC rows for %s" % row["trait"])
            qc[row["trait"]] = row
    check(set(qc) == set(kpn), "QC covers %d traits; the project has %d (missing: %s)"
          % (len(qc), len(kpn), sorted(set(kpn) - set(qc))[:10]))
    for trait, row in qc.items():
        check(row["pigean_commit"] == args.expected_pigean_commit, "%s ran at pigean %s, expected %s"
              % (trait, row["pigean_commit"], args.expected_pigean_commit))
        check(row["kpn_trait_id"] == kpn[trait]["kpn_trait_id"], "KPN id of %s disagrees with the map" % trait)
    models = {(r["min_combined"], r["phewas_mode"], r["anchor_covariate"], r["se_type"]) for r in qc.values()}
    check(len(models) == 1, "The traits ran different factor-PheWAS models: %s" % sorted(models))

    files = {}
    for path in args.links_file:
        with open_text(path) as fh:
            check(fh.readline().rstrip("\n").split("\t") == LINKAGE_COLUMNS, "Unexpected columns in %s" % path)
            first = fh.readline().split("\t", 1)[0]
        check(first in qc and first not in files, "%s holds no rows or a repeated trait (%r)" % (path, first))
        files[first] = path
    check(set(files) == set(qc), "Links cover %d traits; the QC %d" % (len(files), len(qc)))
    order = sorted(files)

    def rows_of(trait):
        with open_text(files[trait]) as fh:
            for row in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                check(row["trait"] == trait, "%s holds rows of %s" % (files[trait], row["trait"]))
                yield row

    p_values = array("d")
    for trait in order:
        before = len(p_values)
        p_values.extend(p_value_key(row["phewas_p_onesided"]) for row in rows_of(trait))
        check(len(p_values) - before == int(qc[trait]["n_rows"]), "%s has %d rows; its QC says %s"
              % (files[trait], len(p_values) - before, qc[trait]["n_rows"]))
    q_values = bh_q_values(np.frombuffer(p_values, dtype=float))

    summary, position, n_links = [], 0, 0
    with open_text(args.output_file, "w") as out:
        out.write(tsv_line(FACTOR_LINK_COLUMNS))
        for trait in order:
            factors = OrderedDict()
            for row in rows_of(trait):
                q = float(q_values[position])
                position += 1
                factor = factors.setdefault(row["factor_id"], {"row": row, "n": 0, "linked": [], "own_q": NA})
                factor["n"] += 1
                if row["is_own_trait"] == "True":
                    factor["own_q"] = "%.3g" % q
                if q <= args.max_q:
                    row["phewas_q"] = "%.3g" % q
                    out.write(tsv_line(row[c] for c in FACTOR_LINK_COLUMNS))
                    factor["linked"].append(row)
                    n_links += 1
            for factor_id, factor in factors.items():
                linked = factor["linked"]
                others = [r for r in linked if r["is_own_trait"] != "True"]
                summary.append({
                    "trait": trait, "kpn_trait_id": factor["row"]["kpn_trait_id"], "factor_id": factor_id,
                    "factor_label": factor["row"]["factor_label"], "n_phenotypes": factor["n"], "n_linked": len(linked),
                    "n_linked_kpn_traits": len({r["phenotype_kpn_trait_id"] for r in others if r["phenotype_kpn_trait_id"] != NA}),
                    "n_linked_atlas_traits": sum(r["is_atlas_trait"] == "True" for r in others), "own_trait_q": factor["own_q"],
                    "top_linked": "; ".join(r["phenotype_name"] if r["phenotype_name"] != NA else r["phenotype"]
                                            for r in others[:TOP_LINKED])})
    write_tsv(args.output_summary_file, FACTOR_LINK_SUMMARY_COLUMNS, summary)
    write_tsv(args.output_manifest_file, LINKAGE_QC_COLUMNS, [qc[trait] for trait in order])
    print("%d links (q <= %g) of %d factor-phenotype tests; %d of %d factors linked to another KPN trait"
          % (n_links, args.max_q, len(q_values), sum(1 for r in summary if r["n_linked_kpn_traits"]), len(summary)))


# -------------------------------------------------------------------------------------------------
# Provenance audit: the Translator artifact-provenance portal against the release's gene sets and collections

PORTAL_COLLECTION_COLUMNS = ["collection_id", "name", "description", "n_gene_sets", "list_status"]
PORTAL_GENE_SET_COLUMNS = ["gene_set_id", "name", "collection_id"]
PROVENANCE_COLLECTION_COLUMNS = ["collection_id", "status", "portal_name", "cfde_label", "library", "in_portal", "in_snapshot",
                                 "in_release", "n_portal_gene_sets", "n_release_gene_sets", "n_release_on_portal",
                                 "n_release_in_same_portal_collection"]
PROVENANCE_MAP_COLUMNS = ["release_collection_id", "cfde_label", "library", "portal_collection_id", "portal_name", "n_gene_sets"]
PROVENANCE_GENE_SET_COLUMNS = ["gene_set_id", "status", "name", "release_collection_id", "cfde_label", "library",
                               "portal_collection_id"]
PORTAL_COLLECTION_RE = re.compile(r'data-app-route="gene_set/list/id=dapper%3A(GeneSetCollection\.[A-Za-z0-9_-]+)">\s*(.*?)\s*</a>'
                                  r'(?:\s*<div class="description">(.*?)</div>)?', re.S)
PORTAL_GENE_SET_RE = re.compile(r'data-app-route="gene_set/details/id=dapper%3A(GeneSet\.[A-Za-z0-9_-]+)">\s*(.*?)\s*</a>', re.S)
PORTAL_COUNT_RE = re.compile(r"([0-9,]+) gene sets? found")


def https_context():
    """An SSL context with a CA bundle: Python's own when SSL_CERT_FILE or its default file exists, else the system
    bundle, else certifi's. The pigean venv's uv-built Python looks for /etc/ssl/cert.pem, which RHEL does not have."""
    import ssl
    paths = ssl.get_default_verify_paths()
    if os.environ.get("SSL_CERT_FILE") or (paths.cafile and os.path.exists(paths.cafile)):
        return ssl.create_default_context()
    for bundle in ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def portal_get(base_url, route, context=None, attempts=3, timeout=600):
    """(HTTP status, body) of one portal page; retries network errors and 5xx, not 4xx."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(base_url.rstrip("/") + "/" + route, headers={"User-Agent": "reveal-mechanisms-lap/provenance-audit"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:
                return error.code, error.read().decode("utf-8", "replace")
        except OSError:
            if attempt == attempts:
                raise
        time.sleep(5 * attempt)


def portal_text(value):
    import html
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def portal_collections(page):
    """[(collection id, name, description)] of the portal's gene-set home page, in page order."""
    rows = [("dapper:" + cid, portal_text(name), portal_text(description)) for cid, name, description in PORTAL_COLLECTION_RE.findall(page)]
    check(rows, "The portal's gene-set home page lists no collections")
    check(len({r[0] for r in rows}) == len(rows), "The portal lists a collection twice")
    return rows


def portal_gene_sets(page):
    """([(gene set id, name)], the count the page states or None) of one collection's list page."""
    found = PORTAL_COUNT_RE.search(page)
    return ([("dapper:" + gid, portal_text(name)) for gid, name in PORTAL_GENE_SET_RE.findall(page)],
            int(found.group(1).replace(",", "")) if found else None)


def cmd_provenance_audit(args):
    """Which gene sets and collections the Translator artifact-provenance portal holds, against the mirrored CFDE
    snapshot and the release's gene sets (the gene-set index the release build publishes).

    Reads the portal's gene-set home page (every collection) and each collection's list page (its gene-set ids), with
    --workers requests at a time, into --work-dir (portal_collections.tsv, portal_gene_sets.tsv). Gene sets are matched
    by id across every portal collection: the portal can hold a release gene set under another collection id. Writes one
    row per collection of the portal or the snapshot, the portal collections that hold each release collection's gene
    sets, and the gene sets in one place only (release gene sets missing from the portal; portal gene sets of a release
    collection that the release lacks).
    """
    from concurrent.futures import ThreadPoolExecutor
    import urllib.parse
    check(args.workers >= 1, "--workers must be positive")
    snapshot = OrderedDict((r["collection_id"], r) for r in read_tsv(args.snapshot_index_file))
    release = OrderedDict((r["collection_id"], r) for r in read_tsv(args.cfde_index_file))
    check(set(release) <= set(snapshot), "The release's collections are not all in the snapshot index")
    context = https_context()
    status, page = portal_get(args.portal_url, "home/gene_set", context)
    check(status == 200, "The portal's gene-set home page returned HTTP %d" % status)
    collections = portal_collections(page)
    os.makedirs(args.work_dir, exist_ok=True)

    def fetch(collection):
        status, body = portal_get(args.portal_url, "gene_set/list/id=" + urllib.parse.quote(collection[0], safe=""), context)
        return (status,) + (portal_gene_sets(body) if status == 200 else ([], None))
    portal_of, also_in, counts, listed, failures = {}, defaultdict(list), Counter(), [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool, \
            open_text(os.path.join(args.work_dir, "portal_gene_sets.tsv"), "w") as out:
        out.write(tsv_line(PORTAL_GENE_SET_COLUMNS))
        for (cid, name, description), (status, gene_sets, stated) in zip(collections, pool.map(fetch, collections)):
            list_status = "ok" if status == 200 and (stated is None or stated == len(gene_sets)) else (
                "http_%d" % status if status != 200 else "count_mismatch_%d_of_%d" % (len(gene_sets), stated))
            if list_status != "ok":
                failures.append((cid, list_status))
            for gid, gene_set_name in gene_sets:
                out.write(tsv_line([gid, gene_set_name, cid]))
                if gid in portal_of:  # a gene set the portal lists in several collections
                    also_in[gid].append(cid)
                else:
                    portal_of[gid] = cid
            counts[cid] = len(gene_sets)
            listed.append({"collection_id": cid, "name": name, "description": description, "n_gene_sets": len(gene_sets),
                           "list_status": list_status})
    write_tsv(os.path.join(args.work_dir, "portal_collections.tsv"), PORTAL_COLLECTION_COLUMNS, listed)
    portal_names = {r["collection_id"]: r["name"] for r in listed}

    # The release's gene sets: where the portal has each one.
    per_release, mapping, release_ids, n_release, n_missing = defaultdict(Counter), defaultdict(Counter), set(), 0, 0
    with open_text(args.output_gene_sets_file, "w") as out:
        out.write(tsv_line(PROVENANCE_GENE_SET_COLUMNS))
        for row in gene_set_index_rows(args.gene_set_index_file):
            cid, gid = row["collection_id"], row["gene_set_id"]
            check(cid in release, "%s is in collection %s, which the release index lacks" % (gid, cid))
            n_release += 1
            release_ids.add(gid)
            counts_of = per_release[cid]
            counts_of["release"] += 1
            where = portal_of.get(gid)
            if where is None:
                n_missing += 1
                out.write(tsv_line([gid, "release_only", row["gene_set_name"], cid, row["cfde_label"], row["library"], NA]))
                continue
            holders = [where] + also_in.get(gid, [])
            counts_of["on_portal"] += 1
            counts_of["same_collection"] += cid in holders
            for holder in holders:
                mapping[cid][holder] += 1
        portal_only = 0
        with open_text(os.path.join(args.work_dir, "portal_gene_sets.tsv")) as fh:
            for portal_row in csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                cid = portal_row["collection_id"]
                if cid in release and portal_row["gene_set_id"] not in release_ids:
                    portal_only += 1
                    out.write(tsv_line([portal_row["gene_set_id"], "portal_only", portal_row["name"], cid, release[cid]["label"],
                                        release[cid]["library"], cid]))
    write_tsv(args.output_map_file, PROVENANCE_MAP_COLUMNS, [
        {"release_collection_id": cid, "cfde_label": release[cid]["label"], "library": release[cid]["library"],
         "portal_collection_id": where, "portal_name": portal_names.get(where, NA), "n_gene_sets": n}
        for cid in release for where, n in sorted(mapping[cid].items(), key=lambda item: (-item[1], item[0]))])

    rows = []
    for cid in list(snapshot) + [c for c in portal_names if c not in snapshot]:
        in_portal, in_snapshot, in_release = cid in portal_names, cid in snapshot, cid in release
        source = snapshot.get(cid, {})
        if in_release:
            status = "in_both" if in_portal else ("gene_sets_elsewhere_on_portal" if per_release[cid]["on_portal"] else "release_only")
        else:
            status = "snapshot_excluded" if in_snapshot else "portal_only"
        rows.append({"collection_id": cid, "status": status, "portal_name": portal_names.get(cid, NA),
                     "cfde_label": source.get("label", NA), "library": source.get("library", NA), "in_portal": in_portal,
                     "in_snapshot": in_snapshot, "in_release": in_release, "n_portal_gene_sets": counts[cid] if in_portal else NA,
                     "n_release_gene_sets": per_release[cid]["release"] if in_release else NA,
                     "n_release_on_portal": per_release[cid]["on_portal"] if in_release else NA,
                     "n_release_in_same_portal_collection": per_release[cid]["same_collection"] if in_release else NA})
    write_tsv(args.output_collections_file, PROVENANCE_COLLECTION_COLUMNS, rows)
    statuses = Counter(r["status"] for r in rows)
    print("Portal: %d collections, %d gene sets (%d in several collections; %d list pages failed: %s). Release: %d "
          "collections, %d gene sets; %d gene sets missing from the portal, %d portal gene sets of release collections missing "
          "from the release. Collections: %s" % (len(collections), len(portal_of), len(also_in), len(failures), failures[:5],
                                                  len(release), n_release, n_missing, portal_only, dict(sorted(statuses.items()))))


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
                 "loading-variant", "output-long-file", "output-top-file", "output-qc-file", "output-log-file"):
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

    p = sub.add_parser("betas-trait", help="Fit pigean betas for one trait on each library's GMT and rank the gene sets")
    for name in ("python", "pigean-src", "repo-dir", "expected-pigean-commit", "profile", "library-gmts-file",
                 "libraries", "gene-stats-file", "gene-set-index-file", "trait-kpn-map-file", "trait", "kpn-trait-id",
                 "response", "work-dir", "output-runs-file", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--exclude-libraries", default="")
    p.add_argument("--output-all-file", help="Every row pigean wrote, with a library column")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--max-num-gene-sets-initial", type=int, default=None,
                   help="pigean keeps at most this many of a library's gene sets (by nominal p) before pruning")
    p.set_defaults(func=cmd_betas_trait)

    p = sub.add_parser("betas-collect", help="Every trait's betas runs in one table")
    p.add_argument("--runs-file", action="append", required=True)
    for name in ("trait-kpn-map-file", "libraries", "expected-pigean-commit", "output-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--exclude-libraries", default="")
    p.set_defaults(func=cmd_betas_collect)

    p = sub.add_parser("linkage-phenotype-stats", help="Keep the PIGEAN gene stats rows eaggl's trait linkage reads")
    for name in ("gene-stats-file", "registry-file", "trait-kpn-map-file", "genes-file", "output-file",
                 "output-phenotypes-file", "output-gene-map-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--min-combined", type=float, required=True)
    p.set_defaults(func=cmd_linkage_phenotype_stats)

    p = sub.add_parser("linkage-trait", help="Link one trait's factors to every PIGEAN phenotype with eaggl")
    for name in ("python", "pigean-src", "repo-dir", "expected-pigean-commit", "phewas-stats-file", "phenotypes-file",
                 "gene-map-file", "gene-stats-file", "genes-file", "trait-factors-file", "trait-factor-index-file",
                 "trait-kpn-map-file", "trait", "kpn-trait-id", "work-dir", "output-file", "output-qc-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--min-combined", type=float, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(func=cmd_linkage_trait)

    p = sub.add_parser("linkage-collect", help="Every trait's factor-phenotype links, with q-values over all traits")
    p.add_argument("--links-file", action="append", required=True)
    p.add_argument("--qc-file", action="append", required=True)
    for name in ("trait-kpn-map-file", "expected-pigean-commit", "output-file", "output-summary-file",
                 "output-manifest-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--max-q", type=float, required=True)
    p.set_defaults(func=cmd_linkage_collect)

    p = sub.add_parser("provenance-audit", help="Audit the Translator provenance portal against the release's gene sets")
    for name in ("portal-url", "snapshot-index-file", "cfde-index-file", "gene-set-index-file", "work-dir",
                 "output-collections-file", "output-map-file", "output-gene-sets-file"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--workers", type=int, default=4, help="Portal requests at a time")
    p.set_defaults(func=cmd_provenance_audit)

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
