# Local integration validation

Validation dates: 2026-09-25–26 (Europe/Vienna); recorded event timestamps are UTC. This report distinguishes deterministic development execution from live Box/Claude execution. The deterministic results below are application tests and are not scientific findings.

## Environment and source identity

- Host Next.js; Docker Compose project `reveal-6ffae89bba` contains only the API and worker for this checkout.
- Existing Aurora MySQL, verified TLS, additive application migration 005. Imported data is preserved.
- Source catalog: 3,367 imported DisMech gaps and 1,756 mapped EAGGL factors, with existing source/embedding/mapping bindings.
- `REVEAL_EXECUTION_MODE=deterministic` for this frontend/backend gate; fixture frontend adapter is not used.
- Google and ORCID OAuth credentials are absent. Real provider login is not claimed.

## Startup and lifecycle

- Initial stopped-state `REVEAL_EXECUTION_MODE=deterministic ./scripts/dev-up.sh --build` succeeded after correcting the backend image's missing `scipy` dependency.
- Supervisor verified API database readiness, running worker, and host frontend health.
- `./scripts/dev-down.sh` completed; independent Docker project inventory and port inspection found no project containers or frontend listener.
- Unit lifecycle checks: seven passed, including PID reuse protection, foreign state rejection, cleanup limited to newly started services, idempotent shutdown, dotenv treated as data, cleanup after diagnostic/stop failures, and restart while TCP connections are in TIME_WAIT. The real socket regression still rejects an active listener.
- Healthy repeated startup succeeded while the browser and gateway checks were running; services were reused.
- Repeated shutdown passed: no project containers, frontend/API listeners, or process-state file remained. A separately launched host `sleep` process and explicitly unrelated Docker sentinel container both stayed alive; the test sentinels were then removed explicitly.
- Browser anonymous identity, original draft, exact selections and failed-job history survived a full stop/rebuild/restart. The completed live account and paragraph survived the final full stop/restart check recorded below.

## Browser and actual gateway checks

Actual browser observations (Codex in-app browser at `http://localhost:3000`):

- Searching `coronary` hid the idle/trending view and returned only knowledge-gap records. Selected the exact CAD reverse-causation question, `dapper:KnowledgeGap.zNV20nhHamt-a4CeAktQQPoAivOJe6xk`.
- Four linked DisMech contexts were shown as read-only, separately from editable anchors. Actual embedding retrieval returned five mapped anchors. Removed four; reloading retained the exact question and sole selected `Blood Vessel Endothelial Changes` anchor.
- Submit-time anonymous continuation created a workspace, saved the draft, submitted an analysis, collapsed the question, retained its anchor, and rendered incremental persisted activity from the actual API.
- Initial collector attempt `242488a7-f35a-4d61-b539-c8f507b2236a` captured real CFDE responses, then failed with `Missing frozen GeneSet alias`. The UI displayed failure and preserved draft `267da8a9-58b4-4a0a-9c40-a497b24b449c`; both draft and failed job were accessible from workspace history. The corrected successful rerun is recorded below.
- Diagnosis: 39 of 100 returned CFDE GeneSet candidates existed in the pinned import; 61 were unavailable. The corrected collector preserves the selected mapped factor, retains exact imported GeneSet identities, and records omitted native IDs in a captured alias-resolution artifact and coverage. No source import was rerun or identity replaced. The subsequent browser rerun completed successfully.
- Desktop 1280px and mobile 390×844px failure-state layouts were visually inspected. At mobile width, DOM content width equaled viewport width (390px), with no horizontal overflow. The viewport override was reset afterward.
- Automatic suggestions exposed a responsiveness issue: synchronous remote embedding calls in the async route delayed unrelated API requests. Backend correction was requested for the next image.

Completed deterministic journey:

- Retried saved draft `267da8a9-58b4-4a0a-9c40-a497b24b449c`; job `0f8bf98a-3ee2-4193-8448-1d98b039b2ad` used exact native factor `factor:portal:RVSVI:cfde-inc-v2:Factor1`. It collected an actual evidence package, SHA-256 `8423360f3f8eb3e356fc64ebb545c8f01ed049f7c6c382cfedaf22fe027aaa9c`, and passed trusted final DAPPER structural validation. Its development content explicitly disclaims a biological conclusion.
- Reloading during execution reconnected to the same persisted job. Completion compressed activity to “Gap analysis complete”; Conclusions remained primary, and associated claims were initially collapsed.
- Accepted account `dapper:ScientificAccount.d7iWlrHihZgJ0ns-ARmLtK2fnqZSdHyg` and claim `dapper:Claim.eETZorG7bt4idUW2Pl2Ig8SriK3PGRa7` were rendered. Keyboard ArrowRight selected Research Statement, showing a real pending state followed by paragraph `dapper:Paragraph.ehIezAcpd3jQUre98sRgyXDQTF7dfezl` with citation metadata revision 1.
- Claim search empty state, clear filters, direction filter, inline expansion and the dedicated Assessment/Proposition/Evidence/Provenance tabs passed browser inspection.
- Word copy completed without a browser error and displayed its success message. The automation clipboard bridge did not expose native clipboard contents, so pasting into Word was not verified.
- Actual browser Markdown, LaTeX and BibTeX downloads were read from disk and retain the same claim target. The LaTeX UI requested its BibTeX companion. Copies and SHA-256 checks are in `data/validation/local-stack/browser-exports/`.
- The current owner downloaded source artifact `catalog-62bd637171ba.attempt-1.json` (816 bytes); SHA-256 `aca5b3fa6542f90f3bdb59368737b0c7e06d232ccf1d5b959767f3101a8ce56c` exactly matched its persisted DAPPER File. Another authenticated anonymous workspace received 404 for the same download. Evidence: `browser-exports/artifact-verification.json`.
- Frontend integration fixes retain job/draft IDs in the URL after submission, clear the job parameter when returning to the question, show Saved for restored drafts, and provide safe same-origin named artifact downloads with status/error feedback.
- Clicking Stop during actual collector job `f8bc48b3-fa11-41a5-b40d-a37a5f6b39de` produced the persisted “Research stopped” state and owner cancellation event. The earlier accepted account remained independently stored. A transient Aurora TLS connection interruption during this check did not terminate the worker; the API returned to healthy operation.

The frontend/backend deterministic integration gate passed. This does not certify the simulated account as a live scientific result or establish that the Box integration gate has passed.

`scripts/validate_gateway.py` supplements browser testing with actual HTTP calls through Next.js. It creates isolated anonymous development principals and preserves its drafts/cancelled job as evidence. It checks source lookups, ownership isolation, CSRF, draft version conflicts, idempotent creation/submission, cancellation, and persisted event replay. Run only while the stack is explicitly deterministic:

```bash
python3 scripts/validate_gateway.py
```

All ten gateway checks passed against Aurora with TLS enabled. Evidence is in `data/validation/local-stack/gateway-validation.json`, including the exact source, embedding and mapping runs. Test draft `8c1bd3a8-f5f6-4729-8566-f1b313871f6b` reached version 2; repeat submissions returned job `da0e3496-5c03-4714-be8c-ed60138cc772` and immutable request `f115b85c-da7e-4afa-8edb-1646081e2519`. Cancellation remained terminal even when the collector's later failed attempt wrote a diagnostic. Replayed event IDs were strictly ordered after the requested cursor.

## Live integration

Both prerequisite gates passed before switching the stack to Box mode: the deterministic application journey above and standalone live account/paragraph validation recorded in `docs/box-validation.md`. The first integrated live execution was correctly rejected, as recorded below. The bounded corrected retry passed trusted account and paragraph acceptance; each run has its own acceptance record.

- A fresh dedicated Chrome session exercises pre-login behavior because the earlier in-app browser surface became unavailable. No prior browser cookies are assumed.
- Fresh suggestion retrieval exposed a public audit-write defect (`owner_id` null). The UI displayed a service error without enabling submission. The final image uses reserved catalog ownership and a matching non-null regression constraint. Retesting before login passed, including context-only cardiovascular suggestions. During the active embedding request, unrelated API readiness returned HTTP 200 in 1.64 seconds.
- The existing mapped catalog returns no exact lexical `CADinT2D` match. It does return `factor:portal:CAD:cfde-inc-v2:Factor1` (Lipoprotein Metabolism and Clearance). The browser selected that sole actual mapped CAD factor through exact native-ID search. The earlier RVSVI factor was not relabeled and no new mapping was imported.
- Manual anchor results now expose their existing trait/factor subtitle and native-ID tooltip so similarly named candidates are distinguishable.


- Actual anonymous browser job `be002a14-6915-42e5-ab9b-9db0c1acdef2`, draft `2f8a4a4f-da93-4823-9ea7-5b5dbcc0eb94`, reached remote Box `boss-hermit-53702` in persisted running phase. Reload preserved the job URL and replayed public activity.
- Actual Anthropic input measurement reduced 26,189 tokens to 22,173 under the enforced 24,000 limit. Frozen dispatch SHA-256: `d77cc239c7fb0f322e204852f14f0b04f2a18be2b1d86d1103f8ce41e9e5c79d`. Coverage retains five nodes/four edges from 17 captured nodes/16 edges and explicitly lists 12 omitted nodes/12 omitted edges. BioIndex observations are marked non-exhaustive. Exact CAD Factor1 remains selected; raw source bytes remain preserved.
- BiomarkerKG and ProKN are configured for this job. Actual selected-graph calls and outcomes will be checked from the completed ledger, separately from configuration.

- The initial live stream exposed two public-event presentation defects: text-delta metadata was dropped and parser tool names were not projected. Fixes preserve optional `message_delta`, map actual tool names/error states, and concatenate only marked adjacent fragments without changing stored events or replay cursors. Nine frontend tests and typecheck passed. Historical unmarked fragments remain unaltered; no heuristic grouping is used. The actual same-job replay through the corrected worker verified one visible text node growing into a complete phrase and tool rows labeled `Read` and `Glob`; no extra paid run was used.
- The running worker resumed the same persisted remote attempt after `RuntimeError`; the Box and frozen input were preserved. The outcome is recorded below; this first attempt is not a scientific success.

- Diagnosis was async heartbeat starvation from synchronous per-event database transactions. The corrected worker moves event writes, checkpoints and cancellation queries to threads; focused slow-storage renewal checks and the backend suite passed (146 passed, eight optional skips).
- Worker crash recovery was then exercised against the already-completed Box result: read-only evidence established remote success, final ledger, 297 public events and $0.57002070 cost. The original acceptance explicitly requires worker restart. After an initial automatic-review rejection based on assumed active model execution, corrected evidence was approved. Only the verified local worker container was killed/recreated; same job, attempt 1, Box, cursor and frozen dispatch were retained. Local output was empty at the guarded transition. Preflight: `data/validation/local-stack/worker-restart-preflight.json`. The new worker fetched the existing Box; it did not create another model execution.

- Recovered replay durably advanced to remote cursor 101 of 297 while retaining Box `boss-hermit-53702` and attempt 1, with repeated lease renewal. Per-event Aurora round trips make backlog draining slow; this is a concrete performance limitation, not continued model inference. The first job became terminal at 21:29:18 UTC; its model had completed at 21:05:11 UTC. This observed delay includes live debugging, review and worker replacement, so it is not equated with the measured per-event SQL baseline.

- First live result: **rejected by scientific validation**, correctly shown in the browser as “Research could not complete.” The independent grounding report rejected causal-direction language derived from factor-loading association. No account was published (`result=null`, owner account count zero); the saved draft remained available and no paragraph was enqueued. The completed Box was deleted, with durable cursor 297 and attempt 1 unchanged. The gate rejected unsupported inference rather than promoting a structurally valid document.
- The complete ledger has 17 calls: local Read/Glob and account-writing/linting tools, with 14 completed and three failed observations retained. Neither BiomarkerKG nor ProKN was actually queried in this run. They were configured; no external graph enrichment is claimed.
- The first run remains a failure record. Before retry, source-inference guardrails were strengthened across every scientific field, bounded investigation of selected graphs was required, and independent acceptance remained unchanged. Event persistence now commits at most 20 events/256 KiB per batch without merging stored events; checkpoint database errors remain recoverable. The combined backend suite passed 169 tests with eight optional skips.

Corrected retry:

- A full stopped-state restart restored the same anonymous workspace and saved draft in Chrome. The actual browser submitted job `431fcc31-46e7-42a3-8f8b-5ff0be1e9403`, using the sole native CAD Factor1 and both selected graphs. Box `natural-mutt-93356` is attempt 1.
- Actual token measurement reduced 26,171 to 22,155 under 24,000. Dispatch SHA-256 `80b306057a832dced2d0384c8be58c0d503a9de759b8cf89a93e99eee77a93f5`; five nodes/four edges retained, with 12 omitted nodes/12 omitted edges explicit.
- Browser reload reconnected to the same persisted job and coherent marked text/tool activity. The ledger contains 33 entries: 28 completed, three failed, two empty. Both selected graphs had schema retrieval and two actual bounded queries. Discovery searches returned disease labels or unrelated sequence matches; the resolved disease/gene queries were empty. No graph result was promoted as biological support.
- Trusted final account validation passed with zero errors/warnings, and independent review supported both claims and the synthesis. Accepted account: `dapper:ScientificAccount.mZ1WzVP3O9i63nEZCHLBk-vMbZkEICZV`; component claim: `dapper:Claim.NZs1bmu2UfabvcU9OGLiAWrLIt_XDE61`. The account identifies a model-specific candidate and explicitly leaves the causal question open. A minor generated-prose imprecision calls the exact BioIndex source a CFDE interactive endpoint; the retained artifact, row pointer and quantitative values are correct. The accepted document was not edited after validation.
- Remote authoring completed at 21:56:40.392990 UTC ($1.00698705); trusted job completion was 21:58:46.174939 UTC. The observed 125.782-second interval includes backlog capture, validation, independent review and publication, so it is not an isolated batch benchmark. All 445 remote events were retained under attempt 1, and the Box was deleted. This run avoided the first run’s long per-event backlog.
- The browser showed Conclusions first, claims collapsed, then the separate paragraph pending state. Claim Assessment, Evidence and Provenance resolved exact source IDs, `/data/0`, activity and payload checksums. A browser-downloaded 2,485-byte source artifact matched DAPPER File SHA-256 `a7ddfb379ef54fe5b14a88ab9deb29c40a3ec84f87c22d6a2ba06e73fd9da1ac`.
- Automatic paragraph job `36d411a1-f24f-4cb7-be66-6ed517394d8d` succeeded, producing `dapper:Paragraph.Wc-KnUcXFLc4YfAkVkjtEdIj3-OHS1m0`. Trusted profile and independent faithfulness checks passed; all ten original account objects were unchanged. Its Box `absolute-polecat-03575` was deleted (34 remote events, attempt 1). Reported paragraph authoring cost: $0.09836205.
- The browser rendered four citation spans and three unique revision-1 references. The KnowledgeGap citation opened the exact `/id/` scientific record, while claim links retained their account context. Actual Markdown (2,466 bytes), LaTeX (1,520 bytes) and BibTeX (2,384 bytes) downloads all retain the same three target IDs; hashes/copies are in `browser-exports/live/`. LaTeX companion guidance appeared. Word copy reported success; the automation clipboard bridge returned no content, so native Word paste is not claimed.
- Accepted Conclusions were visually inspected at 390×844 with document width exactly 390px. The completed Paragraph exposed long reference URLs widening the page to 514px; one `.references { overflow-wrap:anywhere }` rule corrected it to 390px. The actual paragraph, citation jump and wrapped bibliography were then visually verified, and the viewport override was reset. Paragraph rendering and citation requests took approximately 7–14 seconds on this Aurora connection; the UI retained an honest loading state.
- Separate evidence: `data/validation/local-stack/live-browser-retry-validation.json`.

## Final lifecycle and handoff

- Both live jobs were terminal and both Boxes durably deleted before shutdown. Repeated `dev-down` left zero project containers, no listeners on 3000/8000 and no supervisor state. Data and artifacts were preserved.
- Final Box-mode startup passed API/Aurora, worker and frontend readiness. Repeated startup reused frontend PID 93017 and containers `be213ea474a1` / `dde022d60a52`; no duplicate services were launched.
- After full restart, the same anonymous browser workspace rendered the accepted account and cited paragraph, preserved all citation revisions, and listed the saved draft and successful research in workspace history.
- Final state: healthy local stack at `http://localhost:3000`, Box mode, zero nonterminal jobs (checked 2026-09-25T22:08:12Z), no active validation Boxes. Stop with `./scripts/dev-down.sh`. Evidence: `data/validation/local-stack/final-lifecycle.json`.
- The integrated live gate **passed**. The earlier rejected attempt remains recorded as a failure; the accepted retry is separately recorded in `live-browser-retry-validation.json`.
- Verification scope: nine frontend tests, typecheck and production build passed before the final CSS-only wrap correction. That one rule was compiled by Next dev/HMR and checked in the actual mobile browser; production build was not rerun against the active `.next` directory. Native Word paste and real Google/ORCID OAuth remain unverified as noted above.
