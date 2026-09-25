# Design package audit and document map

**September 25, 2026; mapping addendum v12.1.** Audit scope: all authored design versions and specialist docs, source/import reports and scripts, SQL/Prisma, OpenAPI generators/examples/viewers, evidence package/schema, agent skill/bootstrap/linter, and the approved HTML sources/fixtures. [The current plan](design-plan.md) is the single entry point. The original consolidation audit performed no data load, DDL, paid agent execution or app deployment. The later crosswalk load was a separate task; its saved verification is recorded below.

## 1. Conflicts resolved

| Finding | Consolidated decision / repair |
|---|---|
| v9 API accepted arbitrary Questions and editable DisMech context | Current `Composer` accepts exact `SelectedGap` and editable EAGGL anchors only. Linked source context is resolved by backend. Empty draft remains valid; submission needs a gap and an anchor. |
| API represented EAGGL only as a File | Current EAGGL record has DAPPER Mechanism plus catalog File and native factor identity. No new scientific class. |
| Anonymous mode existed only in product prose | Identity/attribution now has `principal_kind`; private anonymous calls require gateway assertion. Separate gateway/provision/claim contracts and schemas document the trust boundary. |
| UI dashboard had no listing/visit endpoints | Added owner-scoped `GET /v1/accounts` and `GET/POST /v1/me/explorations`. |
| UI automatically generates Paragraph; old API flow only showed manual creation | Documented transactional outbox fan-out, paragraph job IDs and account `research_statement` status; explicit job creation supports retries/alternate settings. |
| New package not represented in OpenAPI | Bundled the actual generated `0.2-draft` schema, portable captured package and owner-scoped job-package endpoint. No hand-written competing package schema. |
| Agent activity and graph preparation had only generic summaries | Added typed observable `JobEvent.detail`, preparation/start stages, source/call state, retained/retrieved counts and artifact hashes. Existing replay/cancellation rules retained. |
| Export UI exceeded API contract | Added complete Paragraph export for Markdown, LaTeX, BibTeX and rich text, with citation pins and BibTeX companion reminder. |
| API examples were the old single-result retrieval Question | Current examples reuse the HTML's exact CAD gap, 12 Claims/16 EvidenceItems, Paragraph and 13 citation records. Invented KG assertions remain labeled; schema-valid is not a production grounding pass. |
| Evidence doc title implied `0.1-draft` was current | Current schema/builder is `0.2-draft`; old illustrative packets are explicitly historical. |
| Plan said only GeneSet imports existed and embedding key was pending | Fresh read-only Aurora audit confirms GeneSet + legacy EAGGL + vectors. Current-model/DisMech production search still pending. |
| Prisma's 20 models could be mistaken for 20 applied tables | Verified 11 applied tables, nine unapplied DisMech models. No schema write performed. |
| Agent bootstrap/linter mixed with “planned integration” | Marked implemented local components separately from Box provisioning, read-only mounts, MCP ledger and final grounding gates still to build. |
| Withdrawn graph-path proposal sounded like active evidence profile | `pigean-evidence-profile.md` remains a redirect to account construction and claim templates; do not implement it. |
| Portable viewer directory contained an older extracted copy | Regeneration excludes nested historical export copies and refreshes the handoff mirror/archive from current validated artifacts. |

## 2. Imports and database

**Later same-day update:** migration 004 has now been loaded. Its [verification report](../data/eaggl-cfde-mapping/2026-09-25/database-verification.json) records **14 live tables**, 1,756 factor mappings and 8,780 ranked GeneSet references, of which **4,056 resolve to existing objects**. There are 2,281 unmatched source factors. Prisma now has **23 models: 14 applied + nine pending DisMech**. This replaces the earlier current-model re-embedding prerequisite: the first application uses the existing EAGGL vectors joined to the completed exact-trait/factor crosswalk. Label/gene agreement is not required. See [mapping guide](eaggl-cfde-links.md) and [initial retrieval contract](design-plan.md#initial-crosswalk-backed-retrieval-contract).

The following original 11-table audit and its machine-readable files remain a point-in-time record from **before** that load. They have not been rewritten to imply the original audit inspected the three new tables.

Fresh [Aurora inspection](../data/audit/2026-09-25-consolidation/database.json): certificate-verified TLS, MySQL 8.0.42, read-only consistent snapshot, exact counts and `SHOW CREATE TABLE` definitions. This count/schema audit supplements the earlier content/read-back verifications; it did not recheck every loading value or embedding byte.

| Applied table | Exact rows |
|---|---:|
| `dapper_objects` | 801,935 (801,934 GeneSets + one Activity) |
| `cfde_gene_set_aliases` | 801,934 |
| `gene_set_imports` | 1 complete |
| `eaggl_imports` | 1 complete |
| `eaggl_factors` | 4,037 |
| `eaggl_genes` | 18,477 |
| `eaggl_gene_loadings` | 2,553,330 |
| `eaggl_graph_nodes` | 5,682 |
| `eaggl_graph_edges` | 8,535 |
| `eaggl_embedding_runs` | 1 complete |
| `eaggl_name_embeddings` | 3,103 |

[Prisma comparison](../data/audit/2026-09-25-consolidation/prisma.json): all 11 applied tables mapped; no missing/extra scalar columns, nullability/native-type mismatches or mismatched declared compound keys/indexes and foreign-key targets. Prisma 7.10.0 validation passed with Node 24.19.0. Native SQL types/defaults/indexes/FKs remain inspectable in captured DDL and SQL migrations; existing local-MySQL introspection/diff verification is described in [Prisma README](../schema/prisma/README.md). This audit does not claim a successful native Prisma-to-Aurora TLS connection; that earlier CLI certificate issue remains separate from successful Python TLS access. SQL preserves collations, CHECK constraints and managed timestamps Prisma cannot fully express.

DisMech migration 003 and the importer exist and were tested with disposable MySQL. **No `dismech_*` table exists on Aurora in this audit.** The local paired exports/manifest were validated; applying/verifying the import is an implementation task. The importer preserves raw records and attachments; it does not bulk mint DAPPER gaps/mechanisms or create their embeddings.

The live embedded EAGGL import is `legacy-711-trait-capped-union`; the expansion catalog is `cfde-inc-v2`. The original audit required an explicit routing bridge. That bridge is now populated using the accepted `exact_trait_factor_number` rule. Use its native CFDE IDs for dispatch and retain the source/mapping runs. The initial implementation path is crosswalk-backed search over existing embeddings; a complete new corpus and biological correspondence review are later data improvements.

Existing GeneSet inventory is catalog/import provenance only. Preserve the 801,934 original digests and exact aliases. No gene membership, original creation history or HuBMAP equivalence is supplied by that catalog import.

### Projection compatibility discovered during consolidation

The earlier CAD UI account uses Mechanism `dapper:Mechanism.kEJMDzCkDYE94DpWaXrQg-eC4gFGQyuX`; the current collector uses `dapper:Mechanism.MyhahXB55tDUS667sJzeJZ40BeeQBWOR` for the same native CADinT2D factor. Their names/descriptions differ, so their DAPPER digests correctly differ. The current API selected-anchor/detail example reuses the collector projection and is checked for exact ID agreement. The authored account fixture retains its earlier scientific identity and illustrative evidence; it is not presented as a final accepted output of this package. Production must share the collector-compatible adapter between catalog selection, persistence and package assembly, retain mapping revisions, and reuse supplied Mechanism objects in the agent. Do not patch old IDs in place or rely on label matching to repair this mismatch.

## 3. Current documents by responsibility

| Document | Status / responsibility |
|---|---|
| [design-plan.md](design-plan.md) | Current v12.1 product/architecture/stage map with populated crosswalk |
| [implementation-handoff.md](implementation-handoff.md) | Build order and acceptance gates |
| [api/README.md](../api/README.md) | Current machine-contract handoff and regeneration |
| [design/README.md](../design/README.md) | Approved visual flow; lower iteration notes are historical |
| [design/contract-review.md](../design/contract-review.md) | Historical amendment rationale; v12 encodes wire changes, runtime work still pending |
| [api-discovery.md](api-discovery.md) | BioIndex recipes and source-specific metric/query limitations |
| [interactive-api-inventory.md](interactive-api-inventory.md) | Captured catalog/connections/contextual-edge behavior |
| [data-inventory.md](data-inventory.md) | Generated local inventories and source-scope counts; not live DB state |
| [geneset-import.md](geneset-import.md) | Completed catalog encoder/load, identities and provenance limits |
| [eaggl-factor-import.md](eaggl-factor-import.md) | Completed legacy import/embedding/search; source-version separation |
| [eaggl-cfde-links.md](eaggl-cfde-links.md) | Completed exact trait/factor routing, ranked GeneSet joins, Python/Prisma lookups |
| [dismech-import.md](dismech-import.md) | Prepared importer, SQL, offline/local integration verification; unapplied to Aurora |
| [database-readiness.md](database-readiness.md) | Current status plus future logical persistence invariants |
| [schema/prisma/README.md](../schema/prisma/README.md) | 23 mapped models, 14 applied/nine pending distinction and SQL authority |
| [authentication.md](authentication.md) | Identity, anonymous lifecycle, autosave and durable ownership |
| [gateway-contract.md](gateway-contract.md) | Browser versus service-only session/provision/claim boundaries |
| [dapper-integration.md](dapper-integration.md) | Historical v8 compatibility evidence and current source/identity/assembly invariants |
| [scientific-account-construction.md](scientific-account-construction.md) | Current scientific workflow and synthesis rules |
| [pigean-claim-model.md](pigean-claim-model.md) | Current four biological-proposition templates and Appendix B; preserved |
| [pigean-evidence-profile.md](pigean-evidence-profile.md) | Superseded graph-path draft; redirect only |
| [scientific-account-framing.md](scientific-account-framing.md) | Historical inquiry mismatch audit; current exact-gap invariant |
| [evidence-package.md](evidence-package.md) | Input semantics; separates current 0.2 capture from older 0.1 examples |
| [evidence-package-builder.md](evidence-package-builder.md) | ID-based collection, deterministic build/replay and budget behavior |
| [schema/README.md](../schema/README.md) | Canonical package LinkML and generated JSON Schema |
| [agent-evidence-integration.md](agent-evidence-integration.md) | Observed Proto-OKN tools; required scoped ledger/runtime integration |
| [scientific-account-linting.md](scientific-account-linting.md) | Implemented bootstrap and shared validator, release pin and explicit limits |
| [agent-output-contract.md](agent-output-contract.md) | Proposed worker-owned result manifest; per-account DAPPER files |
| [citation-standard.md](citation-standard.md) | Bibliographic registry/identity/span/export semantics |
| [backend README](../services/backend/README.md) | Callable component/dependency usage; no API service yet |
| [root README](../README.md) | Short navigation and current phase |

[Document inventory](../data/audit/2026-09-25-consolidation/documents.json) records authored Markdown files, titles, line counts and hashes at the original consolidation. Later v12.1 edits and the mapping guide are outside that historical hash snapshot; vendored upstream docs are dependencies, not competing product decisions.

## 4. Version history

| Historical plan | Why it is retained / superseded |
|---|---|
| [September 15](design-plan-2026-09-15.md) | Initial general question/graph/agent concept |
| [v2](design-plan-2026-09-24-v2.md) | Interactive CFDE API, gaps, accounts and persistence proposal |
| [v3](design-plan-2026-09-24-v3.md) | Required EAGGL anchors, Claude Code/Proto-OKN and embedding decisions |
| [v4](design-plan-2026-09-24-v4.md) | GeneSet identity/import extension |
| [v5](design-plan-2026-09-24-v5.md) | Durable citations and communication |
| [v6](design-plan-2026-09-24-v6.md) | Registered login integration |
| [v7](design-plan-2026-09-24-v7.md) | Portable application user IDs and saved research |
| [v8](design-plan-2026-09-24-v8.md) | Audited DAPPER schema/citation contracts |
| [v9](design-plan-2026-09-24-v9.md) | Original 25-operation OpenAPI and shared jobs; [archived contract](../api/history/v9/openapi.json) |
| [v10](design-plan-2026-09-25-v10.md) | HTML simulation and anonymous workspaces |
| [v11](design-plan-2026-09-25-v11.md) | Gap-only interaction and accumulated subsequent model/prototype notes |
| [v12 / v12.1 current](design-plan.md) | Consolidated imports, prototype, current API, evidence package, agent release/skill/linter; v12.1 adopts the loaded crosswalk for initial retrieval |

Old plans remain historical text. Do not implement their free-text entry, graph-path evidence, registered-only submission or `analysis-runs` terminology. There is one job namespace, with `kind=analysis|paragraph`.

## 5. Validation evidence and limits

- Read-only live DB table/count/DDL audit and Prisma schema validation/comparison are linked above.
- [OpenAPI validation](../api/validation.json) checks current request/response/parameter examples, exchanges, local references, DAPPER identity/profile/citations, source artifact hashes and rejected malformed inputs. It does not execute endpoints.
- [Flow validation](../api/flow-validation.json) proves every operation and exchange is reachable from the generated diagram/viewer.
- Package schema generation check and current captured-package structural/identity validation are separate from immutable collection/replay checks. Current capture is distributed under `api/examples/evidence-package`; original collection replay inputs remain a local run artifact.
- Existing import/read-back reports, evidence/schema tests and account-linter tests prove components at their stated scope. The catalog Activity retains its known missing-input provenance warning. No test makes the illustrative KG assertions scientifically real.
- No full auth/Box/MCP/citation/DB-backed application exists to validate end to end. The [handoff checklist](implementation-handoff.md) is the implementation acceptance contract, not a claim that those gates passed.

[Consolidation verification summary](../data/audit/2026-09-25-consolidation/validation.json): 30 OpenAPI operations, 41 exchanges, current package schema/45 DAPPER input objects, and 34 evidence/schema/linter/bootstrap tests passed. The proposed internal transport schemas and their documentation examples also validate.
