# REVEAL API implementation contract

**OpenAPI 3.1.1 · contract 0.2.0-draft · updated September 30, 2026.** This contract is implemented by the local FastAPI service; see [colleague startup](../README.local.md) and [API walkthrough](../docs/api-quickstart.md) and [runtime validation](../docs/validation-report.md). It aligns with the [consolidated plan](../docs/design-plan.md), approved HTML interaction and current evidence-package schema. It has **42 operations**, with schemas and paired input/output examples for every operation. The implementation adds an authorized captured-artifact download route without changing scientific identities.

## Open and share

```bash
# From repository root:
python3 -m http.server 8765 --bind 127.0.0.1 --directory api
```

Open [the API viewer](http://127.0.0.1:8765/) or [interactive flow](http://127.0.0.1:8765/flow.html). To share, send `reveal-openapi-handoff.zip`; your colleague extracts it, changes into `reveal-openapi`, and runs `python3 -m http.server 8765 --bind 127.0.0.1`. The standalone `reveal-api.html` also embeds the specification and Swagger assets. The zip contains the validated contract, examples, captured evidence package and viewer documentation. Links from this README to implementation scripts or design docs require the full repository; the API viewer, bundled schemas, examples and flow are self-contained. Example domains are not deployment endpoints. Serving does not start a backend or call an agent.

- [JSON contract](openapi.json) / [YAML contract](openapi.yaml)
- [Flow diagram](flow.html) / [Markdown](flow.md) / [Mermaid](flow.mmd)
- [Exchange library](examples/exchanges.json): request URL/headers/body, response and curl for every operation
- [Validation report](validation.json) / [flow coverage](flow-validation.json)
- [Design audit](../docs/design-package-audit.md) / [implementation checklist](../docs/implementation-handoff.md)

## Current flow and schemas

| Boundary | Routes | Principal schemas |
|---|---|---|
| Gap discovery and source detail | `GET /v1/knowledge-gaps`, `/search`, `/{gap_id}` | `GapList`, `GapSearchResults`, `GapRecord`, `SelectedGap`, DAPPER KnowledgeGap |
| Mechanism search/defaults | `GET /v1/mechanisms/search`, `/{source_id}`; `POST /v1/mechanisms/suggest` | `SuggestInput`, `Suggestions`, DAPPER Mechanism + catalog File + native CFDE anchor |
| Identity and editable selections | `GET /v1/me`; `GET/POST /v1/drafts`; `GET/PATCH/DELETE /v1/drafts/{id}` | `Me`, `Composer`, `DraftCreate`, `DraftPatch`, `Draft` |
| Immutable research | `GET /v1/research-requests`, `/{id}` | `ResearchRequest` with resolved gap, source context and original actor |
| Analysis / paragraph work | `GET/POST /v1/jobs`; `GET /v1/jobs/{id}`, `/events`; `POST /cancel` | `JobCreate`, `Job`, `AnalysisResult`, `ParagraphResult`, typed `JobEvent` |
| Frozen agent input | `GET /v1/jobs/{id}/evidence-package` | `EvidencePackageResult`; actual generated LinkML `EvidencePackage` schema bundled as components |
| Scientific inspection | `GET /v1/accounts/{id}`, `/claims/{id}`, `/gene-sets/{id}`, `/paragraphs/{id}`, `/objects/{id}` | Hydrated DAPPER documents, exact payload checksums, schema pins, coverage and authorized artifacts |
| Captured source bytes | `GET /v1/artifacts/{sha256}` | Owner-authorized redirect to a signed S3 URL for checksum-verified retained bytes; never a mutable upstream fetch |
| Personal workspace | `GET /v1/accounts`; `GET/POST /v1/me/explorations` | `AccountList`, `ExplorationInput`, `ExplorationList` |
| Render and export | `GET /v1/citations/{id}`; `POST /v1/citations/render`; `GET /v1/paragraphs/{id}/export` | Registry metadata/formats, `CitationRendering`, `ParagraphExport` |

No arbitrary Question input is accepted by Composer. `source_gap` identifies the exact imported DisMech observation; backend resolves immutable scientific text and linked DisMech mechanisms. Drafts may be empty, but jobs require one selected gap and 1–10 resolved `cfde-inc-v2` anchors. Five automatic defaults are total across linked mechanisms, removable and deduplicated. Initial retrieval searches the existing EAGGL embeddings joined to the [populated exact-trait/factor crosswalk](../docs/eaggl-cfde-links.md), with 1,756 mapped factors. Filter mapped candidates before the top-five cutoff; return fewer if necessary. Label/gene agreement and full current-model re-embedding are not requirements.

Search/detail/suggestion responses keep the existing `EagglFactor` shape: native CFDE `source_id` / `cfde_anchor.node_id`, source revision, DAPPER Mechanism and catalog File. A friendly EAGGL label can be presented through `cfde_anchor.label` without rewriting scientific identity. The backend binds the originating source hit, embedding run and mapping run to the saved selection/request; `SourceRef.source_revision` remains the scientific source revision. These application bindings do not require new public endpoints or a new evidence-package field. Join the mapping's ranked GeneSet references through existing aliases for object navigation; full evidence collection still uses the interactive and BioIndex calls. See [routing and revision rules](../docs/design-plan.md#initial-crosswalk-backed-retrieval-contract).

The local implementation supports lexical/fuzzy knowledge-gap search and existing EAGGL semantic retrieval. Gap semantic/hybrid modes return an explicit unavailable-mode error; no gap embedding corpus is fabricated. DisMech mechanism search loads the complete 19,959-record corpus on demand. Pure semantic mechanism search requires `source=eaggl`; hybrid search can combine EAGGL retrieval with DisMech lexical results.

## Evidence, DAPPER and agent outputs

- `EvidencePackage` is imported from [the canonical LinkML schema](../schema/evidence-package.yaml) through its generated [JSON Schema](../schema/evidence-package.schema.json). `Package*` components preserve its prefix-aware DAPPER types; ordinary `Dapper*` components use compact IDs for REST scientific records. These are representation boundaries of the same pinned scientific model, not parallel ontologies.
- [Captured package](examples/evidence-package/evidence-package.yaml) / [manifest](examples/evidence-package/manifest.json) / `sources/` are portable and checksummed. Schema validation alone does not prove source fidelity or authorize dispatch. The deterministic builder and worker gates remain mandatory.
- [Current account example](examples/scientific-account.dapper.json) is the approved 12-claim CAD design fixture; its exact source-selected gap now matches draft/request/account/Paragraph. [Paragraph](examples/paragraph.dapper.json) has 13 exact registry targets. Numerical CFDE observations are captured; KG/membership assertions are **invented and explicitly labeled**. It is not an accepted live-agent output or a production grounding-pass fixture.
- The captured evidence package and authored account fixture share framing but are separate artifacts; never claim that the latter was generated/validated from the former. Similarity scores, user/job timestamps and activity events are illustrative. The account retains an earlier Mechanism projection; selected-anchor examples use the collector projection. Their different labels/descriptions produce different valid digests; see the design audit before treating these as an end-to-end acceptance fixture.
- DAPPER scientific snapshots remain at the historical v8 dependency pin. New agent starts use the separately locked **0.2.0-a1** release with reviewed input-snapshot compatibility. Preserve exact historical IDs/payloads; upgrades are explicit.
- [Account startup/lint](../docs/scientific-account-linting.md) and [worker output manifest](../docs/agent-output-contract.md) are separate from public `Job` responses. One full DAPPER document per account, final backend lint plus grounding/ownership/ledger validation before acceptance.

## Transport and application rules

`ApplicationBearer` accepts an owner-scoped `rvl_` API key or a short-lived trusted-gateway JWT for **registered or anonymous** principals. Paste the raw key into Swagger **Authorize**. The key retains the workspace’s ownership, expiry and job limits, with no administrator or internal-service privileges. See [API-key access](../docs/api-keys.md) for issuance, drafts, jobs and event-stream examples. Requests without a bearer permit only public reads. Provider OAuth tokens and NextAuth cookies are not API bearers. Server derives owner; a digest is not an access grant. Browser session/bootstrap and service-only provisioning/claim are in [gateway contract](../docs/gateway-contract.md) with [schema](../schema/gateway.schema.json); no anonymous `owner_user_id` body can authorize a request.

Idempotency-Key is required on draft create/update, job create and exploration writes. Bind caller/operation/canonical body, retain at least seven days, check replay before version conflict. PATCH replaces complete composer using `expected_version`. Queue creation freezes request and actor transactionally. Pagination defaults 20/max100 and binds cursor to owner/filters/order/snapshot.

Jobs share queued/running/cancel_requested/cancelled/succeeded/insufficient_evidence/failed states. Attempts/leases belong beneath a job. Record Box IDs and reconcile on worker restart before duplicate execution. Stop is best effort; committed results win races; browser close does not cancel. Failed and insufficient-evidence are distinct.

Events use per-job monotonic IDs; JSON replay or SSE. `Last-Event-ID` and `after` must agree when both supplied; deduplicate job/id on reconnect. Expired history returns `EVENT_CURSOR_EXPIRED`; recover using current Job. Use Next.js proxy or authenticated fetch streaming, since native EventSource cannot set a bearer header. Public message/tool excerpts are bounded/sanitized; no private reasoning. Missing counts/timing are null, not invented.

Accept each account with a transactional paragraph outbox entry. Analysis result carries separate paragraph job IDs; account detail carries research-statement status. Analysis success does not wait for paragraph success. Default generation deduplicates by account payload, citation pins, settings and skill version; explicit paragraph jobs allow retry/focus changes without new research.

Provenance resolution is bounded upstream traversal (depth≤5, nodes≤250), with explicit completeness/missing references and opaque continuation. No unauthorized IDs are exposed. Recorded local File locations do not imply a download; artifact access has availability and verification metadata. GeneSet membership/construction coverage remains explicit.

Each provenance page repeats its unchanged terminal root and adds upstream nodes. Use `max_nodes` of at least two for continuation; one returns only the root and explicit missing references. Signed cursors bind the owner, root payload, full immutable document and traversal limits.

Citations pin immutable metadata revisions and code-point spans. Registry revisions never fall back silently to latest. DAPPER digest is not a registered DOI. `/export` returns JSON with content/filename/media type, complete citation targets and required companion files; rich text supplies HTML/plain text, LaTeX uses `\cite` and reminds users to fetch `references.bib`. The runtime supplies canonical URLs, pinned CSL styles/locales and access checks; see the citation standard and backend tests. APA/MLA examples remain formatting-shape fixtures, not measured CSL-renderer output.

## Regenerate and verify

Run from repository root with [OpenAPI dependencies](../scripts/requirements-openapi.txt):

```bash
.venv/bin/python scripts/build_openapi.py
.venv/bin/python scripts/validate_openapi.py
.venv/bin/python scripts/build_api_viewer.py
npm --prefix services/frontend run generate
npm --prefix services/frontend run fixtures
```

Source: [base generator](../scripts/build_openapi.py), [current-contract amendments](../scripts/openapi_current.py), [flow stages](../scripts/api-flow-stages.json), [flow builder](../scripts/build_api_flow.py), [viewer builder](../scripts/build_api_viewer.py). Edit sources and regenerate; do not hand-edit exported JSON/YAML. Validation checks every local ref/example/exchange, DAPPER identity/profile/registry links, source checksums and malformed-input rejection. It also checks exact gap alignment and the bundled evidence-package shape. Runtime owner, source-revision, lineage, exact-observation and scientific checks remain backend responsibilities.

[Archived v9 contract](history/v9/openapi.json) is for comparison only. The `examples/cfde-response.json` capture remains an older graph visualization input used by the HTML builder; it is not the current account's evidence File. The original authored retrieval Question is no longer a production input example.
