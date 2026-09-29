# Legacy host-based development

**New colleague setup:** use [README.local.md](../README.local.md), which imports encrypted configuration and starts the full Docker stack. This page preserves the older host-Next.js workflow for deliberate source development; its disk-backed artifact configuration is not the current deployment default.

The primary local application is now the [deployment stack](local-deployment.md), with Redis workers and real S3 storage at **http://localhost:3000**. Start it with `.venv/bin/python scripts/local_deployment.py up --build`; startup shuts down the legacy dev stack automatically. The workflow below documents the legacy host-based development setup.

The local stack runs Next.js on the host, the FastAPI API and worker in Docker Compose, and agent execution in Upstash Box. Aurora is the existing database; this stack does not create a local substitute. EC2 deployment is outside this round's scope.

## One-time setup

Install Docker Desktop with Compose, Node.js 22 or newer, npm, Python 3.10 or newer, and Git. Start Docker Desktop. Copy `.env.example` to `.env` and configure the database, embedding, gateway/session, and Box/Anthropic values. `.env` is ignored; restrict it with `chmod 600 .env`. Secrets must never use a `NEXT_PUBLIC_` prefix.

Install dependencies from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './services/backend[evidence,eaggl,dismech]'
npm ci --prefix services/frontend
```

Prepare the trusted DAPPER checkout once. The existing release verifier checks repository, annotated tag, exact commit, file inventory and checksums. New remote agent attempts still make their own fresh verified clone.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=services/backend/src .venv/bin/python - <<'PY'
from pathlib import Path
from reveal_backend.dapper_release import clone_release, verify_release
root = Path('.runtime/dapper')
lock = Path('services/backend/agent-runtime/dapper-release.json')
print(verify_release(root, lock) if root.exists() else clone_release(root, lock))
PY
```

Database schema setup and source imports are explicit operations. Before applying additive migrations, save a schema backup and confirm the import manifests. Use the resumable importer only if this exact DisMech import is absent:

```bash
.venv/bin/python scripts/import_dismech.py load --apply --report .runtime/dismech-load.json
.venv/bin/python scripts/import_dismech.py verify --report .runtime/dismech-verification.json
```

Apply the additive application tables explicitly:

```bash
PYTHONPATH=services/backend/src .venv/bin/python -m reveal_backend.repository --apply
```

The collector also needs the exact DisMech YAML source checkout corresponding to the imported manifests, currently commit `df3884a8bba34370ecc0b3a49d956e7f0523bb28`. Set `REVEAL_DISMECH_SOURCE` to that checkout (default sibling `../dismech`). Collection verifies source file hashes; a different checkout cannot silently replace saved source content.

Do not run `prisma db push`, reset the database, reimport the GeneSet catalog, or regenerate the existing factor embeddings. Routine startup performs none of these operations.

Persistent DisMech context vectors have a separate [explicit preparation and backfill pipeline](dismech-embeddings.md). Migration 006 adds their run, vector, and source-binding tables; loading them preserves the existing EAGGL embeddings. Complete and verify that pipeline before selecting its run for automatic suggestions. Neither startup nor a browser request should start a bulk backfill.

## Start and stop

```bash
./scripts/dev-up.sh --build  # first run; stop first when rebuilding code/dependencies
./scripts/dev-up.sh          # reuse existing services
./scripts/dev-down.sh
```

Open [the application](http://localhost:3000). API health is available at [the readiness endpoint](http://127.0.0.1:8000/health/ready). Startup checks required configuration, the Docker daemon, frontend dependencies, available ports and the trusted runtime. It waits for API database readiness and the frontend health route, then prints URLs and log locations. Configuration errors identify variable names without printing values.

Each startup phase prints immediately and reports elapsed time every five seconds
while waiting. During Compose startup the status includes API/worker lifecycle
and health states; readiness waits report HTTP status or connection delays.
Compose can wait for API health before starting the worker, so this dependency is
shown explicitly. These messages do not change startup, reuse or restart behavior.

Captured Compose output is saved to `.runtime/logs/compose-startup.log`. Failures
and timeouts retain redacted output in `.runtime/logs/startup-error.log`, including
partial output on timeout; startup cleanup also saves container diagnostics.
Log files are restricted to the local user. Successful and failed phases show
their duration, and telemetry stops when the operation finishes.

For an explicitly simulated development run:

```bash
REVEAL_EXECUTION_MODE=deterministic ./scripts/dev-up.sh
```

This exercises the actual application database and queue with the deterministic adapter. Its output is labeled as development execution and must not be reported as a live scientific result. Stop the stack before switching execution modes. Box mode requires both Box and Anthropic credentials.

Authoring defaults to 100 turns (`REVEAL_AGENT_MAX_TURNS`), with the existing independent $3 cost cap and 900-second deadline. Configure a lower turn cap explicitly if needed. Changing environment limits requires recreating the worker container, not just restarting it. A turn-limit exit stays an operational failure with retained diagnostics; it never becomes an insufficient-evidence finding. Retrying starts a new attempt, and does not resume the completed failed provider session.

The root `.env` controls the separate spending limits. For demos, for example:

```dotenv
REVEAL_AGENT_MAX_BUDGET_USD=25
REVEAL_GROUNDING_MAX_BUDGET_USD=10
REVEAL_GROUNDING_MAX_TURNS=32
REVEAL_GROUNDING_MAX_REQUEST_BYTES=524288
REVEAL_GROUNDING_MAX_INPUT_TOKENS=128000
```

Both dollar limits accept positive finite USD amounts. Defaults when unset are $3 per authoring job and $0.30 per account/paragraph review. These are individual caps, not a combined request cap: an analysis can produce several accounts and their paragraph jobs. Every explicit review retry receives the configured review allowance. Increasing these values does not bypass scientific validation or the separate time, turn, and context limits. Budget failures show which phase stopped, its cap, and recorded spend when available. Review can stop below the cap when reserving its next call would exceed the remaining amount.

The three review context/turn settings accept positive integers. Defaults remain 8 model calls, 98,304 serialized request bytes and 64,000 actual input tokens. A limit failure identifies which limit stopped review and its configured value. The dollar cap applies throughout; raising the context or turn allowance never bypasses the spending guard or permits acceptance without a verdict.

For `REVIEW_UNAVAILABLE` or `REVIEW_BUDGET_EXCEEDED`, **Retry scientific review** requeues the same job using its verified saved authoring output and frozen evidence. No authoring agent or evidence collection runs again. The retry keeps the original model/runtime provenance, checks captured files and sources again, and writes separate attempt diagnostics. A complete passing verdict is still required for account acceptance; scientific rejections cannot use this shortcut.

Set `REVEAL_CLAUDE_MODEL` in the root `.env` to choose the model for research and paragraph authoring:

```dotenv
REVEAL_CLAUDE_MODEL=claude-sonnet-4-6
# Alternatives (uncomment one and replace the setting above):
# REVEAL_CLAUDE_MODEL=claude-opus-5-5
# REVEAL_CLAUDE_MODEL=claude-fable-5-1
```

Use an exact [Claude API model ID](https://platform.claude.com/docs/en/models/overview) available to your Anthropic API key. Exact IDs let the worker verify the captured runtime against the frozen choice; avoid aliases such as `opus`. The authoring model is frozen when a job is first dispatched and retained during recovery. A later paragraph job selects its own model when dispatched. This setting does not change the independent review model or increase the authoring cost/time limits.

After editing `.env`, let active jobs finish, then reload configuration and the current source:

```bash
./scripts/dev-down.sh
./scripts/dev-up.sh --build
```

These scripts recreate this checkout's containers while preserving database records and artifacts. Running `dev-up.sh` alone reuses existing containers and does not apply changed environment values.

Live account acceptance runs the pinned DAPPER linter, exact source-observation checks, and a separate model review of every Claim and the closing synthesis against captured evidence. The [bounded scientific reviewer](scientific-review.md) uses pinned `claude-sonnet-4-6` and a local evidence-pointer reader, without the authoring conversation or external tools. It enforces request/read/turn limits, actual response usage and `REVEAL_GROUNDING_MAX_BUDGET_USD` (default $0.30 cumulative per account), in addition to the authoring budget. Rejected scientific content is preserved as a diagnostic, never promoted to an accepted account. Operationally unavailable or incomplete reviews produce `REVIEW_UNAVAILABLE` without a scientific verdict. This fallible review does not establish experimental truth. There is no automatic paid repair loop or separate token-count request.

Evidence collection treats the requested candidate count as a maximum. The complete collected package and original source bytes are frozen for recovery and uploaded as files. The agent starts from a bounded index and the project-local parsing skill, then reads relevant exact records progressively. The worker no longer token-counts or prunes the entire stored package to fit an inline prompt. Configured collection, upload, runtime and cost limits remain; see [file-backed agent reading](evidence-package-builder.md#file-backed-agent-reading).

Paragraph authoring is a separate Box job using only the accepted account and exact citation revisions. It has its own configured authoring cap and independent faithfulness review; its failure leaves the accepted account available. The default runtime cap applies per Box job, and each accepted account can enqueue a paragraph job, so it is not an aggregate per-submission spending limit. Cancelled provider sessions may not emit a final cost; consult provider billing for actual totals.

Public activity can lag remote execution on a high-latency Aurora connection; the validation machine measured about 1.13 seconds just for TLS connection setup. The worker batches already available events into atomic writes capped at 20 events and 256 KiB, retaining each event and replay identity. It checkpoints the remote cursor only after persistence; recovery deduplicates committed events if a checkpoint was interrupted. Browser replay and job polling use consistent read-only snapshots and indexed event ranges, so they do not acquire the worker's write mutex. Heartbeats run independently of storage callbacks, and completed remote event backlogs drain before outputs are collected. Connections are still opened per transaction; an active progress display does not necessarily mean Claude is still generating.

The supervisor keeps restricted process state and frontend logs under `.runtime/`. It uses a Compose project name derived from this checkout's absolute path, so another checkout or unrelated containers are outside its scope. It verifies the frontend process group and start time before signaling it. If startup fails, it stops only resources started by that invocation. Rerunning shutdown is safe, and shutdown never deletes volumes, artifacts, imported data or Aurora records.

## Networking and authentication

The browser connects to Next.js; the Next.js server proxies research requests to `REVEAL_API_URL` using short-lived signed assertions. The API verifies the issuer, audience, expiry and server-provisioned principal. A separate gateway service credential is used for principal provisioning. Provider tokens stay at the gateway. Anonymous continuation does not require OAuth configuration.

Next.js runs on the host and reaches the published API port at `127.0.0.1:8000`. Containers connect directly to the Aurora hostname with certificate and hostname verification. They do not connect to a host MySQL port. If a future development service must call the host, Docker Desktop provides `host.docker.internal`; `localhost` inside a container means that container itself.

Aurora must permit this machine's Docker egress path on TCP 3306 through its VPC/security-group/network rules. VPN connectivity and DNS must also work from containers. Supply a trusted RDS CA bundle via `REVEAL_MYSQL_CA_FILE`; Compose mounts the bundle read-only and uses its container path. Do not disable TLS to work around a network or certificate failure. A successful host-side database test does not establish container connectivity; startup readiness checks the API container itself.

Default ports are 3000 and 8000. To change them, set `REVEAL_FRONTEND_PORT`, `REVEAL_API_PORT`, `REVEAL_API_URL`, and `NEXTAUTH_URL` consistently; update OAuth callback registrations too. Existing unmanaged listeners are left alone.

Google and ORCID credentials enable registered sign-in. Register callbacks at `/api/auth/callback/google` and `/api/auth/callback/orcid` on the configured NextAuth origin. Use production HTTPS and appropriately secured cookies outside local development. Authenticated ownership is an application principal, independent of provider and scientific attribution.

## Jobs during shutdown

Shutdown stops the frontend and asks the API/worker containers to exit gracefully with a 120-second grace period. Worker shutdown must cancel its active remote execution and fence its attempt; queued jobs remain persisted. Browser disconnect alone never cancels a job. After an abrupt failure, recovery uses the stored attempt/remote handle and lease state; it must not duplicate paid execution or allow a stale attempt to overwrite cancellation or accepted results. Artifacts remain under `.runtime/artifacts` for inspection and restart.

The final validation report records which shutdown, recovery and live execution behaviors were actually exercised. It is the source for verified coverage; the intended lifecycle above is not itself evidence of a passing test.

Admin telemetry is available at `/admin`; see [admin console configuration and metric definitions](admin-telemetry.md). Set `DISABLE_ADMIN_LOGIN=true` for local `next dev` access, or configure `ADMIN_EMAILS` for verified OAuth sign-in. Restart the frontend after changing these environment variables.
