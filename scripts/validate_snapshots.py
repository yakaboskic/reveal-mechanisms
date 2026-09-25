#!/usr/bin/env python3
"""Validate local extraction counts, composite IDs, checksums, and model scope."""
import gzip
import hashlib
import json
from pathlib import Path


def rows(path):
    with gzip.open(path, "rt") as stream:
        yield from (json.loads(line) for line in stream)


def main():
    root = Path(__file__).resolve().parents[1] / "data"
    dismech = json.loads((root / "dismech/manifest.json").read_text())
    assert dismech["complete"] and not dismech["errors"], "Incomplete DisMech snapshot"
    for filename, entry in dismech["files"].items():
        path = root / "dismech" / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], filename
        seen, count = set(), 0
        for row in rows(path):
            count += 1
            if "id" in row:
                assert row["id"] not in seen, (filename, row["id"])
                seen.add(row["id"])
        assert count == entry["rows"], (filename, count)
    cfde = json.loads((root / "cfde/manifest.json").read_text())
    assert cfde["complete"] and not cfde["errors"], "Incomplete CFDE snapshot"
    assert cfde["successful_queries"] == cfde["phenotype_model_keys"]
    path = root / "cfde/factors.jsonl.gz"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == cfde["sha256"]
    factors = list(rows(path))
    assert len(factors) == cfde["factor_records"]
    assert len({r["id"] for r in factors}) == len(factors)
    assert all(r["raw"]["gene_set_size"] == cfde["model"] for r in factors)
    t2d = json.loads((root / "cfde/t2d/manifest.json").read_text())
    assert t2d["complete"]
    for query in t2d["queries"]:
        d = json.loads((root / "cfde/t2d" / query["file"]).read_text())
        assert d["complete"] and len(d["data"]) == query["row_count"] == d["row_count"]
        assert all(r.get("gene_set_size", t2d["model"]) == t2d["model"] for r in d["data"])
    print(f"Validated DisMech ({dismech['mechanism_records']:,} mechanisms), CFDE ({len(factors):,} factors), and {len(t2d['queries'])} T2D queries.")


if __name__ == "__main__":
    main()
