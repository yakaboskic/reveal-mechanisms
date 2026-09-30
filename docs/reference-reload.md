# Reference reload: versioned EAGGL/CFDE reference generations, archive-on-reload, and the protected flush

This document is the contract for replacing the EAGGL factor and CFDE gene-set reference data in MySQL and Upstash with a new **reference generation**. The data comes from the LAP projection pipeline in `lap/`.

Each reload:
- archives user work built on the old generation, and never deletes it;
- purges the old generation only after every environment has switched to the new one.

The shared identifiers and the helpers live in `services/backend/src/reveal_backend/reference_generation.py`. The DDL lives in `schema/migrations/008_reference_generation.sql`.

## 1. Concepts

**Reference generation.** One complete, immutable set of the factors, traits and gene sets the app serves, plus their factor↔gene-set projections and vectors. It is identified by `generation_id` (64 hex characters). There are two kinds:

| kind | model | what it is | id |
|---|---|---|---|
| `legacy-cfde-inc-v2` | `cfde-inc-v2` | The pre-reload data: the 1,756 EAGGL factors linked to CFDE portal factors (`eaggl_cfde_*`), and gene sets from the `cfde-inc-v2` alias import | `legacy_generation_id(mapping_run_id)` = `digest(['reference-generation','legacy-cfde-inc-v2',mapping_run_id])` |
| `kpn-eaggl-capped` | `eaggl-capped-v1` | A LAP-built generation: all 4,037 EAGGL factors keyed by KPN trait, 44,399 DAPPER CFDE gene sets in 133 collections, per-trait projections | sha256 of the canonical build manifest (`reference_reload build`) |

**Active generation.** Each environment prefix's `<prefix>_records` holds one row `(kind='reference_active', id='active')`. A missing row means **legacy mode**, where the app behaves exactly as before this change. The row can be overridden with `REVEAL_REFERENCE_GENERATION_ID`. It is switched by compare-and-swap (`write_active`).

**Scientific tables are shared.** Prod, QA and local share the scientific database `cyaka_reveal_mechanisms`. Only `<prefix>_records` and `<prefix>_transaction_lock` are separate per environment. Loading a generation is therefore additive and safe, while purging affects everyone. That is why the purge is gated on every allow-listed target having activated the new generation and passed verification.

**Archive.** User work built on a superseded generation keeps every row. Selected rows gain an `archive` stamp and become outdated: read-only for science, but still viewable and publishable. The factors they referenced are frozen in `archived_reference_factors`, which is never purged.

## 2. Identifiers

| name | format | example | used as |
|---|---|---|---|
| KPN trait id | `KPN.TRAIT:NNNNNNN` | `KPN.TRAIT:0000398` | trait identity, from kpn-data-models v0.0.2 `portal_id` |
| factor key | `{kpn_trait_id}::{FactorN}` | `KPN.TRAIT:0000398::Factor1` | canonical key in MySQL; Upstash factor vector id |
| public id | `factor:kpn:{NNNNNNN}:eaggl-capped-v1:{FactorN}` | `factor:kpn:0000398:eaggl-capped-v1:Factor1` | API `source_id`, `cfde_anchor.node_id`, evidence `eaggl_mechanism_ids`. Five colon-free segments, so the existing `factor:x:x:x:x` patterns still parse. |
| EAGGL factor id | `{EAGGL trait}::{FactorN}` | `T2D::Factor1` | atlas id, legacy alias, stored as `reference_factors.eaggl_factor_id` |
| legacy public id | `factor:{group}:{phenotype}:cfde-inc-v2:{FactorN}` | `factor:portal:T2D:cfde-inc-v2:Factor1` | pre-reload `source_id`; still valid inside archived work |
| gene set / collection | `dapper:GeneSet.<32>` / `dapper:GeneSetCollection.<32>` | | CFDE DAPPER ids, also used as their Upstash vector ids |
| archive id | `digest([generation_id, source_id])` | | `archived_reference_factors.archive_id`, `/v1/reference-factors/{archive_id}` |

**Mechanism node of a KPN factor** (`reference_generation.mechanism_node`):
- `name` = `"{phenotype_name} mechanism {FactorN}"`
- `description` = `"EAGGL mechanism {public_id}. KPN trait {kpn_trait_id} ({phenotype_name}). Source label: {label}."`
- Its DAPPER id is `runtime.compute_id(node,'Mechanism',schema)`.
- The catalog and the evidence collector both use this helper, so their ids agree.

## 3. MySQL: migration 008 (shared tables)

See `schema/migrations/008_reference_generation.sql` for the full DDL. Summary:

| table | key | purge |
|---|---|---|
| `reference_generations` | `generation_id`; `status` loading/complete/superseded/retired/failed | retired rows kept as a ledger |
| `kpn_traits` | (gen, `kpn_trait_id`), UNIQUE (gen, `legacy_phenotype_id`) | retired generations |
| `reference_factors` | (gen, `factor_key`), UNIQUE (gen, `public_id`), (gen, `eaggl_factor_id`) | retired generations |
| `cfde_gene_set_collections` / `cfde_gene_sets` | (gen, id) | retired generations |
| `factor_gene_set_projections` | (gen, `scope`='per_trait', `factor_key`, `gene_set_id`) | retired generations |
| `embedding_spaces` | `space_id` | never |
| `reference_vectors` | (gen, `source_kind` in `cfde_gene_set`/`cfde_collection`, `source_id`); float32 LE blob | retired generations |
| `vector_bindings` | (environment, namespace, vector_id) | rows of deleted namespaces |
| `archived_reference_factors` | `archive_id`; UNIQUE (gen, `source_id_sha256`) | **never** |

**Metadata fields:**
- **`reference_factors.metadata`**:
  - every column of the EAGGL `factor_metadata.tsv` (label, gene_set_score, gene_score, top_genes, top_gene_sets, nonzero_gene_loadings, raw_loading_l1/l2, …)
  - every column of the LAP `factor_index.tsv` (global_eaggl_column, n_nonzero_loadings, loading_l2, loading_variant)
  - `kpn` (phenotype_name, trait_group, trait_type, gwas_source_category)
  - `lap` (projection generation, pigean commit)
- **`kpn_traits.metadata`**: `ontology_mappings` (from `kpn_trait_flat.tsv`), the `kpn_release` and its commit.

**`archived_reference_factors.snapshot`** (JSON):

```
{"format": "reveal.archived-reference-factor/1", "generation_id", "model", "source_id", "factor_id",
 "trait", "kpn_trait_id", "label", "mechanism": {"id", "name", "description"},
 "metadata": {...},                                  // eaggl_factors.metadata / reference_factors.metadata
 "top_genes": [{"symbol", "loading"}],               // top 50 by loading (eaggl_gene_loadings x eaggl_genes)
 "top_gene_sets": [{"rank", "gene_set_id", "name", "library", "collection_id", "source_key",
                    "joint_loading", "marginal_loading", "score"}],   // legacy: eaggl_cfde_gene_set_links + aliases
 "generation_manifest_sha256"}
```

## 4. Application records (per prefix, `<prefix>_records`)

**New kinds.** All are owned by `catalog`.

| kind | id | payload |
|---|---|---|
| `reference_active` | `active` | `{generation_id, model, previous_generation_id, vector_snapshot_id, activated_at}` |
| `reference_control` | `reload` | `{closed: bool, changed_at, reason, target, plan_sha256}`. When closed, the reload gate is set. |
| `reference_archive_run` | `digest([prefix, from, to])` | `{prefix, from_generation, to_generation, started_at, completed_at, counts: {kind: n}, dropped_drafts: [...], unresolved: [...]}` |
| `reference_reload` | `digest([plan_sha256])` | audit: `{plan_sha256, target, generation_id, backups, steps: [...], counts, namespaces_deleted}` |

**Catalog bindings** (frozen into `draft_binding.selections[*].binding` and `request_binding.anchors[]`):
- Legacy bindings keep their shape: `mapping_run_id`, `gene_set_import_id`, `cfde_node_id`, `cfde_payload`, …
- KPN bindings add:
  - `reference_generation_id`
  - `model`='eaggl-capped-v1'
  - `factor_key`
  - `kpn_trait_id`
  - `mapping_run_id` and `gene_set_import_id`, both set to the generation id (for compatibility)
  - `cfde_node_id` set to the public id
- `generation_of_binding()` resolves either shape.

**Catalog factor record** (`/v1/mechanisms/{source_id}`, suggestions, search). It keeps the existing EagglFactor shape and adds:
- `model`: `eaggl-capped-v1`
- `reference_generation_id`
- `kpn_trait`: `{id, name, legacy_phenotype_id, trait_group, trait_type}`

### 4.1 The archive stamp

Built by `reference_generation.build_stamp`:

```
"archive": {
  "status": "archived", "reason": "reference_generation_superseded", "archived_at": "<ts>",
  "from_reference_generation": "<gen>", "to_reference_generation": "<gen>",
  "history": [{"from_reference_generation", "to_reference_generation", "archived_at"}],
  "reference": {"model": "cfde-inc-v2", "anchors": [{"source_id", "mechanism_id", "factor_id", "trait",
                "kpn_trait_id", "label", "name", "origin", "archived_reference_factor_id"}]},
  "gap": {"id", "source_id", "source_revision"} | null,
  "analysis": {"job_id", "request_id", "evidence_package_sha256", "account_id", "outcome_id"}
}
```

- On public copies (`publication`, `publication_snapshot`, `outcome_publication`, `outcome_snapshot`), `analysis.job_id` and `analysis.request_id` are null (`public_stamp`).
- A second reload re-archives by advancing `to_reference_generation` and appending to `history` (`build_stamp(previous=…)`). A stamp that already targets the new generation is left unchanged (idempotent).
- Stamping bumps only the row's `version` column. It never changes logical payload versions (`draft.version`, `publication.version`, `outcome_publication.version`) or any `provenance`.

### 4.2 What each record kind gets at cutover

| kinds | action | where the stamp goes |
|---|---|---|
| `account`, `account_membership`, `publication`, `publication_snapshot` | ARCHIVE | `summary.archive`. Never touch `result`; `result.root_id` must stay intact for transfer re-keying. |
| `analysis_outcome`, `outcome_snapshot` | ARCHIVE | `record.archive`, **outside** `provenance` |
| `outcome_summary`, `outcome_publication` | ARCHIVE | `archive` / `summary.archive`. `analysis_outcomes.summary()` must carry it. |
| `request` (kind analysis) | ARCHIVE | `archive` |
| `draft` with ≥1 EAGGL anchor, and its `draft_binding` | DROP, after backfilling `request_binding.anchor_display` from it | — |
| drafts with only a gap | KEEP | — |
| `request_binding`, `evidence`, `artifact`, terminal `job`, `queue`, `attempt`, `execution`, `dispatch`, `event`, `remote_event`, `analysis_outcome_by_job`, `scientific_document`, `object*`, `paragraph`, `citation*`, `grant`, `exploration`, `outbox`, `notification_outbox`, `idempotency`, `workspace_event`, `workspace_cursor`, identity and infrastructure kinds | KEEP untouched | — |
| non-terminal analysis `job` with no `dispatch_input` | CANCEL (`jobs.cancel`) | late finishers are stamped at creation |
| `suggestion`, old `vector_snapshot`/`vector_batch` | DROP at `purge-retired` | — |

`plan` fails closed on any kind that is in none of these lists.

## 5. API contract changes (OpenAPI)

These are generated by `scripts/build_openapi.py` and `scripts/openapi_current.py` into `api/openapi.{json,yaml}`, `services/frontend/src/lib/api.generated.ts` and `reveal-client/openapi.json`.

**Schemas**
- New `ReferenceArchive` schema, matching §4.1.
- New `ArchivedReferenceFactor` schema, matching the §3 snapshot plus `archive_id`, `generation_id`, `captured_at`.
- Optional `archive: ReferenceArchive` on AccountSummary, AccountResult, AnalysisOutcome, AnalysisOutcomeSummary and ResearchRequest.
- `EagglFactor`:
  - `model` becomes the enum `["cfde-inc-v2","eaggl-capped-v1"]`;
  - add optional `reference_generation_id` (hex64);
  - add optional nullable `kpn_trait: {id, name, legacy_phenotype_id, trait_group, trait_type}`.
- `Composer.model`, `SuggestInput.model` and the search `model` query parameter become the same enum. Stored old composers still validate, and the backend guards reject superseded generations on write.
- `Job.status` gets **no** `archived` value. Archive is orthogonal to job state.

**New endpoint and parameters**
- `GET /v1/reference-factors/{archive_id}` → `ArchivedReferenceFactor`. It is public (reference data), with 404 if unknown.
- The listings `GET /v1/accounts`, `GET /v1/analysis-outcomes`, the gap accounts/outcomes lists and the public listings accept `reference_state=current|archived|all` (default `all`, current items first).

**Error codes**

| status | code | when |
|---|---|---|
| 409 | `REFERENCE_GENERATION_SUPERSEDED` | submitting, retrying or re-freezing selections from a non-active generation |
| 410 | `REFERENCE_GENERATION_SUPERSEDED` | `GET /v1/mechanisms/{id}` for a superseded factor. The body includes `archived_reference_factor` when one was captured. |
| 503 | `REFERENCE_RELOAD_IN_PROGRESS` | analysis submits, retry-review and draft anchor writes while the `reference_control` gate is set |

## 6. Upstash layout (KPN generations)

A KPN snapshot keeps the existing `vector_snapshot` manifest format and machinery: register → import batches → verify → complete → activate. It differs as follows:

| kind (manifest list / batch prefix) | namespace | vector id | binding (metadata) |
|---|---|---|---|
| `factors` | `{env}-eaggl-factor-{snapshot_id[:24]}` | factor key | existing factor binding with `factor_id`=factor key, `native_id`=public id, plus `reference_generation_id`, `kpn_trait_id`, `eaggl_factor_id` |
| `contexts` | `{env}-dismech-context-{snapshot_id[:24]}` | `dismech:{source_kind}:{sha256(source_id)[:32]}` | existing context binding |
| `gene_sets` | `{env}-cfde-geneset-{snapshot_id[:24]}` | `dapper:GeneSet.*` | `{source_kind:'cfde_gene_set', source_id, name, collection_id, library, n_genes, input_sha256, generation_id}` |
| `collections` | `{env}-cfde-collection-{snapshot_id[:24]}` | `dapper:GeneSetCollection.*` | `{source_kind:'cfde_collection', source_id, label, library, n_sets, input_sha256, generation_id}` |

- The manifest adds `id_scheme: 'source-id-v1'`, `reference_generation_id`, `gene_set_namespace` and `collection_namespace`.
- The readiness check (`UpstashFactorIndex.check`) and `verify_snapshot` cover every namespace present, with exact counts and the id inventory.
- The retrieval quality gate still uses factors and contexts only.
- After verification, every uploaded vector is recorded in `vector_bindings`.

**Where the vectors come from:**
- Factors: `eaggl_name_embeddings` of the EAGGL embedding run. The LAP raw bundle reproduces the current import `a548ad80…`, so the DisMech context run stays valid.
- Contexts: `dismech_embedding_vectors`.
- Gene sets and collections: `reference_vectors`. These are loaded from the CFDE snapshot's BioBERT matrix (float16, upcast to float32), after a calibration probe re-embeds sample texts through `EMBEDDING_SERVICE_URL` and requires cosine ≥ 0.999.

## 7. Command: `python -m reveal_backend.reference_reload`

Every subcommand prints one JSON result. Nothing destructive runs without `apply` or `purge-retired` and a matching approval.

| subcommand | effect |
|---|---|
| `build --lap-project-dir P --kpn-release v0.0.2 --out DIR` | Pure files. Writes `DIR/<gen>/` (`manifest.json`, `kpn_traits.jsonl`, `reference_factors.jsonl.gz`, `cfde_collections.jsonl`, `cfde_gene_sets.jsonl.gz`, `projections.tsv.gz`). Refuses unless every LAP projection row has `qc_pass`. |
| `embed --bundle DIR/<gen>` | Calibrates and converts the CFDE snapshot vectors into `DIR/<gen>/vectors/`, and records the `embedding_space`. |
| `load --bundle DIR/<gen> [--apply]` | **Additive.** Applies migration 008 and registers the legacy generation. Checks that the EAGGL import and embedding run exist and are complete. Inserts the KPN generation (`status` loading → complete) and verifies counts. |
| `capture --from GEN --prefixes a,b --cold-export [--apply]` | **Additive, idempotent.** Freezes `archived_reference_factors` for every factor referenced by any draft_binding, request_binding, outcome or account in the given prefixes. Backfills `request_binding.anchor_display`. Writes the S3 cold export of the whole generation. |
| `snapshot --generation GEN --target T [--apply]` | Builds, uploads and verifies the Upstash snapshot for the target's vector environment, without activating it. Writes `vector_bindings`. |
| `plan --target T --generation GEN` | Read-only. Writes `plan.json`: counts per kind, archive candidates, drafts to drop, non-terminal jobs, namespaces, the active generation, and the plan sha. |
| `approve --plan plan.json` | Interactive, run by hand. Shows the diff and takes a typed confirmation. Writes `approval.json`. Production also needs `--allow-production` and `REVEAL_RELOAD_PRODUCTION_APPROVAL=<plan_sha>`. |
| `apply --target T --plan plan.json --approval approval.json` | Protected cutover for one prefix: gate → drain/cancel → backups → delta capture → verify snapshot → activate (`vector_active` + `reference_active`, one transaction) → archive pass → drop anchored drafts → verify → audit → open the gate. |
| `verify --target T` | Read-only post-conditions (§9). |
| `purge-retired --plan purge-plan.json --approval …` | Protected. Runs only when every allow-listed target is active on the new generation, has verified, has a cold export, and has no non-terminal old-generation jobs. It then purges retired data child-first in batches. |
| `status` | Generations, the active pointers per allow-listed target, the gates and the last audits. |

**Targets.** Targets come from `config/reference_reload.targets.yaml`: `{name: {database, prefix, vector_environment, upstash_host, production}}`. The command refuses when:
- `REVEAL_APPLICATION_TABLE_PREFIX` or `REVEAL_VECTOR_ENVIRONMENT` in the shell disagrees with the target;
- the MySQL database or Upstash host differs from the target's;
- a namespace outside `{env}-` would be touched;
- any DisMech source table would be deleted from.

**`purge-retired` delete order** (children first, `DELETE … LIMIT 10000` per commit):
1. `eaggl_cfde_gene_set_links`, `eaggl_cfde_factor_links`, `eaggl_cfde_link_runs`
2. DisMech embedding inputs, vectors and runs not bound to the active EAGGL run
3. retired EAGGL imports' name embeddings, embedding runs, graph, loadings, factors, genes and imports
4. `cfde_gene_set_aliases`, `gene_set_imports`, unreferenced `GeneSet`/`Activity` rows in `dapper_objects`
5. retired generations' `factor_gene_set_projections`, `reference_vectors`, `cfde_gene_sets`, `cfde_gene_set_collections`, `reference_factors`, `kpn_traits`
6. per prefix: catalog-owned `suggestion`, old `vector_snapshot`/`vector_batch`
7. old Upstash namespaces `^{env}-(f|c|eaggl-factor|dismech-context|cfde-geneset|cfde-collection)-` that are not active, and their `vector_bindings`
8. mark the retired generations `retired`

Never touched: the DisMech source tables (`003_dismech.sql`) and `archived_reference_factors`.

## 8. App behaviour

**Catalog**
- Resolves the active generation when it loads, and re-checks it at most every 5 s. A change reloads the catalog, so no restart is needed.
- **KPN mode:**
  - it serves all factors of the generation from `reference_factors`, joined to `kpn_traits`;
  - bindings and records follow §4;
  - the vector snapshot must carry the same `reference_generation_id`.
- **Legacy mode:** unchanged.

**Research jobs.** The worker picks the evidence source by anchor model.
- `cfde-inc-v2`: the existing collector (CFDE interactive API and BioIndex).
- `eaggl-capped-v1`: `reveal_backend.reference_evidence`, which builds the same capture set from MySQL:
  - factor gene loadings from `eaggl_gene_loadings`;
  - factor gene-set loadings from `factor_gene_set_projections`;
  - gene-set DAPPER objects from `cfde_gene_sets`/collections.

**Guards**
- Submit, retry-review and anchor writes are rejected with 409 if any selection's generation is not the active one, and with 503 while the gate is set.
- Suggest rejects manual anchors that are not in the active generation.

**Stamping at creation.** Accounts and outcomes produced from a superseded generation (for example a job that finished after cutover) are stamped when they are written (`worker.accept`, `analysis_outcomes.save`).

**Reads**
- Account and outcome detail return `archive`.
- The Mechanism route returns 410 with the frozen factor for superseded ids.
- Listings take `reference_state`, and `counts_by_gap` counts current accounts only.

**Frontend**
- An "Outdated reference" badge and a current/archived/all filter on the workspace and gap pages.
- A banner on account and outcome pages showing the original anchors from `archive.reference`, with a "Start a new analysis on this gap with current factors" button (new draft with the gap, inquiry and KGs copied and empty anchors, then auto-suggest).
- In the Composer: render archived anchors from the stamp; clear copy for `REFERENCE_GENERATION_SUPERSEDED`; no hardcoded model; `mechanism-display` understands `factor:kpn:`.

## 9. Verification post-conditions (`verify`)

**KPN generation counts:** 711 traits / 4,037 factors / 133 collections / 44,399 gene sets / 278,272 projection rows. Every `reference_factors.kpn_trait_id` exists in `kpn_traits`.

**Active target**
- `reference_active` and `vector_active` agree on the generation.
- Upstash namespaces hold exact counts, and their id inventory equals `vector_bindings`.
- A known factor is its own nearest neighbour.
- A DisMech context query returns factor hits.

**Archive**
- Every pre-cutover account and outcome row carries a stamp whose `to_reference_generation` is the active generation.
- Every stamped anchor has an `archived_reference_factors` row.
- Every stamped `gap.id` resolves in the current catalog.
- No anchored draft of the old generation remains.

**After the purge**
- No retired-generation or legacy rows remain, and no non-active namespaces remain.
- DisMech source counts are unchanged.
- The archived work still renders.

## 10. LAP integration

`lap/config/cfde_projection.cfg` adds a `reload_target` class, with instances generated from `config/reference_reload.targets.yaml`, and these `local cmd`s:

| scope | commands |
|---|---|
| project | `db_build` → `db_embed` → `db_load` → `db_capture` (additive) |
| per target | `db_snapshot` → `db_plan` → `db_apply` (needs a hand-made `approval.json`) → `db_verify` |
| project | `db_purge`, a fan-in over every target's verify output, with its own approval |

An unattended LAP run therefore stops at "plans ready". The runbook is in `lap/README.md`.
