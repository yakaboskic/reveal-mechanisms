# Implementation status — September 30, 2026

The local Workflow pilot runs Next.js and FastAPI in Docker at **http://localhost:3100**, with managed Upstash Redis Pub/Sub and Vector, QStash delivery, isolated Aurora/RDS application tables and versioned S3 storage. See the [runtime guide](durable-workflow-runtime.md) and [API walkthrough](api-quickstart.md). The [legacy colleague handoff](../README.local.md) remains available at port 3000 during migration.

## Implemented

- DisMech gap discovery, trending indicators, source detail and EAGGL/CFDE suggestions using stored embeddings.
- Anonymous and registered sessions, Google/ORCID integrations, durable identities, owner-scoped drafts/history, publication and workspace recovery. Local Google identity recovery preserves the original broad-email account; a new cloud callback still needs provider configuration.
- RDS dispatch intent, signed Workflow callbacks, namespace-scoped concurrency controls, fencing, checkpoint recovery and graceful shutdown. Redis Pub/Sub wakes authorized SSE replay and workspace invalidations without Redis polling.
- Versioned S3 artifacts, checksum verification, authorized downloads and bounded RAM scratch across Workflow phases.
- Frozen evidence collection, Box/Claude authoring, shared agent/backend structural and source validation, trusted reference-based assembly and independent scientific review.
- Accepted ScientificAccounts, cited statements/exports, explicit insufficient-evidence explorations, publication and review-retry workflows.
- Administrator telemetry and authorized table/job inspection; see [admin telemetry](admin-telemetry.md).
- Encrypted colleague configuration, automatic pinned-source setup and separate per-clone queues over the same RDS tables.

## Validation and limits

The direct-transfer implementation adds direct Box/S3 transfers, independent
signed cleanup and review steps that update only their checkpoint JSON and
immutable manifest. Initial preparation still builds and validates the input
bundle in API scratch, then atomically saves its S3 reference and frozen settings
before Box allocation. New bootstrap steps download that exact version directly
into Box, with no API checkpoint restore or temporary directory. Old descriptors
keep their existing bootstrap path. Direct capture and review call/tool steps
also use no temporary directory; the API streams captured objects for hash,
credential and ledger validation. Initial preparation, validation, review
initialization and acceptance still use bounded file-based scratch. No workspace
cache was added. Cleanup survives main-workflow completion and generation
changes, retaining remote capacity until deletion is acknowledged; rollback
must drain its durable obligations or keep the cleanup consumer available.

A read-only benchmark restored a saved 244-file, 9,347,440-byte checkpoint in
74.173 seconds serially and 16.570 seconds with four bounded downloads (4.48×),
with no model calls or Redis commands. This single cold-restore comparison does
not measure full job latency. Progress labels also stop attributing collection
time to the completed authoring notice. The backend passed 807 tests (8 skipped,
303 subtests); the frontend passed all 70 tests and typecheck. A live synthetic
probe captured 244 files in 38.013 seconds and retried in 39.021 seconds with
exactly the same object versions. Independent cleanup and duplicate receipt
replay passed; a fresh status request confirmed the Box was deleted with HTTP
404, even though its metadata remains available. A separate live synthetic Box
downloaded its exact input bundle directly from S3 with API download, restore
and temporary directories disabled in the probe. Setup including installation
took 26.267 seconds; the original-handle lost-acknowledgment retry took 1.020
seconds. Remote checksum, fingerprint and subsequent deletion checks passed.
Both local images are healthy, and running transfer modules match the tested source.
The backend is also deployed to QA; production awaits the approval recorded below.
These optimizations have no new paid scientific acceptance run. See the
[runtime guide](durable-workflow-runtime.md#direct-transfers-and-capture-optimizations)
for protocol, rollback and benchmark details.

Historical live browser → RDS → Box/Claude → account/paragraph journeys, recovery checks and provider costs are in [validation report](validation-report.md). Local S3 and Redis failure drills used an isolated verification namespace; disruptive probes are refused on shared application tables. Handoff checks are recorded separately so packaging tests are not mistaken for paid scientific runs.

A pre-refactor validation failure was caused by trusted assembly adding a mechanism named only in prose. Assembly now follows schema-declared references. Replaying that saved candidate passed structural/source validation with no findings; the failed job was preserved and independent review was not rerun. This does not mean the old job became accepted.

The first managed Workflow pilot completed one Box authoring attempt, captured its output in S3 and confirmed Box deletion. Independent review acknowledged 31 calls, then retained an unresolved final-call reservation; the original response status was not recorded. Free token-count checks independently reproduced rejection of the old final-decision schema and acceptance of its correction, which passed 82 focused tests. The original job remains `REVIEW_UNAVAILABLE`, accepted no account and dispatched no paragraph. Its captured account hash was verified through a private operator S3 download; the scientific artifact API returns 404 until acceptance. No paid retry or reauthoring was performed, so scientific acceptance after the fix remains unverified.

The default semantic path uses verified Upstash Vector snapshots and bounded candidate retrieval, with exact scoring and frozen provenance. It does not load full embedding matrices at API startup or silently fall back to NumPy. The old preload measurements remain in [database/evidence performance](database-and-evidence-performance.md). Current test counts and live local provider/browser probes are recorded in the [runtime verification status](durable-workflow-runtime.md#verification-status); those checks do not establish cloud or scientific-run acceptance.

The original `reveal_*` users, jobs, publications and artifact references remain authoritative for production and legacy colleague handoffs. The Workflow pilot and QA use separate application table prefixes, job/notification namespaces and Vector environments. The two-job cap is per namespace; separate stacks can increase total provider spending. Network access and valid credentials remain prerequisites.

## Public deployment

Application source `1102d38d649592bcd3e80665d061b4ac22404355`, exported as platform revision `811028d93b44f710740c2765516d56b2daf52ea5`, is deployed to QA by [QA run 36688872064](https://github.com/broadinstitute/dig-service-platform/actions/runs/36688872064). The follow-up uses the default OpenAPI 3.1 dialect; the public Swagger UI now renders without its unsupported-dialect warning. Paths, payload schemas and examples are otherwise identical. Contract validation, 24 focused backend tests, 70 frontend tests, typecheck, 240 platform/service tests and service validation passed. All 554 exported file checksums match. Nine read-only public API checks passed, including exact contract equality apart from the mounted server prefix, readiness, catalog access and private-route authentication. Report: `.runtime/workflow/qa-openapi-dialect.json`.

The preceding direct-transfer release, source `473aa6f4932d32be0de0045d5d103a81269968bd` and platform `baa1b581eb3bc2b2c644503fa7358321748e8b39`, passed [QA run 36685849303](https://github.com/broadinstitute/dig-service-platform/actions/runs/36685849303), template lint and all 15 public HTTPS acceptance checks, including semantic retrieval, authenticated SSE, reconnect replay and environment isolation. Unsigned cleanup callbacks returned 401.

On that preceding release, managed signed probe `ce4b4ebd-0691-4433-908b-625de20ae032` completed in generation 1 with exactly two durable phases, restoring the same S3 checksum on the deployed ECS task. It created no Box, made no model calls or Redis reads, and left the active Vector snapshot unchanged. No task replacement was requested for this probe. Reports are `.runtime/workflow/qa-direct-transfer-public.json`, `.runtime/workflow/qa-direct-transfer-managed.json` and `.runtime/workflow/qa-direct-transfer-cleanup-auth.json`.

[Production run 36689579490](https://github.com/broadinstitute/dig-service-platform/actions/runs/36689579490) requests the same QA-tested platform revision and awaits required reviewer `sagehen03` (Drew Hite); the current operator cannot approve that environment. The older pending run `36686792641` was cancelled after creating this replacement request. The preceding read-only production preflight found no queued, running or cancellation-pending jobs. All 16 frontend production settings are already uploaded to Vercel as sensitive variables. Frontend deployment awaits production backend readiness. Real Box direct-transfer and deletion probes passed locally. The deployed QA probe verifies task-role S3 checkpoint reads/writes and signed delivery; full cloud Box transfer and long scientific execution remain unverified. No new paid scientific run was made. Shared ingress limits are unchanged. See [platform deployment](platform-deployment.md).

## Data baseline

Imported snapshots include 801,934 DAPPER GeneSets, 4,037 EAGGL factors, 2,553,330 nonzero gene loadings and 3,367 DisMech gaps. Mapping/embedding run IDs remain pinned with saved selections. These are historical verified import counts, not a fresh recount. [Data inventory](data-inventory.md) and [database readiness](database-readiness.md) retain source reports. Routine startup never repeats imports.
