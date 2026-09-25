#!/usr/bin/env python3
"""Snapshot the public CFDE BioIndex; resumable and model-scoped.

No explicit query limit is sent: BioIndex's limit truncates the result and can
return continuation=null even when bytes_read < bytes_total.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = "https://cfde-dev.hugeampkpnbi.org"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def fetch(url):
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers={"User-Agent": "RevealMechanisms-DataDiscovery/0.1"}), timeout=60) as response:
                return json.load(response)
        except (OSError, ValueError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            delay = min(2 ** attempt, 16)
            if isinstance(exc, HTTPError):
                try:
                    delay = max(delay, float(exc.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
            time.sleep(delay)


def query(root, index, keys):
    cache_id = hashlib.sha256(json.dumps([index, keys]).encode()).hexdigest()[:24]
    path = root / "raw" / f"{index}-{cache_id}.json"
    if path.exists():
        result = json.loads(path.read_text())
        if result.get("complete"):
            return result
    url = BASE + "/api/bio/query/" + index + "?" + urlencode({"q": ",".join(keys)})
    first_url = url
    rows, pages, seen = [], [], set()
    while True:
        page = fetch(url)
        if not isinstance(page.get("data"), list):
            raise ValueError(f"Unexpected response for {index}: {list(page)}")
        rows.extend(page["data"])
        pages.append({k: v for k, v in page.items() if k not in ("data", "nonce", "continuation")})
        token = page.get("continuation")
        if not token:
            progress = page.get("progress", {})
            if progress.get("bytes_read", 0) < progress.get("bytes_total", 0):
                raise ValueError(f"Truncated query without continuation: {index} {keys}")
            break
        if token in seen or len(pages) >= 10000:
            raise ValueError("Repeated continuation or excessive pages")
        seen.add(token)
        url = BASE + "/api/bio/cont?" + urlencode({"token": token})
    result = {"url": first_url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "index": index, "keys": keys, "complete": True,
              "restricted": max((p.get("restricted", 0) for p in pages), default=0),
              "pages": pages, "row_count": len(rows), "data": rows}
    write_json(path, result)
    return result


def normalize_factors(results, model):
    factors, anomalies = [], []
    for result in sorted(results, key=lambda x: x["keys"]):
        phenotype_key = result["keys"][0]
        for row in result["data"]:
            if row.get("gene_set_size") != model:
                raise ValueError("Source row escaped the requested model scope")
            if row.get("phenotype") != phenotype_key:
                if str(row.get("phenotype")).casefold() != phenotype_key.casefold():
                    raise ValueError("Source row escaped the requested phenotype scope")
                anomalies.append({"type": "phenotype_case_mismatch", "query_key": phenotype_key,
                                  "returned_phenotype": row["phenotype"], "factor": row["factor"],
                                  "source_url": result["url"]})
            factors.append({"id": "cfde:" + ":".join([model, phenotype_key, row["factor"]]),
                            "phenotype_key": phenotype_key, "source_url": result["url"],
                            "retrieved_at": result.get("retrieved_at"), "raw": row})
    if len({x["id"] for x in factors}) != len(factors):
        raise ValueError("Duplicate composite factor IDs")
    return factors, anomalies


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("data/cfde"))
    p.add_argument("--model", default="cfde-inc-v2")
    p.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    p.add_argument("--scope", choices=["all-factors", "t2d"], default="all-factors")
    args = p.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    if args.scope == "all-factors":
        for name, route in [("openapi", "/openapi.json"), ("indexes", "/api/bio/indexes"),
                            ("factor-keys", "/api/bio/keys/pigean-factor/2")]:
            write_json(root / f"{name}.json", fetch(BASE + route))
        key_data = json.loads((root / "factor-keys.json").read_text())
        keys = sorted({tuple(k) for k in key_data["keys"] if k[1] == args.model})
        results, errors = [], []
        started = datetime.now(timezone.utc).isoformat()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            pending = {executor.submit(query, root, "pigean-factor", list(k)): k for k in keys}
            for n, task in enumerate(concurrent.futures.as_completed(pending), 1):
                try:
                    results.append(task.result())
                except Exception as exc:
                    errors.append({"keys": pending[task], "error": str(exc)})
                if n % 100 == 0 or n == len(keys):
                    print(f"Factors: {n}/{len(keys)} phenotype/model queries; {len(errors)} errors", flush=True)
        factors, anomalies = normalize_factors(results, args.model)
        with gzip.open(root / "factors.jsonl.gz", "wt", encoding="utf-8") as stream:
            for row in factors:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        manifest = {"source": BASE, "model": args.model, "started_at": started,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "phenotype_model_keys": len(keys), "successful_queries": len(results),
                    "factor_records": len(factors), "restricted_queries": sum(bool(r["restricted"]) for r in results),
                    "query_retrieved_at_min": min((r["retrieved_at"] for r in results), default=None),
                    "query_retrieved_at_max": max((r["retrieved_at"] for r in results), default=None),
                    "complete": not errors, "errors": errors,
                    "source_anomalies": anomalies,
                    "scope": "All phenotype keys advertised by pigean-factor/2 for this model; other models excluded",
                    "sha256": hashlib.sha256((root / "factors.jsonl.gz").read_bytes()).hexdigest()}
        write_json(root / "manifest.json", manifest)
        print(json.dumps(manifest), flush=True)
        if errors:
            raise SystemExit(1)
    else:
        seed = {}
        def take(index, keys):
            result = query(root, index, keys)
            name = index + ("-" + keys[-1] if "factor" in index and index != "pigean-factor" else "")
            seed[name] = result
            print(f"T2D: {name}: {result['row_count']} rows", flush=True)
            return result
        factors = take("pigean-factor", ["T2D", args.model])
        for index in ["pigean-gene-phenotype", "pigean-gene-set-phenotype"]:
            take(index, ["T2D", args.model])
        gene_set_key = None
        for factor in factors["data"]:
            for index in ["pigean-gene-factor", "pigean-gene-set-factor"]:
                result = take(index, ["T2D", args.model, factor["factor"]])
                if index == "pigean-gene-set-factor" and result["data"] and gene_set_key is None:
                    gene_set_key = result["data"][0]["gene_set"]
        for index in ["pigean-gene", "pigean-gene-gene-sets"]:
            take(index, ["IRS2", args.model])
        if gene_set_key:
            take("pigean-gene-set-genes", [gene_set_key, args.model])
            take("pigean-gene-set", [gene_set_key, args.model])
            take("pigean-joined-gene-set", ["T2D", gene_set_key, args.model])
        take("pigean-joined-gene", ["T2D", "IRS2", args.model])
        for index in ["pigean-gene-gene-sets", "pigean-gene-set-genes"]:
            write_json(root / "t2d" / f"{index}-models.json", fetch(BASE + "/api/bio/keys/" + index + "/2?columns=gene_set_size"))
        for name, result in seed.items():
            write_json(root / "t2d" / f"{name}.json", result)
        write_json(root / "t2d" / "manifest.json", {"model": args.model, "complete": True,
                   "scope": "T2D factors and direct expansion; IRS2 and one gene set demonstrate second-hop routes",
                   "queries": [{"file": name + ".json", "row_count": r["row_count"], "url": r["url"]} for name, r in seed.items()]})


if __name__ == "__main__":
    main()
