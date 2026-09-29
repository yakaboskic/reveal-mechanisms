# Implementation status — September 30, 2026

The local deployment runs Next.js, FastAPI, Redis, a dispatcher and two workers in Docker at **http://localhost:3000**. Existing Aurora/RDS tables and real S3 provide persistence. See [colleague startup](../README.local.md) and the [API walkthrough](api-quickstart.md).

## Implemented

- DisMech gap discovery, trending indicators, source detail and EAGGL/CFDE suggestions using stored embeddings.
- Anonymous and registered sessions, Google/ORCID integrations, durable identities, owner-scoped drafts/history, publication and workspace recovery. Local Google identity recovery preserves the original broad-email account; a new cloud callback still needs provider configuration.
- RDS dispatch intent, Redis Streams, two workers, namespace-scoped concurrency/drain controls, fencing, checkpoint recovery and graceful shutdown.
- Versioned S3 artifacts, checksum verification, authorized downloads, bounded RAM scratch, and shared API/worker images.
- Frozen evidence collection, Box/Claude authoring, shared agent/backend structural and source validation, trusted reference-based assembly and independent scientific review.
- Accepted ScientificAccounts, cited statements/exports, explicit insufficient-evidence explorations, publication and review-retry workflows.
- Administrator telemetry and authorized table/job inspection; see [admin telemetry](admin-telemetry.md).
- Encrypted colleague configuration, automatic pinned-source setup and separate per-clone queues over the same RDS tables.

## Validation and limits

Historical live browser → RDS → Box/Claude → account/paragraph journeys, recovery checks and provider costs are in [validation report](validation-report.md). Local S3 and Redis failure drills used an isolated verification namespace; disruptive probes are refused on shared application tables. Handoff checks are recorded separately so packaging tests are not mistaken for paid scientific runs.

The latest repeated validation failure was caused by trusted assembly adding a mechanism named only in prose. Assembly now follows schema-declared references. Replaying the exact saved candidate passed structural/source validation with no findings; the failed job was preserved and independent review was not rerun. This does not mean the old job became accepted.

Catalog readiness still loads source records and embedding blobs from remote RDS into API memory; suggestions use NumPy similarity search. It can take minutes. This path does not use native database vector search. See [database/evidence performance](database-and-evidence-performance.md).

The original `reveal_*` users, jobs, publications and artifact references are authoritative. Colleagues share those records with independent queue namespaces and locally generated session keys. The two-job cap is per namespace; separate stacks can increase total provider spending. Network access and valid credentials remain prerequisites.

## Public deployment

Public deployment is pending access to Broad's DIG service platform. The repository could not yet be read with the active `yakaboskic` GitHub account. The personal Vercel project exists but no frontend deployment is asserted. The earlier standalone EC2 route and IAM request are on hold. See [platform deployment](platform-deployment.md); retained [EC2 preparation](cloud-deployment.md) is reference material.

## Data baseline

Imported snapshots include 801,934 DAPPER GeneSets, 4,037 EAGGL factors, 2,553,330 nonzero gene loadings and 3,367 DisMech gaps. Mapping/embedding run IDs remain pinned with saved selections. These are historical verified import counts, not a fresh recount. [Data inventory](data-inventory.md) and [database readiness](database-readiness.md) retain source reports. Routine startup never repeats imports.
