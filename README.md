# REVEAL Mechanisms

REVEAL connects existing DisMech knowledge gaps to EAGGL/CFDE mechanisms and captured scientific evidence. Researchers select a question and anchors, run a bounded investigation, and inspect a validated DAPPER ScientificAccount or an explicit insufficient-evidence outcome. Accepted accounts can produce cited research statements and be published for discovery.

## Start locally

For the durable workflow pilot, follow [the workflow runtime guide](docs/durable-workflow-runtime.md).
It runs the API and frontend at `http://localhost:3100`, with managed Upstash
Redis Pub/Sub and Vector, isolated application tables, and QStash delivery.
`scripts/durable_deployment.py up --build` starts the prepared pilot.
The older colleague handoff below remains an explicit legacy development option.

**Legacy colleague handoff: follow [README.local.md](README.local.md).** Chase provides an encrypted development configuration and a separate password. From a fresh clone:

```sh
python3 scripts/local_setup.py --bundle ~/Downloads/reveal-local.env.json.gpg
# Open http://localhost:3000
```

Setup installs Python tooling, fetches pinned sources, builds images and starts Next.js, FastAPI, Redis, a dispatcher and two workers in Docker. Existing development **Aurora/RDS** and real **S3** provide durable storage. Node and AWS CLI are unnecessary on the colleague's host with the shared configuration bundle. Docker/Compose, Git, curl, Python and GnuPG are prerequisites; see the local README for exact requirements.

For an already configured checkout:

```sh
.venv/bin/python scripts/local_deployment.py up --build
.venv/bin/python scripts/local_deployment.py status
.venv/bin/python scripts/local_deployment.py down
```

Startup preserves the original `reveal_*` tables, accounts, publications and S3 references. Colleague imports assign an independent job-queue namespace while sharing the same scientific records. Secrets, source checkouts and local job data are excluded from Git. Research submissions call live paid providers; setup and read-only API browsing do not launch an investigation.

## Learn the system

- [API walkthrough](docs/api-quickstart.md): request path, authentication, examples, job lifecycle and code map.
- [OpenAPI reference](api/README.md): 43 operations, request/response fixtures, offline viewer and portable ZIP.
- [Workflow runtime](docs/durable-workflow-runtime.md): current local startup, event delivery, verification and release guidance.
- [Legacy local deployment](docs/local-deployment.md): retained worker topology, S3 artifacts and migration/rollback guidance.
- [Implementation status](docs/implementation-status.md): current behavior, validation and remaining deployment blockers.
- [Documentation index](docs/README.md): operating guides, scientific contracts, data imports and historical design records.

## Architecture

The Next.js gateway owns browser sessions and signs short-lived assertions for FastAPI. RDS owns users, drafts, jobs, scientific records, execution fences and dispatch intent. Upstash Workflow delivers bounded execution steps over signed HTTP requests. Artifacts and recovery checkpoints are versioned and checksum-verified in S3. Containers use bounded scratch space. Managed Redis Pub/Sub wakes durable event replay and frontend invalidation without Redis polling.

Workflow steps collect frozen evidence, launch Claude in Upstash Box, capture output, and apply structural/source checks followed by checkpointed independent scientific review. Scientific identifiers and provenance survive publication and deployment changes. Upstash Vector retrieves candidate mechanisms; embeddings do not establish scientific support.

## Deployment status — September 30, 2026

The local Workflow pilot runs with managed Upstash Redis Pub/Sub and Vector. The HTTP-only QA backend is deployed through Broad's [DIG service platform](docs/platform-deployment.md); public HTTPS event/replay, retrieval and signed workflow/S3 checks passed. Final task-replacement and scientific-review checks are in progress. Production requires the platform's approval, and Vercel awaits authorization to upload its prepared frontend secrets. See the [runtime verification status](docs/durable-workflow-runtime.md#verification-status) for the recorded checks.

The existing database already contains the imported scientific catalog. A colleague must not reload it to start the app. Source inventories and import commands are in [data inventory](docs/data-inventory.md), [DisMech import](docs/dismech-import.md), [EAGGL import](docs/eaggl-factor-import.md), and [CFDE GeneSets](docs/geneset-import.md). Historical measured runs remain in the [validation report](docs/validation-report.md).
