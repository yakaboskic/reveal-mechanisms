# DIG service platform deployment

The supported target is the HTTP-only service described in [durable-workflow-runtime.md](durable-workflow-runtime.md). Upstash Workflow delivers signed execution steps, Upstash Vector serves semantic retrieval, and Redis Pub/Sub provides event wakeups without queue polling. Aurora owns durable state and versioned S3 owns artifacts and recovery checkpoints.

## Current rollout status — October 1, 2026

The QA backend uses clean source `ec88469dcda2b75d93cfdef62c32aa41cacfee71`, exported as platform revision `6778259099a339f0ca80d8ff0f76f82807aa5bd8`. [CI run 36787763609](https://github.com/broadinstitute/dig-service-platform/actions/runs/36787763609) and [QA deployment 36788021790](https://github.com/broadinstitute/dig-service-platform/actions/runs/36788021790) passed, including expected-task rollout and public health checks. The workflow runs from its protected `main` reference with the tested platform revision supplied explicitly as the image/source SHA; the image does not use platform main's default source. Service configuration, secrets and production remain unchanged.

The public [QA Preview](https://reveal-mechanisms-qa.vercel.app) uses frontend revision `412bcc2` and ready Vercel deployment `dpl_CGM2xgUjSpfRosUrpWR4HhvyoGZf`. Knowledge gaps have separate Info links and read-only detail pages with collapsed DisMech metadata, experiments, evidence and attached nodes. Scientific accounts and explorations start collapsed and fetch only on expansion. Selected-gap loading and editing no longer show homepage browsing. Registered users can vote on gaps and published accounts with matching vertical controls, and trending questions can be sorted by account count or net votes. Notifications remain event-driven without Redis polling.

The latest frontend-only update makes each scientific account’s full closing summary its link, removes the arrow beside Info, and caps displayed citation titles at 60 characters on a word boundary. Truncated titles have an ellipsis outside the link followed by the full DAPPER ID; full citation text remains in Formatted reference and canonical metadata. All 103 frontend tests, typecheck, and the local image build passed. Local and hosted QA browser checks verified these changes using an existing published account; no research job or ballot was submitted. A subsequent CSS update centers voting beside the actual question in both the composer and gap detail header, with the disease label in a separate row. The local and Vercel builds passed, and browser checks confirmed matching vertical centers in both views. The backend revision is unchanged.

Authoring instructions now ask for distinct, supported propositions that build a coherent account, rather than defaulting to one Claim. Evidence rules and budgets are unchanged. One justified Claim or an insufficient-evidence outcome remains valid. Historical measured prompts are byte-compatible. No new paid scientific run was used to measure this instruction change.

Validation: 934 backend tests and 311 subtests passed (8 optional skips), 98 frontend tests and typecheck passed, both local images built, and 240 platform/service tests passed. All 575 exported files matched their hashes and both environment templates rendered/linted without errors. Local browser checks exercised separate Info/Explore navigation, lazy collapsed result sections, imported metadata, and registered gap/account vote changes in an isolated workspace. Forty live QA checks verified the deployed schemas and vote operations, both ranking modes, exact-gap public collections, an existing published account's vote state, registered reads, anonymous-write rejection and `polling: false`. These acceptance checks created no research job, publication or ballot and issued no Redis commands. The public QA browser also verified Info/Explore navigation, collapsed sections, matching vote rails, and the visitor sign-in prompt without submitting a research job or ballot. Runtime evidence is in `.runtime/workflow/gap-community-release.json`, `.runtime/workflow/qa-gap-community-acceptance.json` and `.runtime/local-voting/gap-community-verification.json`.

## Previous rollout — September 30, 2026

The latest QA release uses source `fc075b9` and platform revision `18bfcd7f0fc07cd975cf63946f5e3d7679513ce7`, deployed by [QA run 36759938582](https://github.com/broadinstitute/dig-service-platform/actions/runs/36759938582). Workspace accounts and explorations now support full-history search and explicit Published/Private badges. Finished submitted draft revisions leave the Drafts selector; new edits and active retries remain available, and stored drafts/run history are retained. Workspace updates continue through events. The backend suite passed 917 tests and 303 subtests (8 optional skips), and all 240 platform tests passed. The dk integration uses the [existing trusted gateway flow](application-gateway.md), with shared QA signing/service credentials delivered privately. Registered workspaces persist without a daily analysis cap. QA permits ten outstanding jobs per workspace while Box/scratch execution stays at two; production settings are unchanged. No additional application-key authentication layer was introduced.

The main frontend is live as a public Vercel **Preview** at [reveal-mechanisms-qa.vercel.app](https://reveal-mechanisms-qa.vercel.app), connected to `https://api-qa.hugeampkpnbi.org/api/reveal`. The QA alias has a deployment-protection exception, so visitors can use the plain URL without a Vercel login or share token. Project-wide deployment protection and application workspace authorization remain enabled. Deployment `dpl_ES56zESrM26gnG2iysXpKGCNpTZv` is ready and the QA alias is assigned. All 16 settings were uploaded to the Preview environment only, with `NEXTAUTH_URL=https://reveal-mechanisms-qa.vercel.app` and artifact redirects restricted to `https://cyaka-reveal-data.s3.us-east-1.amazonaws.com/qa/`. Production settings and alias were not changed. All 82 frontend tests and typecheck passed. Eighteen hosted search/publication checks and fifteen public API/SSE checks passed; these checks launched no paid job. Local browser checks exercised publication badges, search beyond the first page, clear/empty states, and finished-draft filtering. The public QA workspace and its new styles were verified after alias assignment.

QA OAuth callbacks must be registered in the existing provider applications while retaining their other callbacks:

- Google: `https://reveal-mechanisms-qa.vercel.app/api/auth/callback/google`. The live authorization check returned `redirect_uri_mismatch`; registration is still required.
- ORCID: `https://reveal-mechanisms-qa.vercel.app/api/auth/callback/orcid`. The configured sandbox provider (`https://sandbox.orcid.org`) accepted this callback at authorization entry (HTTP 200); completed login and token exchange remain unverified.

The gateway helper and existing authorization/capacity behavior passed 118 focused tests and 11 subtests. All 240 platform tests, 557 export checksums and template lint passed; the QA render adds only the job-limit setting, while the production render is byte-identical. Local and public QA each passed 12 checks for persistent identity, distinct users, draft writes/idempotency, job input validation and authenticated SSE. The checks launched no research jobs or model calls. The deployment pipeline verified the expected task definition and public health. Reports: `.runtime/workflow/gateway-local-acceptance.json`, `.runtime/workflow/gateway-qa-acceptance.json`, `.runtime/workflow/gateway-qa-deployment-acceptance.json`.

The preceding workspace-key release used source `54b9a2cc086922abd0480765c50507c336b106ba`, exported as platform revision `bed2256496d2a6f1ac2df649c68bc350a9fd7e0d`, and was deployed to QA by [QA run 36691740277](https://github.com/broadinstitute/dig-service-platform/actions/runs/36691740277). [Workspace API keys](api-keys.md) now support direct draft/job access and authenticated event streams. Configuration stores only a key hash and existing owner UUID; QA and local keys are separate, and production key access is disabled. Swagger presents one ApplicationBearer input and retains the supported OpenAPI dialect.

Contract validation, 881 backend tests and 303 subtests (8 optional skips), 70 frontend tests, typecheck, 240 platform/service tests, and QA/prod template render/lint passed. All 556 exported file checksums match. Both the running local API and public QA API passed 15 live checks for identity/expiry, environment isolation, internal/admin denial, draft creation/editing/idempotency, job input validation and authenticated SSE. The deployed contract exactly matches the canonical contract apart from its mounted server prefix. Accepted job submission, quota, lifecycle and cross-owner boundaries are covered by behavioral tests; these live smoke checks created no research job or model call. Reports: `.runtime/workflow/api-key-local-acceptance.json`, `.runtime/workflow/api-key-qa-acceptance.json` and `.runtime/workflow/api-key-release.json`.

The preceding direct-transfer release, source `473aa6f4932d32be0de0045d5d103a81269968bd` and platform `baa1b581eb3bc2b2c644503fa7358321748e8b39`, passed [QA run 36685849303](https://github.com/broadinstitute/dig-service-platform/actions/runs/36685849303), template lint and all 15 public HTTPS acceptance checks, including semantic retrieval, authenticated SSE, reconnect replay and environment isolation. Unsigned cleanup callbacks returned 401.

On that preceding release, managed signed probe `ce4b4ebd-0691-4433-908b-625de20ae032` completed in generation 1 with exactly two durable phases, restoring the same S3 checksum on the deployed ECS task. It created no Box, made no model calls or Redis reads, and left the active Vector snapshot unchanged. No task replacement was requested for this probe. Reports are `.runtime/workflow/qa-direct-transfer-public.json`, `.runtime/workflow/qa-direct-transfer-managed.json` and `.runtime/workflow/qa-direct-transfer-cleanup-auth.json`.

[Production run 36692523152](https://github.com/broadinstitute/dig-service-platform/actions/runs/36692523152), for the earlier QA-tested platform revision `bed2256496d2a6f1ac2df649c68bc350a9fd7e0d` with API-key access disabled, now reports completed/success. The current workspace release is QA-only; this rollout did not update production. The older pending run `36689579490` was cancelled after creating this replacement request. The preceding read-only production preflight found no queued, running or cancellation-pending jobs. All 16 frontend production settings are already uploaded to Vercel as sensitive variables. The production frontend connection remains deferred; no production frontend alias was created in this rollout.

After the Broad Upstash credential rotation, [QA replacement run 36702863162](https://github.com/broadinstitute/dig-service-platform/actions/runs/36702863162) replaced the task while preserving its image and task definition. A real QA research run, `aa2d812f-1c01-4f3f-b236-36dbe3e850ef`, completed with the valid `insufficient_evidence` outcome and no execution error. Its downloaded artifact checksum, sandbox deletion and capacity release were verified; it did not produce an accepted ScientificAccount. The signed local workflow probe and QA public checks also passed with the rotated credentials. Evidence: `.runtime/upstash-team-rotation/acceptance.json` and `.runtime/upstash-team-rotation/qa-replacement-evidence/report.json`. Shared ingress limits are unchanged.

| Component | Current deployment target |
| --- | --- |
| Frontend | Vercel project `reveal-mechanisms`; public QA Preview live, production pending |
| Backend | One HTTP Fargate service under `/api/reveal/*`; no worker or dispatcher service |
| Execution delivery | Managed Upstash Workflow/QStash signed callbacks |
| Notifications | Managed Redis REST streaming Pub/Sub; Aurora event replay |
| Semantic retrieval | Verified, environment-specific Upstash Vector snapshots |
| Durable state and files | Existing Aurora database and versioned S3 |

`deploy/dig/service.yaml` selects ARM64, read-only runtime storage, bounded scratch, a 120-second process stop timeout and 300-second target-group drain. QA uses `reveal_workflow_qa_*` tables, separate job/notification namespaces, the `qa/` S3 prefix and Vector environment `qa`. Production preserves `reveal_*` records, writes `prod/`, retains historical `local/` artifact reads and uses its own namespaces. Backend credentials come from environment-specific Secrets Manager records; the frontend receives only its documented gateway/session settings.

Local probes have exercised managed QStash callbacks, Redis fanout with no idle reads, verified Vector retrieval/import and two-tab workspace delivery. The [runtime guide](durable-workflow-runtime.md#verification-status) records test counts and live local evidence separately from cloud acceptance. `scripts/workflow_cloud_config.py` prepares private environment files, and `deploy/dig/render_release.py` emits the HTTP service only.

## Historical worker proposal

The remainder records the initial platform assessment and worker rollout proposal before the Workflow refactor. Its Redis Streams, background-service and provisioning checklist is retained for migration context and rollback planning; it is not the current release procedure. The default `deploy/compose.yaml` is worker-free, while `deploy/compose.legacy.yaml` retains the old pool.

### Initial decision: Fargate workers and Upstash Redis

**The platform can run REVEAL's worker pool.** Its ECS Fargate cluster supports long-running services without a load balancer, and its existing CloudFormation role can create/update services, register task definitions, create service IAM roles and write logs. Its current HTTP template needs a separate background-service deployment, rather than an architectural replacement. The existing genesets service already uses application-specific background infrastructure alongside its API.

The initial proposal used this shape:

| Component | Deployment |
| --- | --- |
| Frontend | Personal Vercel project `reveal-mechanisms` |
| API | One on-demand Fargate task under `/api/reveal/*` |
| Workers | Separate on-demand Fargate service, desired count 2 |
| Dispatcher | Separate on-demand Fargate service, desired count 1 |
| Redis | Upstash Redis, TLS TCP endpoint, eviction disabled |
| Durable state | Existing Aurora database and original `reveal_*` tables |
| Artifacts | Existing S3 bucket: write `prod/`, preserve reads of `local/` |

The scientific agent runs in Upstash Box; these workers coordinate execution, assemble evidence, validate results and save artifacts. They are persistent background containers, with no HTTP endpoint or request-duration limit. ECS service scheduling maintains their desired count. Fargate avoids maintaining a dedicated EC2 host; this is a suitable deployment for the current application.

The legacy `job_transport.py` uses consumer groups, stream writes/reads, `XAUTOCLAIM`, and transactional acknowledgement/deletion through the redis-py TCP client. The initial proposal considered `REVEAL_REDIS_URL=rediss://...`; the REST API cannot replace its blocking `XREADGROUP` path. This queue design was superseded: current Upstash Redis usage is tested REST streaming Pub/Sub only, with no Redis queue or recurring read commands. RDS remains authoritative for jobs and events.

References: [ECS services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html), [Upstash TCP connection](https://upstash.com/docs/redis/howto/connect-client), [XREADGROUP](https://upstash.com/docs/redis/commands/streams/xreadgroup), [XAUTOCLAIM](https://upstash.com/docs/redis/commands/streams/xautoclaim), [transactions](https://upstash.com/docs/redis/commands/transactions/exec), [eviction](https://upstash.com/docs/redis/features/eviction).

### Initial integration notes (historical)

- Backend `SERVICE_PATH_PREFIX=/api/reveal` mounts all API/internal/SSE/artifact/docs routes. `/api/reveal/health` is liveness; `/api/reveal/readyz` checks dependencies and catalog readiness. Unset prefix preserves local URLs.
- `deploy/dig/service.yaml` proposes priority 40, currently unused, with existing database identities and queue namespace `reveal-compose`. Only QA is enabled by the release renderer; the prod block exists solely to satisfy the platform schema.
- The platform branch adds an optional `runtime` block: read-only root, init, 120-second stop timeout, and bounded RAM mounts at `/work` and `/tmp`. Existing service defaults remain unchanged. Read-only tasks disable ECS Exec; use CloudWatch and operator-run maintenance tasks.
- `scripts/platform_bundle.py --fetch-assets` exports an allowlisted service-local build context. The Docker build fetches exact pinned scientific sources; no virtual environment, credentials, local research outputs or nested Git checkout are committed into the platform repository. The manifest records source revision, dirty state and file hashes.
- `deploy/dig/render_workers.py` derives worker/dispatcher services from the API image, environment, secrets and IAM permissions. It uses two workers and one dispatcher, on-demand capacity, health checks and RAM scratch. It creates no ALB routes for background tasks.
- `deploy/dig/network-stack.yaml` prepares a stable client security group and existing-RDS access, without provisioning Redis. An operator must confirm the actual Aurora SG and authorize this network change.
- `deploy/dig/render_release.py` binds an existing backend secret ARN, client SG and immutable ECR image digest, producing API/background CloudFormation templates without deploying them.

[Fargate supports tmpfs](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-ecs-tmpfs-mounts-aws-fargate-managed-instances/). [ECS Exec does not support read-only roots](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html). Initial task allocations (API 1 vCPU/4 GiB, each worker 1 vCPU/3 GiB, dispatcher 0.25 vCPU/1 GiB) are conservative starting points, not measured capacity requirements.

### Initial worker rollout checklist (superseded)

1. The initial proposal required registering `reveal` with validation/build jobs and a background-stack release step. At that assessment, registry entries and CI deployment jobs had not yet been added. A background stack named `svc-reveal-workers-qa` fit the existing deployment role's `svc-*-qa` stack permissions.
2. Create an Upstash Redis database and validate real publish/claim/reclaim/acknowledgement using disposable keys. Supply its TLS TCP URL privately. AWS tasks already have outbound internet access in the platform network shape.
3. Provision the client SG/RDS rule, and create a JSON backend secret containing the seven keys listed in service.yaml. Match fresh gateway keys in Vercel. Task roles provide AWS S3 credentials; never copy local AWS access keys into runtime secrets.
4. Add a maintenance-task path for `python -m reveal_backend.deployment drain --wait 1200`, `status`, and `resume`. The current GitHub deploy role lacks `ecs:RunTask`; an operator can run maintenance tasks, or the role can receive a narrowly scoped extension. A 120-second stop timeout alone cannot finish every research job.
5. Test an actual image build, cloud startup, RDS/S3/Upstash connectivity, authenticated SSE through nginx/ALB/Vercel, and worker restart recovery. Template lint and unit tests do not establish those end-to-end results.

The platform normally deploys both QA and approval-gated prod. Keep only one REVEAL environment active for now. Its backend would be `https://api-qa.hugeampkpnbi.org/api/reveal`; the user's app remains `https://reveal-mechanisms.vercel.app`, without a staging prefix. Prefix-aware `REVEAL_API_URL` already works with the frontend gateway.

Before cutover, drain/stop the original local `reveal-compose` consumers. Do not run local and cloud dispatchers in that same namespace against different Redis endpoints. Preserve all original tables and S3 versions; no imports, resets or table-prefix changes are required. Colleague namespaces remain separate. The local application and [colleague handoff](../README.local.md) remain the active setup; the [EC2 plan](cloud-deployment.md) remains a fallback reference.
