# HTML study — contract alignment and amendments

**Consolidated in [design v12](../docs/design-plan.md) and [OpenAPI 0.2.0-draft](../api/README.md).** This document preserves the rationale discovered during HTML iteration. Its references to an unchanged v9 API and “next contract” are historical: the current contract now includes selected-gap-only inputs, EAGGL Mechanisms, anonymous principals, source detail, typed activity, bounded resolution, account/exploration lists, automatic paragraph state and full exports. See [the audit](../docs/design-package-audit.md) for implemented components versus remaining runtime work. The HTML remains a simulation.

## Historical amendment notes

## Screen → existing contract

| User action | Request / response boundary | Design status |
|---|---|---|
| Search DisMech gaps | `GET /v1/knowledge-gaps/search?q=...` → `GapHit[]` with DAPPER KnowledgeGap, source and ranking | Local typo-tolerant fuzzy matching over 13 real source gaps; search text is never an inquiry submission |
| Select a gap | `GET /v1/knowledge-gaps/{gap_id}` → `GapRecord` | DAPPER text/rationale, source identity and typed attachments; raw source detail is a sidecar |
| Inspect/search context | `GET /v1/mechanisms/{source_id}` and `/search` → v9: DisMech Mechanism or EAGGL source File plus native factor anchor | Revised design: EAGGL factors are Mechanisms too; add their DAPPER projection and preserve catalog Files/source aliases |
| Add five defaults | `POST /v1/mechanisms/suggest` with selected DisMech context, inquiry, dismissals and manual factors → automatic anchors plus ranking provenance | Five total; preview ranking is illustrative, not computed |
| Click “Let’s close this gap” | Gateway session choice, then `POST /v1/drafts` / `PATCH /v1/drafts/{id}` → confirmed draft/version | ORCID/Google/anonymous choice is simulated; no session or save occurs |
| Submit saved revision | `POST /v1/jobs`, `kind=analysis`, draft ID/version and idempotency key → queued job | Nonempty EAGGL guard; frozen source selection is the planned worker input |
| Follow agent activity | `GET /v1/jobs/{id}` and `/events` → state and sequenced events | Automatic, appended activity playback; graph counts are labeled reference-capture observations; no elapsed/token/cost metrics fabricated |
| Inspect account | `GET /v1/accounts/{id}` → DAPPER document with component Claims, Propositions, EvidenceItems and provenance | Existing authored account fixture grounded in a real captured association |
| Inspect evidence | `GET /v1/claims/{id}`, `/gene-sets/{id}`, `/objects/{id}` → exact observations/coverage/artifacts | Source Claim → CFDE File and catalog GeneSet; missing construction provenance is explicit |
| Follow full construction history | Generic object resolver for collection, Activities, Files, membership and edges | Separate corrected HuBMAP bundle; no fabricated link from the CAD account |
| Generate paragraph | `POST /v1/jobs`, `kind=paragraph`, saved account → Paragraph result; `/citations/render` formats its full citation set | Existing Paragraph fixture; clickable Question/Claim citations at metadata revision 1 |

Public browsing without a session is different from private anonymous ownership. An anonymous user must possess a trusted session/assertion to save, submit or read private work. All existing owner and idempotency checks still apply.

## Contract amendments found by the UI

### Multi-claim account packet — rendering and validation findings

The [current packet](data/cad-account/README.md) has one Claim per biological Proposition and direct artifact EvidenceItems. UI selection must follow `component_claims` → `Claim.proposition` / `Claim.has_evidence`; it must not assume the first array entry, a single finding, or a mandatory source Claim. Source-origin labels, grouping and metric presentation are application sidecar data keyed by DAPPER IDs. Illustrative assertions must remain visible as such when used as support or neutral context. Direct artifact evidence requires a bounded source File and exact row locator; reference-only access must not imply that a download exists.

Full schema validation also confirms that a structured Proposition must supply the complete subject/relation/object triple once those fields are used. The packet supplies explicit application relation URIs and complete triples. A checksummed File plus EvidenceItem context is enough for this artifact-based example; current EvidenceItem has no typed numeric-score or locator field. Structured source metrics remain in the retained JSON. This is an authoring/renderer requirement, not a new DAPPER field or an OpenAPI regeneration.

### EAGGL mechanism identity — modeling amendment

An EAGGL factor is a mechanism in this application. Amend EAGGL search/detail/suggestion records and evidence-package references to carry the corresponding DAPPER `Mechanism`, its full native CFDE identity, trait/model context and versioned source-to-digest mapping. Keep the canonical catalog `File` as source provenance. `Factor1`/`Factor12` are interim names until descriptive labels are supplied; no extra factor-to-mechanism Claim or Factor class is required. CFDE calls continue to use native factor IDs and wire types. Different sources or model fits are not equated by a bare factor number or semantic similarity. Apply DAPPER revision rules when hashable labels/content change. The v9 schemas and existing fixtures still expose EAGGL Files only and have not been regenerated or reminted for this amendment.

### 1. Anonymous principal and recovery — accepted behavior, wire amendment required

Add `principal_kind=anonymous|registered` to identity/attribution responses and document anonymous session bootstrap, server provisioning, later first-login upgrade and existing-account claim. Preserve a stable private application UUID, distinguish it from scientific actor IDs, and keep secrets outside the research database. For an existing account, require proof of the current anonymous session plus freshly verified target identity and explicit transfer consent. Atomic/idempotent transfers include active jobs and access grants; historical submitting actors and bylines remain unchanged.

The proposed browser endpoint `POST /api/session/anonymous` belongs to the gateway, not the scientific REST contract. Internal provisioning requires a service identity. Define expiry, replay/conflict, account-claim and quota errors, and add anonymous request/response examples for drafts, jobs, events, cancellation and paragraph generation. No unauthenticated free-form `owner_user_id` or `anonymous: true` body field grants access.

An anonymous DAPPER/citation attribution fixture still needs validation. The current `Agent` is a mixin, so it cannot be minted as a standalone object. The proposed pseudonymous Person has an opaque actor identifier and no invented real name or ORCID; exact identity fields must be checked under the selected DAPPER pin. The HTML intentionally keeps the existing fixture's Example Researcher attribution unchanged.

### 2. Rich source context for a KnowledgeGap

Current `GapRecord` includes `object`, `source`, and `attachments`; it does **not** carry raw evidence, proposed experiments, resolved phenotype objects, or full mechanism descriptions. The UI uses frozen DisMech sidecars for those details. Add a versioned source-detail envelope or an exact, authorized source-artifact reference and resolver. Preserve raw references, resolution/ambiguity, source pointer/hash, available evidence and experiments. Do not duplicate those fields into the DAPPER scientific identity without a schema decision.

### 3. Structured observable job telemetry

`JobEvent` already supports stage/message/status and reconnect cursors. Add a typed event payload for target-specific request status, attempted/completed counts, retained/pruned graph counts, clipping, duration, artifact and graph snapshot references, tool call ID, selected KG and coverage. A missing response is `unknown`/`not_available`, distinct from a successful empty array. Failed/cancelled phases must not acquire completed checkmarks.

**Current presentation:** submission contracts the existing question card upward and keeps read-only mechanism anchors beneath the exact question. One activity panel follows: CFDE expansion, graph-count observations, evidence-package preparation, agent startup, observable tool/activity messages, validation and a terminal outcome. Events append rather than replacing the whole page. A status region announces the current action; scrolling back through history must not pull the reader to the bottom. Respect reduced motion. Remove the former graph sidebar, step navigator and explanatory job hero.

**Wire mapping still to resolve:** preserve existing monotonic event IDs, replay/deduplication and reconnect semantics. Add typed observations for distinct node/edge counts with explicit count scope (retrieved vs retained), snapshot/artifact reference and coverage/truncation. Evidence-package-ready and agent-started need explicit event detail or compatible stage mapping; the current enum has no dedicated stages for them. Public agent messages/tool summaries must carry their source, call ID and completion state. The scripted presentation events in HTML are not claimed to validate as current API responses. Do not infer completed tool calls merely from the arrival of unrelated events.

**Cancellation and outcomes:** Stop calls the existing cancellation endpoint and displays `cancel_requested` as “Stopping…” until a terminal worker event confirms cancellation; a racing committed result remains authoritative. Cancelled/failed work retains the selected inquiry and already received observations. A stopped current step gets no completed checkmark. Completed execution is distinct from sufficient evidence and from closing the underlying gap. Connection loss should display reconnecting and use the existing cursor/status recovery; it does not stop a server job. The local preview pauses if navigated away from and offers Resume when revisited; that is only playback behavior.

If a Haiku/small-model summary is adopted, store summary text, referenced event IDs, summarizer model/version, prompt-template version, input artifact hash and summary generation Activity. Mark it as generated narrative and keep it outside scientific claims unless separately validated. Show observable tool/evidence progress, never private model chain-of-thought. Tokens, cost and timing remain unavailable when not measured.

### 4. Bounded provenance resolution and source access

The generic object endpoint can represent these objects; its transitive-closure behavior is not yet sufficient as a UI promise. Define traversal direction/edge families, maximum depth/node count, continuation, missing/unauthorized references and exact payload observations. Return explicit completeness/coverage instead of letting omitted nodes look like absence of provenance. Collection membership with hundreds of sets needs a bounded/paged view while preserving its immutable identity membership.

Distinguish a recorded `File.location` from an authorized artifact download. Return availability/permission, checksum, byte size and provenance of verification. The original GMT and renamed GMT have different checksums/IDs; row lookup uses `in_gmt_file` + `gmt_entry`. A local source path is displayable provenance, not a public download button.

### 5. CFDE catalog → fully described GeneSet

The existing account uses a catalog-only AMP AD / GTEx GeneSet. The HuBMAP bundle is a different collection and has reminted identifiers under revised identity rules. Its 358 sets, 964-symbol union, collection membership, file references and 79 edges are preserved exactly in this study. No replacement IDs or new CFDE aliases are inferred from name similarity.

Before enriching database objects, adopt a reproducible schema/identity dependency pin, resolve exact CFDE model/source aliases, record old/new identity correspondence when applicable, and retain historical payload observations. Claims must keep the precise GeneSet actually used. They may reference its source collection only through verified membership or a separately supported collection-level assertion. Do not attach a convenient unrelated collection.

The HuBMAP chain reaches 34 preparation inputs (33 ASCT+B tables + human gene info). It does not identify individual source rows for each cell-type set. That finer lineage requires source row selectors or a recorded transformation mapping. The UI explicitly stops at the granularity supplied.

### 6. Trending gaps

The minimal white homepage shows full natural-language questions under **Trending knowledge gaps**, with user-authorized mock counts of associated user-generated ScientificAccounts (24, 11, 38). It labels them **Sample account counts**. There are no observed popularity or account-count data in the source capture or current API. Selection fixes the question on this same page. EAGGL anchors remain visible and linked DisMech mechanisms remain read-only in a collapsed disclosure underneath. Before calling this live trending, decide the ranking signal/window, eligibility (public records only), aggregation/privacy, snapshot/freshness and endpoint or listing-sort semantics. A curated list can remain the initial behavior without claiming measured popularity. Add an application-level `scientific_account_count` summary and count provenance/snapshot to gap hits/listings, not to the DAPPER node. Count distinct saved, authorized-to-display accounts linked to the exact gap digest; specify whether an explicit source-family rollup includes older source revisions. Free-text/edited questions are outside the product; legacy records must not silently inflate the source gap count. Private anonymous/registered work must not leak through public counts. If the count later opens a list, define an account-listing query keyed by gap/source scope; the current API only provides exact account lookup. Search continues using `/v1/knowledge-gaps/search`.

## Fixture truth and limits

- Thirteen DAPPER gap projections derive from real captured DisMech source records; five EAGGL factor records are real captures. Local token/prefix/edit-distance fuzzy matching drives the prototype; default factor relevance is only a layout simulation.
- The saved CADinT2D account remains inspectable at `/#example`, without a new free-text analysis submission. Selected DisMech gaps exercise source/context selection and activity playback. At the user’s explicit request, the existing account renders at completion as an unrelated example for UI review; it retains its original scientific context and is never reminted as an answer to the selected gap.
- The account, agent activities, researcher identity and Paragraph are existing authored contract fixtures. Graph associations are captured; scientific conclusions beyond the source are not asserted. The 4.4007 score is method-specific, not a probability.
- The HuBMAP study at `/#hubmap` is separate. File/Activity identifiers and references are copied from the corrected bundle. Original source input bytes and execution are source-reported. No schema migration, DB mutation, agent call, OAuth request or publication occurs.
- Composer/activity playback remains a local simulation. Personal workspace history now retains explored gaps, saved anchor selections and the existing example account through same-tab reloads using `sessionStorage`; simulated registered profiles use `localStorage`. Neither cache implements credentials, server autosave, quotas or cross-device recovery.

## Review acceptance

Review: search and result selection; read-only linked context and editable EAGGL removal/reset; zero-anchor guard; preserved inquiry through each identity choice; automatic activity playback, upward card transition, scroll-follow behavior, stopping/stopped and terminal states; exact account/claim/proposition/evidence navigation; partial/unknown provenance; GeneSet selection and GMT row selector; original source file inspection; anonymous later-sign-in explanation; mobile layout; keyboard/focus and empty/error states. These checks are for the HTML design, not proof of a live application contract.

### 7. Selected DisMech gap is mandatory — accepted product scope

The v9 `Composer`/`InquiryInput` permit arbitrary Question/KnowledgeGap inputs and null source gaps. Narrow the next application contract: search text is ephemeral lookup text; analysis submission requires a selected imported DisMech `source_gap` with exact revision/payload observation and its DAPPER KnowledgeGap ID. Resolve the scientific inquiry server-side. Reject missing source gap, non-DisMech source, stale/unavailable revisions or changed text/identity rather than minting a user-authored Question. Apply the same rule to registered and anonymous principals and again before worker dispatch.

Before selection an empty draft may exist, but it cannot be submitted. After selection, only EAGGL factors, selected KGs and allowed execution settings are mutable; linked DisMech context is resolved from the selected gap and cannot be unpinned; source-gap changes come from a new catalog selection. Keep source attribution separate from the user's mechanism-selection/operator provenance. Existing DAPPER Question records and account fixtures remain resolvable; their schema support does not enable new free-text research requests.

Add request/error examples and tests for a valid source-selected gap, typed-but-unselected search, forged/edited gap payload, gap with zero mechanisms but a valid EAGGL anchor, zero EAGGL anchors, and stale source revisions. Default gap discovery to `source=dismech&mode=fuzzy`; embeddings still power the separate mechanism→EAGGL bridge. This narrows application authoring, not DAPPER's general scientific vocabulary.

**Source-owned mechanism links:** the backend derives the linked DisMech mechanisms from the chosen gap/source revision. A client cannot alter or remove those attachments in the composer; changing the source gap is the way to change its linked context. In the next request contract, remove client authority to edit that linked list (or require an exact match to the resolved source links). EAGGL anchors remain editable and use the same five-total defaults/nonempty-anchor rule. The UI calls search “Find more mechanisms,” while wire records retain their accurate EAGGL factor/File types. “Possible genetic mechanisms” describes leads to investigate, not validated causation or evidence that a knowledge gap exists.

### 8. Agent presentation and fixture result transition

Use completion checkmarks for CFDE expansion, counts and package preparation. Agent startup switches to a transcript panel with public agent messages, tool names, arguments and results. A single loading indicator marks the active stage. The scientific account is presented only after completion; there is no intermediate draft card. Distinguish preparation progress from tool call activity and account artifact availability in future structured events; keep actual call completion separate from narrative messages. A draft preview is not a validated/persisted account and must not be available as a durable citation target until validated and registered.

The user authorized rendering the existing CADinT2D ScientificAccount after any selected-gap preview, even though it is not semantically associated. The HTML displays an explicit Example label and the original source question; the returned account’s identity, claims, evidence and authorship remain intact. This transition is exclusively for visual iteration. It changes neither API validation nor production job completion rules. One stored account is rendered; no synthetic extra scientific records are minted just to fill the layout. Navigation to its claims, propositions, evidence, provenance and paragraph preserves the preview’s original selected gap when returning to activity.

**Transcript event detail (latest visual iteration):** the future harness adapter needs public message chunks, tool-call ID/name, sanitized display arguments, result/output excerpts, and explicit completion/error states, ordered by the existing event sequence. Use real tool events for execution status; do not convert private reasoning into public messages or invent source results. Bound output size and reference larger artifacts separately. The concrete call names and virtual workspace paths in the HTML are proposed display fixtures, not assertions about the installed Proto-OKN MCP schema. No production endpoint or DAPPER schema changes were made.

### 9. Personal workspace listings

The avatar opens explored gaps and owned scientific accounts. Gap selection can precede job submission, so `GET /v1/research-requests` alone cannot cover all exploration history. Add an owner-scoped exploration projection with exact source/DAPPER gap revision, last explored time and last saved draft/anchors; merge pre-session browsing after session establishment. `GET /v1/accounts/{dapper_id}` remains detail-only: add a paginated owner-scoped account listing with closing remarks, exact gap relationship, claim count, creation time and job provenance. Related-account counts on gap rows are application projections, not hashable DAPPER fields.

Derive ownership from trusted registered or anonymous session assertions, never a public caller-selected user ID. Cover pagination, deduplication, cache partitioning on identity changes, empty/loading/error states and anonymous upgrade/claim. Registered history survives through the database; anonymous recovery depends on session possession. Cache loss is not deletion of scientific records. This HTML-only change leaves the frozen OpenAPI unchanged.
