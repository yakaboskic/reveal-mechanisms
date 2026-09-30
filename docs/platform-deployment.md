# DIG service platform deployment draft

> Durable workflow migration: the supported target is now the HTTP-only service described in [durable-workflow-runtime.md](durable-workflow-runtime.md). The worker/Redis Streams instructions below are retained as historical migration and rollback guidance; they do not describe the new default `deploy/compose.yaml`. Live release status must be verified separately.

Access to `broadinstitute/dig-service-platform` is confirmed. The repository is cloned beside REVEAL; this review used platform commit `7810744ba7f915e23ad88ea5e008297b14efe67e`. Local integration branches are `codex/dig-service-platform` in REVEAL and `codex/reveal-service` in the platform checkout. Nothing has been pushed, merged, provisioned or deployed by this integration work.

## Decision: Fargate workers and Upstash Redis

**The platform can run REVEAL's worker pool.** Its ECS Fargate cluster supports long-running services without a load balancer, and its existing CloudFormation role can create/update services, register task definitions, create service IAM roles and write logs. Its current HTTP template needs a separate background-service deployment, rather than an architectural replacement. The existing genesets service already uses application-specific background infrastructure alongside its API.

Use this initial shape:

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

Upstash supports the commands used by `job_transport.py`: consumer groups, stream writes/reads, `XAUTOCLAIM`, and transactional acknowledgement/deletion. Use `REVEAL_REDIS_URL=rediss://...` with the existing redis-py TCP client. Its REST API does not support blocking `XREADGROUP`, which our queue uses. Keep eviction disabled and select a primary region near us-east-1. Set capacity/command budgets after measuring polling traffic. Redis contains delivery metadata; RDS remains authoritative for jobs, leases and results. No Upstash database has been created or tested live yet.

References: [ECS services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html), [Upstash TCP connection](https://upstash.com/docs/redis/howto/connect-client), [XREADGROUP](https://upstash.com/docs/redis/commands/streams/xreadgroup), [XAUTOCLAIM](https://upstash.com/docs/redis/commands/streams/xautoclaim), [transactions](https://upstash.com/docs/redis/commands/transactions/exec), [eviction](https://upstash.com/docs/redis/features/eviction).

## Local changes prepared

- Backend `SERVICE_PATH_PREFIX=/api/reveal` mounts all API/internal/SSE/artifact/docs routes. `/api/reveal/health` is liveness; `/api/reveal/readyz` checks dependencies and catalog readiness. Unset prefix preserves local URLs.
- `deploy/dig/service.yaml` proposes priority 40, currently unused, with existing database identities and queue namespace `reveal-compose`. Only QA is enabled by the release renderer; the prod block exists solely to satisfy the platform schema.
- The platform branch adds an optional `runtime` block: read-only root, init, 120-second stop timeout, and bounded RAM mounts at `/work` and `/tmp`. Existing service defaults remain unchanged. Read-only tasks disable ECS Exec; use CloudWatch and operator-run maintenance tasks.
- `scripts/platform_bundle.py --fetch-assets` exports an allowlisted service-local build context. The Docker build fetches exact pinned scientific sources; no virtual environment, credentials, local research outputs or nested Git checkout are committed into the platform repository. The manifest records source revision, dirty state and file hashes.
- `deploy/dig/render_workers.py` derives worker/dispatcher services from the API image, environment, secrets and IAM permissions. It uses two workers and one dispatcher, on-demand capacity, health checks and RAM scratch. It creates no ALB routes for background tasks.
- `deploy/dig/network-stack.yaml` prepares a stable client security group and existing-RDS access, without provisioning Redis. An operator must confirm the actual Aurora SG and authorize this network change.
- `deploy/dig/render_release.py` binds an existing backend secret ARN, client SG and immutable ECR image digest, producing API/background CloudFormation templates without deploying them.

[Fargate supports tmpfs](https://aws.amazon.com/about-aws/whats-new/2026/01/amazon-ecs-tmpfs-mounts-aws-fargate-managed-instances/). [ECS Exec does not support read-only roots](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html). Initial task allocations (API 1 vCPU/4 GiB, each worker 1 vCPU/3 GiB, dispatcher 0.25 vCPU/1 GiB) are conservative starting points, not measured capacity requirements.

## Remaining deployment work

1. Review the runtime extension and register `reveal` with validation/build jobs and a background-stack release step. No registry entries or CI deployment jobs have been added yet. A background stack named `svc-reveal-workers-qa` fits the existing deployment role's `svc-*-qa` stack permissions.
2. Create an Upstash Redis database and validate real publish/claim/reclaim/acknowledgement using disposable keys. Supply its TLS TCP URL privately. AWS tasks already have outbound internet access in the platform network shape.
3. Provision the client SG/RDS rule, and create a JSON backend secret containing the seven keys listed in service.yaml. Match fresh gateway keys in Vercel. Task roles provide AWS S3 credentials; never copy local AWS access keys into runtime secrets.
4. Add a maintenance-task path for `python -m reveal_backend.deployment drain --wait 1200`, `status`, and `resume`. The current GitHub deploy role lacks `ecs:RunTask`; an operator can run maintenance tasks, or the role can receive a narrowly scoped extension. A 120-second stop timeout alone cannot finish every research job.
5. Test an actual image build, cloud startup, RDS/S3/Upstash connectivity, authenticated SSE through nginx/ALB/Vercel, and worker restart recovery. Template lint and unit tests do not establish those end-to-end results.

The platform normally deploys both QA and approval-gated prod. Keep only one REVEAL environment active for now. Its backend would be `https://api-qa.hugeampkpnbi.org/api/reveal`; the user's app remains `https://reveal-mechanisms.vercel.app`, without a staging prefix. Prefix-aware `REVEAL_API_URL` already works with the frontend gateway.

Before cutover, drain/stop the original local `reveal-compose` consumers. Do not run local and cloud dispatchers in that same namespace against different Redis endpoints. Preserve all original tables and S3 versions; no imports, resets or table-prefix changes are required. Colleague namespaces remain separate. The local application and [colleague handoff](../README.local.md) remain the active setup; the [EC2 plan](cloud-deployment.md) remains a fallback reference.
