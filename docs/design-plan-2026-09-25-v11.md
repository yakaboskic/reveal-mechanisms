> **Historical plan.** Superseded by the [consolidated v12 plan](design-plan.md). Retained for decision history; not an implementation specification.

# REVEAL Mechanisms — revised design plan

**Personal workspace addition (September 25):** a small avatar in the upper-right corner is available everywhere, including before login. Its two destinations are **Your knowledge gaps** and **Your scientific accounts**, the tabs of a personal dashboard. Gap rows show the exact question, recent exploration and related account counts; account rows show closing remarks and open the full account. Job history remains underlying application data rather than a third primary destination. Anonymous visitors have temporary local exploration history; verified sign-in retains access to their owned research across sessions and devices. The HTML prototype simulates this with a temporary per-tab ID and browser storage. See [workspace continuity](authentication.md#10-avatar-and-personal-dashboard).

**Revision:** September 25, 2026 · v11 · DisMech-gap-only discovery and anchor-focused interaction; v9 OpenAPI and DAPPER v8 dependency pin retained as explicit baselines.  
**Flow:** selected DisMech DAPPER KnowledgeGap → curated mechanism context + required EAGGL anchors → CFDE expansion → Claude Code in Box + selected Proto-OKN evidence → ScientificAccounts / claims → cited Paragraph.  
**History:** [September 25 v10](design-plan-2026-09-25-v10.md), [September 24 v9](design-plan-2026-09-24-v9.md), [September 24 v8](design-plan-2026-09-24-v8.md), [September 24 v7](design-plan-2026-09-24-v7.md), [September 24 v6](design-plan-2026-09-24-v6.md), [September 24 v5](design-plan-2026-09-24-v5.md), [September 24 v4](design-plan-2026-09-24-v4.md), [September 24 v3](design-plan-2026-09-24-v3.md), [September 24 v2](design-plan-2026-09-24-v2.md), [September 15](design-plan-2026-09-15.md).

**What changed in v11:** This product centers on using genetic and other source evidence to address **existing DisMech knowledge gaps**. Search text retrieves gaps by fuzzy match; it is never submitted as a new free-text Question. Selecting a gap fixes its exact question on the same screen. Users adjust possible genetic EAGGL anchors; linked DisMech mechanisms remain read-only in a collapsed disclosure alongside mechanism search and additional KGs. Idle search examples type and backspace real gap questions; they stop on interaction and never become actual input. Trending entries retain full source questions and explicitly sample account counts. Anonymous/ORCID/Google submission remains as settled in v10. The current HTML, [authentication plan](authentication.md), and [contract amendments](../design/contract-review.md) reflect this behavior. The v9 OpenAPI still permits more general inquiry input and must be narrowed before implementation; no backend or schema migration is performed in this design iteration.

## 1. Decisions now settled

- **Existing DisMech gaps only.** User text searches the gap catalog; selecting a result supplies the immutable DAPPER KnowledgeGap and exact source revision. The product does not author or submit arbitrary Questions or edit the gap text. Users choose possible genetic graph anchors to investigate how to address the selected gap; the curated DisMech mechanism links stay fixed.

- **Next.js frontend; independent lightweight backend REST API and worker on AWS EC2.** The backend connects to Aurora MySQL, monitors agent work, and serves any authorized frontend.
- **Submit-time ORCID, Google or anonymous continuation.** Registered login uses NextAuth JWT sessions with no database adapter; anonymous mode uses a trusted first-party session with the same durable application ownership. Authentication needs no NextAuth adapter tables. Small application-owned user/identity-mapping records resolve verified logins to immutable internal user IDs; these own the research data independently of the auth framework. Store available profile/attribution snapshots, with no credentials or sessions in the research database. EC2 verifies trusted server-issued identity assertions.
- **Autosave registered and anonymous workspace drafts; preserve submitted query history.** Drafts, requests, and results use the internal `user_id`. Drafts are editable/versioned; agent requests freeze the submitted revision. A replacement auth integration maps verified identities to the same user IDs without rewriting research ownership or citations.
- **Aurora MySQL is the system of record**, including textual embeddings. `cyaka_reveal_mechanisms` now exists; the GeneSet loader is the first implemented import. Other source and embedding imports remain build steps.
- **DisMech mechanisms provide context; EAGGL factors anchor CFDE.** Every analysis requires at least one selected EAGGL factor. DisMech IDs never enter CFDE anchor payloads.
- **An EAGGL factor is a mechanism.** Factor is CFDE's source/API term for the same domain object. Use its full source identity and best available mechanism label; `Factor1`/`Factor12` are interim names. No separate factor-to-mechanism inference or equivalence Claim is required. DisMech and EAGGL mechanisms retain their own source identities; semantic suggestions do not merge them.
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

The current DAPPER working tree implements Question/KnowledgeGap, `gap_kind`, `about_entities`, ScientificAccount, Paragraph/CitationOccurrence, and the separate `reveal-citation-v1` metadata contract. **287 targeted upstream tests passed** against a captured dependency snapshot. An offline scan covered all 801,934 imported GeneSets; **82 sampled objects and the encoding Activity retain their IDs and pass the current closed schema**. This is a compatibility audit, not a new full revalidation or database migration. The original import dependency and records remain frozen. See [audit evidence and limits](dapper-integration.md#1-audited-dependency-and-results).

The supplied embedding client is now an installable backend module. No authenticated embedding request has been made: the API key is pending. The GeneSet extension has encoded and loaded **801,934 DAPPER GeneSets** and their exact CFDE aliases into `cyaka_reveal_mechanisms` through a resumable importer. Other source collections remain local preparation. No EC2 service was deployed or Box agent launched. The gene-set import manifest and database-load report record encoding and persistence separately.

Supporting artifacts:

- [Interactive CFDE inventory](interactive-api-inventory.md) and [BioIndex inventory](api-discovery.md).
- [Database readiness](database-readiness.md) and [data inventory](data-inventory.md).
- [CFDE → DAPPER GeneSet import](geneset-import.md), including the full catalog, dependency snapshot, script, and database mapping.
- [Embedding module](../services/backend/README.md).
- [Claude Code and Proto-OKN integration](agent-evidence-integration.md).
- [DAPPER integration contract](dapper-integration.md) — implemented schema, source mapping, validation, storage boundaries, and remaining work.
- [DAPPER citation profile](citation-standard.md) — adopted metadata/occurrence contract, human/AI attribution, identifier/date rules, and planned BibTeX/CSL/APA/MLA rendering.
- [Authentication and researcher identity](authentication.md) — Google/ORCID and anonymous continuation, portable ownership, later sign-in, persistent research attribution, and the EC2 API trust contract. This integration remains planned.
- [Pinned DAPPER dependency snapshot](../data/cfde-genesets/2026-09-24/dapper/snapshot.json), [earlier working-tree audit](../data/dapper/schema-audit-2026-09-24-v3.json), and [DAPPER claims design](../../dapper/schema/docs/claims.md).

## 3. Core interaction

### Step 1 — find and select a DisMech knowledge gap

The landing page uses a white background, modern sans-serif type and a compact rounded **gap search** box. While idle, it shows “What would you like to understand?”, backspaces that phrase, then types and erases randomly chosen real DisMech gap questions. These are decorative examples, not input values or selections. Stop the animation on focus/input, pause when the page is hidden, and show a static search hint for reduced-motion users. There is no new-question submit action in the search box. Its circular up arrow shows/focuses gap matches. The selected gap uses the same circular up arrow to start the evidence workflow, replacing the text Go button. A brief card transition connects selection and return to search, with reduced-motion support.

Typing returns related DisMech knowledge gaps by **fuzzy match**. The HTML implements local token/prefix/edit-distance matching over its 13-record sample; the production route is `GET /v1/knowledge-gaps/search?source=dismech&mode=fuzzy&q=...`. No matches prompts a different search; it cannot create a new inquiry or start an agent. Keyboard users can navigate suggestions and explicitly select a gap. Search text never replaces the selected source question.

Below the box, **Trending knowledge gaps** displays each actual natural-language question and associated user-generated ScientificAccount count. The prototype explicitly labels its mock counts. Ranking/counts are application metadata, not hashable DAPPER fields; production visibility and source-revision scope are defined in the amendment inventory. Keep the page free of logos, persistent sign-in controls, design banners and footer links.

Selecting a result keeps the researcher on the same page. Render the exact gap question as non-editable text, followed by subtle guidance: **“Anchor on possible genetic mechanisms to explore evidence for answering this gap.”** Show the chips under the user-facing heading **Mechanism anchors** as the primary editable context. Add-anchor controls and empty-selection guidance use that wording; underlying EAGGL/CFDE identities remain unchanged. Put **Linked DisMech mechanisms** in a collapsed, read-only section alongside **Find more mechanisms** and **Additional knowledge graphs**. The collapsed label shows the linked count. These links come from the curated gap and cannot be unpinned or edited by the user. Resolved mechanism attachments initialize context and the five EAGGL defaults. About this gap opens its rationale, source evidence, typed attachments and experiments when available. Ambiguous references are inspectable without an arbitrary target selection. Changing the gap means choosing another source result, not editing its question.

Allow gap selection and anchor/context editing before any session exists. At the first Go/Submit, offer **Continue with ORCID**, **Continue with Google**, and **Continue anonymously**, preserving the complete composer through the choice or provider redirect. A valid registered or anonymous session can save and submit analysis/paragraph work. Anonymous users receive a durable private application `user_id` and a trusted browser session; no name, email or ORCID is required or inferred. The server derives ownership. Closing the choice submits nothing, and a current session skips this choice on later submissions.

Before a session, edits stay local. Once a registered or anonymous session exists, autosave selected-gap/chip/context changes to a versioned draft after a one-second debounce. Show Saved only after database confirmation. Finish the save before freezing the submitted revision. Anonymous recovery depends on possession of the valid browser session; offer later sign-in to retain cross-device access. Cookie loss does not imply deletion or publication of scientific records, but cannot be recovered by merely naming an old user ID. Query history stores immutable submission snapshots; later draft changes do not alter past runs. Preserve selection origins/dismissals and source revisions. Concurrent-tab version conflicts require reconciliation.

Map the source discussion revision to a **DAPPER KnowledgeGap digest**: prompt → `text`, rationale → `gap_description`, source kind → `gap_kind`, and verified disease/context identifiers → `about_entities`. Preserve detailed attachments, status, experiments, and raw evidence in source records. Four of 3,367 gaps lack a rationale; use the explicitly labeled prompt-preserving fallback in [the adapter contract](dapper-integration.md#2-question-and-dismech-gap-adapter). The application selects imported **KnowledgeGap** records only; arbitrary Question authoring and editing a source question are outside this product. Retain DAPPER Question support for existing records and interoperability, not as a composer input path. The backend resolves the selected gap/source revision and rejects a missing gap or a client-modified question. Analysis does not mark the source gap resolved.

Import all statuses: 2,526 OPEN, 3 RESOLVED, and 838 unspecified. Preserve null status. Source lifecycle status and the scientific identity of a question are separate concerns.

### Step 2 — automatically add EAGGL anchors and allow editing

**Confirmed default: five EAGGL factors total across the selected gap’s linked DisMech mechanisms.** Add them as selected chips labeled “Added from DisMech similarity.” They are removable defaults, not merely unselected recommendations.

Proposed ranking procedure:

1. Embed each source-linked DisMech occurrence using a versioned template containing its name, concise description, and disease context. Search the current `cfde-inc-v2` EAGGL corpus with the same embedding model.
2. Score an EAGGL factor by its **maximum cosine similarity** to any linked DisMech mechanism. Keep the matched mechanisms and per-mechanism scores for explanation/provenance. This aggregation is an initial implementation choice to evaluate.
3. Deduplicate by complete factor identity, sort by score, break ties by stable source ID, and preselect the highest five unique factors. Preserve manually selected factors without duplicate chips.
4. Initialize automatic candidates from the chosen gap/source revision. The linked DisMech set is read-only; only EAGGL additions/removals are user choices. Choosing a different gap starts a new anchor selection. Keep a composer-level dismissal set: removing a factor does not immediately re-add it or silently fill the removed slot. An explicit “Reset suggestions” action clears dismissals.

Five is the initial automatic selection count, not the required count after edits. If fewer than five valid matches exist, show those available and the limitation. If no factors are available or semantic search fails, preserve context and offer manual EAGGL search.

**At least one selected, resolvable EAGGL factor is mandatory.** Enforce this in the composer, the analysis job REST endpoint, and the worker before dispatch. Deleting the last factor is allowed during editing, but blocks submission until one is selected. There is no DisMech-only agent fallback.

Manual search supports keyword/fuzzy and full semantic search over a mechanism subquery separate from the research question. A selected gap with no resolved DisMech mechanisms can receive EAGGL suggestions from its existing text; users still select at least one factor before running. Such a gap remains source-selected, never a free-text fallback. Similarity means retrieval relevance, never biological support or cross-source entity equivalence.

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

The initial evidence package contains the gap digest/source revision, question, selected contexts/factors, selection provenance, frozen CFDE graph, relevant DisMech assertions, source evidence, exact API artifact references, model/source versions, hashes, and coverage limits. Declare document-level `prefixes` for CURIE resolution and explicit handling of opaque source aliases. Record empty/failed queries distinctly. Pin the schema/identity/authoring-instruction versions.

The [evidence-package design](evidence-package.md) specifies a draft internal envelope grouped as `pigean.mechanisms`, `pigean.traits`, and `dismech`, with source-backed YAML/JSON examples and an implemented [LinkML/JSON Schema](../schema/README.md). Mechanism loadings remain separate from trait association statistics and semantic selection scores. It includes source locators, DAPPER input objects, GeneSet provenance coverage and an append-only enrichment boundary. The [account-generation skill](../services/backend/agent-skills/construct-scientific-account/SKILL.md) applies the account/claim templates to that input. Box/worker wiring remains implementation work. The separately illustrated legacy EAGGL subgraph bundle has no established identity crosswalk to `cfde-inc-v2`.

The [collector and deterministic builder](evidence-package-builder.md) now implement package assembly from a DisMech gap ID and supplied EAGGL factor IDs. Collection resolves source context and DAPPER aliases, performs the bounded interactive/BioIndex calls, and saves exact responses. Offline replay validates and rebuilds identical frozen inputs under a pinned builder. This component is separate from the planned API/worker deployment and Box execution.

**Required agent startup:** call the [release bootstrap and account linter](scientific-account-linting.md) before each new research-agent start. Clone the locked DAPPER release (`0.2.0-a1`, immutable commit and source hashes), install the skill and lint script, and record the runtime manifest. A failed clone or verification prevents launch. The agent lints each hydrated account in draft mode; trusted backend assembly mints authored nodes and reruns the same linter in final mode. The Box worker must provide read-only mounts for the release, tools and input evidence. Local preparation/launch helpers are implemented; Upstash provisioning and deployment remain separate.

Create a normal **Upstash Box with the Claude Code harness** and `ANTHROPIC_API_KEY`; pin its Claude model and harness version per run. Configure the Box-local HTTP MCP connection to `https://apps.okn.us/okn-mcp-dev/mcp`. The application worker owns timeouts, retries, progress, cancellation, cleanup, and database writes. Keep database credentials in the EC2 backend. [Upstash configuration](https://upstash.com/docs/box/overall/quickstart).

Initial selected scientific graph allowlist: **`biomarkerkg` and `prokn`**. Read graph schemas before querying; use verified graph URIs, explicit entity cross-references, and species/context constraints. An embedding match does not establish that a CFDE gene symbol and KG entity are identical.

The agent first mines claims from the frozen CFDE evidence, then chooses relevant additional evidence from the selected graphs. It may attach support, disputes, or context. Every newly mined account claim must trace to CFDE evidence directly or through its source claims. Auxiliary source Claims recording Proto-OKN assertions are allowed as evidence without their own CFDE lineage; they must not be promoted into CFDE-grounded findings merely by being included in the bundle. No usable CFDE evidence produces an explicit insufficient-evidence result rather than invented claims.

Every tool call appends to an evidence ledger: tool/arguments, query, selected graph URI/version if exposed, entity mappings, exact returned assertions and qualifiers, source/publication references, timestamp, and content hash. Preserve failures/no matches. Freeze a final manifest for validation/minting; never rewrite the initial bundle. Repeated assertions from shared sources are not independent corroboration.

Initial proposed MCP budget: 20 calls, 100 rows per scientific query, and 5,000 tokens of retained external evidence within the overall context budget. Define a run timeout/cost ceiling. Enforce selected graphs and read-only evidence operations through a tool policy/adapter, not just the prompt: the MCP server exposes a broader federation. A live Box test must verify this configuration and complete tool-result capture.

An external no-match or outage can produce a CFDE-grounded account with explicit enrichment status. It cannot be presented as successful corroboration or evidence of biological absence. See [the integration inventory](agent-evidence-integration.md).

### Step 5 — author ScientificAccounts

Each account uses `question` to reference the **exact selected DisMech KnowledgeGap ID frozen in the research request**, required `context`, ordered distinct `component_claims`, optional `hypothesis`, `conclusion_claims` and `closing_remarks`, and required `was_generated_by`/`was_attributed_to`. Conclusions are a subset of component claims, with at least one component outside the conclusion list. Although generic DAPPER accepts broader inquiry/hypothesis framing, every REVEAL account must satisfy `account.question == submitted_source_gap.dapper_id`. All accounts from the same request share that framing gap. The backend supplies and verifies this reference; the agent cannot invent a replacement question or rephrase/mint a duplicate inquiry. Hypothesis references a Proposition; it is a role, not a confidence category. An account is not a Claim and has no combined confidence score.

Distinguish observed association/loading values from the biological assessment that uses them. Separate source-result Claims are optional; simple examples retain the observations directly in EvidenceItems/source Files. A high factor loading is not sufficient to establish causation. Missing evidence may leave the gap unresolved.

Use two companion documents: [how the agent constructs a ScientificAccount](scientific-account-construction.md) and [the four PIGEAN/EAGGL claim templates](pigean-claim-model.md). The agent identifies the specific unknown, inspects the PIGEAN/EAGGL results, interrogates related knowledge gaps and selected external graphs, constructs one or more scoped Propositions with one Proposition per Claim, and writes a final account synthesis. The templates use gene–factor, gene-set–factor, gene–trait and gene-set–trait results as evidence for biological involvement Propositions. Their Claim statements summarize the interpreted evidence; observed scores and model/fit details remain in the evidence layer. Biological targets use `BIOLOGICAL_INTERPRETATION`. Small examples use one biological Claim with artifact-based EvidenceItems; separately assessed source observations may use `RESULT` Propositions and source Claims when the agent needs that additional structure. Methods can assess a shared Proposition when biological meaning, entities and scope match. No graph-path extension is part of this design.

For the first two templates, gene–factor and gene-set–factor mean gene–mechanism and gene-set–mechanism. The factor is the mechanism being assessed, including before a descriptive label is available. Evidence explains the observed relationship's bearing on the Proposition; it does not need to establish that the factor qualifies as a mechanism.

Require a final synthesis in `ScientificAccount.closing_remarks` explaining how the claims' assessments and scopes work together to address, partially answer or propose an explanation for the selected gap. An account need not have a single overarching Proposition or hypothesis. A new substantive scientific conclusion introduced by the synthesis must also be represented as a component Claim and may be selected through `conclusion_claims`; editorial connections and qualifications can remain in closing prose. For a one-Claim account, omit `conclusion_claims` to preserve the current profile's non-conclusion constraint. This synthesis requirement is a REVEAL output rule; DAPPER's field remains optional. Keep agent instructions as versioned artifacts in Activity/AgenticWorkspace provenance.

One factor result may inform multiple gene-involvement and gene-set/program/readout Propositions. The factor-centered account synthesis can explain which genes inhibit or amplify the mechanism through particular processes and which gene sets describe or read out those processes. Each directional, process-route or readout assertion must be represented by an assessed Claim; a loading's sign or a signature label alone does not supply those meanings. Biological inhibition/amplification belongs in Proposition content, while SUPPORTS/DISPUTES remains the direction of the evidence assessment.

Use **EvidenceItem** for interpretation: `target_proposition`, `direction`, `explanation`, `context`, optional `assumptions`, and attribution/activity. For direct source evidence, use `was_derived_from` to reference a File, an exact artifact/row locator in `context`, and a verbatim `snippet`. Alternatively, use `source_claims` for independently recorded assessments. Both patterns retain the target, direction, explanation and context; the target must match the owning Claim's proposition. Preserve disagreements and uncertainty. Optional MechanisticModel/CausalStep structure can elaborate a Proposition; selected mechanism chips are not automatically causal assertions.

The same source evidence can inform multiple Propositions. In current DAPPER, record a separate EvidenceItem for each target-specific interpretation while reusing the same source Claims/Files. Reuse a single EvidenceItem across Claims only when the target Proposition and interpretation match; shared evidence is not independent corroboration.

The agent returns proposed content and temporary local references in a versioned REVEAL output envelope. The backend supplies trusted source objects, attribution, activities, and citation revisions. Split a multi-account result into one closed provenance document per account for DAPPER's `scientific-account` profile; hydrate existing dependencies before validation. Shared stored objects are reused, not reminted from agent-supplied substitutes.

### Step 6 — validate, mint digests, and persist

Run DAPPER closed-schema, reference/provenance, identity, scientific-content, and citation checks, then REVEAL checks for the frozen question, authorized sources, evidence locators, CFDE lineage, score meanings, and source/model scope. DAPPER checks structure and cycles; it does not establish biological support or prose faithfulness. Inspect each generated assertion against its cited source through a separate recorded grounding check; unresolved outputs remain failed/flagged attempts. Reject new scientific conclusions appearing only in closing prose. See [validation responsibilities](dapper-integration.md#4-agent-output-and-validation-contract).

Mint supported scientific IDs through **DAPPER-ID-1**, using the pinned DAPPER module. The agent does not invent IDs. Use the pinned KnowledgeGap registration with the DisMech mapping adapter. Save the complete object graph, account membership, evidence, and provenance transactionally; retain invalid outputs as failed attempt artifacts with bounded repair.

Accepted, validated outputs become immediately inspectable and digest-addressed. No additional human approval gate is required for minting or Paragraph generation. Review annotations, if introduced later, are separate from identity and evidential support. A digest establishes content identity, not scientific truth.

Register a citation metadata revision for each persisted Claim, Question, and KnowledgeGap using DAPPER's `schema/citations/citation-record.schema.json` and `check_citation_metadata`. Preserve first durable mint/issue dates on replay, ordered human/AI credits and roles, ORCID provenance, and a resolver for the exact digest. Imported source authorship and platform publishing roles stay distinguishable. This registry remains outside the scientific node by design. Bibliographic registration does not change access controls or imply public publication.

Store immutable payload observations separately from scientific identity where DAPPER permits unhashable fields to vary. For example, changing File.location or adding a GeneSet collection backlink leaves its digest unchanged. The existing GeneSet table retains its original payload; a general object store needs append-only payload snapshots, schema/identity pins, and provenance-document references rather than applying the importer's whole-JSON collision rule to all future objects. See [persistence rules](dapper-integration.md#5-persistence-and-identity-boundaries).

Use the operator's recorded attribution snapshot and explicit contribution role alongside the generating agent. Registered users may have a credited name/ORCID; anonymous users retain an explicit anonymous actor snapshot with no invented name or ORCID. Later sign-in changes ownership/access independently of historical scientific/citation attribution. ORCID login authenticates the ORCID account; Google login does not supply an ORCID. Contact email remains private application metadata. Profile changes do not silently alter previously minted scientific objects or citation bylines.

Distinguish original scientific production, ingestion/extraction, user selection, CFDE retrieval, MCP retrieval, agent assessment, validation, and paragraph rendering. Query/agent provenance explains why a claim was mined; it is not automatically evidence that its proposition is true. Operational job/attempt IDs remain distinct from scientific digests.

### Step 7 — inspect accounts and claims

Lead with readable account framing, context, findings, interpretations, and closing. A claim opens its proposition, scope, score meaning, CFDE path, added KG assertions, literature/source references, and provenance. Label added evidence by graph and relationship to the claim. Show missing coverage, enrichment errors, generated status, and scientific support separately.

Each Claim/Question/KnowledgeGap inspector includes **Cite**: APA, MLA, BibTeX, BibLaTeX, CSL-JSON, and permanent link. The citation identifies the exact object and exposes the human/AI byline, roles, source attribution, and underlying evidence. Formatting and object review status remain separate.

### Step 8 — generate a cited Paragraph

“Generate paragraph” submits the **saved account digest**, optional focus claim, and presentation preferences to a second Box/Claude Code activity. When started from a claim, retain its containing account; a claim shared by several accounts needs an explicit account context.

Render framing → context → findings → optional closing from the account and its saved evidence. Do not perform fresh KG research or silently strengthen/add claims. A substantive addition requires a new account/evidence revision.

Have the agent return authored text segments with explicit allowed target/revision pairs. Use DAPPER's `assemble_cited_text` to calculate `Paragraph.text` and `citations`. Each CitationOccurrence has `target_id`, `citation_metadata_revision`, inclusive `start`, exclusive `end`, and optional `exact_text`; offsets count Unicode code points, not JavaScript UTF-16 units. Freeze text after anchoring. The backend supplies the separate paragraph activity and saved `scientific_account` digest.

Validate local target classes/spans and call `check_citation_registry_links` against exact stored metadata revisions. Allowed targets include the framing Question/KnowledgeGap, account Claims, and explicitly selected prior Claims from the saved evidence context; a prior citation does not change account membership. Questions/gaps provide framing, not support for an answer. The resolver returns the exact object and its evidence lineage. Citation changes, including metadata revision pins, change Paragraph identity; metadata edits alone leave existing paragraphs pinned to their earlier revisions.

Render the complete ordered citation set through a pinned CSL processor/style/locale. BibTeX and CSL-JSON are sibling exports of structured metadata. Store numbering/author-date markers and bibliographies in separate rendered artifacts, outside the saved Paragraph text. Changing style needs neither a new agent run nor a new Paragraph digest. See the [citation profile](citation-standard.md) for mappings and examples.

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

Use a lightweight **Python/FastAPI API and independent worker process on EC2**. The graph backend is the lab API; REVEAL still owns source/search records, jobs, agent lifecycle, validation, persistence, and citation resolution. Next.js initially uses trusted-session same-origin server routes to proxy the REST/progress calls. Other clients can use the same independently deployed API with an approved authentication integration.

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

For registered users, resolve validated provider issuer/subject through `login_identities` to an immutable `app_users.user_id` UUID. Store that application ID on drafts and submitted requests, with run/result links and attribution snapshots. The mapping contains identifiers and verification provenance, not passwords, OAuth tokens, client secrets, or sessions. This replaces v6's provider-derived owner keys so ownership remains independent of the auth strategy. Email/display names are attributes, not ownership keys; ORCID email may be absent and Google does not supply an ORCID iD. Multiple verified identities can map to one user when explicitly linked; names/emails never merge histories automatically.

The Next.js server validates its session and issues a separate short-lived signed assertion for EC2 with the verified external identity, trusted gateway issuer, API audience, expiry, and necessary claims. EC2 validates it, resolves the external identity to its internal user ID, and enforces per-resource authorization for draft/history access, progress, mutation, cancellation, and paragraph generation. Do not treat the Auth.js encrypted cookie or a browser-supplied identity as this API credential. Worker jobs retain their initiating actor even after logout. Anonymous sessions use a separately issued trusted assertion identifying their existing anonymous principal, with no fabricated provider subject. Bootstrap, expiry, quotas, ownership and later account claims are defined in [the anonymous lifecycle](authentication.md#8-anonymous-workspace-lifecycle). A new verified login can upgrade the same UUID; claiming into an existing account requires both session proofs and an audited transaction.

Citation credits snapshot the actual operator/publisher identity or explicit anonymous attribution, plus verified or supplied ORCID status when available. Never rewrite historical anonymous bylines automatically on sign-in. Preserve source authorship; keep email private and separate from public bibliography fields. Additional frontends require the equivalent trusted authentication flow rather than being implicitly trusted because they use the API.

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

## 5. DAPPER integration baseline

The [September 24 v8 audit](dapper-integration.md) captures the current schema, identity code, validators, examples, and targeted tests. The base commit remains `ebfc471049240c23d6ce1d3d40cd2785b35e6443` with uncommitted changes, so the commit alone is not a sufficient pin. Root-schema checksum: `219769c9874a3c0a7d6972360d5847a8b344e412a8b9f4e2648f059682440345`. Pin the full dependency manifest as well as `DAPPER-ID-1`; promote a committed/package version before deployment. The sibling repository and existing database import were not modified by this audit.

### Scientific and communication contracts now implemented in DAPPER

- **Question / KnowledgeGap:** required inquiry `text`; optional `scope` and URI/CURIE `about_entities`; KnowledgeGap adds required `gap_description` and optional `gap_kind` (`KNOWLEDGE_GAP` or `HUMAN_MODEL_MISMATCH`). Status is separate source/application metadata.
- **Proposition / Claim / ClaimScore:** distinguish scientific content from its attributed assessment and typed quantitative values. Claim requires a Proposition, generating Activity, and attribution. Loading and retrieval scores are not probabilities. Hypothesis is an account role referencing a Proposition, not a separate class.
- **EvidenceItem:** source observations or optional source Claims bearing on a target Proposition, direction, explanation, context, optional assumptions, and provenance. Direct artifact evidence retains the source File and exact row location. Reference-only literature evidence is also supported; REVEAL requires locatable, authorized sources and explicit interpretation for generated assessments.
- **ScientificAccount:** typed question/hypothesis framing, context, ordered component/conclusion Claims, optional MechanisticModel and editorial closing, and required activity/attribution. REVEAL always carries the submitted question/gap.
- **Paragraph / CitationOccurrence:** separate textual expression with typed account link and ordered inline citation values. Exact targets, positive metadata revisions, Unicode code-point spans, and optional text checks participate in Paragraph identity.
- **Citation metadata:** DAPPER supplies an external JSON Schema plus metadata/link validators, including ordered role-bearing human/AI credit and observed date rules. These records intentionally have no independent DAPPER node digest and do not hash back into the cited target.
- **GeneSet / GeneSetCollection / File:** one GeneSet per named set; collections represent libraries, files represent bytes. `in_gmt_file` and `gmt_entry` form a paired row selector. Collection `members` is complete when supplied; the inverse `in_gene_set_collection` is unhashable and avoids cycles.

Reuse `compute_id`, `assign_ids`, `verify`, the closed-schema provenance linter, `check_scientific_content`, `assemble_cited_text`, `check_citation_metadata`, and `check_citation_registry_links`. The exact file/function references and limits are in the integration contract. There is no remaining requirement to invent Question, gap classification, a citation class, or a new digest algorithm.

### DisMech conversion and identity

Implement the field mapping in Step 1, including the four source-rationale fallbacks. The database maps `(repository, source document, discussion_id, observed snapshot/hash)` to the DAPPER object and exact adapter version. Preserve original discussions, source commit/file hashes, attachment resolution, evidence, and import observations. Lifecycle status is not an answer or a scientific assertion.

Keep mutable source observations separate from the hashable semantic projection used to mint a gap. Hashing the whole repository snapshot or a batch-specific import activity into every gap would change its identity on status-only or unrelated source edits. Use a stable, versioned projection/recording activity and preserve each full observation in the source mapping. Genuine changes to gap text, classification, scope, attribution, or entity context can change identity under DAPPER's actual rules.

Resolve external CURIEs through a pinned prefix map before minting new objects; use a consistent stored representation. Do not retroactively expand or rewrite historic GeneSet payloads. Changed literal external references can change DAPPER IDs even when they denote equivalent entities. Opaque CFDE aliases remain aliases, not ontology identifiers.

### GeneSet compatibility and future enrichment

The current schema adds unhashable GMT row/file locators absent from the existing catalog records; they can change without reminting the named GeneSet. The sampled IDs remain stable; no reimport is indicated by this audit. Membership, source GMT bytes/row keys, and original scientific construction history still require verified source data. Enrichment produces new identities when hashable content changes; all historical aliases stay pinned to their original imports.

Collection backlinks, GMT row/file locators, and file locations are unhashable observations and need the payload-snapshot handling described above. Do not create one GeneSetCollection listing the whole 801,934-set catalog: DAPPER-ID-1 limits each object to 10,000 canonical triples. Keep the catalog in its existing manifest/alias tables; use real source libraries with complete membership only when known and within that limit, or omit their full member list and retain verified inverse links. Never label an arbitrary partial list as a complete collection.

## 6. Application contracts and failure behavior

The v9 machine-readable baseline is [api/openapi.json](../api/openapi.json), with 25 operations and exact schemas, parameters, error examples, idempotency rules and transport examples. [Handoff notes](../api/README.md) explain which requirements need runtime checks beyond OpenAPI. **v11 delta:** mandatory source-selected DisMech gaps, read-only linked DisMech context, rejection of arbitrary Question submission, anonymous session/bootstrap/claim semantics and explicit `principal_kind` are not yet in that file. Follow this plan for product behavior and [the amendment inventory](../design/contract-review.md) before implementing the next contract revision; do not interpret missing bearer authentication as anonymous write permission.

The [interactive user-flow diagram](../api/flow.html) maps the core actions to those endpoints and all validated request/response exchanges. Its [Markdown/Mermaid version](../api/flow.md) also shows login/history, cancellation, failure recovery, and the backend worker stages behind each job.

- `GET /v1/me` — resolved internal user and available profile fields.
- `GET/POST /v1/drafts`, `GET/PATCH /v1/drafts/{draft_id}` — owned editor state, complete-composer replacement with `expected_version`, and idempotent autosave.
- `GET /v1/research-requests` and `/{request_id}` — immutable submitted query history and its frozen DAPPER inquiry.
- `GET /v1/knowledge-gaps/search` — required free text `q`, lexical/fuzzy/semantic/hybrid mode, source/kind/status/disease filters and cursor pagination.
- `GET /v1/knowledge-gaps` and `/{gap_id}` — catalog browsing, source mappings, attachment resolutions and DAPPER content.
- `GET /v1/mechanisms/search` and `/{source_id}` — manual source search and exact source-revision recovery.
- `POST /v1/mechanisms/suggest` — selected gap/revision and its server-resolved linked DisMech mechanisms, manual EAGGL anchors, dismissals and subquery; returns five automatic EAGGL candidates total and ranking provenance. The client saves its resulting selection to the draft.
- `POST /v1/jobs` with `kind=analysis` — owned saved draft ID/version plus permitted budgets and an idempotency key. Resolve the selected DisMech gap revision, reject missing/edited/non-DisMech inquiry input, atomically freeze a research request, validate at least one resolvable EAGGL anchor, and queue a job.
- `POST /v1/jobs` with `kind=paragraph` — saved ScientificAccount digest, optional focus claim, presentation preferences and permitted existing citation context. Queue the separate paragraph agent activity.
- `GET /v1/jobs` and `/{job_id}`; `GET /v1/jobs/{job_id}/events`; `POST /v1/jobs/{job_id}/cancel` — shared history, state, JSON polling/SSE replay and cancellation for both kinds.
- `GET /v1/accounts/{dapper_id}`, `/v1/claims/{dapper_id}`, `/v1/gene-sets/{dapper_id}`, `/v1/paragraphs/{dapper_id}` — DAPPER content/provenance, exact payload checksums and source coverage.
- `GET /v1/objects/{dapper_id}` — generic authorized resolver, including Questions, KnowledgeGaps, evidence and source Files.
- `GET /v1/citations/{dapper_id}` — native metadata or BibTeX/BibLaTeX/CSL-JSON/APA/MLA, with exact metadata revision and locale selection.
- `POST /v1/citations/render` — saved `paragraph_id`, style and locale. The server loads the complete ordered citation set with its pinned metadata revisions and returns contextual labels, bibliography and a rendering manifest.

**DAPPER boundary:** represent both DisMech mechanisms and EAGGL factors as DAPPER `Mechanism` objects in the revised adapter design. An EAGGL factor is the mechanism, with its full native CFDE identity, trait/model context and versioned source-to-digest mapping. Retain exact catalog bytes as a DAPPER `File` for provenance alongside the mechanism, and continue using the native factor ID for CFDE API calls. The v9 wire baseline still exposes EAGGL records as Files only; adding their Mechanism objects and mappings is a pending contract/adapter amendment, not an implemented import. No separate Factor class or factor-to-mechanism Claim is needed. CFDE GeneSets resolve to the already imported DAPPER GeneSet identities.

**Transport defaults:** 20 results per page (maximum 100); up to three accounts; at most ten DisMech contexts and ten selected EAGGL anchors in a bounded composer; five automatic suggestions total. Empty drafts can be saved. Protected requests require a trusted gateway bearer assertion for a registered or anonymous principal. Requests with no session may read only public records. Anonymous private jobs are governed by the same owner checks. Idempotency keys are retained for at least seven days and checked before version conflicts on retries. OpenAPI declares the request/response shape; the backend must enforce ownership, identity, lineage, source applicability and citation integrity.

The upstream account profile expects an unconsumed terminal ScientificAccount. A paragraph activity consumes that account, so paragraph packages use REVEAL's [paragraph profile](../api/paragraph-profile.yaml) in addition to validating the saved account independently. The frozen DAPPER files are unchanged.

Required explicit states:

- **Missing/expired session or unauthorized ownership:** reject protected operations and retain local pending edits. Offer registered or anonymous continuation for a new workspace; do not silently transfer work from a lost anonymous principal. Neither session kind can access another owner’s private results. Missing ORCID email is supported. Anonymous quota exhaustion is an explicit recoverable state.
- **Autosave failure or stale draft revision:** retain pending edits, show unsaved/conflict state, and prevent a stale submission from silently replacing the visible question. Account switches never replay another user's pending saves.
- **Missing/unverified DisMech gap or modified source text:** reject submission, retain the context and return the user to catalog selection. Search text cannot mint a Question or launch a job.
- **Zero EAGGL anchors:** block API/worker dispatch, retain editable context and offer factor search.
- **No usable CFDE evidence:** insufficient-evidence outcome, no fabricated scientific claims or Proto-OKN-only fallback.
- **Partial/empty CFDE responses:** retain target-specific coverage; failures differ from valid empties and neither implies biological refutation.
- **Ambiguous DisMech attachments:** preserve the raw reference and ambiguity; never pick the first name match silently.
- **KG no-match/unavailable:** persist enrichment status and any CFDE-grounded account; never label missing corroboration as support.
- **Invalid agent/citation output:** bounded repair or failed attempt, with raw artifact retained separately from saved scientific objects.
- **Retry/cancellation/restart:** leases, idempotent stage writes, stored Box IDs, terminal-state guards and replayable event history.

## 7. Remaining work and specification details

**No core DAPPER class is missing for the agreed first version.** The audit leaves the following application work, with defaults in [the integration contract](dapper-integration.md#7-remaining-work-and-optional-upstream-improvements):

1. **Source adapters and grounded claims:** implement DisMech → KnowledgeGap and CFDE/Proto-OKN → source Claims/EvidenceItems. Specify exact source locators, verified entity crosswalks, score interpretations, source-only versus mined-claim roles, and the four missing-rationale fallbacks. DAPPER cannot determine that a source actually supports the generated prose.
2. **Agent output and validation:** version the output envelope and authoring instructions; hydrate pinned existing objects, validate one account document at a time, enforce CFDE ancestry and authorized evidence, and define bounded repair plus a recorded grounding/paragraph-faithfulness check. Do not treat the upstream fictional PIGEAN example as a real metric contract.
3. **Durable object/provenance storage:** add schema pins, immutable document/edge captures, payload observations for unhashable changes, run-to-object links, and independent ownership/access grants. Freeze first-mint events and citation revisions transactionally. Preserve existing GeneSet IDs and payloads.
4. **Citation service:** persist DAPPER's now-defined registry records and implement exact-revision resolution, BibTeX/BibLaTeX/CSL export and APA/MLA rendering. Choose the resolver domain and pin processor/styles/locales. No new citation schema or readable accession is required for v1.
5. **Operational completion:** the embedding key and effective model configuration; CFDE request limits/build/score semantics; a real Box/MCP test with complete tool-output capture, selected-graph enforcement and budgets; and EC2/network/storage/auth-client/secret/monitoring configuration. Providers, model family, login strategy, backend/frontend split, and the one-round flow are settled.

Optional DAPPER improvements are a validator profile for standalone inquiry documents, a multi-account wrapper, and a first-class software-agent node if needed for richer export. The current application can validate inquiry nodes directly, package accounts individually, and record software through Activity/AgenticWorkspace plus the citation byline; these are not schema blockers.

Product defaults to evaluate: up to three accounts per gap; maximum-per-mechanism similarity ranking for the confirmed five total factors; default visibility of unspecified-status gaps. Graph embeddings and user-requested later CFDE expansion remain future options. Proto-OKN enrichment is in the current integration scope.

## 8. Implementation sequence and acceptance

**A — schema and database foundation:** the full GeneSet catalog is encoded through pinned DAPPER and loaded using versioned aliases/import provenance. Continue with application-owned users/identity mappings, versioned drafts and immutable query history, DisMech KnowledgeGap conversion, the defined citation registry, general DAPPER payload/provenance storage, other source imports, and embedding/search projections. Preserve GeneSet digests as evidence targets while scientific creation provenance is added in later revisions.

**B — login, composer, and CFDE retrieval:** implement ORCID/Google NextAuth login, anonymous bootstrap and later upgrade/claim, trusted API forwarding and user resolution, durable owner/attribution snapshots, draft autosave/recovery/conflict handling, query history, gap browsing, automatic top-five EAGGL selections/removals, nonempty-anchor gates, one frozen expansion round, merging/bounding/contextual edges, and evidence packages.

**C — account authoring:** integrate Claude Code in Box, selected-graph MCP evidence capture, validation, DAPPER minting, transactional persistence, and account/claim inspection.

**D — paragraphs and citations:** implement the second Box activity, account-digest pinning, structured citation registry/resolver, human/AI attribution and date rules, BibTeX/CSL export, APA/MLA rendering, scope-faithfulness validation, and Paragraph identity. Bibliographic metadata registration belongs alongside object persistence in the foundation phase.

**E — deploy and evaluate:** deploy API/worker on EC2, connect Next.js, verify restart/retry/cancel behavior and independent clients; evaluate retrieval, evidence traceability, KG annotation, generated claims, and paragraph faithfulness.

Acceptance cases:

- ORCID and Google logins work with JWT sessions and no NextAuth adapter tables. Anonymous continuation also saves drafts, queues jobs and retrieves private results through a server-issued principal. Later sign-in preserves access while historical anonymous attribution remains intact. Missing email/name fields are supported; repeated provider logins preserve ownership despite profile changes.
- Draft questions/chips survive login. EC2 rejects invalid API assertions and unauthorized access; client session updates cannot replace verified identity. Agent jobs and citation attribution remain traceable after logout.
- Google and ORCID identities do not silently merge; any later account linking proves both accounts and persists its mapping. Public citation exports omit contact email and retain actual ORCID verification provenance.
- Saved drafts recover across devices under the same internal user ID. Two-tab edits, lost save acknowledgements, expiry/account switches, and duplicate submissions preserve ownership and expose conflicts without losing submitted history.
- Replacing the authentication implementation preserves user IDs, draft/query ownership, scientific objects, and historical citation snapshots through verified identity mappings; the database contains no login credentials or sessions.
- Every advertised `cfde-inc-v2` GeneSet key has a schema-valid DAPPER object and exact source/model alias. Only complete imports serve evidence lookup. Hashable-content conflicts fail; permitted unhashable changes append payload observations without overwriting the original import.
- Unknown assay/species/build/member counts remain absent. Import activity is never represented as original scientific creation. Later enrichment creates new digests/mapping revisions while earlier citations remain resolvable.
- The supplied AIP gap retains its three mechanisms and one phenotype and maps to a DAPPER KnowledgeGap with kind and verified disease context. The four missing-rationale cases use the labeled fallback; product submission requires a selected DisMech gap and does not create free-form Questions. Status-only and unrelated source edits preserve scientific identity, while semantic/attribution changes follow DAPPER hashability.
- Multiple DisMech mechanisms add **five unique EAGGL factors total**; removal persists; zero selected factors blocks frontend/API/worker dispatch.
- Every CFDE anchor is an EAGGL factor; semantic associations never become biological identity/support assertions.
- The observed 63-node gene-set fragment merges with the two anchors; empty target responses stay explicit. No returned candidate triggers an unrequested second expansion.
- Scores above 1 and raw/normalized differences remain retrieval data, not scientific confidence.
- Each newly mined account claim traces to CFDE evidence; source-only Proto-OKN Claims are explicitly distinguished. Proto-OKN annotations reference selected graphs, exact assertions and query/results; no-match states do not fabricate evidence.
- One-finding accounts validate; conclusions cannot exhaust all components; every REVEAL account retains its submitted question. Multi-account results validate as separate complete provenance documents. Explicit EvidenceItems describe interpretations; each activity and referenced artifact has traceable provenance.
- ScientificAccount, Paragraph, Claim and KnowledgeGap digests use the pinned DAPPER identity implementation, independent of human approval status.
- Paragraph citations resolve immutable Claim/Question/KnowledgeGap content and exact bibliographic revisions; Unicode/emoji spans, repeated targets and coincident citations validate. A missing pinned revision never falls back to latest. Unsupported citations or new scientific assertions fail the separate grounding/faithfulness checks.
- Every citable object can export BibTeX/CSL metadata and APA/MLA references. Human/AI roles, source attribution, available ORCIDs, first-mint dates, and exact digests remain traceable. No digest or source-paper DOI is exported as the object's registered DOI.
- Reimports preserve issue dates and metadata history; formatting does not alter scientific identity. Citations introduce no digest cycle. Document-level rendering handles repeated and same-author/year citations consistently.
- Retries do not duplicate accepted content or paid Box work unnecessarily; cancellation and EC2 restarts preserve observable state.
- File relocation, GeneSet collection backlinks/GMT row locators, and activity timestamps can yield new payload observations under the same digest; historical evidence packages retain their exact snapshots. Object ownership is authorized through run/user grants, independent of global digest deduplication.
- DAPPER dependency upgrades replay identity/schema fixtures and preserve historical pins. Large source collections respect the 10,000-triple identity limit; the full catalog remains an import manifest, not a fabricated giant GeneSetCollection.


## 10. HTML interaction and provenance study

The [HTML study](../design/index.html) is the current visual iteration, with [design notes and serving instructions](../design/README.md). It contains 14 source-derived DisMech gap examples, five captured EAGGL mechanism records, the new [multi-claim CAD account packet](../design/data/cad-account/README.md) and its cited paragraph, plus the separate corrected HuBMAP provenance bundle. Search is local fuzzy matching; semantic ranking, sign-in, progress and agent execution are simulations. Direct review routes are `/#example` (account inspection), `/#hubmap` and `/#notes`; the homepage has no review-tool chrome. The twelve-Claim account uses the exact imported CAD reverse-causation gap. Numerical CFDE evidence is captured; KG and membership assertions are visibly illustrative. A completed preview for another selected gap labels the packet as a reference account for a different gap.

The core screens mirror sections 3 and 6: selected DisMech gap → selected context and required EAGGL factors → identity choice → saved/frozen request → one graph expansion round → bounded agent work → ScientificAccount → Claim / Proposition / EvidenceItem → GeneSet and source File (or an optional source Claim) → separate cited paragraph. One account does not imply that the source gap is resolved. Runtime summaries display observable activity and source-linked findings, not private chain-of-thought.

**Job interaction:** after submission and any identity choice, the same question card contracts vertically upward. Its exact source question remains visible with read-only mechanism anchor chips underneath. A single activity panel appears below and begins automatically: CFDE graph expansion → node/edge observations → bounded evidence-package preparation → agent startup → public tool/activity logs → validation and completion. Keep a clear working indicator and Stop action; no separate job hero, navigation steps or graph sidebar. Activity entries append, follow the newest entry only when the reader is already at the bottom, and respect reduced motion. Stop shows “Stopping…” until cancellation is confirmed; terminal states preserve the question, chips and received history. Successful execution, sufficient evidence and resolving a scientific gap are separate outcomes. The HTML uses scripted public summaries and explicitly labeled reference graph counts; it performs no retrieval or agent execution. See the [telemetry and cancellation amendments](../design/contract-review.md#3-structured-observable-job-telemetry).

The corrected HuBMAP bundle contains 358 GeneSets, one collection, 76 Files/C2M2Files, three Activities and 79 reified edges. Collection coverage is **964 distinct source gene symbols**, with the older unexplained 1,533 count retained in the source description. Every set has members, a collection reference, `in_gmt_file` and `gmt_entry`. The original GMT and the renamed DAPPER-ID GMT are different File identities connected by an export Activity. The library derives from a term-to-gene table; preparation uses 33 ASCT+B tables and a human gene-information file. This is activity-level source provenance, not per-GeneSet/per-source-row attribution. Locations on the original filesystem are not public download URLs.

The demo account's AMP AD / GTEx GeneSet still has catalog-only coverage. Do not substitute the HuBMAP collection for missing provenance. The corrected bundle is frozen separately with a checksum and retains its supplied identities; integrating it into the CFDE catalog requires versioned alias mapping and adoption of its schema/identity policy. No database rows or DAPPER dependency pins are changed by the design study.

The [contract review](../design/contract-review.md) identifies missing gap-source detail, structured progress/summary provenance, bounded provenance closure, source-file availability, anonymous identity/attribution, mandatory selected-gap submission and a real trending-query definition. These are amendments to review and validate, not replacement scientific classes.

### Latest HTML refinement: agent work and example account output

The preparation sequence (graph expansion, node/edge counts, evidence package) uses completion marks and collapses when the agent starts. Agent activity is presented as a running transcript of public messages, concrete tool calls with arguments, and result excerpts. One shared animated indicator marks the active stage. The completed ScientificAccount appears below this transcript; no draft account card is shown while work is running. Preserve the question and anchor chips above it; retain Stop and reduced-motion behavior.

For visual iteration, the user explicitly permits showing the existing ScientificAccount after a selected-gap walkthrough without a semantic match. Render its source question and actual claim content with an Example label, and keep links to claims, propositions, evidence and provenance. This is a UI fixture transition only: original DAPPER identities, context and attribution remain untouched, no assertion links it to the selected gap, and production evidence/relevance validation is unchanged. Returning from the account inspector restores the selected gap and completed activity.

**Completed job layout:** compress the entire activity block into one expandable “Gap analysis complete” line, then show ScientificAccounts directly beneath it. Keep the original question and anchor chips above; expand the line to inspect preparation and agent history. Running and cancelled states remain distinct.

### ScientificAccount example-data audit

The current CADinT2D question was hard-coded by `scripts/build_openapi.py` for the older graph-retrieval contract fixture. The HTML displayed that record as the account heading. It is not a DisMech gap or an agent-generated inquiry. The prototype now resolves framing by `account.question` across both `knowledge_gaps` and `questions`, rather than assuming the first Question in a document.

For the gap-centered product, the selected KnowledgeGap is the framing object. An optional hypothesis is a Proposition investigating that same gap; context describes the chosen mechanisms, analytical approach and scope; component Claims and EvidenceItems explain what the retrieved evidence bears on. An account label must not require inventing another Question. Agent search subqueries remain operational context.

The primary HTML fixture now spans the imported CAD gap, captured CAD-in-T2D evidence, twelve biological Claims, explicit illustrative KG/membership uses, an account synthesis and its cited paragraph. Its newly minted objects preserve the framing gap and original catalog GeneSet identities. The old one-Claim account remains the legacy API fixture. Neither account is relabeled as an AIP answer. See [the origin trace, invariant, and replacement-fixture design](scientific-account-framing.md).
