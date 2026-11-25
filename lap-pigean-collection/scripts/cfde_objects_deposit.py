#!/usr/bin/env python3
"""Deposit the CFDE gene sets into a claims-repository object collection (kpn-dapper-toolkit collections/cfde-genesets).

Reads the producer DAPPER documents of the CFDE gene-set collection folder (the cfde_ stage's manifest: every
geneset.provenance.dapper.yaml) and sends their nodes and edges, unchanged, to a claims service as deposits into one
release of the collection: GeneSets (with members), GeneSetCollections, Files and Activities. Ids are the producer's.

    cfde_objects_deposit.py --api http://localhost:8110 --release 2026.11.0 \
        --manifest <project>.cfde_manifest.tsv --tree-dir <project_dir>/cfde_collection --open --close

The key is read from $CLAIMS_API_KEY (a key granted the collection). A deposit holds at most 1,000 objects, 5,000 edges
and 16 MB, so a document is sent in parts; resending a part changes nothing, so a stopped run can be repeated.
C2M2File nodes are left out: the claims repository's DAPPER profile cannot verify their ids.

Runs on the claims repository's Python (httpx, PyYAML).
"""

import argparse
import csv
import json
import os
import sys
import time

import httpx
import yaml

MAX_OBJECTS, MAX_EDGES, MAX_BYTES = 800, 4000, 10_000_000
SKIPPED_GROUPS = {"prefixes", "id", "c2m2_files"}

try:
    Loader = yaml.CSafeLoader
except AttributeError:
    Loader = yaml.SafeLoader


def parts(document):
    """A document's node and edge groups as deposits within the limits: {group: [items]} dicts.

    A deposit must hold at least one object, so a part that would hold edges only (a document with more edges than
    one deposit takes) repeats the document's first object; a repeated object changes nothing.
    """
    anchor = None
    for part in _split(document):
        nodes = [(g, v) for g, v in part.items() if not g.endswith("_edges")]
        if nodes and anchor is None:
            anchor = (nodes[0][0], nodes[0][1][0])
        if not nodes and anchor is not None:
            part = dict([(anchor[0], [anchor[1]])], **part)
        yield part


def _split(document):
    part, objects, edges, size = {}, 0, 0, 0
    for group, items in document.items():
        if group in SKIPPED_GROUPS or not isinstance(items, list):
            continue
        is_edge = group.endswith("_edges")
        for item in items:
            item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
            if part and (size + item_size > MAX_BYTES or (objects >= MAX_OBJECTS and not is_edge)
                         or (edges >= MAX_EDGES and is_edge)):
                yield part
                part, objects, edges, size = {}, 0, 0, 0
            part.setdefault(group, []).append(item)
            size += item_size
            objects, edges = objects + (not is_edge), edges + is_edge
    if part:
        yield part


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api", required=True, help="claims service base URL, e.g. http://localhost:8110")
    parser.add_argument("--collection", default="cfde-genesets")
    parser.add_argument("--release", required=True)
    parser.add_argument("--manifest", required=True, help="the cfde_ stage's manifest (relative_path, role, library, ...)")
    parser.add_argument("--tree-dir", required=True)
    parser.add_argument("--libraries", default=None, help="comma-separated: only these libraries")
    parser.add_argument("--limit", type=int, default=None, help="at most this many documents per library (a test)")
    parser.add_argument("--open", action="store_true", help="open the release first")
    parser.add_argument("--close", action="store_true", help="close the release after the last document")
    parser.add_argument("--state-file", default=None, help="documents already sent (one relative path per line)")
    args = parser.parse_args(argv)
    key = os.environ.get("CLAIMS_API_KEY")
    if not key:
        sys.exit("set CLAIMS_API_KEY to a key granted the collection")
    wanted = set(args.libraries.split(",")) if args.libraries else None
    with open(args.manifest, newline="") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter="\t") if r["role"] == "provenance"
                and (wanted is None or r["library"] in wanted)]
    if args.limit:
        seen, kept = {}, []
        for r in rows:
            seen[r["library"]] = seen.get(r["library"], 0) + 1
            if seen[r["library"]] <= args.limit:
                kept.append(r)
        rows = kept
    done = set()
    if args.state_file and os.path.exists(args.state_file):
        with open(args.state_file) as fh:
            done = {line.strip() for line in fh if line.strip()}

    client = httpx.Client(base_url=args.api.rstrip("/"), headers={"X-API-Key": key}, timeout=600)

    def post(path, body, ok=(200, 201)):
        for attempt in range(1, 5):
            try:
                response = client.post(path, json=body)
            except httpx.TransportError as error:
                if attempt == 4:
                    raise
                print("  retrying after %s" % error, file=sys.stderr)
                time.sleep(5 * attempt)
                continue
            if response.status_code in ok:
                return response.json() if response.content else {}
            if response.status_code >= 500 and attempt < 4:
                time.sleep(5 * attempt)
                continue
            sys.exit("POST %s -> %d: %s" % (path, response.status_code, response.text[:2000]))

    if args.open:
        print("opened", json.dumps(post("/v1/collections/%s/releases" % args.collection, {"version": args.release}))[:300])
    totals = {"documents": 0, "parts": 0, "objects": 0, "edges": 0, "skipped_c2m2_files": 0, "unresolved_references": 0}
    started = time.time()
    for n, row in enumerate(rows, 1):
        if row["relative_path"] in done:
            continue
        with open(os.path.join(args.tree_dir, row["relative_path"])) as fh:
            document = yaml.load(fh, Loader=Loader)
        totals["skipped_c2m2_files"] += len(document.get("c2m2_files") or [])
        for part in parts(document):
            receipt = post("/v1/deposits", {"collection": args.collection, "release": args.release, "document": part})
            totals["parts"] += 1
            totals["objects"] += sum(len(v) for g, v in part.items() if not g.endswith("_edges"))
            totals["edges"] += sum(len(v) for g, v in part.items() if g.endswith("_edges"))
            totals["unresolved_references"] += len(receipt.get("unresolved_references") or [])
        totals["documents"] += 1
        if args.state_file:
            with open(args.state_file, "a") as fh:
                fh.write(row["relative_path"] + "\n")
        if totals["documents"] % 200 == 0 or n == len(rows):
            print("%d / %d documents (%s), %.0f s: %s" % (n, len(rows), row["library"], time.time() - started, totals),
                  flush=True)
    if args.close:
        print("closed", json.dumps(post("/v1/collections/%s/releases/%s:close" % (args.collection, args.release), {}))[:500])
    print(json.dumps(totals))


if __name__ == "__main__":
    main()
