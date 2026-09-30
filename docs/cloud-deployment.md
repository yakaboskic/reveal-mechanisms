# Cloud rollout

> Durable workflow migration: the supported target is now the HTTP-only service described in [durable-workflow-runtime.md](durable-workflow-runtime.md). The worker/Redis Streams instructions below are retained as historical migration and rollback guidance; they do not describe the new default `deploy/compose.yaml`. Live release status must be verified separately.

**Current direction:** use the [DIG service platform](platform-deployment.md#current-rollout-status--september-30-2026) HTTP-only service with managed Workflow delivery, Vector retrieval and Redis Pub/Sub. The QA backend and public [QA frontend Preview](https://reveal-mechanisms-qa.vercel.app) are live; the preview needs no Vercel login or share token. The linked rollout status records hosted checks, the completed QA scientific run and remaining OAuth setup. Production remains approval-gated. The dedicated EC2 worker rollout below remains a historical fallback. Do not apply the earlier IAM setup solely to bypass the platform workflow.

Historical EC2 status on September 29, 2026: prepared locally, **not live**. AWS denied the dedicated runtime-role and ECR repository creation. These EC2-specific denials do not describe the current DIG rollout. The [administrator handoff](../deploy/aws/README.md) applies only if this older route is resumed.

## Confirmed targets

- AWS account `005901288866`, profile `broad`, region `us-east-1`.
- Existing Aurora endpoint `aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com`; original application database and `reveal_*` tables. No table rename, reset, or replay of bootstrap migrations.
- Existing private, versioned S3 bucket `cyaka-reveal-data`. Cloud writes `prod/`; explicit read compatibility with `local/` preserves existing publications and checkpoints.
- VPC `vpc-a53ba7c2`; public subnet `subnet-ab89bbf3` (`us-east-1b`). Its route table has an internet gateway and S3 gateway endpoint.
- Dedicated `m7i.xlarge` host: two workers, API, dispatcher, Redis, and Caddy. Use encrypted gp3 root storage for OS/images/certificate state; all job work uses bounded tmpfs and durable artifacts use S3.
- Amazon Linux 2023 x86-64 AMI inspected: `ami-06135a74df036ebc9` (`al2023-ami-2023.12.20260928.0-kernel-6.1-x86_64`). Revalidate availability immediately before launch.
- Personal Vercel workspace `chase-yakaboskis-projects`; project `reveal-mechanisms`, ID `prj_mEKMUihmi5lcRSlGNaA1B6Lnqbxs`, workspace ID `team_9lGG9RszAuyJ892vy3DXmInE`.
- Production frontend domain: `reveal-mechanisms.vercel.app`, deployment pending. The public QA Preview is live at `reveal-mechanisms-qa.vercel.app`; see the current rollout status above. Framework Next.js, project Node setting 22, functions in `iad1`.

The backend can use its Elastic IP with a trusted IP certificate, so buying a domain is not required. [Let's Encrypt supports six-day IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability). The pinned [Caddy configuration](../deploy/Caddyfile) explicitly uses the public ACME issuer and `shortlived` profile, with HTTP-01 renewal on port 80. Its configuration has been validated locally; actual certificate issuance and renewal still require the live public host. See [Caddy's TLS settings](https://caddyserver.com/docs/caddyfile/directives/tls).

## Release and configuration

1. Resolve the AWS permissions in the administrator handoff, then verify the dedicated instance profile and repository exist. The runtime role's credentials must come from EC2; do not copy local AWS access keys to the host.
2. Review the selected source state. The checkout includes substantial uncommitted work. Build `services/backend/Dockerfile`, target `deployment`, for `linux/amd64`, using the verified `.deployment-assets` already prepared by local deployment. The local candidate tag is `reveal-deployment-backend:ec2-candidate`. Publish to `005901288866.dkr.ecr.us-east-1.amazonaws.com/cyaka-reveal-backend` with a unique release tag; record the registry digest. All Python services use that one digest, including the shared scientific linter bundled for agent execution.
3. Create a dedicated `cyaka-reveal-backend` security group allowing TCP 80/443 publicly. Leave 22, Redis, and API ports closed to public traffic. Verify the existing database permits private MySQL traffic from this host without changing unrelated rules. Launch with instance profile `cyaka-reveal-ec2`, `Project=reveal-mechanisms`, encrypted root storage, IMDSv2 required, and metadata hop limit 2 for containers. Attach a stable Elastic IP. Record the instance ID, group ID, allocation ID, and address before proceeding.
4. Generate cloud configuration using the actual image digest and Elastic IP:

   ```bash
   .venv/bin/python scripts/cloud_deployment.py \
     --app-url https://reveal-mechanisms.vercel.app \
     --api-url https://ACTUAL_ELASTIC_IP \
     --image 005901288866.dkr.ecr.us-east-1.amazonaws.com/cyaka-reveal-backend@sha256:ACTUAL_DIGEST
   ```

   This creates mode-0600 files in ignored `.runtime/cloud/`: `backend-secret.json`, `frontend.env`, `compose.env`, and reusable deployment keys. It preserves the existing RDS credentials and OAuth client IDs, generates separate cloud session/gateway/Redis secrets, and excludes developer AWS credentials.

5. Store `backend-secret.json` as Secrets Manager secret `cyaka/reveal/backend` in `us-east-1`, using a `file://` argument, never a command-line secret value. Use `put-secret-value` for later updates. Upload a release archive containing only `deploy/` to `s3://cyaka-reveal-data/prod/releases/<release>/`; include no `.runtime`, `.env`, or frontend OAuth secrets. Record its SHA-256 and immutable S3 version. Download with the runtime role and verify the checksum before extracting to `/opt/reveal`.
6. Through SSM, run `bash /opt/reveal/deploy/aws/bootstrap-host.sh`. It installs Docker and checksum-verified Compose 5.5.1 and installs the systemd unit, without starting the application. Install the nonsecret `.runtime/cloud/compose.env` at `/etc/reveal/compose.env`, root-owned mode 0600. Confirm `/run` is tmpfs. Startup retrieves the backend secret into `/run/reveal`, logs into ECR using the instance profile, and starts the digest-pinned stack. CloudWatch log group `/cyaka/reveal/backend` must already exist.

## Vercel and login

Use `services/frontend` as the CLI working directory. It is linked to the personal project. For each entry in `.runtime/cloud/frontend.env`, configure a **production-only** Vercel variable using secure stdin/API input; store credentials as sensitive. Do not upload backend, database, Box, Anthropic, or AWS credentials to Vercel.

Add these callback URLs to the existing OAuth applications while retaining local callbacks:

- Google: `https://reveal-mechanisms.vercel.app/api/auth/callback/google`
- ORCID: `https://reveal-mechanisms.vercel.app/api/auth/callback/orcid`

Keeping the same provider client IDs and subjects preserves the existing registered user mappings. The cloud session uses a fresh secret, so users sign in again. OAuth callback registration and live login have not been verified yet.

Deploy with `vercel --prod --scope chase-yakaboskis-projects --cwd services/frontend` after the API is healthy. [vercel.json](../services/frontend/vercel.json) sets Next.js builds and `iad1`; the API proxy permits direct S3 redirects only to the two explicitly configured artifact prefixes.

## Handoff and acceptance

The local and cloud stack share the original tables and queue namespace. Run **one application instance**. Prepare the cloud host and image before taking down localhost, then:

1. Record the original user's account/publication IDs and application counts using read-only queries. Retain the existing local configuration for rollback. Do not seed failure probes into the original `reveal_*` tables.
2. Run `.venv/bin/python scripts/local_deployment.py down`. It drains current workers before stopping containers. If draining times out, leave the old stack running and investigate; do not force-kill paid scientific work.
3. On EC2, `systemctl enable --now reveal`. Startup waits for the API and both worker containers, then resumes the shared queue. Inspect `bash /opt/reveal/deploy/aws/host-stack.sh status`, private `/readyz`, public trusted HTTPS `/healthz`, and CloudWatch logs.
4. Publish the Vercel frontend. Verify anonymous access, Google/Broad login, the original published scientific account and saved drafts, and an authorized download of a retained `local/` artifact. Confirm the cloud writes new versioned objects to `prod/`.
5. Run one bounded real scientific job through Vercel and verify SSE reconnect, evidence collection, shared lint validation, final result, and download. Confirm both worker heartbeats and no duplicate active local consumers. Host reboot/renewal verification remains a live acceptance step; never perform destructive recovery drills against these primary tables.

## Rollback and updates

For normal updates or rollback, drain with `host-stack.sh stop` before replacing containers. Pin the previous ECR digest to roll back code; do not roll back RDS records or S3 object versions. The systemd stop path waits for work to finish before stopping containers.

To return temporarily to localhost after cloud writes exist, first stop the cloud stack. Local S3 configuration must explicitly permit reading `prod/` artifacts (`REVEAL_S3_READ_PREFIXES=prod/`) while retaining `REVEAL_S3_PREFIX=local/`; otherwise cloud-created saved accounts cannot download their files locally. Configure this read prefix in the root environment used by local preparation, then run `scripts/local_deployment.py up`. Keep the same original tables and queue namespace. Before using localhost as a frontend rollback, also verify URL compatibility: saved cloud download links currently retain the Vercel origin, and the local browser's download-origin allowlist would need explicit compatibility with that origin. Rolling back the cloud image at the same public address avoids this additional URL handoff.

The single host is an initial availability limit. RDS and S3 hold durable state; Redis may restart empty and recover delivery from the existing RDS outbox. Caddy's volumes store certificate/account state only.
