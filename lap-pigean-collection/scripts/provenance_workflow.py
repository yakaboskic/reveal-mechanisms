#!/usr/bin/env python3
"""DAPPER provenance of the PIGEAN claims collection (provenance_ stage of config/pigean_collection.cfg).

For every trait, one DAPPER provenance document saying how each of its collection files was produced:

    PIGEAN portal run --generated--> gs.out, gss.out, ggss.out                      (the run's own files, on S3)
    pigean betas (one Activity per CFDE library) --used--> gs.out, <library>.gmt
                                                 --generated--> betas/<library>/<trait>/gene_set_stats.tsv
    format (one Activity per library) --used--> that library's inputs
                                      --generated--> data/<library>/<trait>/*.json     (the collection files)

Beside every file the pipeline writes, <file>.dapper.yaml holds that file's own lineage: the File, the Activity that
generated it, the files that Activity used, and how those were produced in turn.

Every id is the DAPPER-ID-1 address of its node, minted with the claims repository's identity module, so this runs on
the claims repository's Python (>= 3.11) with --claims-repo-dir on the path. File locations are the deposit's S3
locations; the claims publisher matches result files to File nodes by sha256.

Subcommands: trait (one document, and the raw betas files copied beside the collection), collect (one merged document
for the collection's source.yaml, validated as the publisher validates it).
"""

import argparse
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collection_workflow as cw  # noqa: E402
from collection_workflow import WorkflowError, check  # noqa: E402

PROVENANCE_FILES_COLUMNS = ["instance", "trait", "library", "role", "relative_path", "size", "sha256"]
NODE_GROUPS = {"Activity": "activities", "File": "files"}
PIGEAN_REPO_URL = "https://github.com/flannick/pigean"
PIPELINE_REPO_URL = "https://github.com/yakaboskic/reveal-mechanisms"
SIDECAR_SUFFIX = ".dapper.yaml"
SIDECAR_HEADER = ("# DAPPER provenance of %s (beside this file): the File, the Activity that generated it, the files\n"
                  "# that Activity used, and how those were produced in turn. Written by\n"
                  "# reveal-mechanisms/lap-pigean-collection (scripts/provenance_workflow.py); every id is the content\n"
                  "# address of its node, so rerun the pipeline's provenance_ stage instead of editing this file.\n")
HEADER = ("# DAPPER provenance of the PIGEAN claims collection's files for one trait. Written by\n"
          "# reveal-mechanisms/lap-pigean-collection (scripts/provenance_workflow.py); every id is the content\n"
          "# address of its node, so rerun the pipeline's provenance_ stage instead of editing this file.\n")


def identity(claims_repo_dir):
    sys.path.insert(0, claims_repo_dir)
    from service.collections import identity as idn
    return idn


def utc(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Document:
    """Nodes by id and edges, deduplicated, in first-seen order."""

    def __init__(self, idn):
        self.idn, self.nodes, self.edges = idn, {}, {"used_edges": {}, "was_generated_by_edges": {}}

    def node(self, cls, **payload):
        node = self.idn.mint(cls, {k: v for k, v in payload.items() if v not in (None, "")})
        self.nodes.setdefault(node["id"], (cls, node))
        return node["id"]

    def file(self, name, location, size, sha256, description, mime_type="text/tab-separated-values"):
        return self.node("File", name=name, description=description, filename=location.rsplit("/", 1)[-1],
                         location=location, size_in_bytes=int(size), sha256=sha256, mime_type=mime_type)

    def used(self, activity, file_id):
        self.edges["used_edges"][(activity, file_id)] = {"subject": activity, "predicate": "prov:used",
                                                         "object": file_id, "edge_role": "data_input"}

    def generated(self, file_id, activity):
        self.edges["was_generated_by_edges"][(file_id, activity)] = {
            "subject": file_id, "predicate": "prov:wasGeneratedBy", "object": activity}

    def merge(self, doc):
        for group, cls in (("activities", "Activity"), ("files", "File")):
            for node in doc.get(group) or []:
                self.nodes.setdefault(node["id"], (cls, node))
        for group in self.edges:
            for edge in doc.get(group) or []:
                self.edges[group][(edge["subject"], edge["object"])] = edge

    def lineage(self, file_id):
        """The part of the document a file descends from: the file, its generating Activity, the files that used,
        and so on upstream. The file comes first, then nodes in the order the walk meets them."""
        generated_by, used = {}, {}
        for (subject, activity) in self.edges["was_generated_by_edges"]:
            generated_by.setdefault(subject, []).append(activity)
        for (activity, used_file) in self.edges["used_edges"]:
            used.setdefault(activity, []).append(used_file)
        order, queue = [], [file_id]
        while queue:
            node = queue.pop(0)
            if node in order:
                continue
            order.append(node)
            queue.extend(generated_by.get(node, []) + used.get(node, []))
        kept = set(order)
        out = {group: [self.nodes[n][1] for n in order if self.nodes[n][0] == name] for name, group in NODE_GROUPS.items()}
        out.update({group: [e for (a, b), e in edges.items() if a in kept and b in kept]
                    for group, edges in self.edges.items()})
        return out

    def as_dict(self):
        out = {group: [node for cls, node in self.nodes.values() if cls == name] for name, group in NODE_GROUPS.items()}
        out.update({group: list(edges.values()) for group, edges in self.edges.items()})
        return out


def write_yaml(path, header, content):
    import yaml
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(header)
        yaml.safe_dump(content, fh, sort_keys=False, width=200, allow_unicode=True)
    os.replace(path + ".tmp", path)


def write_sidecars(doc, local, root, rows, **columns):
    """<file>.dapper.yaml beside each of `local` ({File id: path under root}): that file's lineage. Adds a manifest row
    per sidecar to `rows`."""
    for file_id, relative in local.items():
        sidecar = relative + SIDECAR_SUFFIX
        path = os.path.join(root, sidecar)
        write_yaml(path, SIDECAR_HEADER % os.path.basename(relative), doc.lineage(file_id))
        rows.append(dict(columns, role="provenance_sidecar", relative_path=sidecar, size=os.path.getsize(path),
                         sha256=cw.sha256_file(path)))


def library_gmt_files(doc, args):
    """A File per library GMT (its deposit location in the CFDE gene-set collection), by library."""
    files = {}
    for row in cw.read_columns(args.library_gmts_file, cw.LIBRARY_GMT_COLUMNS):
        files[row["library"]] = doc.file(
            "%s gene sets (GMT)" % row["library"], args.gmt_url + row["library"] + ".gmt", os.path.getsize(row["file"]),
            row["sha256"], "The %s gene sets of the CFDE release %s as one GMT (%s gene sets; dapper:GeneSet ids, blank "
            "descriptions), the gene sets `pigean betas` was fitted on." % (row["library"], args.cfde_snapshot,
                                                                            row["n_gene_sets"]))
    return files


def cmd_trait(args):
    idn = identity(args.claims_repo_dir)
    trait = cw.trait_row(args.traits_file, args.instance)
    key, instance = trait["trait"], trait["instance"]
    doc, extra, local = Document(idn), [], {}
    gmt = library_gmt_files(doc, args)

    # --- the PIGEAN portal run and its three files
    source = {}
    rows = [r for r in cw.read_columns(args.source_manifest_file, cw.SOURCE_MANIFEST_COLUMNS) if r["instance"] == instance]
    check(sorted(r["role"] for r in rows) == sorted(cw.SOURCE_FILES), "%s has no three source files" % instance)
    described = {"gene_stats": "gene statistics (gs.out): prior, combined and log_bf per gene",
                 "gene_set_stats": "gene-set statistics (gss.out): beta and beta_uncorrected per gene set",
                 "membership": "gene to gene-set memberships (ggss.out)"}
    portal = doc.node("Activity", name="PIGEAN portal run: %s (%s)" % (key, args.source_library),
                      description="The PIGEAN run of trait %s on the %s gene-set library whose outputs are under %s%s/%s/. "
                      "Not rerun by this pipeline." % (key, args.source_library, args.source_url, key, args.source_library),
                      software_name="PIGEAN", repo_url=PIGEAN_REPO_URL)
    for r in rows:
        name = cw.SOURCE_FILES[r["role"]]
        source[r["role"]] = doc.file("PIGEAN %s of %s" % (name, key),
                                     "%s%s/%s/%s" % (args.source_url, key, args.source_library, name), r["size"],
                                     r["sha256"], "PIGEAN %s for %s (%s)." % (described[r["role"]], key, args.source_library))
        doc.generated(source[r["role"]], portal)

    # --- one `pigean betas` fit per CFDE library
    betas, version, patched = {}, args.expected_pigean_commit, ""
    if args.pigean_patch_file:
        version += "+patch.sha256:" + cw.sha256_file(args.pigean_patch_file)
        patched = (" PIGEAN ran at the commit with one patch (%s): the fitted gene-distance correlation is capped at 1."
                   % os.path.basename(args.pigean_patch_file))
    for run in cw.read_columns(args.runs_file, cw.BETAS_RUN_COLUMNS):
        library = run["library"]
        check(run["instance"] == instance and run["pigean_commit"] == args.expected_pigean_commit,
              "%s is not this trait's run at pigean %s" % (args.runs_file, args.expected_pigean_commit))
        command = " ".join(cw.pigean_betas_command(
            "python", "config/profiles/" + os.path.basename(args.profile), library + ".gmt", "gene_stats.tsv",
            args.response, args.seed, library, args.max_num_gene_sets_initial))
        outcome = ("%s of its %s gene sets analyzed, %s with a nonzero beta_uncorrected; prior p %s, sigma2 %s"
                   % (run["n_kept"], run["n_gene_sets"], run["n_nonzero_beta_uncorrected"], run["p"], run["sigma2"])
                   if run["status"] == "fitted" else
                   "none of its %s gene sets survived PIGEAN's filters for this trait, so there are no outputs"
                   % run["n_gene_sets"])
        done = os.path.splitext(run["gene_set_stats_file"])[0].rsplit(".gene_set_stats", 1)[0] + ".done"
        activity = doc.node(
            "Activity", name="pigean betas: %s on %s" % (key, library),
            description="PIGEAN gene-set betas (no outer Gibbs) of trait %s on the CFDE library %s: the portal run's %s "
            "per gene (gene, combined, log_bf and prior of gs.out; genes with a finite log_bf) regressed on the library's "
            "gene sets. %s.%s" % (key, library, args.response, outcome, patched),
            software_name="PIGEAN", software_version=version, code_version=version,
            repo_url=PIGEAN_REPO_URL, entrypoint="python -m pigean betas", command=command,
            generated_at_time=utc(os.path.getmtime(done)) if os.path.exists(done) else None)
        doc.used(activity, source["gene_stats"])
        doc.used(activity, gmt[library])
        if run["status"] != "fitted":
            continue
        outputs = []
        for role, path, what in (("betas_gene_set_stats", run["gene_set_stats_file"],
                                  "every gene set PIGEAN read, with its filter_reason, beta and beta_uncorrected"),
                                 ("betas_params", run["params_file"], "the fit's parameters and options")):
            relative = "betas/%s/%s/%s" % (library, key, os.path.basename(path).split(".", 1)[1])
            target = os.path.join(args.collection_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(path, target + ".tmp")
            os.replace(target + ".tmp", target)
            size, sha = os.path.getsize(target), cw.sha256_file(target)
            extra.append({"instance": instance, "trait": key, "library": library, "role": role,
                          "relative_path": relative, "size": size, "sha256": sha})
            file_id = doc.file("pigean betas %s: %s on %s" % (os.path.basename(relative), key, library),
                               args.deposit_url + relative, size, sha,
                               "`pigean betas` output for %s on %s: %s." % (key, library, what))
            doc.generated(file_id, activity)
            local[file_id] = relative
            outputs.append(file_id)
        betas[library] = outputs[0]

    # --- the formatting step (one Activity per library: a dataset descends only from its own library's inputs) and
    # the collection files
    files = cw.read_columns(args.files_file, cw.FILES_COLUMNS)
    check(files and all(f["instance"] == instance for f in files), "%s is not this trait's file table" % args.files_file)
    kinds = {"gene_stats": "gene statistics", "gene_set_stats": "gene-set statistics", "membership": "gene-set memberships",
             "cfde_gene_set_stats": "gene-set statistics (gene sets named by their CFDE dapper:GeneSet id)"}
    for library in sorted({f["library"] for f in files}):
        if library == args.source_library:
            what = ("the portal run's genes with finite scores, its analyzed gene sets with a nonzero beta_uncorrected, "
                    "and those gene sets' memberships")
            inputs = list(source.values())
        else:
            check(library in betas, "%s has collection files for %s but no fitted betas run" % (key, library))
            what = ("the gene sets `pigean betas` analyzed with a nonzero beta_uncorrected, named by their "
                    "dapper:GeneSet id (their rosters are in the cfde-genesets collection)")
            inputs = [betas[library]]
        formatting = doc.node(
            "Activity", name="Format %s on %s for the PIGEAN claims collection" % (key, library),
            description="Writes the collection files of trait %s on the %s gene-set library (uncompressed NDJSON; "
            "phenotype and gene_set_size on every row): %s." % (key, library, what),
            software_name="lap-pigean-collection", entrypoint="scripts/collection_workflow.py format-trait",
            code_version="sha256:" + cw.sha256_file(cw.__file__), repo_url=PIPELINE_REPO_URL,
            command="collection_workflow.py format-trait --instance %s --source-library %s --trait-group %s"
            % (instance, args.source_library, args.trait_group),
            generated_at_time=utc(os.path.getmtime(args.files_file)))
        for file_id in inputs:
            doc.used(formatting, file_id)
        for f in files:
            if f["library"] != library:
                continue
            file_id = doc.file("%s of %s (%s)" % (f["kind"], key, library), args.data_url + f["relative_path"],
                               f["size"], f["sha256"], "PIGEAN %s of trait %s on the %s gene-set library, as the claims "
                               "collection reads them (%s rows)." % (kinds[f["role"]], key, library, f["rows"]),
                               mime_type="application/x-ndjson")
            doc.generated(file_id, formatting)
            local[file_id] = "data/" + f["relative_path"]

    # --- a sidecar beside every file this pipeline wrote for the trait, then the whole trait in one document
    write_sidecars(doc, local, args.collection_dir, extra, instance=instance, trait=key, library="")
    write_yaml(args.output_file, HEADER, doc.as_dict())
    relative = os.path.relpath(args.output_file, args.collection_dir)
    check(not relative.startswith(".."), "The document must be under --collection-dir")
    extra.append({"instance": instance, "trait": key, "library": "", "role": "provenance", "relative_path": relative,
                  "size": os.path.getsize(args.output_file), "sha256": cw.sha256_file(args.output_file)})
    cw.write_tsv(args.output_files_file, PROVENANCE_FILES_COLUMNS, extra)
    out = doc.as_dict()
    print("%s: %d activities, %d files, %d used and %d generated-by edges; %d sidecars" % (
        key, len(out["activities"]), len(out["files"]), len(out["used_edges"]), len(out["was_generated_by_edges"]),
        len(local)))


def cmd_collect(args):
    """Every trait's document merged into one (JSON: the form the claims collection lists in source.yaml), the
    gene-set build that wrote the library GMTs added, and the result validated as the claims publisher validates a
    provenance document. Checks that every collection file has a File node with its sha256 and a generating Activity."""
    import yaml
    idn = identity(args.claims_repo_dir)
    doc, extra = Document(idn), []
    for path in args.document_file:
        with open(path) as fh:
            doc.merge(yaml.safe_load(fh))
    for path in args.files_file:
        extra += cw.read_columns(path, PROVENANCE_FILES_COLUMNS)
    build = doc.node(
        "Activity", name="Build the CFDE library GMTs (%s)" % args.cfde_snapshot,
        description="One GMT per CFDE library from the local mirror (%s) of the CFDE gene-set release %s: every "
        "genesets.dapper-ids.gmt of the library with its description column blanked, each dapper:GeneSet id once."
        % (args.cfde_snapshot, args.cfde_release_url),
        software_name="lap-pigean-collection", entrypoint="scripts/collection_workflow.py genesets-build",
        code_version="sha256:" + cw.sha256_file(cw.__file__), repo_url=PIPELINE_REPO_URL,
        generated_at_time=utc(os.path.getmtime(args.library_gmts_file)))
    gmt_files = library_gmt_files(doc, args)
    for file_id in gmt_files.values():
        doc.generated(file_id, build)
    # <library>.gmt.dapper.yaml beside each library GMT of the CFDE gene-set collection.
    if args.gmt_sidecar_root:
        rows = []
        write_sidecars(doc, {file_id: "library_gmts/%s.gmt" % library for library, file_id in gmt_files.items()},
                       args.gmt_sidecar_root, rows)
        cw.write_tsv(args.output_gmt_sidecars_file, ["relative_path", "role", "size", "sha256"], rows)
    merged = doc.as_dict()
    with open(args.output_file + ".tmp", "w") as fh:
        json.dump(merged, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(args.output_file + ".tmp", args.output_file)

    from pathlib import Path
    from service.collections import provenance
    loaded = provenance.load({"path": os.path.basename(args.output_file)}, Path(os.path.dirname(args.output_file)))
    by_sha = {node["sha256"]: node_id for node_id, (cls, node) in loaded.nodes.items() if cls == "File"}
    generated = {e["subject"] for e in loaded.edges.get("was_generated_by_edges", [])}
    manifest = cw.read_columns(args.collection_manifest_file, cw.FILES_COLUMNS)
    missing = [f["relative_path"] for f in manifest if by_sha.get(f["sha256"]) not in generated]
    check(not missing, "%d collection files have no generating Activity in the provenance: %s" % (len(missing), missing[:5]))

    # The part the claims publisher needs to link every dataset to its run: the collection files and the Activity that
    # generated each. Small enough to keep with the collection definition (source.yaml `provenance: - path:`); the
    # full graph is the merged document and the per-trait documents beside the data.
    wanted = {f["sha256"] for f in manifest}
    file_ids = {node["id"] for node in merged["files"] if node["sha256"] in wanted}
    edges = [e for e in merged["was_generated_by_edges"] if e["subject"] in file_ids]
    activity_ids = {e["object"] for e in edges}
    slim = {"activities": [n for n in merged["activities"] if n["id"] in activity_ids],
            "files": [n for n in merged["files"] if n["id"] in file_ids], "was_generated_by_edges": edges}
    with open(args.output_slim_file + ".tmp", "w") as fh:
        json.dump(slim, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    os.replace(args.output_slim_file + ".tmp", args.output_slim_file)
    provenance.load({"path": os.path.basename(args.output_slim_file)}, Path(os.path.dirname(args.output_slim_file)))

    for role, path in (("provenance_merged", args.output_file), ("provenance_collection", args.output_slim_file)):
        extra.append({"instance": "", "trait": "", "library": "", "role": role,
                      "relative_path": os.path.relpath(path, args.collection_dir),
                      "size": os.path.getsize(path), "sha256": cw.sha256_file(path)})
    for row in extra:
        path = os.path.join(args.collection_dir, row["relative_path"])
        check(os.path.isfile(path) and os.path.getsize(path) == int(row["size"]), "%s is not the file that was written" % path)
    extra.sort(key=lambda r: r["relative_path"])
    cw.write_tsv(args.output_files_file, PROVENANCE_FILES_COLUMNS, extra)
    print("%d activities, %d files, %d used and %d generated-by edges in %s (%.1f MB); all %d collection files have a "
          "generating Activity; %d provenance and raw betas files" % (
              len(merged["activities"]), len(merged["files"]), len(merged["used_edges"]),
              len(merged["was_generated_by_edges"]), args.output_file, os.path.getsize(args.output_file) / 1e6,
              len(manifest), len(extra)))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--claims-repo-dir", required=True)
        p.add_argument("--library-gmts-file", required=True)
        p.add_argument("--collection-dir", required=True)
        p.add_argument("--gmt-url", required=True, help="S3 prefix of the deposited library GMTs, ending with /")
        p.add_argument("--cfde-snapshot", required=True)

    p = sub.add_parser("trait", help="One trait's DAPPER provenance document and its raw betas files")
    common(p)
    p.add_argument("--instance", required=True)
    p.add_argument("--traits-file", required=True)
    p.add_argument("--source-manifest-file", required=True)
    p.add_argument("--runs-file", required=True)
    p.add_argument("--files-file", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-library", required=True)
    p.add_argument("--trait-group", required=True)
    p.add_argument("--deposit-url", required=True, help="S3 prefix of the collection deposit, ending with /")
    p.add_argument("--data-url", required=True, help="S3 prefix of the collection files (source.yaml location)")
    p.add_argument("--expected-pigean-commit", required=True)
    p.add_argument("--pigean-patch-file", default=None)
    p.add_argument("--profile", required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--seed", required=True)
    p.add_argument("--max-num-gene-sets-initial", type=int, default=None)
    p.add_argument("--output-file", required=True)
    p.add_argument("--output-files-file", required=True)
    p.set_defaults(func=cmd_trait)

    p = sub.add_parser("collect", help="All traits' documents merged, validated and checked against the collection")
    common(p)
    p.add_argument("--document-file", action="append", required=True)
    p.add_argument("--files-file", action="append", required=True)
    p.add_argument("--collection-manifest-file", required=True)
    p.add_argument("--cfde-release-url", required=True)
    p.add_argument("--output-file", required=True)
    p.add_argument("--output-slim-file", required=True)
    p.add_argument("--gmt-sidecar-root", default=None,
                   help="The CFDE gene-set collection folder: writes library_gmts/<library>.gmt.dapper.yaml in it")
    p.add_argument("--output-gmt-sidecars-file", default=None)
    p.add_argument("--output-files-file", required=True)
    p.set_defaults(func=cmd_collect)
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
