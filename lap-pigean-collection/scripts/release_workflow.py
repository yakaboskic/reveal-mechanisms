#!/usr/bin/env python3
"""Deposits of the PIGEAN claims-collection pipeline (cfde_ and deposit_ stages of config/pigean_collection.cfg).

cfde-tree   the CFDE gene-set collection as a folder: every fitted library's GMTs with their producer DAPPER
            provenance documents (the release's own files, linked from the local mirror), the per-library GMTs the
            betas were fitted on, the gene-set index, and a manifest with every file's sha256.
deposit     upload one or more folders to an S3 prefix by their manifests. Never overwrites an object that differs;
            skips the ones already there, so it resumes.

Python 3.9, standard library; S3 through the aws CLI.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collection_workflow as cw  # noqa: E402
from collection_workflow import WorkflowError, check  # noqa: E402

TREE_COLUMNS = ["relative_path", "role", "library", "size", "sha256"]
# The release's files kept per GMT folder: the gene sets, the producer's DAPPER provenance document (GeneSets with
# their members, the GeneSetCollection, the Files and Activities that produced them) and the extractor's metadata.
RELEASE_FILES = OrderedDict([("genesets.dapper-ids.gmt", "gmt"), ("geneset.provenance.dapper.yaml", "provenance"),
                             ("geneset.meta.json", "metadata")])


def link(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.lexists(target):
        os.unlink(target)
    os.symlink(source, target)


def cmd_cfde_tree(args):
    libraries = cw.wanted_libraries(args)
    snapshot, rows = os.path.realpath(args.cfde_dir), []

    def add(source, relative, role, library):
        link(source, os.path.join(args.output_dir, relative))
        rows.append({"relative_path": relative, "role": role, "library": library, "size": os.path.getsize(source),
                     "sha256": cw.sha256_file(source)})

    table = {r["library"]: r for r in cw.read_columns(args.library_gmts_file, cw.LIBRARY_GMT_COLUMNS)}
    counts = {}
    for library in libraries:
        check(library in table, "No GMT for %s in %s" % (library, args.library_gmts_file))
        folders = sorted(folder for folder, _, names in os.walk(os.path.join(snapshot, library, "genesets"))
                         if cw.GMT_NAME in names)
        for folder in folders:
            for name, role in RELEASE_FILES.items():
                path = os.path.join(folder, name)
                check(os.path.isfile(path) or role == "metadata", "%s lacks %s" % (folder, name))
                if os.path.isfile(path):
                    add(path, os.path.join("genesets", os.path.relpath(path, snapshot)), role, library)
        add(table[library]["file"], "library_gmts/%s.gmt" % library, "library_gmt", library)
        check(rows[-1]["sha256"] == table[library]["sha256"], "%s changed since it was built" % table[library]["file"])
        counts[library] = len(folders)
    add(os.path.realpath(args.gene_set_index_file), "gene_set_index.tsv", "gene_set_index", "")
    index = cw.read_columns(args.gene_set_index_file, cw.GENE_SET_INDEX_COLUMNS)
    record = OrderedDict([
        ("release", args.cfde_release_url), ("mirrored", args.cfde_snapshot), ("libraries", libraries),
        ("gene_sets", len(index)), ("gene_set_collections", len({r["collection_id"] for r in index})),
        ("gmt_folders_per_library", counts),
        ("layout", OrderedDict([
            ("genesets/<release key>/genesets.dapper-ids.gmt", "the release's GMT: dapper:GeneSet id, description, genes"),
            ("genesets/<release key>/geneset.provenance.dapper.yaml", "the producer's DAPPER provenance document"),
            ("genesets/<release key>/geneset.meta.json", "the extractor's metadata"),
            ("library_gmts/<library>.gmt", "one GMT per library, blank descriptions, each gene set once"),
            ("gene_set_index.tsv", "gene_set_id, gene_set_name, collection_id, cfde_label, library, n_genes")])),
        ("built", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    ])
    with open(args.output_record_file, "w") as fh:
        json.dump(record, fh, indent=2)
        fh.write("\n")
    rows.sort(key=lambda r: r["relative_path"])
    cw.write_tsv(args.output_file, TREE_COLUMNS, rows)
    print("%d files (%.2f GB) of %d libraries, %d gene sets in %d collections" % (
        len(rows), sum(r["size"] for r in rows) / 1e9, len(libraries), record["gene_sets"], record["gene_set_collections"]))


def cmd_deposit(args):
    check(args.destination_url.endswith("/"), "The destination must end with /")
    bucket, prefix = cw.split_s3(args.destination_url)
    status = cw.aws_json("s3api", "get-bucket-versioning", "--bucket", bucket).get("Status")
    check(status == "Enabled" or args.allow_unversioned, "Bucket %s has versioning %s; turn it on before writing" % (bucket, status))
    local, replaceable = {}, set()
    for spec in args.tree:
        parts = spec.split("=")
        check(len(parts) == 3, "--tree is <prefix under the destination>=<folder>=<manifest>: %s" % spec)
        under, folder, manifest = parts
        check(under == "" or under.endswith("/"), "A tree prefix is empty or ends with /: %r" % under)
        for row in cw.read_tsv(manifest):
            name = under + row["relative_path"]
            check(name not in local, "%s is in two trees" % name)
            path = os.path.join(folder, row["relative_path"])
            check(os.path.isfile(path) and os.path.getsize(path) == int(row["size"]), "%s is not the file its manifest lists" % path)
            local[name] = path
    for spec in args.file:  # single files (manifests, run records): the only objects a later deposit may replace
        under, path = spec.split("=", 1)
        check(os.path.isfile(path), "No file %s" % path)
        name = under + os.path.basename(path)
        local[name] = path
        replaceable.add(name)
    remote = cw.s3_objects(args.destination_url)
    unknown = sorted(key[len(prefix):] for key in remote if key[len(prefix):] not in local)
    check(not unknown or args.allow_unknown, "%d objects under %s are not part of this deposit: %s"
          % (len(unknown), args.destination_url, unknown[:10]))
    to_upload, same = [], 0
    for name, path in sorted(local.items()):
        size = os.path.getsize(path)
        if prefix + name in remote:
            remote_size, etag = remote[prefix + name]
            differs = remote_size != size or ("-" not in etag and cw.md5_file(path) != etag)
            check(not differs or name in replaceable, "s3://%s/%s%s exists and differs from %s: a deposited file is never "
                  "overwritten; deposit a rerun under a new prefix" % (bucket, prefix, name, path))
            if not differs:
                same += 1
                continue
        to_upload.append((name, path, size))
    total = sum(size for _, _, size in to_upload)
    print("%s: %d files to upload (%.2f GB), %d already there%s" % (
        args.destination_url, len(to_upload), total / 1e9, same, " [dry run]" if args.dry_run else ""))
    if not args.dry_run:
        # One `aws s3 cp --recursive` per worker over a folder of links named as the objects: a process per
        # file spends seconds starting the CLI, a recursive copy sends many files at once from one process.
        stage = args.output_file + ".stage"
        shutil.rmtree(stage, ignore_errors=True)
        procs = []
        for i in range(args.workers):
            chunk = to_upload[i::args.workers]
            if chunk:
                folder = os.path.join(stage, str(i))
                for name, path, _ in chunk:
                    target = os.path.join(folder, name)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.symlink(os.path.abspath(path), target)
                procs.append(subprocess.Popen(["aws", "s3", "cp", "--recursive", "--only-show-errors", folder + "/",
                                               "s3://%s/%s" % (bucket, prefix)], stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, universal_newlines=True))
        failed = []
        for proc in procs:
            out = proc.communicate()[0]
            if proc.returncode != 0:
                failed.append(out[-3000:])
        shutil.rmtree(stage, ignore_errors=True)
        check(not failed, "Upload failed:\n%s" % "\n".join(failed))
        remote = cw.s3_objects(args.destination_url)
        for name, path in local.items():
            check(prefix + name in remote and remote[prefix + name][0] == os.path.getsize(path),
                  "s3://%s/%s%s is missing or has another size after the upload" % (bucket, prefix, name))
    with open(args.output_file, "w") as fh:
        json.dump(OrderedDict([("destination", args.destination_url), ("versioning", status), ("dry_run", bool(args.dry_run)),
                               ("files", len(local)), ("uploaded", len(to_upload)), ("uploaded_bytes", total),
                               ("already_there", same), ("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))]),
                  fh, indent=2)
        fh.write("\n")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cfde-tree", help="The CFDE gene-set collection as a folder with a manifest")
    p.add_argument("--libraries", required=True)
    p.add_argument("--exclude-libraries", default="")
    p.add_argument("--cfde-dir", required=True)
    p.add_argument("--cfde-snapshot", required=True)
    p.add_argument("--cfde-release-url", required=True)
    p.add_argument("--library-gmts-file", required=True)
    p.add_argument("--gene-set-index-file", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--output-record-file", required=True)
    p.add_argument("--output-file", required=True)
    p.set_defaults(func=cmd_cfde_tree)

    p = sub.add_parser("deposit", help="Upload folders to an S3 prefix by their manifests (never overwrites)")
    p.add_argument("--destination-url", required=True)
    p.add_argument("--tree", action="append", default=[], help="<prefix under the destination>=<folder>=<manifest>")
    p.add_argument("--file", action="append", default=[], help="<prefix under the destination>=<file>")
    p.add_argument("--plan-file", help="Dependency only: the dry run was made first")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dry-run", action="store_true")
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
