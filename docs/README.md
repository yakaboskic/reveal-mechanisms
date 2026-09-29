# Documentation index

Updated September 30, 2026. Start with [colleague setup](../README.local.md) and the [API walkthrough](api-quickstart.md). Operating guides describe the current code; dated inventories and validation reports describe observations at the stated time. Historical plans are retained for rationale, not as startup instructions.

## Run and develop

- [Run REVEAL on your laptop](../README.local.md)
- [Learn the REVEAL API locally](api-quickstart.md)
- [Local Docker deployment](local-deployment.md)
- [Implementation status — September 30, 2026](implementation-status.md)
- [Colleague implementation handoff](implementation-handoff.md)
- [Legacy host-based development](local-development.md)
- [REVEAL API implementation contract](../api/README.md)
- [REVEAL backend components](../services/backend/README.md)
- [REVEAL frontend](../services/frontend/README.md)

## Product and scientific contracts

- [Login and researcher identity](authentication.md)
- [Authentication gateway contract](gateway-contract.md)
- [Account publication and gap discovery](account-publication.md)
- [Saved explorations and literature search](exploration-outcomes.md)
- [Admin telemetry](admin-telemetry.md)
- [Research-agent output and worker acceptance boundary](agent-output-contract.md)
- [Evidence package for ScientificAccount generation](evidence-package.md)
- [Collect and build an evidence package](evidence-package-builder.md)
- [How the agent constructs a ScientificAccount](scientific-account-construction.md)
- [Agent account linter and pinned DAPPER startup](scientific-account-linting.md)
- [Independent scientific review — 28 September 2026](scientific-review.md)
- [DAPPER object citations — REVEAL profile v1](citation-standard.md)
- [PIGEAN/EAGGL claim templates — biological propositions and result evidence](pigean-claim-model.md)
- [Evidence-package schema](../schema/README.md)

## Deployment and proposed follow-up work

- [DIG service platform deployment](platform-deployment.md)
- [REVEAL deployment plan](deployment-plan.md)
- [Cloud rollout](cloud-deployment.md)
- [REVEAL deployment plan](staging-deployment-plan.md)
- [Refactor REVEAL execution and retrieval to Upstash](durable-workflow-refactor-plan.md)
- [DisMech demo prioritization with Jev](jev-demo-prioritization.md)

## Data, retrieval and imports

- [Extracted data inventory](data-inventory.md)
- [Aurora MySQL readiness and persistence](database-readiness.md)
- [Database and evidence preparation performance](database-and-evidence-performance.md)
- [DisMech database import](dismech-import.md)
- [Persistent DisMech context embeddings](dismech-embeddings.md)
- [EAGGL bundle → factor database and name embeddings](eaggl-factor-import.md)
- [EAGGL → CFDE application links](eaggl-cfde-links.md)
- [CFDE → DAPPER GeneSet inventory](geneset-import.md)
- [Prisma schema for REVEAL](../schema/prisma/README.md)

## Dated validation, discovery and design history

- [Full-stack validation report](validation-report.md)
- [Local integration validation](integration-validation.md)
- [Box / Claude / selected-graph validation](box-validation.md)
- [Anchor retrieval validation](anchor-retrieval-validation.md)
- [Frontend design fidelity audit — 26–28 September 2026](frontend-design-audit.md)
- [Design package audit and document map](design-package-audit.md)
- [REVEAL Mechanisms — consolidated design plan](design-plan.md)
- [REVEAL integration with current DAPPER](dapper-integration.md)
- [Claude Code, Upstash Box, and Proto-OKN evidence](agent-evidence-integration.md)
- [Interactive CFDE API inventory](interactive-api-inventory.md)
- [CFDE BioIndex — verified query recipes](api-discovery.md)
- [ScientificAccount framing and example-data audit](scientific-account-framing.md)
- [Superseded evidence-profile draft](pigean-evidence-profile.md)

## Versioned design plans

- [design-plan-2026-09-15](design-plan-2026-09-15.md) — historical.
- [design-plan-2026-09-24-v2](design-plan-2026-09-24-v2.md) — historical.
- [design-plan-2026-09-24-v3](design-plan-2026-09-24-v3.md) — historical.
- [design-plan-2026-09-24-v4](design-plan-2026-09-24-v4.md) — historical.
- [design-plan-2026-09-24-v5](design-plan-2026-09-24-v5.md) — historical.
- [design-plan-2026-09-24-v6](design-plan-2026-09-24-v6.md) — historical.
- [design-plan-2026-09-24-v7](design-plan-2026-09-24-v7.md) — historical.
- [design-plan-2026-09-24-v8](design-plan-2026-09-24-v8.md) — historical.
- [design-plan-2026-09-24-v9](design-plan-2026-09-24-v9.md) — historical.
- [design-plan-2026-09-25-v10](design-plan-2026-09-25-v10.md) — historical.
- [design-plan-2026-09-25-v11](design-plan-2026-09-25-v11.md) — historical.
