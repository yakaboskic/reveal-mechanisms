# Scientific account provenance and reuse

`GET /v1/accounts/{account_id}/provenance` expands one accepted scientific account into dataset and organization lineage. It follows structured scientific references in propositions and evidence, including inherited source claims, retained factor–GeneSet projections and the published GWAS of the KPN traits the account reaches. Existing leaderboard definitions are unchanged. It is the only trace surface: there is no MCP trace tool.

Use the stored, minted `dapper:ScientificAccount.…` ID returned by account acceptance. A local authoring URN or a JSON filename is not an accepted account ID. Private accounts require their owner's application bearer credential; published accounts are readable anonymously. Withdrawal and revoked dependencies take effect on subsequent reads. Responses use `Cache-Control: private, no-store` and vary by authorization. Configure `REVEAL_GATEWAY_SECRET` with at least 32 bytes for signed provenance cursors.

```http
GET /v1/accounts/{account_id}/provenance?limit=100
Authorization: Bearer <application credential>
```

`payload_sha256` optionally selects an exact account payload. `limit` defaults to 100 and accepts 1–250. Follow `page.next_cursor` on the same endpoint until it is null; omit `limit` to retain the cursor's page size. Changed source observations, supplements, publication state, permissions, or an explicitly changed limit invalidate a cursor with `409 CURSOR_EXPIRED`. An inaccessible account or unavailable requested payload returns 404.

## How a request is served

The route reads in three phases and writes no file (`provenance_api.trace`):

1. **Authorize** (one application read transaction): the caller's identity, the authorized account source, its retained artifact records and the cursor secret.
2. **Hydrate** (no application lease): capture bytes from the artifact store, then the reference reads under the reference lease alone (`provenance_reference.expand`). Only the reload-era (legacy generation) path opens an application read, for registered recovery supplements.
3. **Portal** (no database lease): the bottom-line GWAS graph of every reached trait from the Translator provenance portal, within 15 seconds in all. A failure is coverage, never an error.
4. **Recheck and rank** (one short read transaction): identity, publication or ownership state and the withheld dependencies are read again; any change returns `409 CURSOR_EXPIRED`. Then the walk, the withheld filter, the snapshot and the page.

At most two traces per process are in their database phases at once.

## Reference hydration

Trusted captures bind an account's opaque ids to reference data, each checksum-verified against its retained bytes:

- research captures (`get_factor` binds Mechanisms, re-minted from the captured row; gene-set reads bind gene sets);
- Box evidence-package captures `reveal.reference-evidence.mysql-capture/2` (`reference-release`, `reference-factors`, `candidate-factors`), whose Mechanisms are re-minted with the captured release manifest exactly as the package builder minted them.

A capture pinned to the published reference release is read from its `ref_*` tables, after `ref_release` on the same connection (see [reference releases](reference-release.md)): `ref_factors` (checked against the captured row), every per-library `ref_projections` row, `ref_gene_sets` and `ref_collections`. Gene sets the account reaches get their full node; gene sets reached only by a projection get their name, collection and generating activity, never their members. Collection lineage merges the producer's provenance with the curated source datasets (`reference_lineage.collection_lineage`) and keeps only one gene set's closure (`lineage_closure`). Parsed lineage is cached per release, collection and document checksum (128 MB) and dropped when the release changes.

### Identity binding

A GeneSet or Mechanism the account references that no capture binds, and whose own lineage the document does not carry, binds by its content-addressed id to the **published release** alone (`binding_origin: document_identity`). Hosted (Box) accounts whose packages hold no reference capture trace this way.

- **GeneSet:** its id is a `ref_gene_sets` row; its node comes from `metadata.dapper_gene_set` and its lineage from the collection's `collection_lineage` / `lineage_closure`, as for a captured set. Only sets that no capture and no projection binds are looked up.
- **Mechanism:** its node's description names a public factor id, and the node minted from that factor's `ref_factors` + `ref_traits` row with the release manifest's identity version (`reference_generation.mechanism_node`, as the catalog and Box packages mint it) has exactly its id. Its projections, fitted trait and the trait's GWAS route then follow as for a captured factor.
- **Never names or prose:** a Mechanism whose description names no KPN factor (`identity_lookup: not_applicable`), a tampered or re-minted factor (`no_match`) and an environment with nothing published (`no_published_release`) stay unresolved; `identity_lookup` is reported in `coverage.retained_projection_coverage`.
- **Captures keep precedence**, and a capture and an identity binding that place one gene set in different releases or generations make it ambiguous and blocked (`ambiguous_reference_binding`).
- **Superseded work stays as it was:** an account whose captured work is pinned to a release that is no longer published binds nothing by identity (`identity_lookup: superseded_release`), so none of it is read from the current tables. Reload-era generations are never searched by identity.

Identity bindings are reported as `binding_origin: document_identity` in `coverage.retained_projection_coverage` and on the projection edges' `provenance_source`, with one `document_identity` source observation of the rows that bound.

### Curated lineage

Edges that the release build curated rather than read from the producer's document (`payload.curated_provenance`, from `lap/config/source_datasets.yaml`: the LINCS HZ3/HZ4 Level-5 signatures, IDG HZ2's Enrichr library, the GlyGen HZ2 patch) carry `provenance_source.kind: curated_lineage` with `curation_id` (the map's path) and `curation_sha256`. The stored data sources list them per connection as `curation_ids`.

The reload-era collection-lineage curation ([`data/cfde-provenance/`](../data/cfde-provenance/README.md): `scripts/apply_collection_lineage.py` patches the shared reload-era `cfde_gene_set_collections` with `used_edges` and a `payload.lineage_curation` marker, `scripts/verify_account_provenance.py` previews the endpoint) is reload-era tooling. Its edges are labelled the same way, with `recovery_status[].curated_lineage`, when a capture pinned to that generation reaches them. A published release does not read those tables: the 2026-10-05 producer documents it is built from already link most of the same inputs, and the differences are listed in [the curation README](../data/cfde-provenance/README.md#release-era).

A capture pinned to a release that is no longer published records `reference_release_superseded`. That work is hydrated only from capture bytes and the environment's archived factor snapshot (its top gene sets per library), never from the current tables. Traits are still named from the published release when other work of the account is bound to it; when all of it is superseded, cited traits are `trait_unavailable`. Missing reload-era tables (dropped by `cleanup`) record `reference_tables_unavailable` instead of failing.

## Traits and their GWAS

Every KPN trait the account reaches becomes a `trait` node: a bound Mechanism's fitted trait (`reveal:fittedOnTrait`), or a trait a proposition cites as `KPN.TRAIT:NNNNNNN` or by its kpn-data-models page. Names come from `ref_phenotypes`, else `ref_traits`. A trait unknown to the release is `trait_unavailable`.

The trait's GWAS route (`trait_gwas`) is its release metadata's bottom-line artifact (`metadata.bottom_line.gwas_artifact`, the Mixed ancestry). The portal's `get_provenance` document for that artifact is compacted in memory:

- `R`, the Dataset that holds the artifact's DRS object, is the only node the walk reaches: `trait —reveal:publishedGwas→ R`.
- The document's raw inputs (C2M2 files never generated by an activity and reachable from `R`) are attached by `reveal:portalTransitiveInput`, with their path length, the portal URL and the document checksum. They are shown but never traversed, and never raise missing lineage.

T2D Mixed compacts from 749 nodes to 74. Configure the portal with `REVEAL_PROVENANCE_PORTAL_URL` (set in `deploy/dig/service.yaml` and the local durable deployment). Unset, nothing is fetched and every trait GWAS route is `external_provenance_unavailable`. Graphs are cached in memory (64 MB): fresh for 24 hours, then served only if a refetch fails, for up to 7 days. A 404 or an invalid document is cached for 5 minutes; timeouts, 5xx and 429 never are (each is retried once, honouring `Retry-After` within the budget). Bodies are capped at 2 MiB and parsed in memory, and a fetch, its retry included, ends within 15 seconds even when the portal keeps sending. Each portal fetch is shared by every waiting request. Collection YAML is never fetched: gene-set lineage is local, and the portal is only a link-out for it.

Rare-disease lists have no GWAS (`not_a_gwas_trait`), and some traits are not on the portal (`not_on_portal`); the code is the release's `metadata.bottom_line.gwas_reason`. These, and GWAS Datasets that name no organization (`no_upstream_organizations`), are reported in `coverage.upstream_gaps`. They are gaps of the sources, so they do not make a trace incomplete.

## Response

- `account`, `claims`, `propositions`, and `evidence_items` contain `{record, provenance}` wrappers. `record` preserves the original scientific object. `provenance` adds the total trace count, current-page trace IDs/count, reached dataset and organization IDs, and resolution status.
- `traces` identify the originating claim, proposition/evidence item and source field, dataset reached, route classifications, input roles and organization attributions. `path_steps` describe an auditable witness path; reverse dataset membership steps are explicit.
- `nodes` and `edges` share the traversed graph across traces. Alternative recorded branches remain in the graph, without enumerating every path. Nodes are compact summaries rather than complete GeneSet member lists; a trait and the portal's GWAS nodes carry `href`, a public page built from their ids. The bounded shared graph repeats on each page.
- `dataset_reuse` and `organization_reuse` are full-account totals, computed before pagination. Do not sum totals from successive pages.
- `snapshot` pins the account payload, authorized document, publication version, reference generations and releases, supplements and tracing policy (`account-provenance-v2`). Source observations contain hashes and public scientific coordinates, not private storage locations. A `portal_bottom_line` observation is keyed by the portal URL read, with the checksum of the compacted graph, so a changed portal graph expires cursors.
- `coverage` separates lineage resolution, retained-projection coverage, recovery status and the optional `upstream_gaps`. Missing lineage makes counts explicit lower bounds. Processing limits and storage failures do not produce zero rankings.

The generated [OpenAPI contract](../api/openapi.json) provides exact types and a complete fixture response.

## Counting rules

A dataset contributes once to each distinct component/conclusion claim that reaches it, regardless of how many propositions, evidence items or inherited paths reach the same dataset. Component and conclusion membership is a set. An outside source claim supplies lineage but does not become another counted claim in this account.

Route classifications overlap: `proposition_reference`, `evidence_derivation`, `inherited_source_claim`, `factor_projection` and `trait_gwas`. Their subtotals must not be added. Evidence direction belongs to the originating evidence item; it is not multiplied through source claims. Referencing a factor does not mean every associated dataset directly supports the proposition.

Organization summaries count distinct datasets, claims and claim–dataset pairs, with breakdowns by recorded role. Publishers, creators, contributors and generation agents remain distinct. Award/funder text does not create an Organization identity. Mapping/reference datasets are included, and their recorded input roles are retained; absent roles stay `unknown`.

Traversal uses derivation, generation and activity-input relationships in fields and edge records. A reached File may be associated with its recorded Dataset, but this inverse membership does not expand sibling files or inherit unrelated lineage of the containing dataset. Free-text names, retrieval candidates, KnowledgeGap selection links, embeddings and account-authoring activity inputs do not establish claim reuse.

Factor expansion is pinned to a trusted capture binding or an identity binding to the published release (see [Reference hydration](#reference-hydration)). Release projections are the retained top 50 of each library, joint **or** marginal; reload-era KPN projections are the per-trait top-50 union; legacy links are ranked summaries. Neither establishes the complete model construction history. An unavailable or ambiguous binding stays unresolved rather than using the active catalog.

An activity's used inputs that share one onward lineage (the same generating activities, derivations and Datasets) are followed through one representative once more than 25 of them do, and `collapsed_inputs` is observed on its edge. GlyGen HZ2's 6,620 cached API responses are one path to the GlyGen release. Datasets are always followed one by one. More than 25 inputs with no lineage at all are one `unlinked_inputs` coverage entry (`{activity, count}`).

The processing bounds are 10,000 nodes, 50,000 edges and depth 128; a 100,000-state processing limit also bounds repeated work. Serialized responses are limited to 8 MiB. Exceeding a bound returns `422 PROVENANCE_LIMIT_EXCEEDED` without rankings. Retained storage outages return a service error. A missing full account source returns `409 PROVENANCE_SOURCE_UNAVAILABLE`; conflicting root payload observations return `409 SCIENTIFIC_SOURCE_CONFLICT` (or 404 when an exact payload was requested).

## Stored data sources

Every new account is traced once when it is saved: by the agent run's final save (`Worker.accept_accounts`, both job engines, in its persistence worker thread) and by a local submission (`research_execution.validate_submission` traces, `commit_accounts` stores). The trace runs before the acceptance write fence, over the exact capture records the commit writes, and is stored in the same commit as one `account_data_sources` record per owner and account (`account_data_sources.py`). `GET /v1/accounts/{id}` serves it as `data_sources`: per claim, each Dataset it reaches with its input roles, routes, the cited gene set, mechanism, trait, file or source claim it was reached through (`via_ids`), the credited Organizations and roles, and any curated lineage step (`curation_ids`), plus names for the bubble chart. The account view draws these datasets inside each claim circle.

- **Release-era trace, no portal:** the record comes from `provenance_api.trace_local`, the endpoint's hydration and walk (identity binding included) without phase 3's portal fetch. It reads the reference tables and retained capture bytes, makes no remote request and writes no file. A reached trait's GWAS route ends at its bottom-line artifact as the published release names it (`metadata.bottom_line.gwas_artifact`): a data source of kind `gwas_bottom_line` with id `gwas-bottom-line:<artifact_id>`, its ancestry, the KPN trait and the portal's artifact page (built from ids) as `location`. The endpoint itself still shows the portal's GWAS lineage.
- **Never blocking:** a trace failure (storage, reference database, processing bound, or a 90 s deadline) is stored as `status: unavailable` with an error code; the account is accepted regardless.
- **Exact source:** a record is served only for the document and account payload it traced, under the current format and `TRACING_POLICY_VERSION`; otherwise `data_sources` is `{status: not_traced}`. Records traced under `account-provenance-v1` (before the reference release) are therefore `not_traced` until re-traced. Re-accepting an account keeps its first trace, matching the first-document-wins object index. A private read that withholds revoked borrowed objects drops the connections reached only through them and reports `incomplete`.
- **Publication:** publishing copies the owner's matching record beside the new snapshot (`digest(['publication', snapshot_id])`); a later re-trace never changes what a published snapshot serves.
- **Leaderboard:** the Datasets view ranks by "Most claims traced" (`sort=traced_claims`): the distinct claims, and accounts, whose stored trace reaches each dataset. This is lineage reach, not evidence, so the evidence-based rankings are unchanged. A GWAS bottom-line source is ranked like a dataset, with `source_kind: gwas_bottom_line` and its portal page as `url`. `scope=published` (default) reads the records frozen with publications. `scope=all` also adds every other saved account's own record, as anonymous totals: private accounts and claims are never listed, and their records report only a `withheld` count.
- **Point in time:** the record observes the reference tables when the account was saved (`reference_generation_ids`, `source_observations_sha256`). After a release, lineage or policy change, re-trace with the backfill, which uses the endpoint's exact sources and reads the published release of its `--prefix` environment (dry run first):

```sh
REVEAL_ARTIFACT_STORE=s3 REVEAL_S3_BUCKET=cyaka-reveal-data REVEAL_S3_PREFIX=qa/ AWS_PROFILE=broad \
  .venv/bin/python scripts/backfill_account_data_sources.py --prefix reveal_workflow_qa [--force] [--apply]
```

## Import preservation and historical recovery

Reference releases need no recovery. They retain every provenance node and relationship group that appears before or after `gene_sets` in collection YAML, in `reveal_ref_collections.payload.provenance` (`reference_release.collection_provenance`). Relevant sections are streamed and validated; embedding sections are skipped unparsed. Captures and evidence packages preserve anonymous provenance edges alongside exact scientific nodes. Dataset and organization records reachable only through inverse membership remain in immutable retained captures, because canonical DAPPER reachability is directed; the endpoint expands that verified context without rewriting accepted records, even when the original catalog rows have retired.

Older imports (historical reference generations) may have retained a generating Activity but dropped its input edges. The offline recovery command reads the source with the release build's reader and can register an immutable supplement from the exact original collection YAML. It checks the complete file checksum against both the stored collection and generation manifest, validates extracted relationships, and refuses conflicts. It never rewrites accounts, historical packages, collection payloads or generation manifests. It binds to the historical `reference_generations` and `cfde_gene_set_collections` rows, which `scripts/reference_migration.py cleanup` drops, so run any pending recovery (MoTrPAC included) before cleanup.

With the normal application database/artifact-store configuration, inspect the dry run first:

```sh
python scripts/recover_collection_provenance.py --generation-id <generation-sha256> /path/to/original.GeneSetCollection.yaml
```

Register verified supplements explicitly:

```sh
python scripts/recover_collection_provenance.py --generation-id <generation-sha256> --apply /path/to/original.GeneSetCollection.yaml
```

The command accepts multiple collection files. It stores content-addressed graph artifacts and a catalog-owned `provenance_supplement` record, identified by generation, collection, original source checksum and extractor version. Repeating the same recovery is idempotent. Registry records/artifacts are retained across reference retirement; unavailable retired source bindings are still reported as gaps. The GET endpoint only reads registered supplements and performs no remote recovery or source-file fetching.

Recovery of the supplied MoTrPAC example requires its checksum-matching original collection file. A copied dataset name, collection membership, or co-location in a provenance document is insufficient to reconstruct a missing derivation relationship.
