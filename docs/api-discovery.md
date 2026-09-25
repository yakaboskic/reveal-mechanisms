# CFDE BioIndex — verified query recipes

**Scope update, September 24:** this document records the earlier BioIndex service. Use the [interactive API inventory](interactive-api-inventory.md) for the new primary graph backend. In particular, its gene-set expansion returns membership edges under `cfde-inc-v2`, beyond the membership coverage observed here.

Observed 2026-09-15 against [the provided API documentation](https://cfde-dev.hugeampkpnbi.org/docs). The downloaded [OpenAPI specification](../data/cfde/openapi.json) defines transport parameters; the [index catalog](../data/cfde/indexes.json) defines positional query keys. These sources are needed together because response schemas are largely unspecified in OpenAPI.

## Model selection

Use **`cfde-inc-v2`**. The API calls this selector `gene_set_size`, even though its value is a model/dataset configuration string. The initially supplied `cfde-inc` example is an older configuration and is not merged into this snapshot.

`q` is a comma-separated list of positional keys. Construct the value first and then URL-encode the entire query parameter. Do not insert gene-set labels or arbitrary user text directly into a URL. Confirm exact keys through discovery or returned rows.

## Discover available traits and models

```http
GET /api/bio/indexes
GET /api/bio/keys/pigean-factor/2
```

The second response uses `keys`, not `data`, and returns pairs `[phenotype, gene_set_size]`. Filter the second element to `cfde-inc-v2`: 3,778 advertised phenotype queries in this snapshot. The same catalog advertises 3,701 `cfde-inc` keys and 3,571 `cfde` keys; these are intentionally excluded from factor ingestion.

For membership model coverage:

```http
GET /api/bio/keys/pigean-gene-gene-sets/2?columns=gene_set_size
GET /api/bio/keys/pigean-gene-set-genes/2?columns=gene_set_size
```

Both returned `[["cfde"]]`. Keep these capability probes when refreshing the source; do not infer availability from index names alone.

`GET /api/bio/match/{index}?q=...` is documented as key matching; its exact matching semantics have not been exercised here. The product can initially search the local extracted catalog. `GET /api/bio/count/{index}?q=...` estimates rows, so it is not a completeness guarantee.

## Query recipes

### Disease → EAGGL factors

```http
GET /api/bio/query/pigean-factor?q=T2D%2Ccfde-inc-v2
```

Key order: `phenotype,gene_set_size`. T2D returns 2 rows. Preserve `factor`, `label`, `lambda`, `anchor_any_joint`, `anchor_any_marginal`, `top_genes`, `top_gene_sets`, `trait_group`, and `cluster`. The semicolon-delimited top lists are summaries; use detail endpoints for expansion.

Factor identity is `(model, query phenotype key, factor)`. The complete factor snapshot records one case-only source anomaly: the query key `gcat_trait_orofacial_cleft` returns `gcat_trait_Orofacial_cleft` for `Factor3`. Both strings are preserved and the anomaly is listed in the manifest; a source model mismatch or a different phenotype fails ingestion.

### Disease → associated genes

```http
GET /api/bio/query/pigean-gene-phenotype?q=T2D%2Ccfde-inc-v2
```

Key order: `phenotype,gene_set_size`. Observed 18,321 rows across 3 response pages. Fields include `gene`, `combined`, `log_bf`, `prior`, `factor`, `label`, and `n`. These are associations with separate score fields, not a list of proven causal disease genes.

### Disease → associated gene sets

```http
GET /api/bio/query/pigean-gene-set-phenotype?q=T2D%2Ccfde-inc-v2
```

Key order: `phenotype,gene_set_size`. Observed 5,000 rows across 2 pages. Fields include `gene_set`, `beta`, `beta_uncorrected`, `rs_score`, `source`, `n`, `factor`, and `label`. This is the model-specific form of the user’s final example, with no truncating `limit`.

### Factor → genes

```http
GET /api/bio/query/pigean-gene-factor?q=T2D%2Ccfde-inc-v2%2CFactor1
GET /api/bio/query/pigean-gene-factor?q=T2D%2Ccfde-inc-v2%2CFactor2
```

Key order: `phenotype,gene_set_size,factor`. Observed 541 and 502 rows. These add `factor_value` and `label_factor` to gene/trait score context. The first Factor1 response row is IRS2 with `factor_value=0.3122`; this is a source loading, not a calibrated claim-confidence score.

### Factor → gene sets

```http
GET /api/bio/query/pigean-gene-set-factor?q=T2D%2Ccfde-inc-v2%2CFactor1
GET /api/bio/query/pigean-gene-set-factor?q=T2D%2Ccfde-inc-v2%2CFactor2
```

Key order: `phenotype,gene_set_size,factor`. Observed 597 and 467 rows. Fields include `gene_set`, `factor_value`, `beta`, `beta_uncorrected`, and `rs_score`.

### Gene → other phenotypes

```http
GET /api/bio/query/pigean-gene?q=IRS2%2Ccfde-inc-v2
```

Key order: `gene,gene_set_size`. Observed 6,682 rows across 2 pages. Read each row’s `phenotype`; the route also returns the original phenotype when available, so explicitly exclude T2D only when the user requests “other” phenotypes. Keep broader trait types visible. There is no separate `pigean-gene-other-phenotypes` route in the observed catalog.

### Gene set → other phenotypes

```text
GET /api/bio/query/pigean-gene-set?q={gene_set},{gene_set_size}
```

For the exact gene-set key `LINCS_L1000__all_signatures__LINCS_L1000_Chem_Pert_BRD-K09454191_up` returned by the Factor1 detail route, this returned 82 rows with `cfde-inc-v2`. The summary label `mp_improved_glucose_tolerance` returned no records when used as a gene-set key; do not substitute factor labels for actual keys.

### Joined disease/gene/gene-set context

```text
GET /api/bio/query/pigean-joined-gene?q={phenotype},{gene},{gene_set_size}
GET /api/bio/query/pigean-joined-gene-set?q={phenotype},{gene_set},{gene_set_size}
```

T2D/IRS2/v2 returned 167 rows. T2D/the exact LINCS gene set above/v2 returned 262 rows. Both contain gene and gene-set identifiers with trait context and score fields. These are useful additional expansion inputs. The endpoint name and response shape alone do not establish whether every pair is direct membership or a source-defined join; inspect the upstream construction before turning these rows into `member_of` assertions.

### Gene-set membership

```text
GET /api/bio/query/pigean-gene-gene-sets?q={gene},{gene_set_size}
GET /api/bio/query/pigean-gene-set-genes?q={gene_set},{gene_set_size}
```

Both advertise key order as shown, but their available model catalog contains only `cfde`. Tested v2 queries returned 0 rows. The application should report **“Gene-set membership is unavailable for this model”**, retain the available factor/association graph, and leave membership edges absent. A future alternative export needs an explicit version/correspondence check.

### Additional catalog routes

`pigean-gene-set-source`, `pigean-overall-gene`, `pigean-phenotypes`, and `c2m2-provenance` are present in the index catalog. Their complete behavior and response meanings were not investigated in this pass. They are candidates for provenance enrichment and source browsing, not required dependencies of the first vertical slice.

## Pagination and completeness

An ordinary query response contains `index`, `q`, `count`, `restricted`, `progress`, `page`, `limit`, `data`, and `continuation`.

1. Omit `limit` for full ingestion. It is a result cap, not a conventional reusable page-size knob.
2. Accumulate `data` from the first response.
3. When a continuation token is present, call `GET /api/bio/cont?token={encoded_token}` and continue until exhausted. Tokens are opaque and are not safe substitutes for durable run identifiers.
4. At the final response, require `progress.bytes_read == progress.bytes_total` when supplied. Continuation-page progress is relative to that remaining reader/page state, so it can be smaller than the first response’s byte totals.
5. Reject an exhausted token with unread bytes, detect repeated tokens, and retain `restricted` as a coverage signal.
6. A zero-byte, zero-row result can be complete and still indicate unsupported source coverage.

Live counterexample: T2D/Factor1 gene retrieval with `limit=2` returned 2 rows, `continuation=null`, and `bytes_read=704` / `bytes_total=129163`. The uncapped request returned all 541 rows. The downloader’s regression tests include this failure mode.

The ingestion script retries transient errors with bounded backoff, honors numeric `Retry-After`, limits concurrency to four queries, caches complete query results, and fails the manifest when any advertised factor query fails. It preserves original rows and a source URL. Use a new output directory for a fresh snapshot; the same directory is for resuming the same capture.

## Source evidence and versions

The factor and association indexes have July 2026 build timestamps in the captured catalog, while membership indexes are older. Capture the catalog beside every snapshot. Data completeness here means all records exposed by the endpoint for the requested model/key, not the full statistical model output before source filtering or all possible biomedical relationships.

Current response fixtures, including empty memberships, are in [the T2D manifest](../data/cfde/t2d/manifest.json). They are real source results. No agent-generated claims or scientific conclusions were added to these files.
