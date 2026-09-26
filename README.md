# REVEAL Mechanisms

A CFDE research system: find and select an existing DisMech knowledge gap, inspect source-linked DisMech context and choose required EAGGL mechanism anchors, expand CFDE once, and generate inspectable ScientificAccounts with additional selected Proto-OKN evidence. Claude Code runs in Upstash Box; a separate paragraph job renders an account as a paragraph citing DAPPER Claim and KnowledgeGap digests. The selected gap’s linked DisMech context automatically suggests five similar EAGGL factors total, which users can remove while retaining at least one anchor before submission.

## Start here

- [Run the local application](docs/local-development.md) — one-time environment setup, Docker API/worker, host Next.js, Aurora and remote Box.
- [Implementation status](docs/implementation-status.md) — workstream boundaries, validation gates and current evidence.
- [Validation report](docs/validation-report.md) — distinguishes local checks, Aurora verification and actual Box/Claude/MCP execution.

After one-time setup:

```bash
./scripts/dev-up.sh
# Open http://localhost:3000
./scripts/dev-down.sh
```

- [Implementation handoff](docs/implementation-handoff.md) — implementation sequence and acceptance criteria.
- [Design audit and document map](docs/design-package-audit.md) — current versus historical docs, verified imports and Aurora/Prisma comparison.

- [Frontend HTML study](design/index.html) — fuzzy gap search, fixed selected question, editable anchor context, anonymous/ORCID/Google submission, simulated agent activity, account/claim inspection and HuBMAP source provenance; [serve and review](design/README.md).

- [OpenAPI handoff](api/README.md) — 31 operations, DAPPER schemas, request/response examples, shared jobs, and validation.
- [User interaction flow](api/flow.html) — select a core step to inspect its endpoints and validated request/response examples; [Markdown/Mermaid mapping](api/flow.md).
- [Visual API reference](api/index.html) — offline Swagger UI; [JSON](api/openapi.json), [YAML](api/openapi.yaml), and [shareable ZIP](api/reveal-openapi-handoff.zip).

- [Consolidated v12.1 design plan](docs/design-plan.md) — knowledge-gap workflow, existing EAGGL embeddings and populated CFDE routing, Next.js frontend, separate API/worker, Aurora MySQL, Upstash Box and DAPPER integration.
- [DAPPER integration audit](docs/dapper-integration.md) — model fields, compatibility tests, and source/agent contracts.
- [ScientificAccount construction](docs/scientific-account-construction.md) — agent workflow, scoped propositions and claims, shared evidence, and a final synthesis addressing the selected gap.
- [Evidence-package design](docs/evidence-package.md) — frozen PIGEAN/EAGGL and DisMech input, real-source YAML/JSON examples, coverage/provenance rules, and a account-generation skill and pinned runtime.
- [Evidence-package collector/builder](docs/evidence-package-builder.md) — start from DisMech/factor IDs, retrieve interactive and BioIndex results, and reproduce the package from frozen sources.
- [Evidence-package schema](schema/README.md) — LinkML source, generated JSON Schema and offline validation for the agent input package.
- [Scientific-account linting](docs/scientific-account-linting.md) — pinned DAPPER release cloning at agent startup, agent lint feedback and a shared backend validator.
- [PIGEAN/EAGGL claim templates](docs/pigean-claim-model.md) — biological involvement propositions, evidence-based claim statements, and four source relationship types with captured CAD/T2D observations.
- [Citation standard](docs/citation-standard.md) — DAPPER-defined per-Claim/Question/KnowledgeGap metadata and paragraph spans, registry semantics, human/AI attribution, BibTeX/CSL exports, and APA/MLA rendering.
- [Authentication and drafts](docs/authentication.md) — ORCID/Google and anonymous session contracts, portable application user IDs, draft autosave/query history, durable attribution, and independent API authorization.
- [Interactive API inventory](docs/interactive-api-inventory.md) — live catalog, connections, and contextual-edge probes, including empty results and membership coverage.
- [Agent/evidence integration](docs/agent-evidence-integration.md) — Claude Code configuration and verified Proto-OKN tools/selected KG schemas.
- [Embedding client module](services/backend/README.md) — supplied client packaged with the selected BioBERT default and environment configuration.
- [CFDE → DAPPER GeneSets](docs/geneset-import.md) — full model-scoped catalog, pinned DAPPER encoding, resumable MySQL loader, and source-to-digest mappings.
- [EAGGL factor bundle importer](docs/eaggl-factor-import.md) — labels, complete capped gene loadings, source hierarchy, resumable name embeddings, MySQL loading, and on-demand cosine search.
- [EAGGL → CFDE application links](docs/eaggl-cfde-links.md) — exact trait/factor-number routing and links to CFDE GeneSet records, without label or gene agreement requirements.
- [DisMech importer](docs/dismech-import.md) — validated, resumable loading of gaps, attachments, mechanisms, and supporting source exports.
- [Prisma database schema](schema/prisma/README.md) — Aurora source tables and mapped keys and relations; application storage is added by migration 005.
- [Database readiness](docs/database-readiness.md) — verified RDS connectivity/grants and proposed persistence and embedding search.
- [Data inventory](docs/data-inventory.md) — mechanisms, factors, knowledge gaps, attachment resolutions, manifests, and reproduction commands.
- [BioIndex discovery](docs/api-discovery.md) — bulk ingestion routes and September 15 source observations.

**Current phase:** local full stack implemented and verified, September 25–26, 2026. The real browser → Aurora API/worker → Box/Claude → validated ScientificAccount → cited Paragraph → export journey passed, including restart persistence. Next.js runs on the host; API and worker run in Docker Compose. See the validation report for exact coverage and limits: Google/ORCID live callbacks await credentials, native Word paste is unverified, and EC2 provisioning is deferred. The existing database contains **801,934 DAPPER GeneSets**, their aliases and an encoding Activity; **4,037 EAGGL factors**, **2,553,330 nonzero gene loadings**, hierarchy and **3,103 BioBERT label vectors**. The existing **1,756 mappings** to `cfde-inc-v2` and **4,056 resolved GeneSet links** are preserved. Nine DisMech tables have been loaded and fully read back: **148,039 source rows**, including **3,367 gaps**. Migration 005 adds application storage without changing imported tables. Real Box/Claude/MCP checks are recorded separately from development execution in the validation report.

Source inventory: **18,419 factors**, **19,959 mechanism occurrences**, **3,367 DisMech gaps** (2,604 `KNOWLEDGE_GAP`, 763 `HUMAN_MODEL_MISMATCH`), and **801,934 DAPPER GeneSets**. GeneSet records establish catalog/import provenance; members and original scientific construction history are not yet loaded. The pinned DAPPER implementation supplies ScientificAccount, Question/KnowledgeGap, Paragraph citation spans and the separate citation metadata contract. Historical design audits remain available as source documentation; current application behavior and verification are documented separately.

The gap counts describe the local YAML snapshot, including groupings. The public Discussions Browser serves an older static dataset with 149 discussions and 127 `KNOWLEDGE_GAP` records; see [the count reconciliation](docs/data-inventory.md#september-24-knowledge-gaps-and-integration-inventory).

## Data commands

```bash
python3 -m pip install -r scripts/requirements-genesets.txt
python3 scripts/extract_dismech.py --source ~/src/research/dismech
python3 scripts/extract_dismech_gaps.py --source ~/src/research/dismech
python3 scripts/pull_cfde.py --scope all-factors
python3 scripts/pull_cfde.py --scope t2d
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/validate_snapshots.py
python3 scripts/validate_revision_inventory.py
python3 scripts/write_inventory.py
```

Downloads are resumable. Use a new `--output` directory for a fresh source capture. Raw query caches are ignored by Git; normalized compressed exports, T2D fixtures, provenance, and manifests are available under `data/`.

The interactive API probe and read-only MySQL audit have separate replay instructions in their inventories above. They require network access; the offline validation commands do not.

The [GeneSet importer](scripts/import_cfde_genesets.py) separates `encode` from `load`; loading is a dry run unless `--apply` is provided. See its [reproduction instructions](docs/geneset-import.md) for credentials, verified TLS, bulk loading, and resumable imports.

The planned product keeps source associations, curated assertions, and generated hypotheses distinct. Embeddings retrieve candidate evidence; actual source assertions determine grounding.
