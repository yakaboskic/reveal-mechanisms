# Integration branch `integration/2026-10-10`

This branch brings three lines of work together on top of `main` (`e443397`), so the reference release schema and serving model can be designed against all of the current code.

| Merged | What it adds |
|---|---|
| `claude/suggest-rerank` | Jev ordering of automatic mechanism suggestions ([suggest-rerank.md](suggest-rerank.md)) |
| `codex/account-provenance` | `GET /v1/accounts/{account_id}/provenance` ([account-provenance.md](account-provenance.md)) |
| `origin/chase/reference-release` (`e8a2e32`) | The LAP-built reference release, which replaces the reference reload ([reference-release.md](reference-release.md)) |

`chase/reference-release` was 18 commits on a base 111 commits behind `main`, so its merge carried most of the work. Its design is authoritative for how reference data is built and served. Everything on the `main` side was kept and, where it had depended on removed code, moved onto the release.

## What changed when the release replaced the reload

- **Deleted on purpose:** `reference_reload.py`, `vector_ingestion.py`, `vector_workflow.py`, `evidence_database.py` and their tests.
- **Moved into `reference_release`:** the account-provenance helpers that lived in `reference_reload` (`collection_header`, `_sha256_file`). `read_collection` now keeps every provenance group before and after `gene_sets` in `reveal_ref_collections.payload.provenance`, and refuses repeated sections or invalid edges.
- **Readiness and the catalog poller** now follow the one-row `reveal_ref_release` table (`RELEASE_TTL_SECONDS`, `current_release_id`). The generation and vector pointers are gone.
- **Creation stamping** of work finished after a cutover was removed from the worker and from `research_execution.commit_accounts`, along with the reload gate.
- **Hosted and local-work submission** record the release that all the anchors share, via `reference_generation.shared_release`. A draft whose anchors come from different releases now records none instead of failing.
- **Unchanged by the merge:**
  - API contract: regenerated, with no diff from the two feature merges.
  - `REVEAL_SUGGEST_RERANK=jev` and `REVEAL_LIGHTNING_ENABLED` on QA.
  - All Lightning, OAuth, local-work, API-key and workflow behaviour.

## Open items for the release schema and serving work

These are places where `main`-side code still assumes the reload-era tables or capture formats. They were left for the schema redesign rather than ported onto today's release tables.

1. **Factor explorer** (`factor_details.py`: `/v1/factors/{id}`, `/v1/factor-loadings`, `/v1/catalog/gene-sets/{id}`).
   - It still reads `eaggl_gene_loadings`, `factor_gene_set_projections`, `cfde_gene_sets`, `cfde_gene_set_collections`, `eaggl_cfde_gene_set_links`, `cfde_gene_set_aliases` and `dapper_objects`.
   - Under a release, a binding's `eaggl_import_id` is `None`.
   - It should read `reveal_ref_factor_genes`, `reveal_ref_projections` (ranked per library, no scope), `reveal_ref_gene_sets` and `reveal_ref_collections`.
2. **Reuse of new accounts** (`scientific_reuse._legacy_sql_source`).
   - It accepts only `/1` reference captures.
   - Release-built packages emit `reveal.reference-evidence.mysql-capture/2` with `release_id` origins and `reveal_ref_*` tables.
   - Add the `/2` branch the way `scientific_account_lint.cfde_source_files` does.
3. **Research seeds and data on a release**.
   - `research_seed` and `research_data` still treat `local_work.reference_generation_id` as a generation with an `eaggl_import_id`.
   - Lightning audits and continuation still use `generation_of_anchors`, which raises when a draft's anchors come from different releases.
4. **Cleanup guard** (`scripts/reference_migration.py cleanup`).
   - The reload's research-pin purge guard has no replacement.
   - Before dropping the migration-008 tables that pinned research and provenance recovery still read (`research_data`, `provenance_reference`, `provenance_supplements`), cleanup should refuse while non-released `research_pin` rows reference them. Run any pending MoTrPAC provenance recovery first.
5. **Mechanism identity**.
   - `reference_release.archived_rows` always mints identity v1, while the catalog and `reference_evidence` read `mechanism_identity_version` and `eaggl_import_id` from the release manifest.
   - If a manifest opts into v2, `archived_rows` must mint the same version.
6. **Rebuild before publishing anything from this branch**. The dig-ae-dev-03 release `eaggl_capped__cfde_2026_10_05` was built before these changes. Its `release_id` will change for two reasons:
   - collections now carry full provenance;
   - Mechanism ids are minted with the DAPPER 0.2.0 runtime (`CURRENT_DAPPER_SNAPSHOT`).
   On the rebuild, check that:
   - all 532 collection documents pass the new section and edge validation;
   - the largest `ref_collections` row fits `max_allowed_packet`;
   - the `dapper_edges` count looks right, since it now includes edge sections before `gene_sets`.
7. **Tables the app does not read yet**:
   - `ref_trait_gene_sets` (the betas)
   - `ref_dapper_nodes`
   - `ref_dapper_edges`
8. **Trait ontology mappings**. The Jev rerank's disease pool uses `Catalog.disease_factors`, which recomputes `interpreted_mappings` from the raw `ontology_mappings` in `reveal_ref_traits.metadata`. Keep those mappings in whatever trait schema replaces it.

## Verification on this branch

- **Backend:** 2,526 tests pass and 10 are skipped (1,194 subtests). Two known failures are not caused by the merge:
  - `test_worker_collection.py::test_failed_agent_notice_has_no_forward_timer_or_terminal_job_claim` also fails on `main`.
  - `lap/scripts/tests` compares the committed project `.meta` with the local checkout path, so it passes only on dig-ae-dev-03.
- **Clients:** frontend 327/327 and `reveal-client` 102/102 tests pass, and both typecheck.
- **OpenAPI:** the contract validates.
- **Local stack:** the local stack runs this branch. No reference release is published to the local environment, so reference-backed routes (suggestions, factor search, evidence) answer `503 SOURCE_NOT_READY` until one is.
