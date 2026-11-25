#!/usr/bin/env python3
"""Helpers of the PIGEAN claims-collection LAP pipeline (config/pigean_collection.cfg).

The pipeline turns one PIGEAN portal run plus CFDE gene-set betas into the file layout the claims repository reads
(claims-repository docs/publishing-a-pigean-run.md):

    data/<library>/<trait>/combined_gene_stats.json        one row per gene            (mouse_msigdb only)
    data/<library>/<trait>/combined_gene_set_stats.json    one row per gene set
    data/<library>/<trait>/gene_gene_set_stats.json        one row per (gene, gene set)

Subcommands, in run order: source-list (used by build_meta_yaml.py), source-fetch, genesets-build, betas-trait,
format-trait, collect, deposit. Python 3.9, standard library only; S3 goes through the aws CLI.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter, OrderedDict, defaultdict

NA = "NA"
SOURCE_FILES = OrderedDict([("gene_stats", "gs.out"), ("gene_set_stats", "gss.out"), ("membership", "ggss.out")])
KINDS = OrderedDict([("gene_stats", "combined_gene_stats"), ("gene_set_stats", "combined_gene_set_stats"),
                     ("membership", "gene_gene_set_stats")])
TRAIT_COLUMNS = ["instance", "trait"] + ["%s_%s" % (role, field) for role in SOURCE_FILES for field in ("size", "etag")]
SOURCE_MANIFEST_COLUMNS = ["instance", "trait", "role", "file", "size", "sha256"]
GENE_STATS_COLUMNS = ["phenotype", "gene", "combined", "log_bf", "prior"]
GENE_SET_INDEX_COLUMNS = ["gene_set_id", "gene_set_name", "collection_id", "cfde_label", "library", "n_genes"]
LIBRARY_GMT_COLUMNS = ["library", "file", "n_gene_sets", "sha256"]
BETAS_RUN_COLUMNS = ["instance", "trait", "library", "status", "n_gene_sets", "n_kept", "n_nonzero_beta_uncorrected",
                     "p", "sigma2", "response", "pigean_commit", "seconds", "gene_set_stats_file", "params_file",
                     "log_file", "warnings_file"]
FILES_COLUMNS = ["instance", "trait", "library", "role", "kind", "relative_path", "rows", "dropped_rows", "size",
                 "sha256"]
PIGEAN_GENE_SET_STATS_COLUMNS = {"Gene_Set", "filter_reason", "N", "beta", "beta_uncorrected"}
# pigean's two ways of saying that none of a library's gene sets survived its filters for the trait.
NO_GENE_SETS = ("No gene sets survived the input filters", "No gene sets are left!")
LIBRARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorkflowError(Exception):
    pass


def check(condition, message):
    if not condition:
        raise WorkflowError(message)


def read_tsv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE))


def read_columns(path, columns):
    with open(path) as fh:
        check(fh.readline().rstrip("\n").split("\t") == columns, "Unexpected columns in %s" % path)
    return read_tsv(path)


def write_tsv(path, columns, rows):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\t".join(columns) + "\n")
        for row in rows:
            fh.write("\t".join(str(row.get(c, NA)) for c in columns) + "\n")
    os.replace(tmp, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text(path):
    with open(path) as fh:
        return fh.read()


def git_output(repo_dir, *args):
    return subprocess.run(["git", "--no-optional-locks", "-C", repo_dir] + list(args), check=True,
                          stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()


def check_pigean_clone(repo_dir, expected_commit, patch_file=None):
    """The pinned pigean checkout must be at the expected commit, and its src/ must be clean or, with patch_file, differ
    from the commit by exactly that patch (`git diff HEAD -- src` equal to the file, no untracked files). Returns
    (commit, the patch's sha256 or "none")."""
    head = git_output(repo_dir, "rev-parse", "HEAD")
    check(head == expected_commit, "pigean checkout %s is at %s, expected %s" % (repo_dir, head, expected_commit))
    status = git_output(repo_dir, "status", "--porcelain", "--untracked-files=all", "--", "src")
    if not patch_file:
        check(not status, "pigean checkout %s has local changes under src/:\n%s" % (repo_dir, status))
        return head, "none"
    check("??" not in [line[:2] for line in status.splitlines()], "pigean checkout %s has untracked files under src/" % repo_dir)
    diff = subprocess.run(["git", "--no-optional-locks", "-C", repo_dir, "diff", "HEAD", "--", "src"], check=True,
                          stdout=subprocess.PIPE).stdout
    with open(patch_file, "rb") as fh:
        check(diff == fh.read(), "pigean checkout %s does not differ from %s by exactly %s (apply it with "
              "`git -C %s apply %s`)" % (repo_dir, expected_commit[:7], patch_file, repo_dir, os.path.abspath(patch_file)))
    return head, sha256_file(patch_file)


def split_s3(url):
    match = re.match(r"^s3://([^/]+)/(.*)$", url)
    check(match, "Not an s3:// URL: %s" % url)
    return match.group(1), match.group(2)


def aws_json(*args):
    proc = subprocess.run(["aws"] + list(args) + ["--output", "json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    check(proc.returncode == 0, "aws %s failed: %s" % (" ".join(args), proc.stderr.strip()[-2000:]))
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def s3_objects(url):
    """{key: (size, etag)} of every object under an s3:// prefix."""
    bucket, prefix = split_s3(url)
    objects, token = {}, None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--max-keys", "1000",
                "--no-paginate"]
        if token:
            args += ["--continuation-token", token]
        page = aws_json(*args)
        for item in page.get("Contents", []):
            objects[item["Key"]] = (int(item["Size"]), item["ETag"].strip('"'))
        token = page.get("NextContinuationToken")
        if not token:
            return objects


def instance_name(trait):
    """A LAP-safe instance name for a trait key (keys such as `Female_infertility_(all_causes)` hold shell syntax)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", trait)


def list_source_traits(source_url, library):
    """The traits of the PIGEAN run under source_url (<trait>/<library>/{gs,gss,ggss}.out) that have all three files.
    Returns (rows for the traits table, [(trait, missing files)] for the others)."""
    bucket, prefix = split_s3(source_url)
    check(prefix.endswith("/"), "The source prefix must end with /: %s" % source_url)
    found = defaultdict(dict)
    for key, (size, etag) in s3_objects(source_url).items():
        parts = key[len(prefix):].split("/")
        if len(parts) == 3 and parts[1] == library:
            found[parts[0]][parts[2]] = (size, etag)
    rows, incomplete = [], []
    for trait in sorted(found):
        missing = [name for name in SOURCE_FILES.values() if name not in found[trait]]
        if missing:
            incomplete.append((trait, missing))
            continue
        check(trait == trait.strip() and "/" not in trait and trait, "Trait key %r cannot name a folder" % trait)
        row = {"instance": instance_name(trait), "trait": trait}
        for role, name in SOURCE_FILES.items():
            row[role + "_size"], row[role + "_etag"] = found[trait][name]
        rows.append(row)
    names = Counter(row["instance"] for row in rows)
    clash = sorted(name for name, n in names.items() if n > 1)
    check(not clash, "Trait keys collide as instance names: %s" % clash[:10])
    return rows, incomplete


def trait_row(traits_file, instance):
    rows = [r for r in read_columns(traits_file, TRAIT_COLUMNS) if r["instance"] == instance]
    check(len(rows) == 1, "Instance %s is not in %s" % (instance, traits_file))
    return rows[0]


def source_path(source_dir, library, trait, role):
    return os.path.join(source_dir, trait, library, SOURCE_FILES[role])


# -------------------------------------------------------------------------------------------------
# source_: the PIGEAN run, downloaded once

def cmd_source_list(args):
    rows, incomplete = list_source_traits(args.source_url, args.source_library)
    write_tsv(args.output_file, TRAIT_COLUMNS, rows)
    print("%d traits with %s under %s; %d without all three files%s" % (
        len(rows), ", ".join(SOURCE_FILES.values()), args.source_url, len(incomplete),
        (": " + ", ".join(t for t, _ in incomplete[:40])) if incomplete else ""))


def cmd_source_fetch(args):
    """Download every listed trait's gs.out, gss.out and ggss.out (aws s3 sync; resumable), then check each file's
    size against the listing and record its sha256."""
    traits = read_columns(args.traits_file, TRAIT_COLUMNS)
    os.makedirs(args.source_dir, exist_ok=True)
    command = ["aws", "s3", "sync", args.source_url, args.source_dir, "--only-show-errors", "--exclude", "*"]
    for name in SOURCE_FILES.values():
        command += ["--include", "*/%s/%s" % (args.source_library, name)]
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    check(proc.returncode == 0, "aws s3 sync failed:\n%s" % proc.stdout[-3000:])
    rows = []
    for trait in traits:
        for role in SOURCE_FILES:
            path = source_path(args.source_dir, args.source_library, trait["trait"], role)
            check(os.path.isfile(path), "Not downloaded: %s" % path)
            size = os.path.getsize(path)
            check(str(size) == trait[role + "_size"], "%s is %d bytes; the listing says %s (the run changed: rebuild "
                  "the meta)" % (path, size, trait[role + "_size"]))
            etag = trait[role + "_etag"]
            if "-" not in etag:  # a single-part upload's ETag is the file's MD5
                check(md5_file(path) == etag, "%s does not match its S3 ETag %s" % (path, etag))
            rows.append({"instance": trait["instance"], "trait": trait["trait"], "role": role, "file": path,
                         "size": size, "sha256": sha256_file(path)})
    write_tsv(args.output_file, SOURCE_MANIFEST_COLUMNS, rows)
    print("%d files of %d traits, %.1f GB, in %s" % (len(rows), len(traits), sum(r["size"] for r in rows) / 1e9,
                                                    args.source_dir))


# -------------------------------------------------------------------------------------------------
# genesets_: the CFDE gene sets the betas are fitted on, one GMT per library

def wanted_libraries(args):
    excluded = {library for library in (args.exclude_libraries or "").split(",") if library}
    libraries = [library for library in args.libraries.split(",") if library and library not in excluded]
    check(libraries and len(set(libraries)) == len(libraries), "--libraries must list distinct libraries")
    for library in libraries:
        check(LIBRARY_RE.match(library), "Library name %r cannot name a folder" % library)
    return libraries


GENE_SET_RE = re.compile(r"^dapper:GeneSet\.[A-Za-z0-9_-]+$")
COLLECTION_RE = re.compile(r"^dapper:GeneSetCollection\.[A-Za-z0-9_-]+$")
GMT_NAME, DOCUMENT_NAME = "genesets.dapper-ids.gmt", "geneset.provenance.dapper.yaml"


def yaml_scalar(text):
    """A YAML scalar as a one-line string (plain, quoted or folded over several lines)."""
    text = " ".join(part.strip() for part in text.split("\n")).strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        text = text[1:-1].replace("''", "'")
    elif len(text) >= 2 and text[0] == text[-1] == '"':
        text = json.loads(text)
    return " ".join(text.split())


def document_names(path):
    """({gene-set id: name}, collection id) of a producer's DAPPER document, read line by line: the documents list
    every member of every gene set, so a YAML parser would spend minutes on the large ones."""
    names, collections, section, current, field = {}, [], None, None, None
    with open(path) as fh:
        for line in fh:
            if line[:1] not in (" ", "-", "\n"):
                section, current, field = line.split(":", 1)[0], None, None
                continue
            if section not in ("gene_sets", "gene_set_collections"):
                continue
            if line.startswith("- id:"):
                current, field = line.split(":", 1)[1].strip(), None
                if section == "gene_set_collections":
                    collections.append(current)
            elif line.startswith("  name:") and current and section == "gene_sets":
                names[current], field = line.split(":", 1)[1].rstrip("\n"), "name"
            elif field == "name" and line.startswith("    "):
                names[current] += "\n" + line.rstrip("\n")
            else:
                field = None
    check(len(collections) == 1 and COLLECTION_RE.match(collections[0]),
          "%s must hold exactly one GeneSetCollection: %s" % (path, collections[:3]))
    return {gene_set_id: yaml_scalar(name) for gene_set_id, name in names.items()}, collections[0]


def collection_label(relative_dir):
    """<library>__<partition>__<model>[__<comparison>] from <library>/genesets/<partition>/models/<model>/extractor[/..]."""
    parts = relative_dir.split(os.sep)
    check(len(parts) >= 6 and parts[1] == "genesets" and parts[3] == "models" and parts[5] == "extractor",
          "Unexpected gene-set folder %s" % relative_dir)
    return "__".join([parts[0], parts[2], parts[4]] + parts[6:])


def cmd_genesets_build(args):
    """One GMT per library and one gene-set index, from a local mirror of the CFDE release (every
    <library>/genesets/**/genesets.dapper-ids.gmt with its producer DAPPER document).

    The library GMTs keep each gene set's dapper:GeneSet id and its genes in GMT order and blank the description
    column: the release fills it with free text, and pigean splits GMT lines on any whitespace. A gene set that several
    GMTs of a library hold (the same id) is written once, from the first GMT in path order; its genes must agree.
    """
    libraries = wanted_libraries(args)
    snapshot = os.path.realpath(args.cfde_dir)
    for library in libraries:
        check(os.path.isdir(os.path.join(snapshot, library, "genesets")), "No %s/genesets in %s" % (library, snapshot))
    os.makedirs(args.output_dir, exist_ok=True)
    for name in os.listdir(args.output_dir):
        if name.endswith(".gmt"):
            os.unlink(os.path.join(args.output_dir, name))
    index, table, seen = [], [], {}
    for library in libraries:
        gmts = []
        for folder, _, names in os.walk(os.path.join(snapshot, library, "genesets")):
            if GMT_NAME in names:
                gmts.append(folder)
        check(gmts, "No %s under %s/genesets" % (GMT_NAME, library))
        path, n_sets, n_gmts, repeats, bad_tokens = os.path.join(args.output_dir, library + ".gmt"), 0, 0, 0, 0
        with open(path + ".tmp", "w") as out:
            for folder in sorted(gmts):
                relative = os.path.relpath(folder, snapshot)
                names, collection_id = document_names(os.path.join(folder, DOCUMENT_NAME))
                label, n_gmts = collection_label(relative), n_gmts + 1
                with open(os.path.join(folder, GMT_NAME)) as fh:
                    for row, line in enumerate(fh, 1):
                        if not line.strip():
                            continue
                        fields = line.rstrip("\n").split("\t")
                        gene_set_id, genes = fields[0], [g for g in fields[2:] if g]
                        check(GENE_SET_RE.match(gene_set_id), "Malformed gene-set id %r in %s" % (gene_set_id, relative))
                        check(gene_set_id in names, "%s is not in the DAPPER document of %s" % (gene_set_id, relative))
                        # Tokens pigean would misread (it splits on any whitespace, and `:` separates a weight)
                        # are not gene symbols: `LSM\xa06.00` in IGVF. They are left out and counted.
                        usable = [g for g in genes if ":" not in g and len(g.split()) == 1]
                        bad_tokens += len(genes) - len(usable)
                        genes = list(OrderedDict.fromkeys(usable))
                        check(genes, "%s has no usable gene" % gene_set_id)
                        if gene_set_id in seen:
                            check(seen[gene_set_id] == (library, genes), "%s differs between %s and an earlier GMT"
                                  % (gene_set_id, relative))
                            repeats += 1
                            continue
                        seen[gene_set_id] = (library, genes)
                        out.write("\t".join([gene_set_id, ""] + genes) + "\n")
                        index.append({"gene_set_id": gene_set_id, "gene_set_name": names[gene_set_id],
                                      "collection_id": collection_id, "cfde_label": label, "library": library,
                                      "n_genes": len(genes)})
                        n_sets += 1
        os.replace(path + ".tmp", path)
        table.append({"library": library, "file": os.path.abspath(path), "n_gene_sets": n_sets,
                      "sha256": sha256_file(path), "n_gmts": n_gmts, "n_repeated": repeats, "n_bad_tokens": bad_tokens})
    write_tsv(args.output_index_file, GENE_SET_INDEX_COLUMNS, index)
    write_tsv(args.output_file, LIBRARY_GMT_COLUMNS, table)
    print("%d gene sets in %d libraries: %s" % (len(index), len(libraries), "; ".join(
        "%s %d (%d GMTs, %d repeats, %d unusable gene tokens)" % (t["library"], t["n_gene_sets"], t["n_gmts"], t["n_repeated"],
                                                                 t["n_bad_tokens"]) for t in table)))


# -------------------------------------------------------------------------------------------------
# betas_: `pigean betas` of each trait's gene stats on each CFDE library

def finite(text):
    try:
        return math.isfinite(float(text))
    except (TypeError, ValueError):
        return False


def read_source_gene_stats(path):
    """gs.out rows as (gene, combined, log_bf, prior) text, each gene once."""
    rows = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        missing = {"Gene", "combined", "log_bf", "prior"} - set(reader.fieldnames or [])
        check(not missing, "%s lacks the columns %s" % (path, sorted(missing)))
        for r in reader:
            rows.append((r["Gene"], r["combined"], r["log_bf"], r["prior"]))
    genes = Counter(row[0] for row in rows)
    check(len(genes) == len(rows), "Duplicate genes in %s" % path)
    return rows


def pigean_betas_command(python, profile, gmt_file, gene_stats_file, response, seed, prefix, max_initial):
    """One library's `pigean betas` run: the GWAS profile on the trait's gene stats (no outer Gibbs)."""
    cap = [] if max_initial is None else ["--max-num-gene-sets-initial", str(max_initial)]
    return [python, "-B", "-m", "pigean", "betas", "--config", profile, "--X-in", gmt_file] + cap + [
            "--gene-stats-in", gene_stats_file, "--gene-stats-id-col", "gene", "--gene-stats-log-bf-col", response,
            "--retain-all-beta-uncorrected", "--deterministic", "--seed", str(seed), "--hide-progress", "--hide-opts",
            "--gene-set-stats-out", prefix + ".gene_set_stats.tsv", "--params-out", prefix + ".params.tsv",
            "--warnings-file", prefix + ".warnings.txt", "--log-file", prefix + ".log"]


def read_params(path):
    params = defaultdict(list)
    for row in read_tsv(path):
        params[row["Parameter"]].append(row["Value"])
    return params


def kept_gene_sets(path):
    """The gene sets of a pigean gene-set stats file that PIGEAN analyzed (filter_reason kept), in file order."""
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        missing = PIGEAN_GENE_SET_STATS_COLUMNS - set(reader.fieldnames or [])
        check(not missing, "%s lacks the pigean columns %s" % (path, sorted(missing)))
        return [r for r in reader if r["filter_reason"] == "kept"]


def cmd_betas_trait(args):
    """One trait: its gene stats as pigean reads them (gene, combined, log_bf, prior from gs.out), then one
    `pigean betas` fit per library on that library's GMT alone. A library whose outputs and fingerprint (gene stats,
    GMT, profile, response, seed, initial cap, pigean commit) are in --work-dir is not refitted."""
    trait = trait_row(args.traits_file, args.instance)
    head, patch = check_pigean_clone(args.repo_dir, args.expected_pigean_commit, args.pigean_patch_file)
    libraries = wanted_libraries(args)
    table = {r["library"]: r for r in read_columns(args.library_gmts_file, LIBRARY_GMT_COLUMNS)}
    missing = [library for library in libraries if library not in table]
    check(not missing, "No GMT for libraries %s in %s" % (missing, args.library_gmts_file))

    source = source_path(args.source_dir, args.source_library, trait["trait"], "gene_stats")
    check(str(os.path.getsize(source)) == trait["gene_stats_size"], "%s changed since it was listed" % source)
    stats = read_source_gene_stats(source)
    usable = [row for row in stats if finite(row[2])]
    check(usable, "No gene of %s has a finite log_bf" % source)
    write_tsv(args.output_gene_stats_file, GENE_STATS_COLUMNS, [
        dict(zip(GENE_STATS_COLUMNS, (trait["trait"],) + row)) for row in usable])

    os.makedirs(args.work_dir, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=args.pigean_src, PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    base = "%s %s %s %s %s %s %s" % (sha256_file(args.output_gene_stats_file), sha256_file(args.profile), args.response,
                                     args.seed, args.max_num_gene_sets_initial, head, patch)
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
            started = time.time()
            proc = subprocess.run(pigean_betas_command(args.python, args.profile, entry["file"],
                                                       args.output_gene_stats_file, args.response, args.seed, prefix,
                                                       args.max_num_gene_sets_initial),
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
        run = dict(files, instance=trait["instance"], trait=trait["trait"], library=library,
                   status="fitted" if os.path.getsize(files["params_file"]) else "no_gene_sets",
                   n_gene_sets=entry["n_gene_sets"], response=args.response, pigean_commit=head, seconds=seconds)
        learned = {"p": NA, "sigma2": NA}
        if run["status"] == "fitted":
            params = read_params(files["params_file"])
            used = params.get("option_gene_stats_log_bf_col", [NA])[-1]
            check(used == args.response, "pigean regressed %s on %s, expected %s" % (library, used, args.response))
            learned = {name: params.get(name, [NA])[-1] for name in ("p", "sigma2")}
        kept = kept_gene_sets(files["gene_set_stats_file"])
        for name in ("p", "sigma2"):  # below --update-hyper-min-gene-sets pigean keeps its default and records none
            used = {r.get(name + "_used") for r in kept}
            if learned[name] == NA and len(used) == 1 and None not in used:
                learned[name] = used.pop()
        run.update(learned, n_kept=len(kept),
                   n_nonzero_beta_uncorrected=sum(1 for r in kept if float(r["beta_uncorrected"]) != 0))
        runs.append(run)
    write_tsv(args.output_runs_file, BETAS_RUN_COLUMNS, runs)
    print("%s: %d genes; %s" % (trait["trait"], len(usable), ", ".join(
        "%s %s" % (r["library"], "%d kept" % r["n_kept"] if r["status"] == "fitted" else r["status"]) for r in runs)))


# -------------------------------------------------------------------------------------------------
# format_: the collection's NDJSON files

def number(text, what):
    value = float(text)
    check(math.isfinite(value), "Non-finite %s" % what)
    return value


class NdjsonWriter:
    """Uncompressed NDJSON, one object per line, written whole (tmp then rename); records rows, size and sha256."""

    def __init__(self, path):
        self.path, self.rows, self.digest, self.size = path, 0, hashlib.sha256(), 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path + ".tmp", "wb")

    def write(self, row):
        line = (json.dumps(row, allow_nan=False, ensure_ascii=False) + "\n").encode("utf-8")
        self.fh.write(line)
        self.digest.update(line)
        self.size += len(line)
        self.rows += 1

    def close(self):
        self.fh.close()
        os.replace(self.path + ".tmp", self.path)


def read_gmt_members(path, wanted):
    """{gene_set_id: [genes in GMT order]} for the wanted ids of a GMT (id, description, genes; tab-separated)."""
    members = {}
    if not wanted:
        return members
    with open(path) as fh:
        for line in fh:
            gene_set_id = line.split("\t", 1)[0]
            if gene_set_id in wanted:
                members[gene_set_id] = [g for g in line.rstrip("\n").split("\t")[2:] if g]
    check(set(members) == set(wanted), "%s lacks %d of the analyzed gene sets" % (path, len(set(wanted) - set(members))))
    return members


def cmd_format_trait(args):
    """One trait's collection files, one folder per library.

    <source library> (the PIGEAN run itself): gene stats from gs.out (every gene with finite metrics), gene-set stats
    from gss.out (the analyzed gene sets with a nonzero beta_uncorrected) and their memberships from ggss.out.
    Each CFDE library: gene-set stats from the `pigean betas` fit (same rule; n is PIGEAN's N, the member genes it
    used) and the whole roster of those gene sets from the library's GMT. A CFDE library
    has no gene file: its betas regress the source run's gene stats, which the source library's dataset already holds.
    A library with no gene-set rows gets no folder.

    Every row carries phenotype (= the trait folder) and gene_set_size (= the library folder).
    """
    trait = trait_row(args.traits_file, args.instance)
    key, files = trait["trait"], []
    dims = lambda library: OrderedDict([("trait_group", args.trait_group), ("phenotype", key), ("gene_set_size", library)])
    slim = lambda library: OrderedDict([("phenotype", key), ("gene_set_size", library)])

    def out_path(library, role):
        return os.path.join(args.output_dir, library, key, KINDS[role] + ".json")

    def record(library, role, writer, dropped):
        writer.close()
        files.append({"instance": trait["instance"], "trait": key, "library": library, "role": role,
                      "kind": KINDS[role], "relative_path": "%s/%s/%s.json" % (library, key, KINDS[role]),
                      "rows": writer.rows, "dropped_rows": dropped, "size": writer.size,
                      "sha256": writer.digest.hexdigest()})

    # A previous run's files for this trait (a library may have lost its gene sets since).
    libraries = [r["library"] for r in read_columns(args.runs_file, BETAS_RUN_COLUMNS)]
    for library in [args.source_library] + libraries:
        for role in KINDS:
            if os.path.exists(out_path(library, role)):
                os.unlink(out_path(library, role))

    # --- the PIGEAN run's own library
    library = args.source_library
    stats = read_source_gene_stats(source_path(args.source_dir, library, key, "gene_stats"))
    writer, dropped, spelling = NdjsonWriter(out_path(library, "gene_stats")), 0, {}
    for gene, combined, log_bf, prior in stats:
        spelling.setdefault(gene.upper(), gene)
        if not (finite(combined) and finite(log_bf) and finite(prior)):
            dropped += 1
            continue
        row = OrderedDict([("gene", gene), ("prior", float(prior)), ("combined", float(combined)),
                           ("log_bf", float(log_bf))])
        row.update(dims(library))
        writer.write(row)
    check(writer.rows, "No gene of %s has finite combined, log_bf and prior" % key)
    record(library, "gene_stats", writer, dropped)

    kept = [r for r in kept_gene_sets(source_path(args.source_dir, library, key, "gene_set_stats"))
            if float(r["beta_uncorrected"]) != 0]
    writer, ids = NdjsonWriter(out_path(library, "gene_set_stats")), set()
    for r in kept:
        check(r["Gene_Set"] not in ids, "Duplicate gene set %s for %s" % (r["Gene_Set"], key))
        ids.add(r["Gene_Set"])
        row = OrderedDict([("gene_set", r["Gene_Set"]), ("source", r.get("label") or library),
                           ("beta", number(r["beta"], "beta of %s" % r["Gene_Set"])),
                           ("beta_uncorrected", number(r["beta_uncorrected"], "beta_uncorrected of %s" % r["Gene_Set"])),
                           ("n", int(r["N"]))])
        row.update(dims(library))
        writer.write(row)
    record(library, "gene_set_stats", writer, 0)

    writer, dropped, pairs, with_members = NdjsonWriter(out_path(library, "membership")), 0, set(), set()
    with open(source_path(args.source_dir, library, key, "membership"), newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        check({"Gene", "gene_set"} <= set(reader.fieldnames or []), "ggss.out of %s lacks Gene or gene_set" % key)
        for r in reader:
            pair = (r["Gene"], r["gene_set"])
            if r["gene_set"] not in ids or pair in pairs:
                dropped += 1
                continue
            pairs.add(pair)
            with_members.add(r["gene_set"])
            row = OrderedDict([("gene", r["Gene"]), ("gene_set", r["gene_set"])])
            row.update(slim(library))
            writer.write(row)
    # A few analyzed gene sets have no row in ggss.out (1 to 4 for some traits): they keep their gene-set row and
    # have no roster.
    without_members = len(ids - with_members)
    record(library, "membership", writer, dropped)

    # --- the CFDE libraries
    index = {r["gene_set_id"]: r for r in read_columns(args.gene_set_index_file, GENE_SET_INDEX_COLUMNS)}
    gmts = {r["library"]: r["file"] for r in read_columns(args.library_gmts_file, LIBRARY_GMT_COLUMNS)}
    for run in read_columns(args.runs_file, BETAS_RUN_COLUMNS):
        library = run["library"]
        check(run["instance"] == trait["instance"] and run["pigean_commit"] == args.expected_pigean_commit,
              "%s is not this trait's run at pigean %s" % (args.runs_file, args.expected_pigean_commit))
        kept = [r for r in kept_gene_sets(run["gene_set_stats_file"]) if float(r["beta_uncorrected"]) != 0]
        if not kept:
            continue
        members = read_gmt_members(gmts[library], {r["Gene_Set"] for r in kept})
        writer = NdjsonWriter(out_path(library, "gene_set_stats"))
        member_writer = NdjsonWriter(out_path(library, "membership"))
        for r in kept:
            gene_set_id = r["Gene_Set"]
            check(gene_set_id in index and index[gene_set_id]["library"] == library,
                  "%s is not a %s gene set of the index" % (gene_set_id, library))
            # The gene set's whole roster, as the library's GMT lists it. A gene the trait's gene stats also hold is
            # written in their spelling (CFDE C10orf71 is PIGEAN's C10ORF71), so it is the same gene id in both.
            genes = OrderedDict((spelling.get(gene.upper(), gene), True) for gene in members[gene_set_id])
            row = OrderedDict([("gene_set", gene_set_id), ("gene_set_name", index[gene_set_id]["gene_set_name"]),
                               ("source", index[gene_set_id]["cfde_label"]),
                               ("collection_id", index[gene_set_id]["collection_id"]),
                               ("beta", number(r["beta"], "beta of %s" % gene_set_id)),
                               ("beta_uncorrected", number(r["beta_uncorrected"], "beta_uncorrected of %s" % gene_set_id)),
                               ("n", int(r["N"]))])
            row.update(dims(library))
            writer.write(row)
            for gene in genes:
                pair = OrderedDict([("gene", gene), ("gene_set", gene_set_id)])
                pair.update(slim(library))
                member_writer.write(pair)
        record(library, "gene_set_stats", writer, 0)
        record(library, "membership", member_writer, 0)
    write_tsv(args.output_file, FILES_COLUMNS, files)
    print("%s: %s; %d %s gene sets without membership rows" % (
        key, "; ".join("%s %s %d" % (f["library"], f["role"], f["rows"]) for f in files), without_members,
        args.source_library))


# -------------------------------------------------------------------------------------------------
# collect_ and deposit_

def cmd_collect(args):
    """Fan-in over all traits: every file of the collection with its sha256 (checked against the disk), every betas
    run, and the run record (what was run, on what, with which code)."""
    traits = read_columns(args.traits_file, TRAIT_COLUMNS)
    libraries = wanted_libraries(args)
    files, runs = [], []
    for path in args.files_file:
        files += read_columns(path, FILES_COLUMNS)
    for path in args.runs_file:
        runs += read_columns(path, BETAS_RUN_COLUMNS)
    by_instance = defaultdict(list)
    for run in runs:
        by_instance[run["instance"]].append(run)
    expected = {t["instance"] for t in traits}
    check(set(by_instance) == expected, "Runs cover %d traits; the project has %d (missing: %s)"
          % (len(by_instance), len(expected), sorted(expected - set(by_instance))[:10]))
    check({f["instance"] for f in files} == expected, "Files cover %d traits; the project has %d"
          % (len({f["instance"] for f in files}), len(expected)))
    for instance, trait_runs in by_instance.items():
        check(sorted(r["library"] for r in trait_runs) == sorted(libraries), "%s has runs for %s, expected %s"
              % (instance, sorted(r["library"] for r in trait_runs), sorted(libraries)))
        for run in trait_runs:
            check(run["pigean_commit"] == args.expected_pigean_commit, "%s/%s ran at pigean %s, expected %s"
                  % (instance, run["library"], run["pigean_commit"], args.expected_pigean_commit))
    paths = Counter(f["relative_path"] for f in files)
    check(len(paths) == len(files), "Two traits write the same file")
    for f in files:
        path = os.path.join(args.data_dir, f["relative_path"])
        check(os.path.isfile(path) and os.path.getsize(path) == int(f["size"]), "%s is not the file that was written" % path)
        if args.verify_sha256:
            check(sha256_file(path) == f["sha256"], "%s changed since it was written" % path)
    files.sort(key=lambda f: (f["library"], f["trait"], f["role"]))
    runs.sort(key=lambda r: (r["trait"], r["library"]))
    write_tsv(args.output_files_file, FILES_COLUMNS, files)
    write_tsv(args.output_runs_file, BETAS_RUN_COLUMNS[:-4], runs)

    gmts = {r["library"]: r for r in read_columns(args.library_gmts_file, LIBRARY_GMT_COLUMNS)}
    datasets = Counter(f["library"] for f in files if f["role"] == "gene_set_stats" and int(f["rows"]))
    datasets[args.source_library] = sum(1 for f in files if f["library"] == args.source_library and f["role"] == "gene_stats")
    record = OrderedDict([
        ("collection_layout", "data/{library}/{trait}/{kind}.json"),
        ("kinds", list(KINDS.values())),
        ("traits", len(traits)),
        ("libraries", [args.source_library] + libraries),
        ("datasets_per_library", OrderedDict(sorted(datasets.items()))),
        ("files", len(files)),
        ("bytes", sum(int(f["size"]) for f in files)),
        ("source_run", OrderedDict([
            ("location", args.source_url), ("library", args.source_library),
            ("files", list(SOURCE_FILES.values())),
            ("description", "PIGEAN portal run; gene, gene-set and membership rows of this library are that run's own")])),
        ("cfde_betas", OrderedDict([
            ("software", "PIGEAN"), ("mode", "betas (gene-set betas on existing gene stats; no outer Gibbs)"),
            ("pigean_commit", args.expected_pigean_commit),
            ("pigean_patch", OrderedDict([("file", os.path.basename(args.pigean_patch_file)),
                                          ("sha256", sha256_file(args.pigean_patch_file)),
                                          ("diff", read_text(args.pigean_patch_file))]) if args.pigean_patch_file else None),
            ("profile", os.path.basename(args.profile)),
            ("profile_sha256", sha256_file(args.profile)), ("response", args.response), ("seed", args.seed),
            ("max_num_gene_sets_initial", args.max_num_gene_sets_initial),
            ("command", " ".join(pigean_betas_command("python", "<profile>", "<library>.gmt", "<trait>.gene_stats.tsv",
                                                      args.response, args.seed, "<prefix>",
                                                      args.max_num_gene_sets_initial))),
            ("cfde_snapshot", args.cfde_snapshot),
            ("library_gmts", OrderedDict((library, OrderedDict([("n_gene_sets", int(gmts[library]["n_gene_sets"])),
                                                                ("sha256", gmts[library]["sha256"])]))
                                         for library in libraries))])),
        ("finished", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    ])
    with open(args.output_record_file, "w") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    print("%d files (%.1f GB) of %d traits; datasets per library: %s" % (
        len(files), record["bytes"] / 1e9, len(traits), dict(record["datasets_per_library"])))


def cmd_deposit(args):
    """Upload the collection to its S3 prefix: data/ (the files the claims CLI reads) and run/ (manifests and the run
    record). Never overwrites: an object already there must have the same size (and MD5, when its ETag is one); files
    already there are skipped, so an interrupted deposit resumes. --dry-run only reports."""
    check(args.destination_url.endswith("/"), "The destination must end with /")
    bucket, prefix = split_s3(args.destination_url)
    status = aws_json("s3api", "get-bucket-versioning", "--bucket", bucket).get("Status")
    check(status == "Enabled" or args.allow_unversioned, "Bucket %s has versioning %s; turn it on before writing "
          "(aws s3api put-bucket-versioning --bucket %s --versioning-configuration Status=Enabled)" % (bucket, status, bucket))
    files = read_columns(args.files_file, FILES_COLUMNS)
    local = {"data/" + f["relative_path"]: os.path.join(args.data_dir, f["relative_path"]) for f in files}
    for path in args.run_file:
        local["run/" + os.path.basename(path)] = path
    remote = s3_objects(args.destination_url)
    to_upload, same, unknown = [], 0, sorted(key[len(prefix):] for key in remote if key[len(prefix):] not in local)
    for name, path in sorted(local.items()):
        size = os.path.getsize(path)
        if prefix + name not in remote:
            to_upload.append((name, path, size))
            continue
        remote_size, etag = remote[prefix + name]
        differs = remote_size != size or ("-" not in etag and md5_file(path) != etag)
        check(not differs or name.startswith("run/"), "s3://%s/%s%s exists and differs from %s: a published file is "
              "never overwritten; deposit a rerun under a new prefix" % (bucket, prefix, name, path))
        if differs:
            to_upload.append((name, path, size))
        else:
            same += 1
    check(not unknown or args.allow_unknown, "%d objects under %s are not part of this collection: %s"
          % (len(unknown), args.destination_url, unknown[:10]))
    total = sum(size for _, _, size in to_upload)
    print("%s: %d files to upload (%.2f GB), %d already there%s" % (
        args.destination_url, len(to_upload), total / 1e9, same, " [dry run]" if args.dry_run else ""))
    if not args.dry_run:
        for start in range(0, len(to_upload), 200):
            batch = to_upload[start:start + 200]
            procs = []
            for chunk in [batch[i::args.workers] for i in range(args.workers)]:
                if not chunk:
                    continue
                script = "\n".join("aws s3 cp --only-show-errors %s %s" % (shell_quote(path), shell_quote(
                    "s3://%s/%s%s" % (bucket, prefix, name))) for name, path, _ in chunk)
                procs.append(subprocess.Popen(["bash", "-e", "-c", script], stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, universal_newlines=True))
            for proc in procs:
                out = proc.communicate()[0]
                check(proc.returncode == 0, "Upload failed:\n%s" % out[-3000:])
        remote = s3_objects(args.destination_url)
        for name, path in local.items():
            check(prefix + name in remote and remote[prefix + name][0] == os.path.getsize(path),
                  "s3://%s/%s%s is missing or has another size after the upload" % (bucket, prefix, name))
    with open(args.output_file, "w") as fh:
        json.dump(OrderedDict([("destination", args.destination_url), ("location", args.destination_url + "data/"),
                               ("path", "{library}/{trait}/{kind}.json"), ("versioning", status),
                               ("dry_run", bool(args.dry_run)), ("files", len(local)), ("uploaded", len(to_upload)),
                               ("uploaded_bytes", total), ("already_there", same),
                               ("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))]), fh, indent=2)
        fh.write("\n")


def shell_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


# -------------------------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def libraries(p):
        p.add_argument("--libraries", required=True)
        p.add_argument("--exclude-libraries", default="")

    def betas(p):
        p.add_argument("--expected-pigean-commit", required=True)
        p.add_argument("--pigean-patch-file", default=None,
                       help="The one patch the pinned clone's src/ carries on top of the commit")
        p.add_argument("--profile", required=True)
        p.add_argument("--response", required=True)
        p.add_argument("--seed", required=True)
        p.add_argument("--max-num-gene-sets-initial", type=int, default=None)

    p = sub.add_parser("source-list", help="List the traits of the PIGEAN run on S3")
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_source_list)

    p = sub.add_parser("source-fetch", help="Download the PIGEAN run's gs.out, gss.out and ggss.out")
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--traits-file", required=True)
    p.add_argument("--source-dir", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_source_fetch)

    p = sub.add_parser("genesets-build", help="One GMT per library and the gene-set index, from the CFDE mirror")
    libraries(p)
    p.add_argument("--cfde-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--output-index-file", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_genesets_build)

    p = sub.add_parser("betas-trait", help="Fit pigean betas for one trait on each library's GMT")
    libraries(p)
    betas(p)
    p.add_argument("--python", required=True)
    p.add_argument("--pigean-src", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--traits-file", required=True)
    p.add_argument("--source-dir", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--source-manifest-file", help="Dependency only: the download finished")
    p.add_argument("--library-gmts-file", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--output-gene-stats-file", required=True)
    p.add_argument("--output-runs-file", required=True)
    p.set_defaults(func=cmd_betas_trait)

    p = sub.add_parser("format-trait", help="Write one trait's collection files (NDJSON), one folder per library")
    p.add_argument("--expected-pigean-commit", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--traits-file", required=True)
    p.add_argument("--source-dir", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--trait-group", required=True)
    p.add_argument("--runs-file", required=True)
    p.add_argument("--gene-set-index-file", required=True)
    p.add_argument("--library-gmts-file", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_format_trait)

    p = sub.add_parser("collect", help="Every trait's files and runs in one manifest each, and the run record")
    libraries(p)
    betas(p)
    p.add_argument("--traits-file", required=True)
    p.add_argument("--files-file", action="append", required=True)
    p.add_argument("--runs-file", action="append", required=True)
    p.add_argument("--library-gmts-file", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--cfde-snapshot", required=True)
    p.add_argument("--verify-sha256", action="store_true")
    p.add_argument("--output-files-file", required=True)
    p.add_argument("--output-runs-file", required=True)
    p.add_argument("--output-record-file", required=True)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("deposit", help="Upload the collection to its S3 prefix (never overwrites)")
    p.add_argument("--destination-url", required=True)
    p.add_argument("--files-file", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument("--run-file", action="append", default=[])
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--plan-file", help="Dependency only: the dry run was made first")
    p.add_argument("--allow-unversioned", action="store_true")
    p.add_argument("--allow-unknown", action="store_true")
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_deposit)
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
