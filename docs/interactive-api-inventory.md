# Interactive CFDE API inventory

**Verified:** 2026-09-24. **Host:** `https://dev.cfdeknowledge.org`. **Model:** `cfde-inc-v2`. These are live, read-only query probes, including the supplied POST examples. Complete requests, responses, timing, and hashes are saved in [the fixtures directory](../data/interactive/2026-09-24/).

This API is the proposed primary graph-search/expansion backend. The [BioIndex inventory](api-discovery.md) remains relevant for bulk factor ingestion and source-level diagnostics. Its pagination and coverage observations must not be applied to this separate API automatically.

## 1. Factor catalog

```http
GET /api/interactive/catalog?entity_type=factor&q=age&limit=8&model=cfde-inc-v2
```

[Recorded response](../data/interactive/2026-09-24/catalog-factor-age.json): HTTP 200, eight `items`.

Each item contains `node_id`, `node_type`, `node_key`, `label`, and `subtitle`. The first two exactly match the factors supplied in the request. Preserve the returned `node_id` as the opaque upstream identity and retain the full item for subsequent anchor payloads.

Observed factor identity shape:

```text
factor:{trait_group}:{phenotype_key}:{model}:{factor_key}
factor:gcat_trait:gcat_trait_language_measurement:cfde-inc-v2:Factor5
factor:portal:CADinT2D:cfde-inc-v2:Factor1
```

This adds `trait_group` to the earlier application factor key. Store the interactive ID as an alias on the source factor record. All eight `age` results could be matched to the September 15 BioIndex snapshot using its existing `trait_group`, phenotype key, model, and factor fields, with identical labels. This is an eight-record cross-check, not proof that the entire current catalog equals that snapshot.

Catalog search behavior is broader than exact disease selection: `q=T2D` returned `CADinT2D` and other related trait names among the first eight items. Confirm the selected item's ID and subtitle; never assume the first hit is the disease typed. No pagination cursor or total count appeared in these responses. Full enumeration, fuzzy matching behavior, and semantic search are not established by these probes. Semantic suggestions should come from the application's embedding index.

## 2. Connection expansion

```http
POST /api/interactive/connections
Content-Type: application/json
```

The saved requests contain the exact two supplied anchors and labels. Their shared parameters are:

```json
{
  "anchor_items": ["full catalog items; see request fixtures"],
  "context": "",
  "target_type": "gene_set",
  "reducer": "mean",
  "connection_scope": "direct",
  "limit": 100,
  "exclude_node_ids": ["the two selected factor IDs"],
  "model": "cfde-inc-v2"
}
```

The abbreviated anchor strings above explain the envelope; **use the object payloads in the fixtures for replay**.

Four target types were exercised with the same anchor set:

- [Genes](../data/interactive/2026-09-24/connections-gene.json): HTTP 200, **0 candidates**, 0 nodes, 0 edges.
- [Gene sets](../data/interactive/2026-09-24/connections-gene_set.json): HTTP 200, **63 candidates**, 63 nodes, 63 edges.
- [Traits](../data/interactive/2026-09-24/connections-trait.json): HTTP 200, **0 candidates**, 0 nodes, 0 edges.
- [Factors](../data/interactive/2026-09-24/connections-factor.json): HTTP 200, **0 candidates**, 0 nodes, 0 edges.

Repeating gene/trait/factor queries with only the first factor also returned zero. Therefore these empty results are not explained solely by requiring support from both selected anchors. Coverage, direction, thresholds, and relation-family availability still need source-owner confirmation.

### Response contract observed

The envelope has `candidates`, `graph`, and `candidate_count`. Each candidate includes:

- `candidate`: the node item, with both `node_id`/`node_type` and graph aliases `id`/`type` in the tested responses.
- `aggregate_score`, `raw_max_score`, `raw_mean_score`.
- `support_path_count`, `support_anchor_count`, `anchor_count`.
- `supporting_paths`: `anchor_id`, `family`, `relation`, `raw_score`, `normalized_score`, `path_nodes`, `extra`.
- `edges`: the supporting graph edges for that candidate.

Edges contain `id`, `source`, `target`, `weight`, `label`, `relation`, `family`, `raw_score`, `normalized_score`, `path_nodes`, and `extra`.

**Graph fragments omit anchor nodes.** The 63 returned gene-set nodes do not include the two factor anchors, although edge sources reference them. The application must merge its original anchors with the response graph before validating endpoint references. Do not display the response as a self-contained graph.

**DAPPER GeneSet references:** all 71 distinct gene-set IDs across these captured fixtures resolve to the full `cfde-inc-v2` catalog import. The backend will retain the interactive node ID and attach its DAPPER GeneSet digest using the selected complete import. See [the GeneSet inventory and mapping contract](geneset-import.md); this provides catalog/import provenance while full members and original creation history remain future enrichment.

**Scores are retrieval quantities.** One returned gene set has one supporting anchor out of two, raw maximum `0.9682999849`, raw mean `0.4841499925`, normalized path score `4.2407725548`, and aggregate score `2.1203862774`. This example is consistent with the mean counting an unsupported anchor as zero. It is not sufficient to establish the general formula. Preserve the supplied values and support counts; do not label normalized weights or aggregate scores as probabilities or biological confidence.

`candidate_count` matched returned list length in each probe. No separate pre-limit count, truncation flag, or continuation cursor appeared. A response at the requested limit is potentially clipped. Do not claim corpus completeness, infer that `candidate_count` is the unbounded total, or copy BioIndex continuation handling into this adapter.

### Control queries and membership

A catalog-selected **CADinT2D Factor1** returned eight genes and eight gene sets at `limit=8`, but no traits. These responses show that gene expansion works for some factor anchors. They do not establish universal factor/trait reachability.

Using an exact gene-set candidate from the supplied example as the sole anchor returned eight genes at `limit=8`. The source explicitly labels the paths and edges **`gene_gene_set_membership`**, with raw/normalized scores of 1.0. See [the membership probe](../data/interactive/2026-09-24/geneset-direct-gene.json).

This revises the earlier limitation: the old BioIndex membership routes lacked v2 coverage, but this interactive route **does return membership edges under a `cfde-inc-v2` request**. Its underlying membership data/model provenance is not included in `extra` in this sample and should be clarified with the maintainers. Preserve this endpoint's identity and request model rather than claiming it came from the old membership index.

Factor → gene set → gene would require a second expansion round if the gene was not already selected. The revised default workflow expands the original anchors once; this control query demonstrates an available future/manual operation, not an instruction to add recursive expansion.

### Validation probes

- Empty `anchor_items` returns HTTP 200 with an empty graph. The application must reject an empty EAGGL anchor set at its own API/worker boundary. The v3 design requires at least one EAGGL factor; there is no DisMech-only run.
- An invalid `target_type` returns HTTP 422 and declares the allowed values `gene|trait|factor|gene_set`.
- We tested `reducer=mean`, `connection_scope=direct`, and empty `context`. Other reducers/scopes and nonempty-context semantics remain unverified. Passing the knowledge-gap prose as `context` should wait for confirmation of what this field does.

## 3. Contextual edges

```http
POST /api/interactive/contextual-edges
Content-Type: application/json
```

Request fields: `node_ids` and `model`. The supplied set of **17 IDs** (two factors and fifteen gene sets) returned HTTP 200 with **15 edges** in an `edges` array. The edge fields match those in the connection response, including `family`, scores, and `path_nodes`. See [exact request and result](../data/interactive/2026-09-24/contextual-edges-user-example.json).

Sending just the two factor IDs returned an empty edge list. This endpoint enriches the selected graph with relationships among supplied nodes in the tested examples. It does not return prose context or a complete evidence package. The backend still assembles knowledge-gap text, DisMech records/evidence, provenance, and graph interpretation notes for the agent.

Application procedure: freeze the selected anchors; fetch four direct connection result sets; merge and bound selected nodes; request contextual edges for that node set; deduplicate edge occurrences while retaining every query's provenance. Validate both edge endpoints against selected nodes and preserve supporting path information. Do not silently promote unexpected endpoint IDs into additional expansion seeds.

## 4. Documentation, availability, and remaining API questions

`/openapi.json` returned 404. `/api/openapi.json` returned HTTP 200 **HTML for the playground**, not a JSON specification. This demonstrates why the adapter must check content type and response shape as well as status. There may be documentation elsewhere; no authoritative OpenAPI document was found at these two locations.

All intended example requests succeeded without an authentication header. That is an observation about this development endpoint, not a production access or availability guarantee. Single-request timings are captured for diagnosis, not a service-level benchmark.

Questions for the maintainers before finalizing the contract:

1. Which anchor/target combinations and edge families are supported with `direct`? What causes the gene/trait/factor empties above?
2. What are the exact reducer and score-normalization rules, including missing-anchor handling?
3. Does `context` filter/rank graph results, perform semantic retrieval, or have another role?
4. What are the limits on anchors, target count, contextual node IDs, request size, concurrency, and pagination?
5. How are graph/model build versions, membership sources, and underlying assertion locators exposed?
6. Are edge IDs stable across requests/builds? Are factor/gene-set IDs stable, and how is species represented for `gene:SYMBOL`?
7. What are the production endpoint, authentication requirements, and refresh expectations?

## 5. Repeat the inventory

**September 25 CAD-in-T2D follow-up:** the [contextual query](../data/interactive/2026-09-25/cad-in-t2d-contextual-edges.json) supplied the factor plus the eight genes and eight gene sets from the September 24 CAD captures. It returned 16 edges, all exactly equal to the saved direct-expansion edges: eight `factor_gene_direct` and eight `factor_gene_set_direct`. No distinct contextual relationships or gene-set membership edges were added. This is a separate retrieval time, not evidence of an immutable shared graph build. The [worked claim model](pigean-claim-model.md) also records two bounded BioIndex source-row checks and the distinction between factor loadings and gene/trait scores.

```bash
python3 scripts/probe_interactive_api.py --output data/interactive/NEW_CAPTURE
```

If the local Python distribution lacks certificate roots on macOS, set `SSL_CERT_FILE=/etc/ssl/cert.pem` for the command. TLS verification stays enabled. Probes are bounded and read-only; the script records failed/empty responses rather than inventing missing edges.
