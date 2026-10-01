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
- **`cfde_gene_sets.metadata`**: the `gene_set_index` fields (cfde_label, partition, model, comparison, program, gmt_row, cfde_snapshot) and `dapper_gene_set`, the exact GeneSet node as written in its GeneSetCollection document (bundle schema 2; `build` streams each document's `gene_sets` list and refuses when it differs from `gene_set_index`). The KPN evidence collector binds these exact GeneSets. `cfde_gene_set_collections.payload` holds the collection node (without members), its `cfde_index` row, the document sha256 and `provenance` (prefixes, organizations, datasets, files, activities).
- **`reference_vectors`**: `input_sha256` = sha256 of `input_text`; `vector` is little-endian float32 and `vector_sha256` is the sha256 of that blob. All gene-set and collection vectors of a generation share one `space_id`, whose `embedding_spaces` row has the EAGGL run's dimensions and metric `cosine`.

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
| `reference_archive_run` | `digest([prefix, from, to])` | `{prefix, from_generation, to_generation, started_at, completed_at, counts: {kind: n}, dropped_drafts: [...], unresolved: [...], runs}`; counts are cumulative over reruns |
| `reference_reload` | `digest([plan_sha256])` | audit: `{plan_sha256, target, generation_id, from_generation, status, error, backups, steps: [...], counts, verification, namespaces_deleted}`; `purge-retired` gates on `verification.passed`. An audit whose steps include `activated` (or `resumed`) but whose status is not `complete` makes the next plan a resume plan (§7). |

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
| `request_binding`, `evidence`, `artifact`, terminal `job`, `queue`, `attempt`, `execution`, `dispatch`, `event`, `remote_event`, `analysis_outcome_by_job`, `scientific_document`, `object*`, `paragraph`, `citation*`, `grant`, `exploration`, `outbox`, `notification_outbox`, `idempotency`, `workspace_event`, `workspace_cursor`, `vote` and `vote_total` (community ballots and tallies, keyed by gap or account id), identity and infrastructure kinds | KEEP untouched | — |
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

- The manifest adds `id_scheme: 'source-id-v1'`, `reference_generation_id`, `reference_model`, `gene_set_embedding_space` (the `reference_vectors` space), `gene_set_namespace` and `collection_namespace`. `mapping_run` and `geneset_import` carry the generation id, as KPN catalog bindings do.
- The `vector_snapshot` record keeps compact manifest rows for gene sets and collections, `{id, batch, original_vector_sha256, roundtrip_sha256}` with `id` = the DAPPER id: full bindings would add about 38 MB to that single record. The full bindings live in the export batches (`read_export`) and in the Upstash metadata, which import readback checks vector by vector.
- The readiness check (`UpstashFactorIndex.check`), `verify_snapshot` and the durable Workflow import (`vector_workflow`, inventory and finalize) cover every namespace present, with exact counts and the id inventory. For gene sets and collections the Workflow inventory compares source kind, source id, snapshot, embedding space and original checksum.
- The retrieval quality gate still uses factors and contexts only.
- After verification, every uploaded vector is recorded in `vector_bindings` (`record_vector_bindings`).
- Only `deletable_namespace(name, env)` names can be deleted: `{env}-(f|c)-<48 hex>` (legacy) or `{env}-(eaggl-factor|dismech-context|cfde-geneset|cfde-collection)-<24 hex>`, never an active one.

**Where the vectors come from:**
- Factors: `eaggl_name_embeddings` of the EAGGL embedding run. The LAP raw bundle reproduces the current import `a548ad80…`, so the DisMech context run stays valid.
- Contexts: `dismech_embedding_vectors`.
- Gene sets and collections: `reference_vectors`. These are loaded from the CFDE snapshot's BioBERT matrix (float16, upcast to float32), after a calibration probe re-embeds sample texts through `EMBEDDING_SERVICE_URL` and requires cosine ≥ 0.999.

## 7. Command: `python -m reveal_backend.reference_reload`

Every subcommand prints one JSON result. Nothing destructive runs without `apply` or `purge-retired --apply` and a matching approval, or `abandon --apply` and its typed confirmation. `capture --apply` and `snapshot --apply` need no approval but write outside the shared scientific tables: capture into the live records of every prefix it is given, snapshot into the target's Upstash environment.

| subcommand | effect |
|---|---|
| `preflight [--bundle DIR/<gen>] [--eaggl-import-id ID] [--eaggl-source-version V] [--eaggl-embedding-run-id ID] [--check-embedding-service] [--compare BEFORE.json] [--out FILE]` | Read-only: one `READ ONLY` transaction of `SELECT`/`SHOW` statements, rolled back. Reports `server` (version, read-only flags, `sql_mode`, TLS cipher), `grants`, `lock_free`, `reference_tables` (which migration-008 tables exist), `prefixes` (each target's records and lock tables), `inventory` (row counts, exact for the source and reference tables) and `sources`: the mapping run, EAGGL import and embedding run, DisMech import and DisMech context run that `load` would use, selected with the same pins (the context run as vector ingestion selects it: complete, of the selected DisMech import, then `REVEAL_DISMECH_EMBEDDING_RUN_ID`). It blocks unless the connection uses TLS to a writable server with the grants `load` needs (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP` and `REFERENCES` on the database, granted directly or through a role that is active in a new session), the lock is free, exactly one complete source of each kind is selected (the deployed apps' readiness needs exactly one), and the arguments select the EAGGL import and embedding run that `load` registers as the legacy generation: the served mapping run's import and its run that `REVEAL_EMBEDDING_RUN_ID` selects. `--bundle` adds `load`'s EAGGL source and embedding-space checks of the bundle; `--check-embedding-service` embeds one probe text with the run's model and service. `--compare` diffs with an earlier result: a removed table, a new table outside migration 008, a changed row count of a source table or a changed served source is a blocker. The JSON has `ok`, `blockers` and `warnings`; `--out` also writes it to a file. |
| `build --lap-project-dir P --kpn-release v0.0.2 --out DIR` | Pure files. Writes `DIR/<gen>/` (`manifest.json`, `kpn_traits.jsonl`, `reference_factors.jsonl.gz`, `cfde_collections.jsonl`, `cfde_gene_sets.jsonl.gz`, `projections.tsv.gz`). Refuses unless every LAP projection row has `qc_pass`. |
| `embed --bundle DIR/<gen> [--embeddings-dir D] [--model M --model-revision R --provider P --service-url U]` | Calibrates and converts the CFDE snapshot vectors into `DIR/<gen>/vectors/`, and records the `embedding_space` (model, revision, provider, dimensions, service URL hash). The settings default to `EMBEDDING_*`; pass the served EAGGL embedding run's config, which `load` requires. |
| `load --bundle DIR/<gen> [--eaggl-import-id ID \| --eaggl-source-version V] [--eaggl-embedding-run-id ID] [--apply]` | **Additive.** Before any write it selects the complete EAGGL import (`--eaggl-import-id`, else the one complete import of `--eaggl-source-version`, default `legacy-711-trait-capped-union`) that holds every bundle factor and label, and its one complete embedding run (`--eaggl-embedding-run-id`, else `REVEAL_EMBEDDING_RUN_ID`) that holds every factor label vector. It refuses unless the bundle's embedding space equals that run's config (model, revision, provider, dimensions, service URL), and unless that import and run are the ones the served mapping run and `REVEAL_EMBEDDING_RUN_ID` select (the legacy generation is registered from those). The dry run stops there and reports them. `--apply` then applies migration 008, adds `STRICT_ALL_TABLES` to its session's `sql_mode` (a truncated value fails instead of warning), takes the global lock, registers the legacy generation (from the runs and import that `REVEAL_MAPPING_RUN_ID`, `REVEAL_EMBEDDING_RUN_ID` and `REVEAL_DISMECH_IMPORT_ID` select), inserts the KPN generation (`status` loading → complete), verifies counts and reads the rows back before marking it complete: vector lengths and SHA2 checksums, per-factor projection counts and a sample of text and JSON columns. It never writes `eaggl_*` or `dismech_*` rows. |
| `abandon --generation GEN [--drop-empty-schema] [--apply]` | Rollback of a load (below). Dry run by default. Removes a KPN generation that never served (`loading`, `failed` or `complete`): its rows child-first, its embedding space when no other generation uses it, then its `reference_generations` row. Refuses once an allow-listed prefix's `reference_active` names it, or once `vector_bindings` or `archived_reference_factors` hold rows of it. `--apply` is interactive: it holds the global lock, needs the first 12 characters of GEN typed back, then checks the refusals again in a fresh transaction. `capture --apply` and `snapshot --apply` hold the same lock, so neither writes rows of the generation while `abandon` runs. `--drop-empty-schema` then drops the migration-008 tables, but only when nothing but legacy registrations remains in them and no allow-listed prefix holds reload records (`reference_active`, `reference_control`, `reference_reload`, `reference_archive_run`). |
| `capture --from GEN\|active --prefixes a,b --cold-export [--export-dir D] [--apply]` | **Idempotent, but writes to live prefix records.** Freezes `archived_reference_factors` for every factor referenced by any draft_binding, request_binding, outcome or account in the given prefixes. Backfills `request_binding.anchor_display` in the live records of every given prefix (LAP gives it every allow-listed prefix, prod and QA included). Writes the cold export of the whole generation (content-addressed gzip JSONL parts plus a root index, recorded in `reference_generations.cold_export_ref`, format `reveal.reference-cold-export/2`). It goes to S3 only with `REVEAL_ARTIFACT_STORE=s3`; otherwise it is written to `--export-dir` or `.runtime/reference-exports`. The export holds every row `purge-retired` deletes for the generation: besides its own rows, the `Activity` and unaliased `GeneSet` rows of `dapper_objects` (legacy), every embedding run of its EAGGL import with their name embeddings, and the DisMech context runs, vectors and inputs bound to them. An export of an older format is written again. `--apply` holds the global lock (inside `apply`, apply's own). `--from active` resolves the one generation the prefixes serve now (the registered legacy generation in legacy mode) and refuses when they serve different ones. |
| `snapshot --generation GEN --target T [--batch-size 200] [--apply]` | Builds, uploads and verifies the Upstash snapshot for the target's vector environment, without activating it. Reuses a verified snapshot of the generation. Writes `vector_bindings`; `--apply` holds the global lock. Needs `REVEAL_ARTIFACT_STORE=s3` and `UPSTASH_VECTOR_WRITE_TOKEN`. |
| `plan --target T --generation GEN` | Read-only. Writes `plan.json`: counts per kind, archive candidates, drafts to drop, non-terminal jobs, namespaces, the active generation, and the plan sha. The sha pins the drafts to drop, the rows archived by assumption, the anchor display backfill, the pointers, the snapshot and the namespaces. Row counts, archive candidates and job states are under `observed` and excluded, so normal activity between plan and apply does not drift the plan (stamping is idempotent and jobs are re-evaluated under the gate). An unverified target snapshot is a blocker. When the target is already active on GEN but no `complete` audit of that cutover exists (it failed after activation, or its verification failed), the plan is a **resume plan** (`resume: {from_generation, interrupted_plans}`); otherwise "already active" is a blocker. |
| `approve --plan plan.json` | Interactive, run by hand. Shows the diff and takes a typed confirmation. Writes `approval.json`. Production also needs `--allow-production` and `REVEAL_RELOAD_PRODUCTION_APPROVAL=<plan_sha>`. |
| `apply --target T --plan plan.json --approval approval.json --backup-dir DIR (--backup-snapshot-id ID \| --create-aurora-snapshot) [--aurora-cluster-id C] [--cancel-active] [--drain-seconds 600] [--keep-gate] [--allow-production]` | Protected cutover for one prefix. It re-plans and refuses on drift, then pre-flights, before the gate closes and before any job is cancelled: the target snapshot is verified, a schema-only mysqldump (`--no-data`, removed afterwards) succeeds, and the Aurora snapshot is available (or the cluster to snapshot exists). Then: global lock → gate → drain (cancel uncollected jobs, wait for collected ones or stop them with `--cancel-active`; stopped jobs keep their row owner) → backups (mysqldump of `<prefix>_records` with the password only in `MYSQL_PWD`, plus an available or new Aurora cluster snapshot) → delta capture → check the snapshot is verified → activate (`vector_active` + `reference_active`, one transaction, both compare-and-swap) → mark the old generation `superseded` → archive pass, which also drops anchored drafts and their `draft_binding` → capture again for this prefix (stamps whose anchors came from frozen Mechanism nodes) → verify → reopen the gate (unless `--keep-gate`) → audit → release the lock. **Resume plan:** the backup options are not needed; after lock → gate it checks that `reference_active` and `vector_active` still name the plan's generation and snapshot, then runs only the idempotent tail (archive pass → capture → verify → gate → audit, step `resumed`), so the audit records the verification `purge-retired` needs. |
| `verify --target T [--generation GEN]` | Read-only post-conditions (§9). |
| `purge-retired [--generation GEN] [--out purge-plan.json]` | Read-only: writes the purge plan (approve it like an apply plan). The per-prefix record counts of step 6 are under `observed` and excluded from the sha: that step drops every catalog-owned `suggestion` and each non-active snapshot, recomputed at purge time. |
| `purge-retired --apply --plan purge-plan.json --approval … [--allow-production]` | Protected. Runs only when every allow-listed target is active on the new generation, has a recorded passing verification and passes a fresh one, has a cold export of the current format whose root index and every part exist (in S3 when a production target is allow-listed), and has no non-terminal old-generation jobs. It also refuses while an EAGGL mapping run other than the registered legacy one references a retiring EAGGL or gene-set import (no export holds its links, and steps 3–4 would fail on its foreign keys partway through). It re-plans, refuses on drift, and purges retired data child-first in batches with the freshly computed statements. |
| `gate --target T --open\|--close [--reason R]` | Sets or clears the target's reload gate, for example to reopen it after `apply --keep-gate`. |
| `status` | Generations, the active pointers per allow-listed target, the gates and the last audits. |

**Exit codes.** 0 when the command ran (a plan with blockers still exits 0 and lists them); 1 when the result has `ok: false` (verify failed, or apply ended `verify_failed`); 2 when a protection refused (`{"ok": false, "refused": "..."}`). Every command first loads the targets file.

**Generation status is shared.** `apply` marks the old generation `superseded` in the shared `reference_generations` table at the first target's cutover, while other prefixes may still serve it. Nothing decides superseded-ness from that status: the catalog, guards, stamps and evidence collector compare with the prefix's own `reference_active` (legacy mode serves the legacy generation), and `complete` and `superseded` generations both stay servable and verifiable.

**Deployed apps and source pins.** The deployed backends share the scientific database too (this round: QA at `ec88469`, prod at `54b9a2c`). They never read the migration-008 tables and pin only `REVEAL_MAPPING_RUN_ID`; their catalog needs exactly one complete DisMech import and exactly one complete embedding run of the pinned EAGGL import, or `/readyz` returns 503. So `load` must never add `eaggl_*` or `dismech_*` rows, and the commands run with `REVEAL_MAPPING_RUN_ID`, `REVEAL_EMBEDDING_RUN_ID` and `REVEAL_DISMECH_IMPORT_ID` pinned to what those apps serve (the LAP cfg's `reload_*` keys, §10). The CLI loads `.env` without overriding the shell, so exported pins win. `preflight` confirms them against the live database. Every `apply` needs the merged code deployed to that prefix first: older code returns 503 for semantic search against a KPN snapshot.

**Connection and storage.** `REVEAL_MYSQL_CA_FILE` must point at the AWS RDS global CA bundle (`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`); the reviewed sha256 pin is `RDS_CA_SHA256` in `scripts/platform_assets.py` on main. S3 is used only with `REVEAL_ARTIFACT_STORE=s3`; `REVEAL_S3_BUCKET` alone does not switch it. The cold export and the Vector snapshot batches go there, and `snapshot` needs it together with `UPSTASH_VECTOR_WRITE_TOKEN`. `capture --apply` writes into the live records of every prefix it is given, so it waits for the cutover round (§10): run it from the merged code, with those records backed up.

**Rolling back a load.** Until a generation is captured or snapshotted, nothing reads it: no prefix is active on it and the deployed apps never read the migration-008 tables. In that window `abandon --generation GEN --apply` removes it, and `--drop-empty-schema` also drops the migration-008 tables when nothing but legacy registrations is left in them. `abandon` is valid only before any capture or snapshot. Afterwards it either refuses (`vector_bindings` or `archived_reference_factors` rows) or would leave behind what those steps wrote elsewhere (prefix records, the cold export, Upstash namespaces). A generation that no target activates is never served, so after that point leaving its plans unapproved is the rollback.

**Targets.** Targets come from `config/reference_reload.targets.yaml`: `{name: {database, prefix, vector_environment, upstash_host, production}}`. The command refuses when:
- `REVEAL_APPLICATION_TABLE_PREFIX` or `REVEAL_VECTOR_ENVIRONMENT` in the shell disagrees with the target;
- the MySQL database or Upstash host differs from the target's;
- a namespace outside `{env}-` would be touched;
- any DisMech source table would be deleted from.

**`purge-retired` delete order** (children first, `DELETE … LIMIT 10000` per commit):
1. `eaggl_cfde_gene_set_links`, `eaggl_cfde_factor_links`, `eaggl_cfde_link_runs`
2. DisMech embedding inputs, vectors and runs bound to an embedding run of a retired generation's EAGGL import, except those bound to a kept generation's run (exactly what the cold exports hold; step 3 needs them gone)
3. retired EAGGL imports' name embeddings, embedding runs, graph, loadings, factors, genes and imports
4. `cfde_gene_set_aliases`, `gene_set_imports`, unreferenced `GeneSet`/`Activity` rows in `dapper_objects`
5. retired generations' `factor_gene_set_projections`, `reference_vectors`, `cfde_gene_sets`, `cfde_gene_set_collections`, `reference_factors`, `kpn_traits`
6. per prefix: catalog-owned `suggestion`, old `vector_snapshot`/`vector_batch`
7. old Upstash namespaces `^{env}-(f|c|eaggl-factor|dismech-context|cfde-geneset|cfde-collection)-` that are not active, and their `vector_bindings`
8. mark the retired generations `retired`

Never touched: the DisMech source tables (`003_dismech.sql`) and `archived_reference_factors`.

## 8. App behaviour

**Catalog**
- Resolves the active generation on its cold load. Once loaded, `load()` does no I/O (as before reloads), because draft saves and job submits call it inside pooled write transactions. The API's background poller re-checks the active generation every 5 s, off the request path; a change reloads the catalog beside the serving one and swaps it in, so no restart is needed. A failed reload fails closed: the next request reloads inline into a fresh catalog, never on top of the previous generation's state.
- `GET /v1/reference-factors/{archive_id}` caches hits for the process and misses for 5 s; while `archived_reference_factors` is missing or empty (legacy mode) it connects at most once per 5 s.
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
- MySQL holds no PIGEAN trait-level (phenotype) associations, so KPN packages record the explicit capture blockers `bioindex:trait:trait:kpn:NNNNNNN:(gene|gene_set):not_captured` instead of trait-scope rows. The dispatch gate (`box_adapter.dispatchable_capture`) accepts a KPN package whose only blockers are these, and refuses any other incomplete capture.

**Guards**
- Submit, retry-review and anchor writes are rejected with 409 if any selection's generation is not the active one, and with 503 while the gate is set. Rename-only edits, unchanged anchors, removing anchors, gap-only drafts and paragraph jobs stay allowed under the gate.
- Suggest rejects manual anchors that are not in the active generation.
- Legacy mode never rejects by generation: as before, a saved selection keeps the exact run bindings first saved with it, even after a mapping-run switch.

**Stamping at creation.** Accounts and outcomes produced from a superseded generation (for example a job that finished after cutover) are stamped when they are written (`worker.accept`, `analysis_outcomes.save`).

**Reads**
- Account and outcome detail return `archive`.
- The Mechanism route returns 410 with the frozen factor for superseded ids.
- Listings take `reference_state`, and `counts_by_gap` counts current accounts only.

**Frontend**
- An "Outdated reference" badge and a current/archived/all filter on the workspace and gap pages.
- A banner on account and outcome pages showing the original anchors from `archive.reference`, with a "Start a new analysis on this gap with current factors" button (new draft with the gap, inquiry and KGs copied and empty anchors, then auto-suggest).
- In the Composer: render archived anchors from the stamp; clear copy for `REFERENCE_GENERATION_SUPERSEDED`; no hardcoded model; `mechanism-display` understands `factor:kpn:`.
- A cutover's public `catalog.updated` event has `entity_id` `reference` (a `vector_active` change; publications emit `catalog`). Only that event makes an open composer re-read its anchors, now and again after 7 s, 30 s and 120 s, since each API process switches catalogs on its next poll. A draft dropped at cutover (404 on save) is replaced by a new draft holding the composer's edits.
- A frozen KPN factor displays its KPN phenotype name (`metadata.kpn.phenotype_name`), not the EAGGL trait code in its `trait`.
- URLs: the new-analysis button creates a gap-only draft and opens `/?draft=<id>&suggest=current`, which runs auto-suggest; the workspace keeps its filter in `?tab=accounts|explorations&reference=current|archived` (absent means all).

## 9. Verification post-conditions (`verify`)

**KPN generation counts:** 711 traits / 4,037 factors / 133 collections / 44,399 gene sets / 278,272 projection rows, matching the stored manifest, with status `complete` or `superseded`. Every `reference_factors.kpn_trait_id` exists in `kpn_traits`.

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

`lap/config/cfde_projection.cfg` adds a `reload_target` class, with instances generated from `config/reference_reload.targets.yaml`, and these `local cmd`s. A local cmd runs inside run.pl on the submit host, even under `--bsub`, so `--bsub` gains nothing for them: run LAP in tmux.

| scope | commands |
|---|---|
| project | `db_build` → `db_embed` → `db_load` (additive) → `db_capture` (writes to live prefix records) |
| per target | `db_snapshot` → `db_plan` → `db_apply` (needs a hand-made `approval.json`) → `db_verify` |
| project | `db_purge_plan` (read-only, a fan-in over every target's verify output) → `db_purge` (needs its own approval) |

- **Source pins.** The cfg's `reload_*` CONFIGURATION keys hold what the deployed apps serve (§7): `reload_mapping_run_id`, `reload_dismech_import_id`, `reload_eaggl_import_id`, `reload_eaggl_embedding_run_id`, and that run's `reload_embedding_model`, `reload_embedding_model_revision`, `reload_embedding_provider` and `reload_embedding_service_url`. Every `db_` command exports `REVEAL_MAPPING_RUN_ID`, `REVEAL_EMBEDDING_RUN_ID` and `REVEAL_DISMECH_IMPORT_ID` from them; `db_embed` passes `--model`, `--model-revision`, `--provider` and `--service-url`, and `db_load` passes `--eaggl-import-id` and `--eaggl-embedding-run-id`. They are config defaults; `preflight` confirms them live.
- Approval files are written only by the interactive `approve`: `out/projects/<project>/reload/targets/<t>/<t>.approval.json` per target and `<project>.purge.approval.json` for the purge. A re-plan deletes the earlier approval.
- `db_capture` runs `capture --from $reload_from_generation`; the meta key defaults to `active`, so a later reload captures the generation the targets serve without regenerating the meta.

An unattended LAP run therefore stops at "plans ready", but only after `db_capture` and the snapshots have run. The reload is done in two rounds, and LAP is started by command name.

**Load round** (the current one; nothing an app reads changes):
1. `preflight` (before), by hand, with `--out`.
2. `lap_run --only-cmd '^db_(build|embed)_cmd$'`.
3. `load` dry run, by hand, with the pins (`--eaggl-import-id`, `--eaggl-embedding-run-id` and the exported `REVEAL_*` ids).
4. `lap_run --only-cmd '^db_load_cmd$'`.
5. `preflight --compare` against step 1 (after), then `status`.

Until the cutover round, never run `--only-cmd '^db_'` or a plain LAP run without `--skip-cmd '^db_'`: both continue into `db_capture`. A load is rolled back with `abandon` (§7), which is valid only before any capture or snapshot.

**Cutover round** (later): from the merged code, with every allow-listed prefix's records backed up, the merged code deployed to each prefix before its apply, and `REVEAL_ARTIFACT_STORE=s3` and `UPSTASH_VECTOR_WRITE_TOKEN` set. Then `--only-cmd '^db_'` runs capture, the snapshots and the plans, and each target is applied after its `approve`.

The runbook is in `lap/README.md`, section "Reference reload (`db_` stage)".
