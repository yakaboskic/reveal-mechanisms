# Lightning audit merge handoff

Prepared October 10, 2026 for the agent coordinating the combined backend release.

## Branch and integration state

- Repository: `yakaboskic/reveal-mechanisms`.
- Feature branch: `codex/lightning-audit` (merge the whole branch; the nine implementation commits are one feature).
- Implementation head: `ae33e5986cc848a9c8f864626314832faa1caf54`. A subsequent documentation commit adds this handoff.
- Original base: `5501ba6`; latest fetched `origin/main` at preparation: `81304cf` (seven newer main commits).
- A non-mutating Git merge preview against `81304cf` succeeded without conflicts. Candidate tree: `3d5738611489d2c72ae5ecc7f9eae6a223b51f36`.
- The only source file edited by both histories is `services/backend/src/reveal_backend/box_remote.py`. Preserve Lightning's `lightning_audit` prompt argument together with main's pretty-printed drafts for in-place repair and low-effort agent invocation; retain the spend-reporting integration in `box_research.py`.
- Main was not changed, the feature was not rebased, and no deployment was performed during handoff preparation. The running local demo uses the feature implementation, not a combined-main image.

Fetch current main again before merging; this compatibility statement applies to the recorded revision. Coordinate all intended backend changes first, then build and deploy the combined revision once. Do not deploy this branch separately.

## Delivered behavior

Private Lightning audits assess whether the selected CFDE evidence already in Reveal can help address a knowledge gap. They retain exact evidence snapshots and references, show preparation/thinking status, stream the actual public answer, validate the complete structured result, and offer an editable brief for local or online research. Reloads resume persisted progress without making another model call. The compact title and mode icons are included; Lightning is first with a Recommended badge.

Each child research run gets a fresh immutable request and independent generation pin. Audit prose is preliminary guidance, excluded from scientific evidence eligibility. Existing quotas, ownership, receipt checks, acceptance, dispatch, and reference cutover rules remain authoritative. The automatic Jev assessment remains separate.

Read [the feature contract](lightning-audit.md) for request limits, retention, API behavior, streaming, and continuation. There is one provider attempt per explicit audit, a 120-second deadline, a 100,000-byte request limit, and a 3,000-token output limit. No fresh literature or graph search is performed by the audit.

## Combined release configuration

1. Merge this feature and the other intended backend work into the release revision. Preserve main's current authoring, cost telemetry, and demo caps.
2. Build the backend image from that combined revision and deploy it once using the existing environment's deployment workflow. All backend roles should use the same release image; there is no separate Lightning worker service or queue.
3. Lightning uses new record kinds in the existing application record store (`lightning_audit`, `lightning_audit_progress`, `lightning_audit_idempotency`, `lightning_continuation`). It needs no SQL schema migration, reference reload, database reset, new infrastructure, or new credentials.
4. The backend flag is `REVEAL_LIGHTNING_ENABLED=true` in the target backend service configuration. The existing `ANTHROPIC_API_KEY` and configured `REVEAL_CLAUDE_MODEL` are used. Creation remains disabled by default until the release owner enables it.
5. For the Reveal frontend, set `NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED=true` **at frontend build time**. A runtime-only frontend environment change will not expose the mode. The frontend Docker build argument and local/durable setup propagation are included. In cloud releases, configure the backend flag explicitly; the frontend flag does not enable the API by itself.
6. Build/deploy the frontend from the same merged source. Audit response previews use the existing authenticated detail endpoint and revision-aware long polling; no new public ingress or streaming-proxy configuration is required.
7. After the combined release is healthy, run one explicitly authorized live audit in the intended environment and verify thinking, growing text, final references, and continuation availability. Automated suites use mocked provider responses. Do not start local/online research solely to demonstrate the buttons unless that research run is intended.

The standalone `reveal-client` receives updated contracts and shared mode-menu styling; the Lightning audit page and mode integration live in `services/frontend`.

## Validation already completed

- Feature implementation: **135** targeted Lightning backend tests and **190** related backend tests with **61** subtests passed. Related coverage includes CFDE/Jev preparation, database round-trip budgets, retention, ownership, setup, workflow recovery, and archival.
- Frontend: **327** tests; standalone client: **102** tests. Both typechecks passed.
- OpenAPI validation, generated contracts, and backend/frontend image builds passed.
- Browser regression scenarios covered lazy submission, mode keyboard navigation, local/online behavior, streaming/reload recovery, failures, and preservation of an edited brief.
- Two actual local provider runs completed in **38.671** and **41.173** seconds from request creation to completion. First persisted text appeared approximately **10.6** and **11.5** seconds after worker processing began. Thinking, growing text, and a reload during streaming were verified in the browser.
- Latest local sample: `e2d0ddae-0e59-4244-bba9-03c3c3927c70`, prompt `lightning-audit-v8`; these records are local-demo verification, not production acceptance evidence.

### Integration validation against current main

An isolated source snapshot of the merge candidate passed **240 backend tests and 23 subtests**, with no failures or skips:

- Lightning stream, payload, audit, continuation, and Box execution: **181 tests + 7 subtests**.
- Current-main worker, budgets, job metrics, runtime spend, workflow failure diagnostics, draft repair hints, exact-document validation, and workflow bootstrap: **59 tests + 16 subtests**.

These checks did not modify either branch or call a live database/provider. Exact-document tests used the existing lock-verified DAPPER release through `REVEAL_DAPPER_ROOT` and `REVEAL_TEST_DAPPER_RELEASE`, pointing to `.deployment-assets/dapper` in the feature worktree (DAPPER 0.2.0 at `e44a913`, 35 files verified against the candidate lock). Other backend branches have not been incorporated into this candidate; recheck after the final combined merge.

## Merge checks and remaining limitations

- Re-run affected checks after incorporating any later main/backend changes. Keep evidence preparation/provider calls outside write transactions, optional preview persistence bounded, and generation/owner/attempt fences intact.
- Audit progress deliberately does not trigger workspace list events for every chunk. Keep serial long polling and hidden-tab suspension; avoid restoring frequent list refreshes.
- Keep source generators and exports together. If API sources change during integration, regenerate with `scripts/build_openapi.py`, `scripts/validate_openapi.py`, and `scripts/build_api_viewer.py`; regenerate `services/frontend` contracts/fixtures, copy `api/openapi.json` into `reveal-client/openapi.json`, and regenerate the standalone client contracts. Do not resolve generated artifacts by discarding their generator changes.
- One early local audit failed during pre-provider evidence persistence. Timing was consistent with existing database lock contention, but the exact original exception was not retained. Commit `ae33e59` adds safe private stage/type/error-code diagnostics without storing SQL, exception messages, or credentials. Database pool and workflow retry changes remain outside this feature.
- Valid references and schema checks establish format and provenance, not scientific correctness. Live output still needs scientific review: it can overinterpret factor co-loading, miss relevant perturbation GeneSets, or generalize source metadata too widely. The audit is preliminary; broader rollout remains gated by the release owner's validation.
- Existing failed audit records remain unchanged; viewing or reloading them never retries inference.
- Local runtime receipts and credentials are ignored and are not part of the branch. Reuse the intended environment's configuration rather than copying the local demo environment or restarting setup/bootstrap tooling.
