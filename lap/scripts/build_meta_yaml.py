#!/usr/bin/env python3
"""Generate config/cfde_projection.meta.yaml for the CFDE -> EAGGL projection LAP pipeline.

meta-sanity has no TSV input, so this script enumerates the instances from the inputs themselves:
one project, one geneset_collection per row of the CFDE index.tsv, and one trait per EAGGL trait
(with its KPN.TRAIT id). Instances are written as explicit `classes:` entries, parents first.
Every string is JSON-quoted (valid YAML), so trait names such as `NO` or `1e5` stay strings.

Then convert with meta-sanity:
  PYTHONPATH=/humgen/diabetes2/users/chase/packages/meta-sanity python3 -m meta_sanity.generate_meta \
      config/cfde_projection.meta.yaml config/cfde_projection.meta
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from projection_workflow import (COLLECTION_ID_PREFIX, WorkflowError, check, load_kpn_registry,  # noqa: E402
                                 map_traits_to_kpn, read_factor_ids, read_tsv, traits_in_order)

LAP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# LAP keys; `${name}` references are resolved by meta-sanity.
KEYS = [
    ("base_dir", LAP_DIR),
    ("unix_out_dir", "${base_dir}/out"),
    ("log_dir", "${base_dir}/log"),
    ("raw_dir", "${base_dir}/raw"),
    ("eaggl_share_dir", "${raw_dir}/EAGGL_capped_union_graph_share/data"),
    ("cfde_snapshot", "2026-09-28"),
    ("cfde_dir", "/humgen/diabetes/users/chase/data/dig-s3/gene_sets/cfde/${cfde_snapshot}"),
    ("kpn_dir", "/humgen/diabetes/users/chase/packages/dig-portal-data-models"),
    ("lap_home", "/humgen/diabetes/users/chase/lap"),
    ("web_out_dir", "http://internal.broadinstitute.org/~cyakabos/reveal-lap"),
    ("default_umask", "002"),
]


def resolve(value, keys):
    while "${" in value:
        value = re.sub(r"\$\{(\w+)\}", lambda m: keys[m.group(1)], value)
    return value


def q(value):
    return json.dumps(str(value))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", default="eaggl_capped__cfde_2026_09_28")
    parser.add_argument("--loading-variant", default="capped", choices=["capped", "uncapped"])
    parser.add_argument("--kpn-release", default="v0.0.2")
    parser.add_argument("--output-file", default=os.path.join(LAP_DIR, "config", "cfde_projection.meta.yaml"))
    args = parser.parse_args(argv)

    keys = dict(KEYS)
    for name in list(keys):
        keys[name] = resolve(keys[name], keys)
    kpn_registry = "${kpn_dir}/versions/trait/%s/kpn_trait_registry.tsv" % args.kpn_release
    project_files = [
        ("factor_loadings_in_file", "${eaggl_share_dir}/capped_factor_gene_loadings.tsv.gz"),
        ("factor_ids_in_file", "${eaggl_share_dir}/factor_ids.tsv"),
        ("factor_metadata_in_file", "${eaggl_share_dir}/factor_metadata.tsv"),
        ("eaggl_genes_in_file", "${eaggl_share_dir}/genes.tsv"),
        ("capped_entries_in_file", "${eaggl_share_dir}/capped_entries.tsv.gz"),
        ("cfde_index_file", "${cfde_dir}/index.tsv"),
        ("kpn_trait_registry_file", kpn_registry),
        ("kpn_trait_flat_file", "${kpn_dir}/versions/trait/%s/kpn_trait_flat.tsv" % args.kpn_release),
    ]
    for _, path in project_files:
        check(os.path.isfile(resolve(path, keys)), "Missing input %s" % resolve(path, keys))

    cfde_dir = keys["cfde_dir"]
    collections = []
    for row in read_tsv(os.path.join(cfde_dir, "index.tsv")):
        label = row["label"]
        check(row["collection_id"].startswith(COLLECTION_ID_PREFIX), "Malformed collection id for %s" % label)
        paths = {}
        for prop, column in (("cfde_gmt_file", "dapper_ids_gmt_path"), ("cfde_names_gmt_file", "source_gmt_path"),
                             ("cfde_collection_yaml_file", "yaml_path")):
            path = row[column]
            check(os.path.isfile(path), "Missing %s for %s: %s" % (column, label, path))
            check(os.path.realpath(path).startswith(os.path.realpath(cfde_dir) + os.sep),
                  "%s for %s is outside the CFDE snapshot" % (column, label))
            paths[prop] = "${cfde_dir}/" + os.path.relpath(os.path.realpath(path), os.path.realpath(cfde_dir))
        collections.append((label, row["collection_id"], row["library"], paths))

    factor_ids = read_factor_ids(resolve("${eaggl_share_dir}/factor_ids.tsv", keys))
    traits = list(traits_in_order(factor_ids))
    kpn = map_traits_to_kpn(traits, load_kpn_registry(resolve(kpn_registry, keys)))

    names = [args.project] + [c[0] for c in collections] + traits
    check(len(set(names)) == len(names), "Instance names collide across classes")
    bad = [n for n in names if not INSTANCE_NAME_RE.match(n)]
    check(not bad, "Unsafe instance names: %s" % bad[:10])
    check(len(collections) == 133 and len(traits) == 711,
          "Expected 133 collections and 711 traits, found %d and %d" % (len(collections), len(traits)))

    lines = [
        "# GENERATED by lap/scripts/build_meta_yaml.py -- do not edit by hand; rerun the script instead.",
        "# 1 project, %d CFDE gene-set collections (index.tsv rows), %d EAGGL traits (KPN %s ids)."
        % (len(collections), len(traits), args.kpn_release),
        "config: %s" % q(os.path.join(LAP_DIR, "config", "cfde_projection.cfg")),
        "",
        "keys:",
    ]
    lines += ["  %s: %s" % (name, q(value)) for name, value in KEYS]
    lines += ["", "classes:",
              "  %s:" % q(args.project),
              "    class: project",
              "    parent: null",
              "    properties:",
              "      loading_variant: %s" % q(args.loading_variant),
              "      kpn_release: %s" % q(args.kpn_release)]
    lines += ["      %s: %s" % (prop, q(path)) for prop, path in project_files]
    lines.append("")
    lines.append("  # CFDE gene-set collections: instance name = CFDE label")
    for label, collection_id, library, paths in collections:
        lines += ["  %s:" % q(label),
                  "    class: geneset_collection",
                  "    parent: %s" % q(args.project),
                  "    properties:",
                  "      collection_id: %s" % q(collection_id),
                  "      library: %s" % q(library)]
        lines += ["      %s: %s" % (prop, q(paths[prop])) for prop in sorted(paths)]
    lines.append("")
    lines.append("  # EAGGL traits: instance name = EAGGL trait = KPN legacy_phenotype_id")
    for trait in traits:
        lines += ["  %s:" % q(trait),
                  "    class: trait",
                  "    parent: %s" % q(args.project),
                  "    properties:",
                  "      kpn_trait_id: %s" % q(kpn[trait]["portal_id"])]
    with open(args.output_file, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("Wrote %s: %d collections, %d traits" % (args.output_file, len(collections), len(traits)))


if __name__ == "__main__":
    try:
        main()
    except WorkflowError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(1)
