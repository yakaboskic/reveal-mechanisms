# REVEAL Mechanisms

A CFDE research system: find and select an existing DisMech knowledge gap, inspect source-linked DisMech context and choose required EAGGL mechanism anchors, expand CFDE once, and generate inspectable ScientificAccounts with additional selected Proto-OKN evidence. Claude Code runs in Upstash Box; a separate paragraph job renders an account as a paragraph citing DAPPER Claim and KnowledgeGap digests. The selected gap’s linked DisMech context automatically suggests five similar EAGGL factors total, which users can remove while retaining at least one anchor before submission.

## Start here

- [Implementation handoff](docs/implementation-handoff.md) — build order, acceptance gates and remaining integration tasks.
- [Design audit and document map](docs/design-package-audit.md) — current versus historical docs, verified imports and Aurora/Prisma comparison.

- [Frontend HTML study](design/index.html) — fuzzy gap search, fixed selected question, editable anchor context, anonymous/ORCID/Google submission, simulated agent activity, account/claim inspection and HuBMAP source provenance; [serve and review](design/README.md).

- [OpenAPI handoff](api/README.md) — 30 operations, DAPPER schemas, request/response examples, shared jobs, and validation.
- [User interaction flow](api/flow.html) — select a core step to inspect its endpoints and validated request/response examples; [Markdown/Mermaid mapping](api/flow.md).
- [Visual API reference](api/index.html) — offline Swagger UI; [JSON](api/openapi.json), [YAML](api/openapi.yaml), and [shareable ZIP](api/reveal-openapi-handoff.zip).

- [Consolidated v12.1 design plan](docs/design-plan.md) — knowledge-gap workflow, existing EAGGL embeddings and populated CFDE routing, Next.js frontend, separate API/worker, Aurora MySQL, Upstash Box and DAPPER integration.
- [Current DAPPER integration audit](docs/dapper-integration.md) — implemented fields, compatibility tests, source/agent contracts, and remaining application work.
- [ScientificAccount construction](docs/scientific-account-construction.md) — agent workflow, scoped propositions and claims, shared evidence, and a final synthesis addressing the selected gap.
- [Evidence-package design](docs/evidence-package.md) — frozen PIGEAN/EAGGL and DisMech input, real-source YAML/JSON examples, coverage/provenance rules, and a account-generation skill and pinned runtime.
- [Evidence-package collector/builder](docs/evidence-package-builder.md) — start from DisMech/factor IDs, retrieve interactive and BioIndex results, and reproduce the package from frozen sources.
- [Evidence-package schema](schema/README.md) — LinkML source, generated JSON Schema and offline validation for the agent input package.
- [Scientific-account linting](docs/scientific-account-linting.md) — pinned DAPPER release cloning at agent startup, agent lint feedback and a shared backend validator.
- [PIGEAN/EAGGL claim templates](docs/pigean-claim-model.md) — biological involvement propositions, evidence-based claim statements, and four source relationship types with captured CAD/T2D observations.
- [Citation standard](docs/citation-standard.md) — DAPPER-defined per-Claim/Question/KnowledgeGap metadata and paragraph spans, planned registry, human/AI attribution, BibTeX/CSL exports, and APA/MLA rendering.
- [Authentication and drafts](docs/authentication.md) — planned ORCID/Google login and anonymous workspaces with trusted sessions, portable application user IDs, draft autosave/query history, durable attribution, and independent API authorization.
- [Interactive API inventory](docs/interactive-api-inventory.md) — live catalog, connections, and contextual-edge probes, including empty results and membership coverage.
- [Agent/evidence integration](docs/agent-evidence-integration.md) — Claude Code configuration and verified Proto-OKN tools/selected KG schemas.
- [Embedding client module](services/backend/README.md) — supplied client packaged with the selected BioBERT default and environment configuration.
- [CFDE → DAPPER GeneSets](docs/geneset-import.md) — full model-scoped catalog, pinned DAPPER encoding, resumable MySQL loader, and source-to-digest mappings.
- [EAGGL factor bundle importer](docs/eaggl-factor-import.md) — labels, complete capped gene loadings, source hierarchy, resumable name embeddings, MySQL loading, and on-demand cosine search.
- [EAGGL → CFDE application links](docs/eaggl-cfde-links.md) — exact trait/factor-number routing and links to CFDE GeneSet records, without label or gene agreement requirements.
- [DisMech importer](docs/dismech-import.md) — validated, resumable loading of gaps, attachments, mechanisms, and supporting source exports.
- [Prisma database schema](schema/prisma/README.md) — existing Aurora tables, the EAGGL/CFDE links, and the pending DisMech tables, with mapped keys and relations.
- [Database readiness](docs/database-readiness.md) — verified RDS connectivity/grants and proposed persistence and embedding search.
- [Data inventory](docs/data-inventory.md) — mechanisms, factors, knowledge gaps, attachment resolutions, manifests, and reproduction commands.
- [BioIndex discovery](docs/api-discovery.md) — bulk ingestion routes and September 15 source observations.

**Current phase:** design and data foundation, revised September 25, 2026. The embedding client, three importers, EAGGL→CFDE lookup, deterministic evidence collector/builder, package schema, pinned DAPPER bootstrap and account skill/linter are implemented; the Next.js application and EC2 REST API/worker remain planned. The database contains **801,934 DAPPER GeneSets**, their aliases and an encoding Activity ([load report](data/cfde-genesets/2026-09-24/database-load.json)); **4,037 EAGGL factors**, **2,553,330 nonzero gene loadings**, hierarchy and **3,103 BioBERT label vectors** ([read-back](data/eaggl/2026-09-25/database-verification.json)). The later [crosswalk verification](data/eaggl-cfde-mapping/2026-09-25/database-verification.json) records **14 live tables**, **1,756 mappings** to `cfde-inc-v2` and **4,056 resolved GeneSet links**. The first application uses those existing embeddings and exact trait/factor-number mappings, ignoring label/gene differences; broader mappings and a full new embedding corpus can follow later. Nine DisMech tables remain prepared but unapplied. No Box runs have been generated.

Prepared locally: **18,419 factors**, **19,959 mechanism occurrences**, **3,367 DisMech gaps** (2,604 `KNOWLEDGE_GAP`, 763 `HUMAN_MODEL_MISMATCH`), and **801,934 DAPPER GeneSets**. GeneSet records establish catalog/import provenance; members and original scientific construction history are not yet loaded. The current DAPPER snapshot implements ScientificAccount, Question/KnowledgeGap with kind/entity context, Paragraph citation spans, and the separate citation metadata contract. The revised plan adopts those fields directly. Its audit passed 287 targeted upstream tests and current-schema/identity checks for 82 sampled imported GeneSets plus their encoding Activity. Source adapters, general scientific storage, resolvers and citation rendering remain application work; the existing import was not changed.

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
