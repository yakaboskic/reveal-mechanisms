# Collect and build an evidence package

The entry point takes **a DisMech knowledge-gap ID and one or more EAGGL factor IDs**. It resolves the local DisMech records, calls CFDE's interactive and BioIndex APIs, resolves imported DAPPER GeneSets, and writes the evidence package plus every captured source needed to rebuild it.

Code: [CLI](../scripts/build_evidence_package.py), [collector](../services/backend/src/reveal_backend/evidence_collector.py), [deterministic builder](../services/backend/src/reveal_backend/evidence_package.py).

## Run from IDs

From the repository root, install the optional dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e 'services/backend[evidence]'
```

Collect the CAD example:

```bash
.venv/bin/python scripts/build_evidence_package.py collect \
  --dismech-id cad_pgsxc_reverse_causation \
  --factor factor:portal:CADinT2D:cfde-inc-v2:Factor1 \
  --limit 8 \
  --output data/evidence-captures/cad-example
```

Omit `--limit 8` to use the default cap of 100 rows/candidates per query. Repeat `--factor` for multiple anchors, up to ten. The collector accepts full interactive IDs, not bare `Factor1`, legacy `CAD::Factor1`, or a DAPPER digest without its CFDE alias.

For an embedded EAGGL search hit, first use the populated [routing crosswalk](eaggl-cfde-links.md#application-lookup). For example, `lookup --factor-id 'AD::Factor1'` returns `factor:portal:AD:cfde-inc-v2:Factor1`; pass that returned ID to `--factor`. The application adapter will pin the source hit/mapping run with the request and use the existing collector unchanged. Matching requires exact trait/factor number only, irrespective of labels/genes. Missing top-GeneSet summary links do not prevent anchor selection; required retained GeneSets still follow the collector's hydration rules below.

`--dismech-id` accepts either the unambiguous source `discussion_id` above or the full ID:

```text
dismech:disorders/Coronary_Artery_Disease#discussion:cad_pgsxc_reverse_causation
```

The output directory must be new. Collection makes public read requests and creates local files. It does not write to MySQL, call an embedding provider, start a Box, generate claims, or query Proto-OKN.

### Configurable local dependencies

The repository already contains the frozen DisMech index, complete GeneSet export and DAPPER snapshot. Defaults are:

| Option | Default |
|---|---|
| `--model` | `cfde-inc-v2` |
| `--dismech-source` | Sibling `../dismech` checkout |
| `--dismech-index` | `data/dismech-gaps/2026-09-24` |
| `--geneset-import` | `data/cfde-genesets/2026-09-24` |
| `--dapper-snapshot` | `data/dapper/2026-09-24-v8` |
| `--max-nodes` | 250, including all selected anchors |
| `--max-edges` | 1,000 |

DisMech source files must match the index's recorded hashes. If the checkout has changed, select the corresponding source revision or regenerate the index with the existing extraction workflow. An ambiguous short gap ID fails instead of choosing a disease arbitrarily. Linked mechanisms, other attached context and up to ten related same-document gaps are included; unresolved attachments remain explicitly unresolved.

GeneSet aliases are resolved against the checksummed completed local import. Existing DAPPER objects and their encoding Activity are reused. An unknown retained alias fails; no replacement GeneSet or membership assertion is invented. The service's future MySQL adapter can provide the same source-to-object mappings without changing the package model.

## Retrieval sequence

| Operation | Calls and purpose |
|---|---|
| Interactive `GET /api/interactive/catalog` | Per distinct trait: look up factor labels and exact interactive IDs. |
| BioIndex `GET /api/bio/query/pigean-factor` | Per trait: verify the selected factor's group, trait, model and source label. If catalog search omits it, an exact matching BioIndex row can supply its anchor metadata. |
| Interactive `POST /api/interactive/connections` | Four parallel calls, targeting gene, gene_set, trait and factor, all using the same frozen selected anchors. |
| Interactive `POST /api/interactive/contextual-edges` | One call for the retained bounded nodes. |
| BioIndex `pigean-gene-factor`, `pigean-gene-set-factor` | Two calls per selected factor, retaining mechanism loadings and row-level trait statistics. |
| BioIndex `pigean-gene-phenotype`, `pigean-gene-set-phenotype` | Two calls per distinct trait, retaining additional trait-level observations for the retained entities. |

One factor in one trait requires **11 successful requests**. Transient failures allow at most one retry per request. Each attempt and its exact response bytes are saved. The client uses verified TLS, a 30-second per-request timeout, and an 8 MB response limit. A terminal failure leaves the source captures and `collection-error.json`; it does not emit a misleading successful package.

This is one graph expansion round. Returned factors never become new anchors. Neither DisMech context nor a semantic match is sent as a CFDE graph node. The caller supplies selected EAGGL IDs; this collector does not compute or fabricate semantic similarity.

Candidate selection is deterministic: aggregate score descending, full node ID ascending for ties, with every selected anchor retained. Edges whose endpoints/path nodes are outside that set are omitted; an edge cap uses stable edge-ID order and records omissions. Exact repeated edges merge their source locators. Conflicting observations with the same edge ID fail validation.

The BioIndex queries are deliberately bounded. Reaching the row limit, or receiving `continuation: null`, does not establish completeness. The package records query limits, returned counts and source progress. It does not claim that absent retained entities or empty queries prove biological absence.

## Output and offline replay

```text
cad-example/
  build-input.json              Frozen builder configuration, bindings and artifact hashes
  inputs/                       Exact source bodies, HTTP attempt records and instructions
  package/
    evidence-package.yaml       Readable agent input
    evidence-package.json       Canonical JSON representation
    manifest.json               Package/file hashes and validation counts
    sources/                    All referenced artifacts, named by content hash
```

Replay from the saved inputs without any API calls:

```bash
.venv/bin/python scripts/build_evidence_package.py replay \
  --input data/evidence-captures/cad-example/build-input.json \
  --output data/evidence-captures/cad-example-replayed
```

The replay input uses paths relative to its containing directory. `--source-root` can explicitly supply another artifact root. Paths may not escape that root. Copying the whole capture to a different location does not change the output. Writing an identical package to an existing output is idempotent; conflicting output is rejected.

**Determinism means identical frozen inputs, policy and pinned code produce identical package bytes and SHA-256 hashes.** Starting a new live collection may produce different responses, nonces, retrieval times or source versions, so the same IDs do not promise the same hash across live runs.

The builder is a pure assembly function. It generates no clock values, random IDs, embeddings or model-written text. JSON uses sorted keys, UTF-8, finite numbers, compact separators and a final newline; this is a versioned local serialization, not a claim of RFC 8785 compliance. YAML rendering is pinned to PyYAML 6.0.2. Existing DAPPER payloads and meaningful within-object list order are preserved; unordered document collections and source maps are sorted explicitly.

Each capture pins the builder source hash, saves the collector/builder source, freezes the authoring instructions, and records the DAPPER dependency manifest. Replaying with changed builder code fails clearly. Treat migration to a new builder version as a new assembly, not an overwrite of a historical package.

## Validation and agent handoff

The package now has a [LinkML schema and generated JSON Schema](../schema/README.md). Run `python scripts/evidence_package_schema.py validate path/to/evidence-package.yaml` to check its structure and pinned DAPPER identities without changing the capture. The schema targets `0.2-draft`; existing builder source pins and replay bytes remain unchanged.

The builder checks source checksums/locators; agreement between exact HTTP bodies and parsed responses; required BioIndex query coverage; exact gap text and DAPPER identities; model/trait/factor scope; consistent seed sets; finite metrics; edge endpoint integrity; prefix bindings; GeneSet alias mappings; dependency closure; and node/edge/package-byte budgets. Unknown fields at the build-input root and malformed/duplicate JSON/YAML keys are rejected. All DAPPER input nodes undergo closed-schema shape checks.

The output is `reveal.evidence-package/0.2-draft`, implementing the [evidence-package design](evidence-package.md). Compared with the earlier hand-authored 0.1 example, graph observations carry all source locators, and trait associations contain an `observations` list so factor-query and phenotype-query occurrences do not overwrite each other. Multiple occurrences are not independent corroboration. `readiness.input_capture_complete` records collection completeness; it does not claim agent dispatch has been authorized.

The final worker must still bind trusted attribution and job ownership, measure tokens for the selected model, and enforce runtime/tool budgets before dispatch. The builder's byte cap is not a substitute for that tokenizer check. Proto-OKN enrichment remains a later append-only ledger; this initial package does not fabricate external assertions or a ScientificAccount.

### Initial model reading and token measurement

The live worker measures the exact research prompt and `dispatch-view.json` with the configured model's token-count endpoint before execution. The default 24,000-token preparation limit applies to those initial bytes; it excludes harness instructions and subsequent source/tool reads and is not a total-run token cap. Runtime cost, duration and tool limits apply separately.

The reading view contains the scientific fields, selected identities, scores, source references and coverage from the chosen canonical package. It defers the repeated `source_artifacts` catalogue and `dapper_context.files` collection to exact read-only lookups. The full canonical package and every captured source remain available unchanged. The view is a separate application document, not a replacement schema-valid evidence package. Agents must read exact cited source rows before authoring; metadata deferral does not authorize invented provenance or interpretations.

The frozen dispatch manifest binds the package, view, prompt, model and measurement hashes. Candidate reduction still uses the deterministic builder, preserves every selected anchor, and records omissions. Sidecars live outside the builder-owned package directory so interrupted manifest publication can recover without changing its immutable file set. Legacy frozen inputs retain their earlier full-package measurement scope.

### Python entry points

`collect_package(...)` accepts factor/gap IDs and configured source adapters; it returns a `BuiltPackage`. `build_package(spec, blobs, runtime)` accepts frozen bytes directly for service/worker integration. `BuiltPackage.write(directory)` publishes a fully validated bundle through a temporary directory.

### Tests

```bash
PYTHONPATH=services/backend/src .venv/bin/python -m unittest discover \
  -s services/backend/tests -p 'test_evidence_package.py' -v
```

Tests use captured rows with a fake HTTP transport. They cover exact call inputs, multiple anchors sharing genes, collection/replay, directory independence, reordered input maps, edge deduplication/conflicts, missing queries/mappings, response-body fidelity, changed source hashes, prefix resolution, limits and retries. The live API check is separate from these reproducible offline tests.
