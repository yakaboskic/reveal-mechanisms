#!/usr/bin/env python3
"""Link the CFDE gene sets' provenance to objects for their source resources (GTEx V8, KPMP atlas, ...).

The producer documents of the cfde-genesets collection (geneset.provenance.dapper.yaml) end at root input Files: a
URL, with no object saying which public dataset the file belongs to or who publishes it. This reads the registry
config/cfde_source_datasets.yaml, matches every root input file against it and writes one DAPPER document:

    organizations           an Organization per organization the matched datasets name; the CFDE programs
                            (GTEx, HuBMAP, ...) are among them, first in their datasets' was_attributed_to
    datasets                a Dataset per matched source dataset (publisher, was_attributed_to -> Organizations)
    was_derived_from_edges  root File -> Dataset, and GeneSetCollection -> each source Dataset of its document
                            (datasets marked `reference: true` are left out of the collection edges)

    cfde_sources.py --claims-repo-dir <claims-repository> --registry config/cfde_source_datasets.yaml \
        --manifest <project>.cfde_manifest.tsv --tree-dir <project_dir>/cfde_collection \
        --output-file <dir>/cfde_sources.dapper.yaml --report-file <dir>/cfde_sources.report.tsv

No producer object is changed: the edges only name ids the producer minted. A root input recorded as a C2M2File
(not deposited into the claims repository) gets no File edge, but still decides its collection's datasets. The run
fails when a root file matches no rule (extend the registry, or list it under `ignore`) unless --allow-unmatched.

Runs on the claims repository's Python (its identity module mints the ids; PyYAML).
"""

import argparse
import csv
import os
import re
import sys
from collections import OrderedDict

import yaml

try:
    Loader = yaml.CSafeLoader
except AttributeError:
    Loader = yaml.SafeLoader

HEADER = ("# Source resources of the CFDE gene sets: an Organization and Dataset node per public resource, a\n"
          "# was_derived_from edge from each root input File of the gene-set documents to its Dataset, and one from\n"
          "# each GeneSetCollection to the source Datasets of its document. Written by\n"
          "# reveal-mechanisms/lap-pigean-collection (scripts/cfde_sources.py) from config/cfde_source_datasets.yaml;\n"
          "# every node id is the content address of its node, so rerun the script instead of editing this file.\n")
DATASET_SLOTS = ("name", "description", "version", "alternate_identifier", "location")
REPORT_COLUMNS = ["library", "dataset", "dataset_id", "cfde_program", "cfde_program_id", "cfde_page", "reference",
                  "root_files", "c2m2_root_files", "gene_set_collections"]


class Registry:
    def __init__(self, path):
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        self.organizations, self.datasets, self.libraries = raw["organizations"], raw["datasets"], raw["libraries"]
        self.rules = {}
        for key, spec in self.datasets.items():
            if spec.get("program"):
                if not (self.organizations.get(spec["program"]) or {}).get("cfde_program"):
                    sys.exit("dataset %s: program %r is not an organization with cfde_program" % (key, spec["program"]))
                spec["attributed_to"] = [spec["program"]] + [k for k in spec.get("attributed_to") or [] if k != spec["program"]]
            for org in [spec.get("publisher")] + list(spec.get("attributed_to") or []):
                if org and org not in self.organizations:
                    sys.exit("dataset %s: unknown organization %r" % (key, org))
            self.rules[key] = [self.rule(f, "dataset %s" % key) for f in spec.get("files") or []]
        for library, keys in self.libraries.items():
            for key in keys:
                if key not in self.datasets:
                    sys.exit("library %s: unknown dataset %r" % (library, key))
        self.ignore = [self.rule(f, "ignore") for f in raw.get("ignore") or []]

    @staticmethod
    def rule(spec, where):
        if not (spec.get("pattern") or spec.get("location")):
            sys.exit("%s: a file rule needs `pattern` or `location`" % where)
        return tuple(re.compile(spec[k]) if spec.get(k) else None for k in ("pattern", "location"))

    @staticmethod
    def hit(rule, name, location):
        pattern, where = rule
        return (pattern is None or pattern.search(name or "")) and (where is None or where.search(location or ""))

    def match(self, library, name, location):
        """The dataset key of a root file, "" when it is an ignored workflow artefact, None when nothing matches."""
        for key in self.libraries.get(library, []):
            if any(self.hit(rule, name, location) for rule in self.rules[key]):
                return key
        if any(self.hit(rule, name, location) for rule in self.ignore):
            return ""
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claims-repo-dir", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--manifest", required=True, help="the cfde_ stage's manifest (relative_path, role, library, ...)")
    parser.add_argument("--tree-dir", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--report-file", required=True)
    parser.add_argument("--allow-unmatched", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, args.claims_repo_dir)
    from service.collections import identity as idn

    registry = Registry(args.registry)
    with open(args.manifest, newline="") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t") if r["role"] == "provenance"]

    file_dataset = OrderedDict()        # root File id -> dataset key
    collection_datasets = OrderedDict()  # GeneSetCollection id -> [dataset keys]
    counts, unmatched, unlinked = {}, OrderedDict(), []

    def count(library, key, column, item):
        counts.setdefault((library, key), {c: set() for c in REPORT_COLUMNS[7:]})[column].add(item)

    for n, row in enumerate(rows, 1):
        library = row["library"]
        with open(os.path.join(args.tree_dir, row["relative_path"])) as fh:
            doc = yaml.load(fh, Loader=Loader)
        generated = {e["subject"] for e in doc.get("was_generated_by_edges") or []}
        used = {e["object"] for e in doc.get("used_edges") or []}
        sources = []
        for group, column in (("files", "root_files"), ("c2m2_files", "c2m2_root_files")):
            for node in doc.get(group) or []:
                if node["id"] not in used or node["id"] in generated:
                    continue
                name, location = node.get("filename") or node.get("name"), node.get("location")
                key = registry.match(library, name, location)
                if key is None:
                    unmatched[node["id"]] = (library, name, location)
                elif key:
                    if group == "files":
                        file_dataset[node["id"]] = key
                    count(library, key, column, node["id"])
                    if not registry.datasets[key].get("reference") and key not in sources:
                        sources.append(key)
        for collection in doc.get("gene_set_collections") or []:
            if sources:
                known = collection_datasets.setdefault(collection["id"], [])
                known.extend(k for k in sources if k not in known)
                for key in sources:
                    count(library, key, "gene_set_collections", collection["id"])
            else:
                unlinked.append((library, collection["id"], row["relative_path"]))
        if n % 1000 == 0:
            print("%d / %d documents" % (n, len(rows)), flush=True)

    for node_id, (library, name, location) in list(unmatched.items())[:40]:
        print("unmatched root file  %s  %s  %s  %s" % (library, node_id, name, location), file=sys.stderr)
    for library, collection, path in unlinked[:40]:
        print("collection without a source dataset  %s  %s  %s" % (library, collection, path), file=sys.stderr)
    print("%d root files matched, %d unmatched; %d collections linked, %d without a source dataset"
          % (len(file_dataset), len(unmatched), len(collection_datasets), len(unlinked)))
    if (unmatched or unlinked) and not args.allow_unmatched:
        sys.exit("extend %s (or its `ignore` list), or pass --allow-unmatched" % args.registry)

    organizations, datasets = OrderedDict(), OrderedDict()

    def organization(key):
        if key not in organizations:
            spec = registry.organizations[key]
            organizations[key] = idn.mint("Organization", {k: spec[k] for k in ("name", "ror") if spec.get(k)})
        return organizations[key]["id"]

    def dataset(key):
        if key not in datasets:
            spec = registry.datasets[key]
            node = {slot: spec[slot] for slot in DATASET_SLOTS if spec.get(slot) not in (None, "")}
            program = registry.organizations.get(spec.get("program") or "")
            if program and program.get("cfde_page"):  # an Organization has no slot for a URL
                node["description"] = "%s CFDE program: %s (%s)." % (
                    " ".join(str(node.get("description") or "").split()), program["name"], program["cfde_page"])
            if spec.get("publisher"):
                node["publisher"] = organization(spec["publisher"])
            if spec.get("attributed_to"):
                node["was_attributed_to"] = [organization(k) for k in spec["attributed_to"]]
            datasets[key] = idn.mint("Dataset", node)
        return datasets[key]["id"]

    used_keys = [k for k in registry.datasets if any(k == key for _, key in counts)]
    for key in used_keys:
        dataset(key)
    edges = [{"subject": subject, "predicate": "prov:wasDerivedFrom", "object": dataset(key)}
             for subject, key in file_dataset.items()]
    edges += [{"subject": subject, "predicate": "prov:wasDerivedFrom", "object": dataset(key)}
              for subject, keys in collection_datasets.items() for key in keys]

    os.makedirs(os.path.dirname(os.path.abspath(args.output_file)), exist_ok=True)
    with open(args.output_file + ".tmp", "w") as fh:
        fh.write(HEADER)
        yaml.safe_dump(OrderedDict_to_dict([("organizations", list(organizations.values())),
                                            ("datasets", list(datasets.values())),
                                            ("was_derived_from_edges", edges),
                                            ("prefixes", {"ror": "https://ror.org/"})]),
                       fh, sort_keys=False, width=200, allow_unicode=True)
    os.replace(args.output_file + ".tmp", args.output_file)
    with open(args.report_file, "w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(REPORT_COLUMNS)
        for (library, key), sets in sorted(counts.items()):
            program = registry.datasets[key].get("program")
            writer.writerow([library, key, datasets[key]["id"],
                             registry.organizations[program]["cfde_program"] if program else "",
                             organizations[program]["id"] if program else "",
                             registry.organizations[program].get("cfde_page", "") if program else "",
                             "yes" if registry.datasets[key].get("reference") else "no"]
                            + [len(sets[c]) for c in REPORT_COLUMNS[7:]])
    print("%s: %d organizations, %d datasets, %d edges" % (args.output_file, len(organizations), len(datasets), len(edges)))
    return 0


def OrderedDict_to_dict(pairs):
    return dict(pairs)


if __name__ == "__main__":
    sys.exit(main())
