# Local Docker deployment

> Durable workflow migration: the supported target is now the HTTP-only service described in [durable-workflow-runtime.md](durable-workflow-runtime.md). The worker/Redis Streams instructions below are retained as historical migration and rollback guidance; they do not describe the new default `deploy/compose.yaml`. Live release status must be verified separately.

**Current startup:** use the [Workflow runtime guide](durable-workflow-runtime.md). For an existing legacy encrypted handoff, [README.local.md](../README.local.md) documents `scripts/local_setup.py`. The HTTP-only DIG QA backend is deployed and its public HTTPS and managed callback checks passed; production and frontend promotion remain pending. The worker and EC2 sections below describe the retained earlier option.

This stack runs the deployable API image, Redis Streams, a dispatcher, and two worker containers against the **existing dev RDS database**. A separately built Next.js container represents Vercel. Artifacts use the real AWS bucket `cyaka-reveal-data` in `us-east-1`: this stack uses `local/`, and EC2 will use `prod/`. Public deployment URLs remain `https://<domain>` and `https://api.<domain>`, without a staging prefix.

This legacy stack uses the original application records. `up` shuts down the original development stack through `scripts/dev_stack.py down`, including its host Next.js process, before taking over port 3000. It uses the **existing `reveal_records` and `reveal_transaction_lock` tables**, preserving the original users, login identities, drafts, scientific accounts, and publications. The earlier `reveal_compose_*` tables are retained as an inactive verification workspace. The Workflow pilot uses separate `reveal_workflow_local_*` tables. Startup never imports catalogs or resets the database.

## Run

Prerequisites: Docker with at least 10 GiB available to its VM, Docker Compose 2.30 or newer, either the encrypted shared-credential bundle or AWS CLI v2 with a working project profile, the project's Python virtual environment, and the working root `.env` with the existing RDS credentials and CA file. Keep the pinned DAPPER checkout and DisMech source checkout available. Preparation checks their release/file hashes and packages the required source files plus the DisMech schema from the index’s pinned Git revision into the image; deployed containers do not mount laptop source directories.

Configure storage once by copying `deploy/storage.env.example` to `.runtime/deployment/storage.env`, then select your AWS profile. The bucket must already exist with versioning and all four Block Public Access settings enabled. Startup checks these settings without changing bucket policy. Local preparation rejects the `prod/` prefix.

From the repository root:

```sh
.venv/bin/python scripts/local_deployment.py up --build
.venv/bin/python scripts/local_deployment.py status
```

When first adopting the original filesystem-backed application, stop its dev stack and run `scripts/migrate_local_artifacts.py` with the project Python. It inventories and checksums files without writing by default; `--apply` uploads and reads back the exact bytes, preserves job workspaces and review captures, saves an S3 rollback snapshot, and updates operational storage references in one version-checked transaction. Original files remain available for rollback. Startup rejects unmigrated artifact records. Signing in again with the same OAuth provider reconnects the original account after the namespace correction.

Open **http://localhost:3000**. The API is bound to localhost port 18000. S3 uses its regional AWS HTTPS endpoint; no storage emulator runs. Redis has no host port. Configuration and generated secrets live in ignored `.runtime/deployment/` files with restricted permissions. Secrets are runtime environment variables and excluded from image build contexts. Repeated preparation reuses local signing keys so sessions continue to work. AWS CLI resolves credentials from the selected profile and writes them only to the private backend environment file; the frontend receives the allowed download URL, never AWS credentials. If your profile uses temporary credentials, renew its login and rerun `up` before expiration. EC2 uses an instance role instead.

`up` prepares configuration and builds requested images, shuts down the legacy dev stack, drains an existing deployment worker pool before replacing services, and resumes claims after all services are healthy. The earlier legacy release loaded source records and stored embedding blobs from remote RDS into an in-memory NumPy similarity index during readiness, which could take minutes. The current default uses verified Upstash Vector snapshots and does not load full matrices at startup. The source embeddings remain in Aurora `MEDIUMBLOB` columns; its `aurora_use_vector_instructions=ON` setting enables CPU optimizations, as described in the [AWS parameter reference](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Reference.ParameterGroups.html), rather than providing an embedding-search index. See the [Workflow guide](durable-workflow-runtime.md) for current readiness prerequisites.

The frontend uses a production Next.js build. Google/ORCID login requires a registered callback matching `http://localhost:3000/api/auth/callback/<provider>`; preparation does not change provider registrations. Administrative login bypass is disabled.

Normal application submissions use the configured **live Box/Claude execution** and may incur provider costs. The disruptive `verify` command **refuses to run against the original application tables**, before any stack mutation. Earlier verification used an explicitly isolated namespace and made no scientific or provider calls. It created ten probe jobs and exercised:

1. Both worker replicas, with a database-enforced maximum of two active leases.
2. Versioned, checksum-verified S3 uploads and an authorized direct download larger than 5 MiB through the real Next.js/API authorization path. Unauthorized access is rejected.
3. Redis restart without AOF/RDB persistence, followed by redispatch from RDS.
4. Forced replacement of both active workers, discarding tmpfs, followed by S3 recovery of checkpoint bytes unavailable in RDS.
5. Draining active work to completion before resuming claims.
6. Reading retained AWS S3 object versions and checking their checksums again after worker replacement.

The earlier report is retained at `.runtime/deployment/verification.json`. Infrastructure failure drills require a separately configured test namespace; never replace the primary application's records to run them. Only this deployment stack runs after the startup handoff.

```sh
.venv/bin/python scripts/local_deployment.py logs
.venv/bin/python scripts/local_deployment.py down
```

`down` sets the durable drain flag, waits for work to finish, then stops containers. It preserves RDS records and AWS S3 objects. A timeout leaves containers running for investigation. Do not delete S3 object versions referenced by RDS. Any retained volume from the former emulator is an archive, not an active storage dependency.

## Storage and job semantics

Application containers have read-only root filesystems. `/work` and `/tmp` are bounded RAM-backed scratch space; each completed worker invocation clears its scratch. Frozen inputs, captured outputs, diagnostics, and recovery manifests are uploaded to S3 before their database references or successful results are committed. A remote Box is retained if its capture cannot be durably checkpointed. RDS stores application records and scientific documents; S3 stores retained files. Only images and bounded Docker logs need host disk storage.

Artifacts use content-addressed keys, exact S3 version IDs, and SHA-256 checksums. Workers verify every restored file. The API authorizes downloads before issuing a 60-second signed URL; the frontend only forwards redirects to the configured S3 origin/path. Bytes then travel directly from S3 to the browser. Previously issued URLs remain usable until expiration if access is revoked during that interval. The AWS bucket has versioning, SSE-S3 encryption, Bucket owner enforced ownership, all public access blocked, and an HTTPS-only bucket policy (`deploy/s3-bucket-policy.json`). Prefixes are separate key namespaces, not authorization boundaries: the EC2 role must be restricted to `prod/` and local credentials to `local/` when scoped roles are provisioned. The current local stack uses the selected developer profile’s existing permissions. The smoke test also checks that unsigned direct S3 downloads are denied.

Redis carries job IDs and dispatch generations, not scientific payloads. Enqueue and dispatch intent commit together in RDS. The dispatcher republishes missing messages after broker loss. One Redis Streams consumer group acknowledges terminal/stale deliveries and reclaims abandoned messages. RDS leases and fencing reject duplicate/stale commits. Delivery is at least once; this is not an exactly-once guarantee for external provider calls. Redis uses authentication and `noeviction`; its queue is reconstructible without local persistence.

Replica count and concurrency are separate controls: Compose selects two replicas, and `REVEAL_MAX_RUNNING_JOBS` limits active leases within each job namespace. Handoff imports generate a unique `REVEAL_JOB_NAMESPACE` per clone while retaining the original application tables; status, drain and worker health are namespace-scoped. The original checkout keeps `reveal-compose`. Keep these namespaces stable across restarts. Separate namespaces can increase aggregate provider spending. Changes require editing runtime configuration and recreating containers; `prepare`/`up` regenerates local defaults. Local and EC2 leases are 90 seconds, with a 90-second Redis reclaim interval. Research workers and infrastructure probes renew leases throughout AWS transfers; replacement drills therefore include the real lease-expiry wait. Per-job budgets remain active. Aggregate provider spending limits and automated dead-letter handling remain deployment hardening work.

## EC2 and Vercel configuration

For this retained worker topology, use `deploy/compose.legacy.yaml` for the backend; `deploy/compose.local.yaml` adds the local frontend. The default `deploy/compose.yaml` now runs the Workflow API. Build/push for the chosen EC2 CPU architecture, select the immutable image digest using `REVEAL_BACKEND_IMAGE`, and provision runtime configuration at the same paths. Do not run the local preparation/bootstrap helper on EC2.

| Setting or service | EC2 / Vercel value |
| --- | --- |
| RDS | Existing dev endpoint, verified TLS, restricted security groups, application credentials |
| Application tables | Existing `reveal` users, jobs, and publications. Preserve these records when moving from local to EC2; migrate their S3 references as described below before changing the storage prefix |
| S3 | `cyaka-reveal-data`, `REVEAL_S3_PREFIX=prod/`; IAM instance role restricted to that prefix |
| S3 endpoints | Leave both endpoint overrides empty; set `AWS_REGION` and `AWS_S3_US_EAST_1_REGIONAL_ENDPOINT=regional` so the signing origin matches the regional download URL below |
| AWS credentials | Remove exported developer AWS credentials and `AWS_EC2_METADATA_DISABLED`; use the instance profile with IMDSv2 configured for the container network |
| Runtime | `REVEAL_ENVIRONMENT=production`, `REVEAL_LOCAL_DEPLOYMENT=0`, `REVEAL_EXECUTION_MODE=box`, lease/reclaim 90s/90000ms |
| Redis | Private service, strong password, no published port; matching credentials for API, dispatcher, and workers |
| API | TLS reverse proxy on EC2 forwarding to the loopback API port; disable SSE buffering |
| Frontend | Vercel root `services/frontend`, Node 22, build `npm run build` |
| Vercel gateway | `REVEAL_API_URL=https://api.<domain>`, shared gateway secret/service token, issuer, audience |
| Sessions and OAuth | Stable `AUTH_SECRET`/`NEXTAUTH_SECRET`, `NEXTAUTH_URL=https://<domain>`, Google/ORCID credentials and registered HTTPS callbacks |
| Canonical links | Backend `NEXTAUTH_URL` and `REVEAL_CANONICAL_URL` set to `https://<domain>` |
| Downloads | Vercel `REVEAL_ARTIFACT_DOWNLOAD_BASE_URL=https://cyaka-reveal-data.s3.us-east-1.amazonaws.com/prod/`, matching the signing endpoint and object prefix |

Provision the selected application tables explicitly before enabling workers. Vercel receives gateway/OAuth credentials, never RDS, Redis, Box, Anthropic, or AWS credentials. API, dispatcher, and workers use the same image. For updates, run `python -m reveal_backend.deployment drain --wait 1200` in a tools container, wait for active work to finish, replace containers, then run `python -m reveal_backend.deployment resume`. Health/status/drain/resume work on EC2; probes/bootstrap require the isolated local environment.

RDS artifact references bind the bucket, prefix, checksum and exact object version. Changing `local/` to `prod/` does not move existing records. The prepared cloud configuration uses `REVEAL_S3_READ_PREFIXES` to retain authorized reads of existing `local/` artifacts while writing new `prod/` artifacts; the frontend allowlist must include both approved prefixes. Preserve the original application tables and source objects during the handoff. See [cloud preparation](cloud-deployment.md) for the retained configuration; no cloud cutover has happened.

Local verification exercises the actual S3 bucket and developer credentials. It does not establish an EC2 IAM role, EC2 networking, TLS/DNS, Vercel runtime behavior, OAuth registration, or a successful paid Box run. Complete those environment checks using the [deployment plan](deployment-plan.md). Do not expire referenced S3 object versions; retention/garbage collection must respect RDS references.

An optional real-browser session check is available with `node services/frontend/scripts/check-local-deployment.mjs`. It uses Playwright (`PLAYWRIGHT_MODULE` can point to an installed module), checks anonymous session persistence and the gateway, and never submits scientific work. Its report is `.runtime/deployment/browser-verification.json`.

When provisioning another bucket, [AWS recommends waiting 15 minutes after first enabling versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/manage-versioning-examples.html) before writing objects. No empty folder objects are needed for `local/` or `prod/`; the first object under each prefix creates its namespace.

References: [Docker raw environment-file support](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/) and [Vercel function limits](https://vercel.com/docs/functions/limitations). Direct redirects keep large artifact bytes outside Vercel functions. SSE streams close periodically so browsers reconnect with fresh assertions and resume persisted event IDs.
