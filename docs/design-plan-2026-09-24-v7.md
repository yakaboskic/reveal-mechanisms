> **Historical plan.** Superseded by the [consolidated v12 plan](design-plan.md). Retained for decision history; not an implementation specification.

# REVEAL Mechanisms — revised design plan

**Revision:** September 24, 2026 · v7 · design for the implementation specification.  
**Flow:** DAPPER knowledge gap → DisMech context + required EAGGL anchors → CFDE expansion → Claude Code in Box + selected Proto-OKN evidence → ScientificAccounts / claims → cited Paragraph.  
**History:** [September 24 v6](design-plan-2026-09-24-v6.md), [September 24 v5](design-plan-2026-09-24-v5.md), [September 24 v4](design-plan-2026-09-24-v4.md), [September 24 v3](design-plan-2026-09-24-v3.md), [September 24 v2](design-plan-2026-09-24-v2.md), [September 15](design-plan-2026-09-15.md).

## 1. Decisions now settled

- **Next.js frontend; independent lightweight backend REST API and worker on AWS EC2.** The backend connects to Aurora MySQL, monitors agent work, and serves any authorized frontend.
- **NextAuth login with ORCID or Google, using JWT sessions and no database adapter.** Authentication needs no NextAuth tables. Small application-owned user/identity-mapping records resolve verified logins to immutable internal user IDs; these own the research data independently of the auth framework. Store available profile/attribution snapshots, with no credentials or sessions in the research database. EC2 verifies trusted server-issued identity assertions.
- **Autosave signed-in drafts; preserve submitted query history.** Drafts, requests, and results use the internal `user_id`. Drafts are editable/versioned; agent requests freeze the submitted revision. A replacement auth integration maps verified identities to the same user IDs without rewriting research ownership or citations.
- **Aurora MySQL is the system of record**, including textual embeddings. `cyaka_reveal_mechanisms` now exists; the GeneSet loader is the first implemented import. Other source and embedding imports remain build steps.
- **DisMech mechanisms provide context; EAGGL factors anchor CFDE.** Every analysis requires at least one selected EAGGL factor. DisMech IDs never enter CFDE anchor payloads.
- **Five EAGGL factors total** are automatically selected from semantic matches across all selected DisMech mechanisms. Deduplicate them; users may delete them. At least one must remain before submission.
- **The lab embedding service** uses `pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb` by default. No alternative embedding provider needs selection.
- **One CFDE expansion round** runs against the frozen selected factors, followed by contextual-edge enrichment among retained nodes.
- **Upstash Box runs Claude Code with an Anthropic API key.** Proto-OKN MCP is part of the account-authoring integration, initially using BiomarkerKG and ProKN as selected graphs.
- **Mined claims have a CFDE evidence basis.** The agent may add relevant support, counterevidence, or context from the selected Proto-OKN graphs. A match is not guaranteed for every claim.
- **All advertised `cfde-inc-v2` gene sets get DAPPER GeneSet objects.** CFDE graph nodes resolve through versioned database aliases to those digests, establishing the initial provenance target before membership and original construction history are available.
- **Scientific objects use DAPPER identity digests.** Use DAPPER's KnowledgeGap class and maintain source-to-digest mappings for DisMech in the database. Digest minting is independent of human review.
- **Paragraph generation is a separate agent activity** expressing a saved account and citing durable Claim and KnowledgeGap records.
- **Claims, Questions, and KnowledgeGaps are individually citable scholarly objects.** Store versioned bibliographic metadata with human/AI credit, ORCIDs when available, mint/issue dates, exact DAPPER identifiers, and resolver links. Export BibTeX/BibLaTeX and CSL-JSON; use CSL to render APA/MLA. A DAPPER digest is the citation identifier; a DOI field is reserved for a registered DOI.

## 2. Current implementation and verified evidence

Prepared locally: **18,419 `cfde-inc-v2` factors**, **19,959 DisMech mechanism occurrences**, and **3,367 gap records**: 2,604 `KNOWLEDGE_GAP` plus 763 `HUMAN_MODEL_MISMATCH`. Preserve these source kinds separately. Their 6,422 attachment references include 6,329 uniquely resolved occurrences, 79 whole sections, 4 whole documents, and 10 ambiguous targets.

These DisMech counts come from the local all-KB YAML snapshot, not the public Discussions Browser. The public browser's static dataset has 149 discussions (127 knowledge gaps and 17 human/model mismatches) and matches a checked-in June 13 asset. The September YAML in the browser's disorder/module scope has 4,033 discussions and 2,593 knowledge gaps; our inclusion of groupings adds 18 discussions and 11 knowledge gaps. Keep source scope/version visible in the application. See [the verified reconciliation](../data/dismech-gaps/2026-09-24/browser-reconciliation.json).

The interactive API probes verified eight catalog matches; the supplied two factors returned 63 gene sets and zero genes/traits/factors. Their supplied contextual request returned 15 edges. A separate CADinT2D factor returned genes, and a gene-set anchor returned membership edges under `cfde-inc-v2`. Graph fragments omit anchor nodes; merge them with the original factors before validating endpoints.

The Proto-OKN MCP server initialized successfully and exposed 23 tools. Its catalog includes `biomarkerkg` and `prokn`; both schemas were retrieved. This verifies discovery, not a completed Box-to-MCP agent run or claim-level scientific validation.

DAPPER is evolving alongside this project. The schema snapshot captured for this GeneSet import now also includes Question, KnowledgeGap, and a typed `ScientificAccount.question` reference, alongside ScientificAccount/Paragraph. This supersedes the earlier literal-gap audit. Paragraph citation spans and the DisMech-to-KnowledgeGap conversion remain integration work. Exact schema and identity files are frozen for this import, so parallel DAPPER changes cannot alter its IDs.

The supplied embedding client is now an installable backend module. No authenticated embedding request has been made: the API key is pending. The GeneSet extension has encoded and loaded **801,934 DAPPER GeneSets** and their exact CFDE aliases into `cyaka_reveal_mechanisms` through a resumable importer. Other source collections remain local preparation. No EC2 service was deployed or Box agent launched. The gene-set import manifest and database-load report record encoding and persistence separately.

Supporting artifacts:

- [Interactive CFDE inventory](interactive-api-inventory.md) and [BioIndex inventory](api-discovery.md).
- [Database readiness](database-readiness.md) and [data inventory](data-inventory.md).
- [CFDE → DAPPER GeneSet import](geneset-import.md), including the full catalog, dependency snapshot, script, and database mapping.
- [Embedding module](../services/backend/README.md).
- [Claude Code and Proto-OKN integration](agent-evidence-integration.md).
- [DAPPER citation profile](citation-standard.md) — bibliographic records, human/AI attribution, identifier/date rules, BibTeX/CSL examples, APA/MLA rendering, and the digest-cycle constraint.
- [Authentication and researcher identity](authentication.md) — Google/ORCID sign-in, database-free auth sessions, persistent research attribution, and the EC2 API trust contract. This integration remains planned.
- [Pinned DAPPER dependency snapshot](../data/cfde-genesets/2026-09-24/dapper/snapshot.json), [earlier working-tree audit](../data/dapper/schema-audit-2026-09-24-v3.json), and [DAPPER claims design](../../dapper/schema/docs/claims.md).

## 3. Core interaction

### Step 1 — pose or select a knowledge gap

The landing page asks **“What would you like to understand?”** A researcher enters a question or browses DisMech gaps by disease/module, text, kind, and status. A selected gap shows its prompt, rationale, source evidence, attachments, and proposed experiments when present. Resolved mechanism attachments become selected DisMech context; phenotype and whole-section attachments retain their actual types. Ambiguous references remain inspectable without an arbitrary target selection.

Allow drafting before login. Offer **Continue with ORCID** and **Continue with Google** through NextAuth, retaining the question/chips through the redirect. Require authentication before saving personal research or submitting an agent/paragraph job. Capture the provider's stable identity plus available name/email/ORCID fields; the backend resolves it to the durable application `user_id` and determines ownership server-side.

Autosave signed-in question/chip/context edits to a versioned draft, initially after a one-second debounce. Show Saved only after database confirmation. Anonymous or temporarily disconnected edits remain local pending successful authenticated save. Query history stores immutable submission snapshots; later draft changes do not alter past runs. Preserve selection origins/dismissals and source revisions so restored drafts retain their meaning. Concurrent-tab version conflicts require reconciliation rather than silent overwrite.

Map the source discussion revision to a **DAPPER KnowledgeGap digest** using the now-present class after the DisMech adapter is implemented. User-authored gaps use the same object type with user attribution and no fabricated DisMech ID. Editing the question creates derived content; it does not change DisMech or mark a source gap resolved.

Import all statuses: 2,526 OPEN, 3 RESOLVED, and 838 unspecified. Preserve null status. Source lifecycle status and the scientific identity of a question are separate concerns.

### Step 2 — automatically add EAGGL anchors and allow editing

**Confirmed default: five EAGGL factors total across selected DisMech mechanisms.** Add them as selected chips labeled “Added from DisMech similarity.” They are removable defaults, not merely unselected recommendations.

Proposed ranking procedure:

1. Embed each selected DisMech occurrence using a versioned template containing its name, concise description, and disease context. Search the current `cfde-inc-v2` EAGGL corpus with the same embedding model.
2. Score an EAGGL factor by its **maximum cosine similarity** to any selected DisMech mechanism. Keep the matched mechanisms and per-mechanism scores for explanation/provenance. This aggregation is an initial implementation choice to evaluate.
3. Deduplicate by complete factor identity, sort by score, break ties by stable source ID, and preselect the highest five unique factors. Preserve manually selected factors without duplicate chips.
4. Recompute automatic candidates when DisMech selections change while preserving manual selections. Keep a composer-level dismissal set: removing a factor does not immediately re-add it or silently fill the removed slot. An explicit “Reset suggestions” action clears dismissals.

Five is the initial automatic selection count, not the required count after edits. If fewer than five valid matches exist, show those available and the limitation. If no factors are available or semantic search fails, preserve context and offer manual EAGGL search.

**At least one selected, resolvable EAGGL factor is mandatory.** Enforce this in the composer, the analysis-run REST endpoint, and the worker before dispatch. Deleting the last factor is allowed during editing, but blocks submission until one is selected. There is no DisMech-only agent fallback.

Manual search supports keyword/fuzzy and full semantic search over a mechanism subquery separate from the research question. A question with no DisMech context can receive EAGGL suggestions from its own text; the user selects factors before running. Similarity means retrieval relevance, never biological support or cross-source entity equivalence.

Record automatic/manual origin, matching DisMech records, retrieval mode, similarity scores, embedding/model/template versions, input hashes, and user removals. Display full trait/model context for an EAGGL factor so a semantically related factor is not mistaken for the same disease.

### Step 3 — expand CFDE once

Freeze a nonempty seed set **S0 containing only the selected EAGGL factor items**, with valid interactive IDs and `model=cfde-inc-v2`. DisMech mechanisms and their bounded source context accompany the evidence package; no DisMech-to-gene/trait identity mapping is required to anchor CFDE. The bridge is the semantic factor selection above.

Call `/api/interactive/connections` four times against the **same S0**, targeting `gene`, `gene_set`, `trait`, and `factor`. Use the tested parameters: `reducer=mean`, `connection_scope=direct`, `exclude_node_ids=S0`, and empty `context`. Until the upstream meaning of `context` is documented, pass the question separately to the agent.

“One expansion” means one round of four typed queries. Returned candidates never become seeds in the same run. Factor → gene set → gene would be a second round and must not be added silently to compensate for empty gene results.

Merge response fragments with S0. Preserve original edge direction, families, paths, raw/normalized scores, aggregate ranks, and anchor coverage. Scores may exceed 1 and are not probabilities. The API's observed `candidate_count` is the returned count; no unbounded total/cursor was exposed, so a result at its limit may be clipped.

Bound the merged node set, then call `/api/interactive/contextual-edges` once for those IDs. Deduplicate edge occurrences without losing query provenance. Validate endpoints/path nodes and retain clipping decisions. Contextual edges are relationships, not generated explanatory text.

Resolve every retained CFDE `gene_set:` node through the selected complete GeneSet import into a DAPPER GeneSet digest. Retain both IDs, the model/import revision, and membership/construction-provenance coverage. Carry these references into the evidence package; an unknown alias remains explicit rather than receiving an invented ID.

Initial proposed budgets: 10 DisMech context mechanisms, 10 EAGGL factors (five automatic by default), 100 candidates per target, 250 retained nodes, 1,000 edges, and a 24,000-token total evidence context. Preserve selected anchors and required path endpoints. The upstream contextual request-size limit still needs verification.

### Step 4 — freeze inputs; run Claude Code with Proto-OKN tools

The initial evidence package contains the gap digest/source revision, question, selected contexts/factors, selection provenance, frozen CFDE graph, relevant DisMech assertions, source evidence, exact API artifact references, model/source versions, hashes, and coverage limits. Record empty/failed queries distinctly. Pin the schema/identity/authoring-instruction versions.

Create a normal **Upstash Box with the Claude Code harness** and `ANTHROPIC_API_KEY`; pin its Claude model and harness version per run. Configure the Box-local HTTP MCP connection to `https://apps.okn.us/okn-mcp-dev/mcp`. The application worker owns timeouts, retries, progress, cancellation, cleanup, and database writes. Keep database credentials in the EC2 backend. [Upstash configuration](https://upstash.com/docs/box/overall/quickstart).

Initial selected scientific graph allowlist: **`biomarkerkg` and `prokn`**. Read graph schemas before querying; use verified graph URIs, explicit entity cross-references, and species/context constraints. An embedding match does not establish that a CFDE gene symbol and KG entity are identical.

The agent first mines claims from the frozen CFDE evidence, then chooses relevant additional evidence from the selected graphs. It may attach support, disputes, or context. Each mined claim must trace to CFDE evidence directly or through its source claims. Proto-OKN-only assertions cannot silently replace the required CFDE basis. No usable CFDE evidence produces an explicit insufficient-evidence result rather than invented claims.

Every tool call appends to an evidence ledger: tool/arguments, query, selected graph URI/version if exposed, entity mappings, exact returned assertions and qualifiers, source/publication references, timestamp, and content hash. Preserve failures/no matches. Freeze a final manifest for validation/minting; never rewrite the initial bundle. Repeated assertions from shared sources are not independent corroboration.

Initial proposed MCP budget: 20 calls, 100 rows per scientific query, and 5,000 tokens of retained external evidence within the overall context budget. Define a run timeout/cost ceiling. Enforce selected graphs and read-only evidence operations through a tool policy/adapter, not just the prompt: the MCP server exposes a broader federation. A live Box test must verify this configuration and complete tool-result capture.

An external no-match or outage can produce a CFDE-grounded account with explicit enrichment status. It cannot be presented as successful corroboration or evidence of biological absence. See [the integration inventory](agent-evidence-integration.md).

### Step 5 — author ScientificAccounts

Each account contains framing (question, gap, or hypothesis), required context, ordered claims including at least one finding, optional explicit conclusion claims/editorial closing, and attribution/provenance. Hypothesis is a role, not a confidence category. An account is not a Claim and does not acquire an overall proposition, conjunction, causal chain, or combined confidence score from membership.

Separate source association/loading claims from biological interpretations. A high factor loading is not sufficient to establish causation. Missing evidence may leave the gap unresolved.

The current DAPPER implementation expresses an interpretation using **EvidenceItem**: source claims, target proposition, direction, explanation, context, assumptions, and attribution/activity. Require explicit interpretation whenever source findings justify another scientific claim. Preserve disagreements and uncertainty.

### Step 6 — validate, mint digests, and persist

Validate schema and cross-record references, required findings/context/provenance, evidence membership in the initial bundle or allowed tool outputs, CFDE lineage for mined claims, score meanings, source/model scope, and non-circular evidence dependencies. Reject scientific assertions appearing only in editorial closing prose.

Mint supported scientific IDs through **DAPPER-ID-1**, using the pinned DAPPER module. The agent does not invent IDs. Use the pinned KnowledgeGap registration with the DisMech mapping adapter. Save the complete object graph, account membership, evidence, and provenance transactionally; retain invalid outputs as failed attempt artifacts with bounded repair.

Schema-valid persisted outputs become immediately inspectable and digest-addressed. No additional human approval gate is required for minting or Paragraph generation. Review annotations, if introduced later, are separate from identity and evidential support. A digest establishes content identity, not scientific truth.

Register a citation metadata revision for each persisted Claim, Question, and KnowledgeGap. Preserve first durable mint/issue dates on replay, ordered human/AI credits and roles, ORCID provenance, and a resolver for the exact digest. Imported source authorship and platform publishing roles stay distinguishable. Bibliographic registration does not change access controls or imply public publication. Store post-mint citation metadata separately from hashable scientific content to avoid identifier cycles.

Use the authenticated operator's recorded name/ORCID and explicit contribution role for platform attribution, alongside the generating agent. ORCID login authenticates the ORCID account; Google login does not supply an ORCID. Contact email remains private application metadata. Profile changes do not silently alter previously minted scientific objects or citation bylines.

Distinguish original scientific production, ingestion/extraction, user selection, CFDE retrieval, MCP retrieval, agent assessment, validation, and paragraph rendering. Query/agent provenance explains why a claim was mined; it is not automatically evidence that its proposition is true. Operational job/attempt IDs remain distinct from scientific digests.

### Step 7 — inspect accounts and claims

Lead with readable account framing, context, findings, interpretations, and closing. A claim opens its proposition, scope, score meaning, CFDE path, added KG assertions, literature/source references, and provenance. Label added evidence by graph and relationship to the claim. Show missing coverage, enrichment errors, generated status, and scientific support separately.

Each Claim/Question/KnowledgeGap inspector includes **Cite**: APA, MLA, BibTeX, BibLaTeX, CSL-JSON, and permanent link. The citation identifies the exact object and exposes the human/AI byline, roles, source attribution, and underlying evidence. Formatting and object review status remain separate.

### Step 8 — generate a cited Paragraph

“Generate paragraph” submits the **saved account digest**, optional focus claim, and presentation preferences to a second Box/Claude Code activity. When started from a claim, retain its containing account; a claim shared by several accounts needs an explicit account context.

Render framing → context → findings → optional closing from the account and its saved evidence. Do not perform fresh KG research or silently strengthen/add claims. A substantive addition requires a new account/evidence revision.

Persist the Paragraph separately, with its account digest and text spans referencing durable **Claim, Question, and KnowledgeGap IDs**, plus the citation metadata revision used. Questions/gaps are framing citations; they do not serve as evidence for an answer. Citation numbers and author-date labels are presentation-local. The resolver returns the scientific object, bibliographic metadata, and underlying source/evidence lineage. Validate every target, span, account membership/context, and scope/uncertainty match. Text changes affect Paragraph identity; scientific changes affect the relevant DAPPER objects under their hashable fields. Existing references remain immutable and resolvable.

Render the paragraph's complete ordered citation set through a pinned CSL processor/style/locale so author-year disambiguation and repeated citations are consistent. BibTeX and CSL-JSON are sibling exports of structured citation metadata. Changing style is a rendering operation and does not require another agent run; retain a rendering manifest. The structured citation/Paragraph identity extension must specify whether style markers are stored in a rendered artifact or in Paragraph text. See the [citation profile](citation-standard.md) for field mappings and examples.

## 4. Backend architecture and deployment

```mermaid
flowchart LR
    UI[Next.js browser UI] --> AUTH[Next.js server / NextAuth]
    AUTH --> IDP[Google / ORCID]
    AUTH -->|Verified API assertion| API[REST API on AWS EC2]
    OTHER[Other authorized clients] --> API
    API --> DB[(Aurora MySQL)]
    API --> SEARCH[Lexical and semantic retrieval]
    SEARCH --> EMB[Lab BioBERT service]
    SEARCH --> DB
    API --> JOB[Durable queue / EC2 worker]
    JOB --> CFDE[CFDE interactive API]
    CFDE --> BUNDLE[Frozen bundle + DisMech context]
    BUNDLE --> BOX[Upstash Box / Claude Code]
    BOX --> OKN[Proto-OKN: selected graphs]
    OKN --> LEDGER[Evidence ledger]
    BOX --> VALIDATE[Validate and mint DAPPER IDs]
    LEDGER --> VALIDATE
    VALIDATE --> DB
    API --> PARAGRAPH[Separate Box paragraph job]
    PARAGRAPH --> VALIDATE
```

Use a lightweight **Python/FastAPI API and independent worker process on EC2**. The graph backend is the lab API; REVEAL still owns source/search records, jobs, agent lifecycle, validation, persistence, and citation resolution. Next.js initially uses authenticated same-origin server routes to proxy the REST/progress calls. Other clients can use the same independently deployed API with an approved authentication integration.

Initially one EC2 instance can host API and worker services. Use a MySQL-backed durable job queue with transactional claiming, leases/heartbeats, idempotency keys, and per-attempt records. Persist Box/run IDs before monitoring; after process restarts, reconcile existing executions before launching duplicate paid work. Browser disconnects do not cancel runs. Cancellation and late results use guarded state transitions so an old attempt cannot overwrite a newer result.

Keep progress events with sequence IDs; expose polling and server-sent events with reconnect support. Put large immutable evidence/tool artifacts in durable storage (proposed S3), with hashes/locations in MySQL. Configure HTTPS, API access control/CORS for authorized frontends, RDS network access, secret configuration, and stage/timeout/cost monitoring. EC2 size/image/network and operational settings remain deployment details.

Proposed layout:

```text
apps/frontend/                     Next.js composer, account/claim/paragraph views
services/backend/src/reveal_backend/ REST, retrieval, adapters, repositories
services/backend/.../worker/         CFDE/Box jobs, monitoring, validation
packages/contracts/                 Versioned API and evidence-bundle contracts
agent-skills/                       Account-authoring / paragraph instructions
schema/migrations/                  MySQL migrations
scripts/                            Ingestion and audits
data/                               Development fixtures and manifests
docs/                               Design and integration inventories
```

Only the embedding component is currently implemented under `services/backend`.

### Login, ownership, and attribution

Use NextAuth/Auth.js with `session.strategy="jwt"` and no database adapter. Google uses the built-in provider; ORCID uses a custom OpenID Connect authorization-code configuration verified against its discovery/profile contract. Register provider applications and callback URLs during implementation. The [authentication design](authentication.md) records official provider documentation and acceptance checks.

Resolve validated provider issuer/subject through `login_identities` to an immutable `app_users.user_id` UUID. Store that application ID on drafts and submitted requests, with run/result links and attribution snapshots. The mapping contains identifiers and verification provenance, not passwords, OAuth tokens, client secrets, or sessions. This replaces v6's provider-derived owner keys so ownership remains independent of the auth strategy. Email/display names are attributes, not ownership keys; ORCID email may be absent and Google does not supply an ORCID iD. Multiple verified identities can map to one user when explicitly linked; names/emails never merge histories automatically.

The Next.js server validates its session and issues a separate short-lived signed assertion for EC2 with the verified external identity, trusted gateway issuer, API audience, expiry, and necessary claims. EC2 validates it, resolves the external identity to its internal user ID, and enforces per-resource authorization for draft/history access, progress, mutation, cancellation, and paragraph generation. Do not treat the Auth.js encrypted cookie or a browser-supplied identity as this API credential. Worker jobs retain their initiating actor even after logout.

Citation credits snapshot the actual human operator/publisher identity and verified or supplied ORCID status. Preserve source authorship; keep email private and separate from public bibliography fields. Additional frontends require the equivalent trusted authentication flow rather than being implicitly trusted because they use the API.

### Drafts, query history, and database portability

Persist `research_drafts(owner_user_id, version, composer_payload, timestamps)` and immutable `research_requests(owner_user_id, source_draft_id, source_draft_version, submitted_payload, attribution_snapshot, submitted_at)`. The payload retains the question, selected gap/source revision, mechanism/factor selections and origins, dismissals, subquery, CFDE model, selected KGs, and composer settings. Autosaving alone never expands CFDE or launches an agent.

Serialize saves per draft and use expected-version updates with explicit conflicts. Keep pending edits bound to the original user through expiry/account switches. Submission verifies the saved draft revision, freezes its exact payload, and queues an idempotent run transactionally. Missing/failed save confirmation is visible; a close/reload before a successful server save cannot be presented as guaranteed recovery. See [the draft contract](authentication.md#7-draft-autosave-and-query-history).

For a database handoff, preserve internal user IDs, external identity mappings, drafts, requests, scientific objects, and provenance. A colleague can replace NextAuth while retaining those records and owner foreign keys. The new trusted integration resolves the same verified issuer/subject or uses an explicit verified migration mapping when subject namespaces change. Old sessions/signing/provider secrets are deployment state and are not required in the research database export. A change of auth framework does not itself prove two differently named external identities are the same person.

### Embedding and search contract

Service: `https://embedding-service-27386110942.us-east1.run.app`; `POST /embed`; authentication `X-API-Key`; provider `huggingface`; default model `pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb`. The module reads the pending key from `EMBEDDING_SERVICE_API_KEY`. See [module usage and supplied wire contract](../services/backend/README.md).

Store versioned input text/templates/hashes, source revisions, model/provider, dimensions, normalization, and float32 vectors in MySQL. Build a reproducible backend vector matrix/index; benchmark exact cosine initially before selecting approximate search. MySQL native vector indexing is not established by the audit. Lexical/fuzzy and semantic searches retrieve independently; semantic search is not confined to a lexical shortlist.

The [model card](https://huggingface.co/pritamdeka/BioBERT-mnli-snli-scinli-scitail-mednli-stsb) reports 768 dimensions and `max_seq_length=100`. Confirm the service's loaded configuration, model revision, pooling, and truncation with the live key. Long EAGGL labels need intentional concise templates or documented chunk aggregation. Store effective input/version so truncation or model changes cannot silently mix incompatible retrieval artifacts.

### GeneSet inventory and provenance boundary

The first import enumerates **801,934** gene-set keys from `pigean-gene-set/2` for `cfde-inc-v2`. It does not use truncated factor top lists or source summaries as the complete universe. Each object has a computed DAPPER GeneSet ID, readable name, exact term/source aliases, `member_type=gene`, a namespace where unambiguous, and the separate encoding activity. See [the importer and data contract](geneset-import.md).

The database stores immutable DAPPER objects, import manifests, and `(model, exact CFDE node ID, import revision) → DAPPER ID` mappings. Evidence packages, source result claims, and the claim inspector use these references immediately. Original creation provenance, members, assay/data type, organism, genome build, and gene counts are enriched only from verified sources; missing fields are not populated from example defaults or inferred from association scores.

Keep initial IDs and source snapshots. An enriched object receives the digest determined by its new hashable content and an explicit new alias/import revision. This allows the application to trace evidence to a stable GeneSet now and later follow its scientific construction history without rewriting historical claims.

## 5. Evolving DAPPER integration

The audited base commit is `ebfc471049240c23d6ce1d3d40cd2785b35e6443`, with local changes to schema, identity, and validation tooling. Pin both commit and working-file hashes in development; a base commit alone does not identify this schema. Deploy against a committed, validated version. This project preserves the sibling work and captures [the exact GeneSet schema/identity dependency](../data/cfde-genesets/2026-09-24/dapper/snapshot.json).

### Available in the inspected working tree

- ScientificAccount and Paragraph extend HashableNode and are registered in identity document groups.
- Question and KnowledgeGap are registered, and `ScientificAccount.question` references a Question object, including its KnowledgeGap subtype.
- ScientificAccount has question/gap/hypothesis framing, context, component/conclusion claims, and provenance; Paragraph references an account and hashes its text/associated fields.
- The scientific-content checker checks references, at least one finding outside closing conclusions, and circular evidence dependencies.
- EvidenceItem links source claims to a target proposition with evidential direction/context/assumptions.

Reuse DAPPER-ID-1 canonicalization, `compute_id` / `assign_ids` / `verify`, and test vectors. Do not substitute an application JSON hash, coerce accounts into the older CompositeClaim, or assign placeholder IDs presented as valid DAPPER digests.

### KnowledgeGap integration with the evolving schema

The GeneSet import's pinned schema now defines **Question** with required `text` and optional `scope`, and **KnowledgeGap** as its subclass with required `gap_description`. Both are registered with DAPPER identity. `ScientificAccount.question` is a typed Question reference that can target a KnowledgeGap; there is no longer a need to propose a new literal-to-gap slot here.

Implement and test the DisMech adapter against those actual fields: prompt → text; a faithful source-supported description of missing knowledge → gap_description; contextual scope and supported provenance. Preserve source kind, status, disease, attachments, and experiments in versioned source records unless the pinned DAPPER schema has suitable fields. Missing source rationale needs an explicit mapping rule, never an invented scientific conclusion. Paragraph framing citations target the computed KnowledgeGap digest.

Database mapping: `(repository, document identity, discussion_id, source snapshot/hash) → DAPPER KnowledgeGap ID`. Preserve original discussion payload, source commit/file hash, attachment strings and resolutions, import activity, and historical mappings. DisMech provides discussion identities but not the immutable content versions required here; REVEAL captures those observations. Changed semantic content creates a new digest; identical content can reuse its digest without erasing ingestion history.

Use the pinned class’s hashable fields for identity; observed source status/timestamps live on source or assessment records unless DAPPER explicitly models them otherwise. Closing a discussion should not change the identity of its question. User-authored gaps use the same class with user provenance. This is implementation of the chosen identity strategy, not a choice between digest schemes.

### Remaining citation extension

Current Paragraph fields do not include structured citation spans. Define targets, offset convention, account/question/gap relationships, citation metadata revisions, and their participation in Paragraph identity. Implement a durable HTTP resolver and structured bibliographic registry over stored DAPPER IDs. A digest by itself does not provide a URL or access policy. Adding evidence after a saved result creates the DAPPER revisions implied by its hashable fields; existing citations continue to resolve to their original content.

DAPPER already has `RecommendedCitation` with citation text/required elements, but `has_recommended_citation` is hashable. A citation containing its target's digest cannot also be hashed into that target through this back-reference. Initially keep citation records in the external registry keyed by the minted ID. Coordinate a future DAPPER-native extension with an acyclic identity design; do not mutate the imported GeneSets to add self-referential citations. See [the current citation compatibility audit](../data/dapper/citation-audit-2026-09-24.json).

## 6. Application contracts and failure behavior

Proposed REST surface:

- `GET /v1/me` — caller's resolved internal user ID and available profile/verification fields; no client-selected owner ID.
- `GET/POST /v1/drafts`, `GET/PATCH /v1/drafts/{id}` — owned draft listing/creation/recovery and version-checked autosave; `expected_version` required for updates, `409 Conflict` for stale revisions.
- `GET /v1/research-requests` — immutable submitted query history owned by the resolved user.
- `GET /v1/analysis-runs` — caller-authorized research history, filtered by persisted stable ownership.
- `GET /v1/knowledge-gaps` and `/{id}` — source kind/status/disease/search, mapping and immutable content.
- `POST /v1/mechanisms/suggest` — gap/question, selected DisMech revisions, dismissals, source/model filters, subquery/mode; returns top-five automatic EAGGL selection provenance.
- `GET /v1/mechanisms/{id}` — source context, API aliases and embedding/source versions.
- `POST /v1/analysis-runs` — owned saved draft ID/version plus permitted run budgets and idempotency key; atomically freezes its question/context/selections into a request and queues the run. Validate nonempty EAGGL anchors, source/model scope, and selection provenance from that frozen snapshot; return request/run IDs.
- `GET /v1/analysis-runs/{id}` and `/events`; `POST /v1/analysis-runs/{id}/cancel` — durable state, reconnectable progress and cancellation.
- `GET /v1/accounts/{id}`, `/v1/claims/{id}` — digest-addressed content and provenance.
- `GET /v1/citations/{id}` — native bibliographic metadata and exact object resolver; optional `format=bibtex|biblatex|csl-json|apa|mla`, metadata `revision`, and `locale` for a single-item export.
- `POST /v1/citations/render` — ordered citation occurrences, target/metadata revisions, style, and locale; returns contextual in-text labels, bibliography, and rendering manifest.
- `GET /v1/gene-sets/{dapper_id}` — immutable GeneSet payload, source aliases/import revision, and provenance coverage; CFDE adapters resolve exact aliases against a pinned complete import internally.
- `POST /v1/accounts/{id}/paragraph-runs` — saved account digest, focus claim, presentation preferences.
- `GET /v1/paragraphs/{id}` — text, account reference, citations and generation provenance.

Required explicit states:

- **Missing/expired login or unauthorized ownership:** reject protected operations; retain the draft for sign-in. A signed-in user cannot access another user's private results. Missing ORCID email is a supported profile state.
- **Autosave failure or stale draft revision:** retain pending edits, show unsaved/conflict state, and prevent a stale submission from silently replacing the visible question. Account switches never replay another user's pending saves.
- **Zero EAGGL anchors:** block API/worker dispatch, retain editable context and offer factor search.
- **No usable CFDE evidence:** insufficient-evidence outcome, no fabricated scientific claims or Proto-OKN-only fallback.
- **Partial/empty CFDE responses:** retain target-specific coverage; failures differ from valid empties and neither implies biological refutation.
- **Ambiguous DisMech attachments:** preserve the raw reference and ambiguity; never pick the first name match silently.
- **KG no-match/unavailable:** persist enrichment status and any CFDE-grounded account; never label missing corroboration as support.
- **Invalid agent/citation output:** bounded repair or failed attempt, with raw artifact retained separately from saved scientific objects.
- **Retry/cancellation/restart:** leases, idempotent stage writes, stored Box IDs, terminal-state guards and replayable event history.

## 7. Remaining specification details

1. **DAPPER coordination:** DisMech conversion into the now-present KnowledgeGap/Question classes, Paragraph citation spans, structured bibliographic attribution, acyclic citation identity, and the pinned schema/identity release. GeneSet imports already freeze the exact dependency files. The citation profile is chosen; resolver domain, pinned CSL styles/processor, and any optional readable accession remain implementation details.
2. **Embedding operation:** receive the service key; verify returned model/dimension and text truncation/pooling; finalize templates and retrieval evaluation. Provider/model choice is settled.
3. **CFDE operational checks:** production access/auth, request limits, score/reducer semantics, build versions, and empty target behavior. Endpoint selection and the one-round flow are already decided and tested.
4. **Box/MCP operation:** pin the Claude model/harness; verify configuration, selected-graph enforcement, full tool-output capture, credentials, and time/cost budgets. Harness/provider and MCP endpoint are settled.
5. **Deployment settings:** EC2 size/image/network/ingress, artifact storage, Google/ORCID client registrations and callback URLs, pinned NextAuth/custom-provider configuration, API assertion keys/trusted clients, secrets, and monitoring. NextAuth with ORCID/Google and JWT sessions is settled, as is the independent EC2 backend and Next.js frontend split.

Product defaults to evaluate: up to three accounts per gap; maximum-per-mechanism similarity ranking for the confirmed five total factors; default visibility of unspecified-status gaps. Graph embeddings and user-requested later CFDE expansion remain future options. Proto-OKN enrichment is in the current integration scope.

## 8. Implementation sequence and acceptance

**A — schema and database foundation:** the full GeneSet catalog is encoded through pinned DAPPER and loaded using versioned aliases/import provenance. Continue with application-owned users/identity mappings, versioned drafts and immutable query history, DisMech KnowledgeGap conversion, citation extensions, other source imports, and embedding/search projections. Preserve GeneSet digests as evidence targets while scientific creation provenance is added in later revisions.

**B — login, composer, and CFDE retrieval:** implement ORCID/Google NextAuth login, authenticated API forwarding and user resolution, durable owner/attribution snapshots, draft autosave/recovery/conflict handling, query history, gap browsing, automatic top-five EAGGL selections/removals, nonempty-anchor gates, one frozen expansion round, merging/bounding/contextual edges, and evidence packages.

**C — account authoring:** integrate Claude Code in Box, selected-graph MCP evidence capture, validation, DAPPER minting, transactional persistence, and account/claim inspection.

**D — paragraphs and citations:** implement the second Box activity, account-digest pinning, structured citation registry/resolver, human/AI attribution and date rules, BibTeX/CSL export, APA/MLA rendering, scope-faithfulness validation, and Paragraph identity. Bibliographic metadata registration belongs alongside object persistence in the foundation phase.

**E — deploy and evaluate:** deploy API/worker on EC2, connect Next.js, verify restart/retry/cancel behavior and independent clients; evaluate retrieval, evidence traceability, KG annotation, generated claims, and paragraph faithfulness.

Acceptance cases:

- ORCID and Google logins work with JWT sessions and no NextAuth adapter tables. Missing email/name fields are supported; repeated provider logins preserve ownership despite profile changes.
- Draft questions/chips survive login. EC2 rejects invalid API assertions and unauthorized access; client session updates cannot replace verified identity. Agent jobs and citation attribution remain traceable after logout.
- Google and ORCID identities do not silently merge; any later account linking proves both accounts and persists its mapping. Public citation exports omit contact email and retain actual ORCID verification provenance.
- Saved drafts recover across devices under the same internal user ID. Two-tab edits, lost save acknowledgements, expiry/account switches, and duplicate submissions preserve ownership and expose conflicts without losing submitted history.
- Replacing the authentication implementation preserves user IDs, draft/query ownership, scientific objects, and historical citation snapshots through verified identity mappings; the database contains no login credentials or sessions.
- Every advertised `cfde-inc-v2` GeneSet key has a schema-valid DAPPER object and exact source/model alias. Only complete imports serve evidence lookup; conflicting content under an existing ID is rejected.
- Unknown assay/species/build/member counts remain absent. Import activity is never represented as original scientific creation. Later enrichment creates new digests/mapping revisions while earlier citations remain resolvable.
- The supplied AIP gap retains its three mechanisms and one phenotype and maps to a DAPPER KnowledgeGap; changed/unchanged source imports preserve appropriate digest history.
- Multiple DisMech mechanisms add **five unique EAGGL factors total**; removal persists; zero selected factors blocks frontend/API/worker dispatch.
- Every CFDE anchor is an EAGGL factor; semantic associations never become biological identity/support assertions.
- The observed 63-node gene-set fragment merges with the two anchors; empty target responses stay explicit. No returned candidate triggers an unrequested second expansion.
- Scores above 1 and raw/normalized differences remain retrieval data, not scientific confidence.
- Each mined claim traces to CFDE evidence. Proto-OKN annotations reference selected graphs, exact assertions and query/results; no-match states do not fabricate evidence.
- One-finding accounts validate; explicit EvidenceItems describe biological interpretations; each activity has traceable provenance.
- ScientificAccount, Paragraph, Claim and KnowledgeGap digests use the pinned DAPPER identity implementation, independent of human approval status.
- Paragraph citations resolve immutable Claim/Question/KnowledgeGap content and pinned bibliographic metadata; unsupported citations or new scientific assertions fail validation.
- Every citable object can export BibTeX/CSL metadata and APA/MLA references. Human/AI roles, source attribution, available ORCIDs, first-mint dates, and exact digests remain traceable. No digest or source-paper DOI is exported as the object's registered DOI.
- Reimports preserve issue dates and metadata history; formatting does not alter scientific identity. Citations introduce no digest cycle. Document-level rendering handles repeated and same-author/year citations consistently.
- Retries do not duplicate accepted content or paid Box work unnecessarily; cancellation and EC2 restarts preserve observable state.
