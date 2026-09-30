# DIG service platform deployment

The supported target is the HTTP-only service described in [durable-workflow-runtime.md](durable-workflow-runtime.md). Upstash Workflow delivers signed execution steps, Upstash Vector serves semantic retrieval, and Redis Pub/Sub provides event wakeups without queue polling. Aurora owns durable state and versioned S3 owns artifacts and recovery checkpoints.

## Current rollout status — September 30, 2026

The HTTP-only QA backend is deployed at `https://api-qa.hugeampkpnbi.org/api/reveal`, using REVEAL source `cf87246e1aedf06afa3deedd023a09a19e002d3f` and platform image revision `c563f40e1061c408459081310f4dc534f0038680`. [Platform run 36657927190](https://github.com/broadinstitute/dig-service-platform/actions/runs/36657927190) completed successfully; the platform suite passed 240 tests. Public HTTPS acceptance passed all 15 checks, including authorized SSE/replay, stored-context Vector retrieval and QA isolation.

Managed signed QA probe `1e817d2a-d6bc-4558-b6d0-de3cf8ff9ce3` completed in generation 1 with exactly two phases across distinct ECS tasks (`c0531f378af645e382c26b85f412c02c` → `d027d3b88a0b4cf6b45310397070830c`), restoring the same S3 checkpoint checksum. It created no Box and left the serving Vector snapshot unchanged. Evidence is saved in `.runtime/workflow/qa-managed-replacement-final.json`. The [read-only lifecycle audit](https://github.com/broadinstitute/dig-service-platform/actions/runs/36660817274) passed with no mutations: the old task is stopped, the replacement is the sole running task, and both use `svc-reveal-qa:2` with image digest `sha256:b9f27ba0ebc2ed31cb0f3c4f8b7ec7218399a0347609a8a2d715d2e68536ff93`. Its report is `.runtime/workflow/qa-replacement-completion-audit/report.json`.

The final-review schema correction is deployed, but scientific acceptance after that fix remains unverified: no paid rerun was performed. Cloud ingress limits are unchanged, and long scientific phases still need qualification through the full ingress path. [Production run 36660894864](https://github.com/broadinstitute/dig-service-platform/actions/runs/36660894864) is queued for platform revision `c563f40e1061c408459081310f4dc534f0038680`, awaiting the existing required reviewer `sagehen03`. Vercel secret-upload approval and frontend deployment are also pending.

| Component | Current deployment target |
| --- | --- |
| Frontend | Personal Vercel project `reveal-mechanisms`; deployment pending |
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
