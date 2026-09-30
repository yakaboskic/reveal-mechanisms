# Durable execution and workspace delivery

The default deployment is an HTTP API plus the Next.js frontend. Upstash Workflow
delivers bounded, signed execution requests through QStash. Aurora owns job state,
step fences, dispatch intent and event replay; versioned S3 objects hold immutable
inputs, captured outputs and review checkpoints. Upstash Vector provides semantic
candidate retrieval. Upstash Redis provides Pub/Sub wakeups only.

## Integration contract

- Python dependencies: `upstash-workflow==2.0.0`, `qstash==3.4.0`,
  `upstash-vector==0.8.0`; existing Box SDK remains `upstash-box==0.3.0`.
- Workflow endpoint: `/internal/workflows/research-v1`. With the DIG prefix this
  is `/api/reveal/internal/workflows/research-v1`. `REVEAL_WORKFLOW_URL` is the
  complete externally reachable URL. Both signing keys are mandatory when
  workflow routing is enabled. Application gateway credentials do not authorize
  workflow callbacks.
- `QSTASH_URL=https://qstash-us-east-1.upstash.io` selects the verified US project.
  The SDK default is the EU project; credentials are region-specific.
- Workflow transport retries reuse a stable run/dispatch identity. RDS fences
  prevent duplicate or stale commits. A lost Box creation response is explicitly
  retained for operator recovery rather than creating a second paid agent.
- Box observation performs one bounded status request followed by a durable
  Workflow sleep. The selected Python SDK does not expose `wait_for_event`.
  These are Box requests, not Redis polling. No handler remains alive while
  waiting for the agent's next observation.
- Managed reconciliation pushes to `/internal/workflows/reconcile-v1` once per
  minute. It repairs RDS dispatch/notification outboxes and stale execution.
  It does not scan, read, or ping Redis. Its stable schedule ID includes the
  application table prefix and job namespace.
- Redis consumers use the supplied HTTPS REST streaming subscription. A backend
  shares subscriptions across interested browser clients. Idle SSE heartbeats
  write HTTP comments only. There are no recurring Redis commands. Pub/Sub has
  no replay; RDS provides authorized replay after reconnection.
- `/v1/me/workspace/events` emits committed invalidations and signed reconnect
  cursors. `workspace_change`, `ready`, `resync_required`, `connection_degraded`,
  and `access_revoked` are the stream event names. Job SSE keeps its existing
  event contract but wakes from notifications. A stream renews before gateway
  authorization expires.
- Vector snapshots require compatible embedding spaces, complete source-binding
  inventories, verified numeric readback and an ANN quality gate. Activation
  is a compare-and-swap registry update. Semantic outages return an explicit
  availability error. There is no automatic local-matrix fallback.

## Local pilot

Supply the root `.env` credentials from `.env.example`; never expose provider
credentials to the frontend. Existing verified scientific assets and AWS storage
configuration are reused from `.deployment-assets` and `.runtime/deployment`.

For local scheduling, run the pinned development server in its own terminal:

```sh
mkdir -p .runtime/workflow
npx --yes @upstash/qstash-cli@2.37.18 dev -port 18080 > .runtime/workflow/qstash.log 2>&1
```

Then prepare and run the pilot:

```sh
.venv/bin/python scripts/durable_deployment.py prepare
# Import/verify/activate the local Vector snapshot before the API is ready.
# Import steps need UPSTASH_VECTOR_WRITE_TOKEN; serving searches use the read token.
.venv/bin/python scripts/durable_deployment.py up --build
```

The pilot uses `http://localhost:3100`, API port `18001`, application tables
`reveal_workflow_local_*`, job namespace `reveal-workflow-local`, and Vector
environment `local`. It does not overwrite existing users, accounts, jobs or
publications in `reveal_*`. Only API and frontend containers run. The local
QStash server schedules requests separately; the actual managed Redis and Vector
services are used.

For managed QStash against a local HTTPS tunnel, configure the public
`REVEAL_WORKFLOW_URL` and use `--scheduler managed`. The tunnel is a development
callback path; cloud deployment uses the stable DIG HTTPS endpoint.

After startup, reconciliation can be inspected/reapplied idempotently:

```sh
.venv/bin/python scripts/configure_workflow_schedule.py \
  --env-file .runtime/workflow/backend.env --host --apply
```

## Release and rollback

`deploy/compose.yaml` is the worker-free deployment. The former pool is retained
in `deploy/compose.legacy.yaml`, used only by `scripts/local_deployment.py` during
migration. Do not run it with managed notification Redis as its queue.

The DIG manifest uses the standard HTTP service and versioned callback route.
QA has separate `reveal_workflow_qa_*` tables, job/event namespace, `qa/` S3 write
prefix and Vector environment. Production uses the existing application records.
The release renderer emits only the HTTP service; it no longer emits a background
worker stack. Production remains subject to the platform's environment gate.

Prepare the private environment files without changing remote configuration:

```sh
.venv/bin/python scripts/workflow_cloud_config.py qa
.venv/bin/python scripts/workflow_cloud_config.py prod
```

The helper follows `deploy/dig/service.yaml`, reuses the saved environment keys,
and refuses an accidental credential rotation. Only the frontend environment
file belongs in Vercel. Backend secrets are bound through AWS Secrets Manager.

Before production cutover, drain legacy authoring and cleanup, activate the
verified production Vector registry, enable workflow routing for new jobs, then
remove legacy services. Existing jobs retain their transport and generation;
legacy claimers exclude workflow-owned records. Keep endpoint v1 available until
its runs finish. Roll back future routing independently from existing workflow
recovery and Vector snapshot activation; never let a legacy worker claim an
in-progress workflow job.

## Verification status

Tests and live probes are recorded in the ignored `.runtime/workflow` directory.
Unit tests do not prove a cloud release or a paid scientific run. Record actual
deployment URLs, image/source revisions, live callback and browser results at
release time; do not infer those outcomes from a successful template render.

September 30 local checks completed: five non-scientific local-QStash probes,
including two jobs resumed after forced API replacement; one managed-QStash
probe through an HTTPS tunnel; and real Redis fanout with no idle read commands.
The active local Vector snapshot passed all 24 frozen reference queries with
minimum recall@10 and top-result agreement of 1.0, and maximum cosine error
`3.4934e-7`. Its verified inventory contains 1,756 mapped factor aliases and
20,588 context bindings; 2,281 unmapped factor bindings are archived separately
in immutable S3. Browser, scientific-run, and cloud acceptance are still pending.

Provider references: [FastAPI integration](https://upstash.com/docs/workflow/quickstarts/fastapi),
[QStash regions](https://upstash.com/docs/qstash/howto/multi-region),
[Redis streaming Pub/Sub](https://upstash.com/docs/redis/features/restapi),
[Vector Python SDK](https://upstash.com/docs/vector/sdks/py/gettingstarted).
