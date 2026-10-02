# Issue #7: draft editing, research inputs, and account exploration

Date: October 1, 2026\
Status: implemented and locally verified; awaiting user review\
Issue: [#7 — UI Updates](https://github.com/yakaboskic/reveal-mechanisms/issues/7)\
Implementation branch: `codex/issue-7-ui-improvements`\
Eventual PR: target `main`, with `Closes #7`

This document consolidates the issue and subsequent product decisions. Develop and review the changes locally before pushing to QA. The implementation was authorized after this plan was written. The local migration and canonical fixture seed are applied; QA and PR publication remain deferred until local review.

This plan supersedes earlier proposals to automatically retain a saved draft whenever a knowledge gap is selected. Opening an editor, explicitly saving a draft, and starting research are three distinct actions. Existing code references below were audited at commit `872b54f`; line numbers may move during implementation.

## 1. Product decisions

- Selecting a knowledge gap immediately opens a dedicated draft/editor URL. Mechanism retrieval must not delay navigation once the temporary editing record exists.
- Mechanism anchors remain visible first, followed by related DisMech evidence. One expandable **Additional context** area comes last, containing a writing space and document attachments. It accepts research direction, background and hypothesis text; this is not a cause-and-effect diagram editor. Existing separate direction/hypothesis values remain editable inside that area.
- A draft appears in Saved drafts only after an explicit Save action. Unsaved working copies are discarded when abandoned.
- Put a Save icon opposite the existing **“Let’s close this gap”** action. First save asks for a name; subsequent saves update the named draft.
- Starting research freezes the current inputs and creates an independent run. It does not implicitly save a draft.
- Saved drafts and research runs have separate workspace destinations and separate editor/status views.
- Uploaded documents must reach the research agent and scientific review, with immutable storage references and provenance. Merely storing them in S3 is insufficient.
- The scientific account view should support nested bubbles: account → claims → evidence, datasets, and provenance, following the issue’s reference image.
- Build a canonical, dataset-rich example account tied to one imported knowledge gap, with a repeatable local database seed command, as the reference for bubble development and acceptance tests.
- Existing scientific evidence requirements remain in force. Hypotheses and uploaded material are not automatically validated scientific support.

## 2. Audit of current behavior

| Finding | Current implementation | Required change |
| --- | --- | --- |
| Selecting a gap leaves the URL unchanged | [Composer.tsx](../services/frontend/src/components/Composer.tsx), `selectGap`, around line 332 | Create the temporary editing record and navigate before fetching suggestions. |
| Editing automatically retains drafts | [Composer.tsx](../services/frontend/src/components/Composer.tsx), `save` and debounce effect, around lines 274–302 | Separate temporary synchronization from explicit saving. |
| Draft links become run links | [WorkspaceDrafts.tsx](../services/frontend/src/components/WorkspaceDrafts.tsx), around lines 24–86 | “Open draft” must always open the editor; runs get independent links. |
| Run status controls draft visibility | [workspace.ts](../services/frontend/src/lib/workspace.ts), `workspaceRuns`, around lines 28–41 | Saved-draft visibility must not depend on queued or terminal jobs. |
| A run can show later draft edits | [Composer.tsx](../services/frontend/src/components/Composer.tsx), restore logic around lines 182–200 | Run views must always read the frozen request, never prefer the mutable source draft. |
| Composer has no researcher-context/upload contract | [openapi_current.py](../scripts/openapi_current.py), around lines 61–69 | Add explicit input fields and regenerate contracts. Do not repurpose mechanism search text. |
| Submission already freezes inputs and source bindings | [app.py](../services/backend/src/reveal_backend/app.py), around lines 560–577 | Extend that transaction to freeze verified user inputs and attachment versions. |
| Binary uploads cannot use the current proxy unchanged | [backend gateway route](../services/frontend/src/app/api/backend/[...path]/route.ts), around line 18 | Use JSON upload-control requests and direct signed S3 transfers. |
| Published provenance can authorize file downloads | [publication.py](../services/backend/src/reveal_backend/publication.py), around lines 65–71 | Add explicit handling for private user uploads before publication. |

The main application is under `services/frontend`. `reveal-client` is the separate API integration client; keep its generated contracts compatible without duplicating the full application UI there.

## 3. Editor, saved draft, and run lifecycle

### Temporary editing

Gap selection creates an owner-scoped temporary working record and opens `/drafts/<id>`. Existing `/?draft=...` and `/?gap=...` links retain compatibility handling. An intentional gap-selection click provisions the existing anonymous principal if the user has no session. Visiting the discovery page alone does not create an editing record or anonymous workspace.

The temporary record supports upload ownership, version checks, and pending operations, but does not count as a saved draft. Background synchronization must never display “Saved” or add it to Saved drafts.

All gap-selection entry points use one operation: search results in `Composer`, cards in [GapBrowser.tsx](../services/frontend/src/components/GapBrowser.tsx), and “Explore this gap” in [GapDetail.tsx](../services/frontend/src/components/GapDetail.tsx). Public information links remain read-only.

### Explicit saving and naming

Use the existing action row in `Composer.tsx`, around line 510, and styling in [composer.css](../services/frontend/src/components/composer.css):

```text
[Save icon]                              [Let’s close this gap ↑]
```

- Give the icon the accessible name and tooltip “Save draft,” with visible keyboard focus.
- First save opens a naming dialog, reusing the conventions in `WorkspaceDrafts.tsx`. Confirming saves the name, input snapshot, and attachment references atomically. Canceling does not save anything.
- Subsequent Save actions update the same named draft. Keep rename available separately.
- Display “Not saved,” “Unsaved changes,” “Saving…,” or “Saved” according to confirmed state. Failed saves retain editable input and offer retry.
- Editing an existing saved draft uses a separate working copy. Its last explicitly saved content must not be overwritten by background synchronization.
- Preserve optimistic version checks and idempotent retries. A lost response must not create duplicate drafts or silently overwrite a newer save.

### Discard behavior

Deliberate navigation away, returning home, clearing the gap, or switching gaps discards unsaved working changes. Reopening or reloading a saved draft restores its last explicitly saved revision. Reloading or reopening an unsaved editor starts a fresh gap-bound working copy, or shows an expired-editor state if the gap binding is unavailable; it does not restore the previous unsaved edits. An abandoned unsaved editor is not restored later as a saved draft.

Preserve a bounded, single-use continuation only for an explicitly initiated Save or Submit across authentication redirects and uncertain network responses. Sign-in by itself is not a Save action. Replace the existing broad session-storage recovery behavior in `Composer.tsx` and audit [submission.ts](../services/frontend/src/lib/submission.ts).

Browser close/crash cleanup must use server-side expiration; unload requests are not reliable. Deliberate discard invalidates the relevant editing session, with version/session checks so it cannot discard a newer session’s work. Cleanup must not race an in-flight Save or committed submission.

### Independent research runs

“Let’s close this gap” freezes the current working inputs without requiring the user to retain a named draft. After the transaction commits, navigate to a dedicated `/runs/<job_id>` view. Always resolve inputs through `job.research_request_id` to the immutable request and bindings.

A run owns its input snapshot even if the temporary editor is discarded or its source saved draft is subsequently edited or deleted. Keep the original draft identity/revision as historical provenance when applicable; it is not a live dependency.

The existing backend blocks deleting drafts with active research (`app.py`, around lines 499–501). Adjust lifecycle handling only after verifying frozen request, source bindings, attachment references, and retry receipts are independently durable. Discard must never cancel a run or remove its inputs.

“Edit these inputs” on a run creates a new temporary working copy. It never modifies the submitted request.

### Workspace organization

- **Saved drafts:** explicitly saved, named input snapshots plus preserved legacy drafts; open, rename, copy, or delete. Legacy unnamed drafts retain a fallback display label until renamed. Their visibility does not change when a run starts or finishes.
- **Research runs:** queued, running, completed, failed, and canceled work, with status and links to outputs.
- **Scientific accounts and explorations:** preserve the existing scientific result destinations. An exploration outcome and a draft are different objects.

Implement this separation in [workspace/page.tsx](../services/frontend/src/app/workspace/page.tsx), `WorkspaceDrafts.tsx`, and `workspace.ts`. Gap visit history may remain, but temporary draft creation/discard must not create retained draft clutter. Clear stale draft pointers without deleting run or gap history.

## 4. Draft presentation and input contract

The editor presents the selected DisMech gap, a prominent “What would you like the agent to explore?” field, context and hypotheses, attachments, supporting source controls, then the Save/analysis action row.

Keep the selected canonical gap and imported DisMech context read-only. Mechanism suggestions and graph choices move into a collapsible supporting section. Preserve input text if suggestions fail. The current backend requires at least one mechanism anchor; retain that requirement for the initial implementation, with automatically suggested anchors and a clear missing-anchor state.

Retain the existing system typography and white/navy/blue palette. Use a spacious, left-aligned editor. Extract focused editor, attachment, and persistence components rather than adding all responsibilities to `Composer.tsx`.

Proposed logical fields are `research_direction`, `context`, `hypotheses`, and selected `upload_ids`. These are separate from `mechanism_subquery`, which remains a retrieval filter. Exact wire shapes are to be finalized in the contract implementation.

Update the current amendment in [openapi_current.py](../scripts/openapi_current.py), not just the superseded composer definition in [build_openapi.py](../scripts/build_openapi.py). Regenerate OpenAPI, examples, and frontend/client types. Update defaults in [composer.ts](../services/frontend/src/lib/composer.ts), calls in [client.ts](../services/frontend/src/lib/client.ts), copy semantics, and authentication/submission recovery.

## 5. Persistence and migration

The application uses indexed, versioned JSON records through [repository.py](../services/backend/src/reveal_backend/repository.py) and [005_application.sql](../schema/migrations/005_application.sql). This scope does not inherently require new relational draft tables.

Represent temporary working state separately from the last saved snapshot, with explicit lifecycle/retention metadata and an expiration timestamp. A saved marker on one continually overwritten composer is insufficient: unsaved edits to an existing draft must be discardable. Add owner-scoped upload records and adapt job submission to consume the exact working revision without implicitly marking it saved.

Provide an explicit, idempotent migration with dry-run reporting, scoped to the configured application table prefix:

- Add compatible empty defaults for new user-input fields on editable legacy drafts.
- Existing drafts have no reliable explicit-save marker. Preserve them as legacy saved work; do not infer disposability from an absent name or a completed run.
- Do not rewrite historical research requests, accepted accounts, attribution, scientific identities, evidence-package hashes, or existing S3 references. Old frozen inputs mean “no user inputs” under their original schema.
- Preserve optimistic revisions and workspace invalidation events when changing mutable records.
- Record affected identifiers and a recovery strategy before applying the migration locally.

## 6. Uploads and S3 references

Each upload has an opaque ID, owner, original filename, detected media type, byte size, SHA-256, lifecycle status, and verified immutable storage descriptor. Derived reading content has its own checksum/reference and a link to the original.

Use JSON initiation/status/finalization requests through the authenticated gateway. Upload bytes directly to a constrained signed S3 destination. The backend chooses the destination and verifies the actual size, checksum, type, and object version before accepting completion; browser-supplied keys are not authority. Configure allowed browser origins and staging cleanup for local development.

Reuse [artifact_store.py](../services/backend/src/reveal_backend/artifact_store.py) for retained, checksum-verified objects. Its current canonical key structure is `<prefix>artifacts/sha256/<first-two>/<sha256>`. Keep bucket, key, `version_id`, checksum, size, and media type in durable references. Never persist an expiring signed URL as the source identity.

The attachment UI shows filename, status, progress, errors, retry/remove actions, and expandable storage details. Expose the stable `s3://bucket/key` pointer and version/checksum only through authorized metadata; generate authorized downloads on demand. A storage location alone is not evidence that bytes are captured or verified.

Choose supported formats and extraction limits during implementation, starting with common document/text/table inputs. Show unsupported or failed extraction explicitly. Do not silently omit an attachment or submit while selected inputs are unfinished. Current Box constraints include 8,000,000 bytes per source artifact and 40,000,000 aggregate source bytes in [box_adapter.py](../services/backend/src/reveal_backend/box_adapter.py); allocate user-input budgets alongside collected evidence rather than promising unlimited uploads.

Discard removes the working copy’s references. Physical cleanup retains anything referenced by a saved draft, frozen request, execution checkpoint, or accepted provenance. Shared content-addressed objects cannot be deleted merely because one draft disappeared. Pending/failed staging objects need bounded expiration.

User uploads are private by default. Use owner-only upload access rather than inheriting the public fallback of the existing scientific artifact route. Publication must handle upload visibility explicitly; neither raw uploads nor private context should become public merely because they entered an agent input bundle.

## 7. Agent and review integration

```text
Temporary working inputs or explicitly saved draft
    → immutable research request and verified attachment versions
    → captured evidence package, including user inputs
    → agent input bundle
    → scientific review using the same frozen inputs
    → accepted results and provenance
```

Extend the freeze transaction in `app.py` to resolve upload IDs under the current owner and pin exact original/derived object versions. Later draft changes, file replacement, and detachment cannot change those references.

Add a versioned user-input section to the evidence package, distinguishing research direction, hypotheses, supplied documents, and extracted content from collected scientific evidence. Preserve compatibility with existing frozen package versions and replay.

| Boundary | Existing implementation to update |
| --- | --- |
| Frozen selection → collection | [worker.py](../services/backend/src/reveal_backend/worker.py), `collect`, around line 199 |
| Collection and strict package assembly | [evidence_collector.py](../services/backend/src/reveal_backend/evidence_collector.py), [evidence_package.py](../services/backend/src/reveal_backend/evidence_package.py) |
| Package schema and generated validation | [evidence-package.yaml](../schema/evidence-package.yaml), generated JSON Schema |
| Agent-readable file index and prompt | [evidence_files.py](../services/backend/src/reveal_backend/evidence_files.py), [dispatch_view.py](../services/backend/src/reveal_backend/dispatch_view.py) |
| Authoring guidance | [read-evidence-package skill](../services/backend/agent-skills/read-evidence-package/SKILL.md), [construct-scientific-account skill](../services/backend/agent-skills/construct-scientific-account/SKILL.md) |
| Bundle and durable checkpoint | [box_adapter.py](../services/backend/src/reveal_backend/box_adapter.py), [workflow_execution.py](../services/backend/src/reveal_backend/workflow_execution.py) |
| Review input access | [scientific_review_reader.py](../services/backend/src/reveal_backend/scientific_review_reader.py) and existing review preparation |

The agent must read the supplied direction/context and relevant documents, assess hypotheses against evidence, and retain source/checksum/page-or-section lineage for extracted material. Uploaded text is data, not authority to override execution instructions or scientific validation. Review must be able to inspect the same material. Do not infer scientific acceptance from upload or parsing success.

## 8. Scientific account bubble view

The [attached reference image](https://github.com/user-attachments/assets/788043b0-92bb-4cb5-82b5-3451cfbdc089) was visually inspected. It uses nested circle packing, with larger containing circles and progressively smaller objects. The issue also links the [Cytoscape example](https://idekerlab.ndexbio.org/cytoscape/1027cfd1-a022-4947-a1e7-3f604f3ae26a/networks/9a77e88b-1a06-11ef-bbe3-005056aecf54).

Integrate into `AccountView` in [Scientific.tsx](../services/frontend/src/components/Scientific.tsx), around line 93:

- The outer bubble represents the account; retain `closing_remarks` as its visible synthesis.
- Claim bubbles correspond to `component_claims` in canonical order, with C1, C2, and subsequent identifiers inside their circles. The bounded overview explicitly reports omitted records; Conclusions retains the full claim collection.
- The visible hierarchy contains account, claims, datasets and files. Evidence, activity and other provenance records supply the real associations and remain available in the inspector without adding intermediate circles.
- A shared inspector reuses existing `EvidenceView`, `ProvenanceView`, and source-artifact rendering. Preserve existing object/claim deep links.
- Provide breadcrumbs, zoom-to-selection, back/reset and keyboard navigation on the graph itself, without a duplicate visible record list or external circle captions. Full scientific text appears on circle hover and keyboard navigation. Use a side inspector on larger screens and a stacked/mobile treatment on narrow screens.

Use Cytoscape as requested, with an externally calculated packed-circle layout. Native Cytoscape compound nodes have rectangular shape constraints; its circle layout is not hierarchical packing. [D3 pack](https://d3js.org/d3-hierarchy/pack) can compute circle positions/radii for ordinary Cytoscape nodes using a preset layout. Prototype nested hit testing, draw order, labels, focus, and zoom before committing to the full interaction. See [Cytoscape node styling](https://js.cytoscape.org/#style/node-body).

Vary circle sizes modestly for visual interest using a stable FNV hash of each display occurrence ID. Inherit radius multipliers through claims (±25%), datasets (±18%) and files (±12%), clamp each final leaf multiplier to 0.65–1.45, and square it for the packing weight. D3 packs these weighted leaves and calculates containing circles; do not resize circles after packing. Shared ancestor multipliers give otherwise identical claim subtrees visible variation. Padding and normalization affect final radii, so these multipliers are not promised pixel-size ranges. Canonical identities, C-number order and full-text hover remain unchanged across repeat layouts.

Build a pure hierarchy adapter and a client-only renderer, with shared inspector components. Reuse the authorized, bounded provenance projection in [acceptance.py](../services/backend/src/reveal_backend/acceptance.py), `object_envelope`, and [app.py](../services/backend/src/reveal_backend/app.py), scientific reads around line 728. Do not create a parallel scientific graph store.

Data rules:

- Follow actual claim/evidence/derivation/activity/dataset references. Do not attach every returned file to every claim.
- A shared source may have multiple display occurrences with one canonical scientific identity. Mark shared occurrences and synchronize selection.
- Distinguish evidentiary support/refutation from computational inputs. Bubble size accommodates nested content and decorative variation; it does not encode confidence, scientific strength or another quantitative comparison.
- Load more detail on demand, bound to the same account/publication snapshot. Show partial coverage and retry/load-more states; missing fetched data does not mean missing evidence.
- Display dataset locations and concrete file locations separately from authorized download availability. Clear identity-bound caches on account/user changes.

The implementation pins Cytoscape 3.34.3 and d3-hierarchy 3.1.2 in [frontend package.json](../services/frontend/package.json).

### Canonical example account and database seed

Create a versioned reference fixture that can be inserted into the local application database and explored through the normal knowledge-gap, account, claim, object, and artifact APIs. This is an implementation deliverable, not just browser-mocked example data.

Start from the existing [CAD-in-T2D design packet](../design/data/cad-account/README.md) and [example builder](../design/build_account_example.py). It already contains one exact DisMech knowledge gap, 12 claims/propositions, 16 evidence items, four files, and two gene sets. Its CFDE observations are captured, but some KG/membership relationships and biological interpretations are explicitly illustrative. It has no Dataset objects. Preserve those distinctions when extending it; this is a canonical UI example, not a claim that a live agent or independent scientific review accepted its conclusions.

The reference account should tell one coherent story about that gap, with closing remarks explaining what the combined claims support and what remains unresolved. Target the following coverage, with exact object counts and identities recorded in a fixture manifest:

| Element | Required example coverage |
| --- | --- |
| Knowledge gap | One exact imported gap, with pinned source revision and canonical ID. Every claim contributes to the account's treatment of this question. |
| Claims | Approximately 12 distinct claims covering several mechanisms/relationships where supported by the chosen material, including differing assessments and limitations. Each claim has an inspectable path through evidence to its source dataset/files. |
| Datasets and files | At least three Dataset objects and six small captured File objects, including raw and derived artifacts, using only relationships permitted by the pinned DAPPER schema. |
| Shared sources | At least one dataset/file used by several claims and one claim drawing on several datasets, exercising repeated display occurrences without duplicate scientific identities. |
| Provenance | Multiple activities with used/generated/derived relationships, software/agent attribution where applicable, and a multi-step raw-input → analysis → derived-output chain. |
| Evidence detail | Source locators/excerpts, metric meanings, citations, and contrasting evidence directions where justified; any invented demonstration material is explicitly labeled as such. |
| Storage | Actual verified S3 versions/checksums for retained fixture bytes, visible location pointers, and working authorized downloads. Do not substitute invented S3 URLs or expiring signed URLs for provenance. |

Keep the complete canonical fixture internally consistent and fully resolvable. Add separate derived test variants for missing/unavailable files, partial traversal, long labels, and crowded layouts; do not intentionally break the canonical document to test error states. A private supplied-document example can exercise the upload-to-provenance path once that contract exists, without presenting an upload as independently validated evidence.

Proposed deliverables, to be created during implementation:

- `data/fixtures/bubble-account-v1/`: scientific document, captured source files, fixture manifest, provenance/expected-relationship inventory, validation report, and a README describing captured versus illustrative content.
- A reproducible builder extending the existing example-builder approach, with computed DAPPER IDs and reminting of affected references when content changes.
- `scripts/seed_bubble_account.py`: explicit local seed tooling with dry-run, apply, and verification modes. Require a target workspace owner and verify the isolated local application table prefix before writes.
- A seed receipt recording fixture version/content hash, owner, gap/account IDs, inserted record IDs, verified S3 object references, and local editor-independent account/gap URLs.
- A reproducible frontend/test projection generated from the same scientific fixture, so the browser example and database-backed account cannot silently diverge.

Validate with the pinned DAPPER identity/schema/reference rules and [scientific-account linter](../scripts/lint_scientific_account.py). Check closure, dataset/file membership, evidence locators, and actual file checksums before inserting. Structural validation must be reported separately from scientific acceptance. Fail clearly if the target imported gap revision is unavailable rather than silently rebinding by similar question text.

The seed must persist the complete read model, not only an `account` row: account membership/summary, scientific document, per-object projections and document bindings, observations, owner grants, citations, and artifact references as required by the current readers. Audit/extract the persistence logic in [worker.py](../services/backend/src/reveal_backend/worker.py), `accept_accounts`, around lines 528–582, and [account_discovery.py](../services/backend/src/reveal_backend/account_discovery.py). Reuse validation/projection helpers such as `object_envelope`; do not call the live acceptance workflow merely to seed, because it also queues paragraph jobs. Set research-statement status to `not_requested`, or import an explicitly authored paragraph and citation packet coherently. Represent fixture origin/attribution explicitly in application metadata without inventing a successful agent run or review, or changing scientific objects with unsupported fields.

Key insertions by fixture version/content and owner so rerunning the same seed is idempotent: stable scientific IDs, no duplicate membership/counts, no repeated uploads for identical retained bytes, and no overwrite of unrelated work. Retain a receipt for removing only fixture-owned references if needed; shared S3 objects follow the same reference-aware retention rules as other artifacts. Keep the fixture private to the selected local workspace by default. Do not dispatch research, review, or paragraph jobs, publish it, or seed QA as a side effect.

The database-backed acceptance flow is: open the bound gap in workspace scope → open the canonical account → inspect multiple claims → follow a shared dataset and a derived file → inspect provenance and download verified bytes. Refresh/deep links must resolve the same identities. This fixture should be ready before full bubble implementation so layout, selection, data loading, and storage links are tested against a stable, complete example.

## 9. Implementation sequence and local validation

1. Create `codex/issue-7-ui-improvements` from the reviewed base and keep this plan with the implementation.
2. Define temporary/saved/run contracts and upload metadata; add compatibility and migration tooling.
3. Implement immediate editor navigation, explicit Save/name behavior, discard semantics, and separate run/workspace views. Fix mutable-input display in runs.
4. Implement verified S3 upload/download, extraction, retention, and publication visibility handling.
5. Carry frozen user inputs through collection, agent bootstrap, retry/restore, and scientific review.
6. Build, validate, and seed the canonical dataset-rich account into the isolated local database; record its IDs and local URLs. Prototype and then implement the account bubble view against it.
7. Validate the integrated local flow, review it with the user, and only then prepare the PR/QA release.

Use the current [Workflow local pilot](durable-workflow-runtime.md) and [durable_deployment.py](../scripts/durable_deployment.py). It uses `reveal_workflow_local_*` application tables, local workflow/notification namespaces, and development S3 storage. “Local” still uses real backing services. Verify the selected configuration before applying the new migration; the older colleague setup uses different/shared application tables. No QA deployment is part of this design-writing step.

Local acceptance checklist (automated checks plus the real-service checks recorded below; user review is still pending):

- [x] Gap selection opens the editor URL before suggestions complete; retry does not create duplicate editing records.
- [x] Temporary editors do not appear in Saved drafts. Canceling naming saves nothing.
- [x] Explicit Save atomically retains the named revision; failed saves and conflicts remain recoverable.
- [x] Discarding edits preserves the prior saved revision. Run status never hides a saved draft or changes its editor destination.
- [x] Reload restores the last explicit save for saved drafts and does not resurrect edits from an unsaved editor; authentication/commit recovery follows its separate continuation rules.
- [x] Submit works without implicitly saving a named draft; run views show exactly the frozen inputs even after source edits/deletion.
- [x] Authentication continuation and lost Save/Submit acknowledgments do not lose committed work or duplicate a run.
- [x] Abandoned working state expires without relying on unload events; cleanup preserves live sessions, pending transactions, saved inputs, and run-referenced files.
- [x] Upload ownership, anonymous workspace transfer, binary integrity, size/type limits, failed completion, and checksum/version mismatches are tested.
- [x] Migration dry run/replay preserves legacy work and historical scientific/request identities.
- [x] Agent and reviewer can actually read the supplied context/hypotheses/files after checkpoint restore and retry; nothing is silently omitted.
- [x] Publishing an account does not accidentally expose private uploaded bytes or context.
- [x] Bubbles preserve claim membership, shared identities, provenance relationships, partial coverage, and storage/download distinctions.
- [x] The canonical example has multiple claims tied to one exact gap, multiple datasets/files, shared-source and multi-step provenance paths, and a clear captured/illustrative manifest.
- [x] Local seed dry-run/apply/verify modes validate the complete account read model; repeating apply creates no duplicate scientific objects, membership, or gap counts and dispatches no agent jobs.
- [x] The seeded gap/account/claim/object routes and verified S3 downloads work through ordinary authorized APIs, survive refresh, and respect workspace isolation.
- [x] Browser checks cover selection/zoom/back, synchronized scientific inspection, keyboard use, narrow screens, and failure/retry states.
- [x] Typecheck, build, API/schema validation, and relevant existing regression suites pass before local acceptance.

Extend existing frontend navigation/workspace/contract tests and browser harnesses, plus backend application, artifact-checkpoint, worker-file-evidence, Box-execution, Workflow-bootstrap, and publication tests. Use the canonical fixture and its separate error-state variants above for bubble and seed coverage; preserve existing contract fixtures until regenerated or deliberately migrated.

## 10. Resolved implementation choices

- Temporary editors use the existing versioned draft store with `lifecycle: temporary` and a 24-hour expiry. Ordinary edits live only in the current component. Explicit Save promotes or updates a named saved record; browser continuation storage is limited to explicit Save/Submit receipts.
- Submission uses a temporary snapshot when a saved draft is dirty or reused. `source_draft_id` and `source_draft_version` preserve the editor's loaded lineage, including when another session saves a newer revision. The run reads the frozen request and its captured question independently of current catalog availability.
- Uploads support PDF, DOCX, UTF-8 text, Markdown, CSV, TSV, JSON and YAML. Limits are five selected files, 8 MB per original, 16 MB total originals, 500 KB extracted text per file, and 1 MB aggregate extraction JSON including locators. See [research-inputs.md](research-inputs.md) for transfer quotas, parser bounds, and retention.
- Original and extracted bytes use verified immutable S3 descriptors. Local direct uploads use constrained presigned POSTs; staging expires after one day. Shared retained objects are not deleted merely because one editor disappears.
- Results using private researcher text or uploads remain private: publication fails with `PRIVATE_RESEARCH_INPUTS` until a deliberate disclosure workflow is designed.
- Account continuation already supports snapshot-bound pages. The bubble adapter merges those pages, preserves canonical identities across repeated display occurrences, and exposes incomplete traversal explicitly.

## 11. Local implementation and verification record

The implemented code map adds [ResearchInputs.tsx](../services/frontend/src/components/ResearchInputs.tsx), [draft-save.ts](../services/frontend/src/lib/draft-save.ts), the `/drafts/[id]` and `/runs/[id]` routes, [user_inputs.py](../services/backend/src/reveal_backend/user_inputs.py), [AccountGraph.tsx](../services/frontend/src/components/AccountGraph.tsx), and [account-graph.ts](../services/frontend/src/lib/account-graph.ts). Existing components and worker/review paths in the audit above were updated in place. Public API artifacts, both TypeScript clients, evidence-package schema, and agent reading instructions are regenerated.

The migration preserved eight legacy local drafts as saved and changed no frozen research records. Its private recovery backup is `.runtime/workflow/research-inputs-backup.json`. Browser CORS and staging expiry apply to the development bucket's `local/` prefix and `http://localhost:3000`; prior rules are retained in `.runtime/workflow/upload-storage-before.json`.

The canonical account is seeded privately into the existing local Chase Yakaboski workspace. It contains 12 claims, 17 evidence items, four datasets, eight retained files, four activities and two gene sets. The fixture includes exact captured DisMech YAML whose checksum matches the selected database import, alongside the original CFDE captures and explicitly illustrative assertions. The seed receipt `.runtime/workflow/issue-7-seed-receipt.json` records all inserted/reused keys, immutable S3 versions/checksums, and successful readback. No research job was created by seeding.

- Account: <http://localhost:3000/accounts/dapper%3AScientificAccount.05Vs-l6pVZHt9ttebmJqK2VNodZUouTb>
- Knowledge gap: <http://localhost:3000/knowledge-gaps/dapper%3AKnowledgeGap.zNV20nhHamt-a4CeAktQQPoAivOJe6xk>
- Retained source prefix: `s3://cyaka-reveal-data/local/artifacts/sha256/` (exact versions/checksums are in the receipt and authorized source details).
- Fixture: [README and reproducible seed commands](../data/fixtures/bubble-account-v1/README.md).

Automated acceptance includes 30 lifecycle browser scenarios, 14 navigation scenarios, four workspace scenarios and four graph scenarios. These cover explicit saving/naming, discard/reload, conflict recovery, lost acknowledgments, bounded deadlines, OAuth continuation, upload retries, independent frozen runs, catalog failure, keyboard navigation, mobile layouts and snapshot-bound graph continuation. Test entry points and scope are documented in [frontend scripts](../services/frontend/scripts/README.md).

The broad backend run passed 945 tests and 311 subtests with eight skips; its seven failures resulted from the fixture revision being updated during the run. The corrected fixture and affected backend suites subsequently passed, including 30 lifecycle/application checks after the lineage fix. Both clients typecheck; OpenAPI validation covers 54 operations and the evidence schema is regenerated. The frontend suite passed 114 tests and the integration client passed 36 tests. Both production Docker images built successfully and the local API/frontend containers are healthy. No live paid research-agent execution is claimed by these tests.

### Real local service acceptance

The production build is running at <http://localhost:3000> with the API at <http://127.0.0.1:18001>. These checks used the isolated `reveal_workflow_local_*` tables and development `local/` S3 prefix:

- In the browser, selecting a gap opened a temporary editor before mechanism suggestions finished. A synthetic text attachment completed a real direct S3 upload and extraction; the editor displayed the original/extracted S3 locations, version and checksum.
- The Save icon opened the naming dialog and created **Local UI acceptance — context and upload** (`1cf40069-2193-416f-ad3d-c2df6d2af88c`) in the existing anonymous browser workspace. Reload after an unsaved context change restored the explicitly saved context and ready attachment.
- Leaving a separate temporary editor without saving kept the saved-draft count at four (the named acceptance draft and three preserved legacy drafts). The existing run appeared separately in Research runs with a `/runs/` destination.
- Authorized API reads resolved the seeded account, its claim and dataset deep links, and its exact knowledge-gap association. All eight retained downloads matched the receipt's SHA-256 values. The private canonical account was inaccessible without authentication.
- Migration replay found no additional rows to change. Seeding and acceptance did not submit a new research job. Agent input delivery, checkpoint/retry and reviewer access were validated with isolated tests; a new live paid agent execution remains outside this acceptance run.

Local evidence is retained under `.runtime/workflow/`: `issue-7-browser-smoke.json`, `issue-7-editor.png`, `issue-7-api-smoke.json`, `issue-7-frontend-tests.log`, `issue-7-client-tests.log`, `issue-7-openapi-validation.log`, `issue-7-final-build.log`, and `issue-7-migration-recheck.log`. These runtime files are ignored by Git. The canonical fixture remains explicitly illustrative and structurally validated, not scientifically reviewed.

All implementation work remains on `codex/issue-7-ui-improvements`. Nothing has been pushed, deployed to QA, or published as a PR. The next product checkpoint is local user review before preparing the eventual `main` PR with `Closes #7`.

### Local review refinements — eight browser comments

The first local review requested a quieter, evidence-led editor. The following refinements supersede the original three-field presentation:

1. Knowledge-gap Info links appear inline immediately after the final word as `[info]`, including for wrapped questions, and fade/slide to the right on hover or keyboard focus with a stable click target. Touch devices retain a visible control and reduced-motion preferences disable animation.
2. Remove the explanatory paragraph about scientific accounts versus saved explorations from the editor footer.
3. Keep mechanism chips visible, then related DisMech evidence and source options, followed by collapsed **Additional context**. New notes use `composer.context` through one textarea; attachment upload/state handling stays mounted while collapsed. Existing `research_direction` and `hypotheses` remain separately editable under **Previously saved research inputs**, preserving their original values and limits.
4. Replace the top Saved drafts link with a **Back** button offering **Knowledge gaps** and **Saved drafts**. Both use the existing navigation/discard lifecycle.
5. Remove the standalone DisMech label beside the question's About link.
6. Order workspace tabs and avatar destinations: Knowledge gaps, Scientific accounts, Saved drafts, Explorations, Research runs.
7. Remove the workspace toolbar Refresh button, retaining automatic updates and error recovery.
8. Remove “Your” from avatar menu destination labels.

Code: [DraftNavigation.tsx](../services/frontend/src/components/DraftNavigation.tsx), [Composer.tsx](../services/frontend/src/components/Composer.tsx), [ResearchInputs.tsx](../services/frontend/src/components/ResearchInputs.tsx), [GapBrowser.tsx](../services/frontend/src/components/GapBrowser.tsx), [workspace page](../services/frontend/src/app/workspace/page.tsx), [Session.tsx](../services/frontend/src/components/Session.tsx), and their component styles. Draft/run previews now fall back to context when no earlier research-direction value exists. No backend contract or data migration is needed for these presentation changes.

Refinement verification: the production frontend build and TypeScript checks passed, along with all 114 frontend unit tests. Browser checks against the real local app confirmed the hidden-to-hover Info transition and detail navigation, visible mechanism chips, one new-context textarea, collapse/reopen retention, both Back destinations, Escape dismissal, the reordered workspace/menu, removed footer/source label/Refresh button, and preserved earlier direction, context, hypothesis and ready attachment. Leaving the temporary browser-check draft discarded it and kept the saved-draft count at four. Existing browser harness selectors were updated for the new disclosures/menu; those full intercepted suites were not rerun in this refinement pass. Screenshots: `.runtime/workflow/issue-7-refinement-editor.png`, `issue-7-refinement-hover.png`, and `issue-7-refinement-workspace.png`. No research run was submitted and QA remains unchanged.

Follow-up refinement: Info uses natural inline text flow after the question, replacing the separate right-aligned control and arrow. A reserved inline footprint avoids reflow during the fade/rightward slide. Question and Info remain independent links; ordinary question clicks still open the editor, while modifier clicks retain native new-tab behavior.

Inline Info verification: TypeScript and the local production build passed. Browser inspection confirmed that a wrapped question’s `[info]` sits on its final text baseline immediately after the last word, transitions from hidden to visible without text reflow, and opens the gap details independently. Clicking the question text still opens its draft editor.

### Account review refinements — seven browser comments

The second local review separates the visual overview from the scientific reading view:

1. Add an accessible X to the anonymous-work prompt. Remember dismissal for the signed-in identity in this browser session; keep **Move my anonymous work** in the avatar menu so dismissal does not discard or transfer anything.
2. Place a compact **Publish** or **Unpublish** control to the right of the knowledge-gap question. Both open a confirmation dialog with Cancel and Escape support. Offer **Update snapshot** only when accepted content has unpublished changes. Preserve the existing ownership, snapshot, idempotency and version checks.
3. Remove the canonical-example notice from the account page. Keep fixture provenance, illustrative assertions and validation metadata in the underlying scientific records and reproducible fixture.
4. Add **Overview** as the initial reading tab, alongside **Conclusions** and **Research Statement**. Normal account navigation always starts on Overview, even after reading a different tab. Explicit `?view=overview`, `?view=conclusions` and `?view=statement` links still choose that reading view; claim filters and expansion preferences remain separate.
5. Put the bubble component in Overview without the extra heading, subtitle, Bubbles/List switch or duplicate visible record list.
6. Show claims, datasets and their files. Traverse evidence and activity records to find real source associations without displaying those intermediate circles. Dataset containment must come from actual membership; a file with no known dataset stays attached to its claim. Preserve shared canonical IDs and bounded/missing-source information. The latest review replaces external text captions with C1, C2, and subsequent identifiers inside claim circles, numbered by canonical `component_claims` order rather than packed position. Retain keyboard navigation, zoom, Back and the scientific inspector. Show the full scientific label in a smooth popover on circle hover without selecting it, and when navigating the canvas with the keyboard. External circle captions are removed.
7. Keep the existing synthesis and **Associated claims** disclosure together in Conclusions. Research Statement retains its existing statement workflow; Overview does not duplicate the claims list or provenance disclosure.

Code: [Scientific.tsx](../services/frontend/src/components/Scientific.tsx) and [scientific.css](../services/frontend/src/components/scientific.css) own tabs/header/reading placement; [AccountPublication.tsx](../services/frontend/src/components/AccountPublication.tsx) and its stylesheet own the compact publication dialog; [AccountGraph.tsx](../services/frontend/src/components/AccountGraph.tsx), [account-graph.ts](../services/frontend/src/lib/account-graph.ts), [account-graph-layout.ts](../services/frontend/src/lib/account-graph-layout.ts) and graph styles own the display hierarchy, packing and interaction; [Session.tsx](../services/frontend/src/components/Session.tsx), [continuity-prompt.ts](../services/frontend/src/lib/continuity-prompt.ts) and menu styles own prompt dismissal. These changes require no database migration or publication-state change.

Account refinement verification before the latest numbering revision: all 124 frontend tests, TypeScript and the local production build passed. Graph regressions cover shared identities, actual dataset membership, hidden intermediate records, upstream lineage, C2M2 files, edge-only source associations, incomplete continuation and the 600-node display bound. The browser confirmed Overview/Conclusions/Research Statement separation, preserved Associated claims, dismissal across reload with the transfer action still in the menu, and publication-dialog cancellation/Escape with focus restored. Keyboard navigation reached a claim, dataset and retained file with its S3 pointer. Full text appeared on caption focus without changing the selected record and Escape dismissed it. At the review's 967-pixel viewport, resize refit the diagram and kept the popover in bounds; the 390-pixel layout stacked without horizontal overflow. No browser errors were recorded. Existing browser harnesses were updated and syntax-checked but their intercepted suites were not rerun in this pass.

For the latest numbering revision, the existing graph harness now checks C1/C2 labels against canonical claim membership, absence of external captions, full-text circle hover without selection, and keyboard navigation with ArrowRight/ArrowDown. It retains tooltip bounds/Escape, inspector, resize, continuation and mobile checks. The harness is syntax-checked; its intercepted browser suite has not been rerun for this revision. The earlier caption-focus check is superseded by canvas keyboard navigation.

The numbering revision passed TypeScript and the local production build. Each C-number sits inside its claim circle and targets that claim for hover, focus and selection, even where nested dataset circles lie beneath the label. Full-text hover and the simplified claim/dataset/file hierarchy are retained.

Local browser verification confirmed all twelve internal numbers, no external captions, full-text focus popovers without changing selection, Escape dismissal, and C1 opening the matching SHH claim. The 748-pixel review layout was checked. Proof: `.runtime/workflow/issue-7-claim-labels-hover.png`; build log: `issue-7-claim-labels-build.log`.

The subsequent sizing refinement adds stable, varied circle sizes using the inherited packing weights described above. It preserves C1/C2 numbering, full-text hover and the actual source hierarchy. TypeScript and the production frontend build pass. A numerical check of the canonical fixture verified 157 finite, positive circles, containment, 186 nonoverlapping sibling pairs, identical repeat layouts and an unchanged source tree. Final maximum/minimum radius ratios were 1.21 for claims, 1.58 for datasets and 2.17 for files; claim radii ranged from 88.64 to 107.09 on the 1000-unit layout. Browser verification in the local app confirmed varied circles, all twelve claim numbers, a full-text C3 focus tooltip without changing Account selection, C1 opening the matching SHH claim, and Reset returning to the overview. No browser errors were recorded. Proof: `.runtime/workflow/issue-7-varied-bubbles.png`. This verification was local only; no QA deployment was performed.

Local proof: `.runtime/workflow/issue-7-account-overview.png`, `issue-7-account-hover.png`, `issue-7-account-refinement-tests.log` and `issue-7-account-refinement-build.log`. No publication state was changed, anonymous work was not transferred, and no research job or QA deployment was initiated.

## Community discovery and project introduction — October 1 review

- Default ordinary scientific-account links to Overview, without restoring a previously selected reading tab. Remove the 80-character width cap from the account's Conclusions and Research Statement panels so prose fills the available content div.
- Add matching line icons for claims, datasets and files to the bubble diagram and legend. Preserve canonical C-numbers, full scientific hover text, keyboard navigation and varied circle sizes. Small source circles reveal their symbols when zoom makes them legible.
- Landing discovery offers **Trending knowledge gaps** and **Trending scientific accounts**, both public community views. Scientific-account search must use published snapshots and filter the complete result set before pagination. Private workspace listings keep their current scope. Source: `Composer.tsx`, `GapBrowser.tsx`, the client account-list API, and backend account discovery.
- Native selectors should stop showing a lingering pointer-focus outline after a click while retaining a visible keyboard focus indicator.
- Add **About** at the upper left in `Session.tsx` and a dedicated `/about` page (`src/app/about/page.tsx` and `about.css`). The page explains the goal, researcher judgment, agents assembling accounts, claim-to-data traceability and community contributions.
- Introduce the landing page with “Let’s use Common Fund Data to close known biomedical knowledge gaps.” Link Common Fund Data to the [CFDE Knowledge Center](https://cfdeknowledge.org/r/kc_landing) and knowledge gaps to [DisMech discussions](https://dismech.monarchinitiative.org/app/discussions/index.html). Use the original CFDE logo from the Knowledge Center with its proportions preserved; asset provenance is recorded in `public/brand/README.md`.

The user-provided `close_the_gap.v4.pptx` informs the About page. Slide 1 depicts a researcher providing context, posing a falsifiable hypothesis, testing predictions against new or existing data and weighing evidence for or against. Its original embedded image appears in the About page. The rest of the deck establishes the broader vision: agents assemble multiple claims into an account; provenance links findings to datasets and analyses; researchers review and publish; the community prioritizes gaps and builds on prior contributions. Proposed repositories, partnerships and contribution workflows are described as future vision, not shipped product capabilities. Image provenance: `public/about/README.md`.

Design: retain the app's white background, ink `#20293a`, reading text `#4d5e76`, blue `#3e68a0`, muted `#69798e`, and fine dividers `#e0e5ec`. Keep existing system typography; use a larger, left-aligned About heading and spacious explanatory sections around the supplied scientific-method image. Preserve a compact navigation and the research-first landing layout.

All work remains local on the issue-7 branch.

Public discovery implementation: [Composer.tsx](../services/frontend/src/components/Composer.tsx) and [GapBrowser.tsx](../services/frontend/src/components/GapBrowser.tsx) now switch between community gaps and published accounts. Gap browse/search explicitly uses public scope; account browse/search calls `api.publicAccounts` with public scope and vote or publication-date sorting. The new account list fences stale responses by query, sort and identity, retains canonical IDs across pages, and refreshes on workspace events or browser return. [community-discovery.ts](../services/frontend/src/lib/community-discovery.ts) rejects private/owner response metadata before display. Selecting a gap still opens its draft; normal account links open Overview.

The read-only [accounts endpoint](../services/backend/src/reveal_backend/app.py) accepts `scope=public|workspace` and `sort=recent|votes`. Its default remains the existing authenticated workspace listing. Public scope reads only frozen publication summaries, filters the full authorized collection before pagination, omits private job identifiers, and binds cursors to query, scope, ranking, registered viewer and current publication/vote state. No database migration or publication mutation is needed. [OpenAPI source](../scripts/openapi_current.py), generated contract/client types, fixtures and portable API documentation were refreshed.

Focused checks passed: 35 backend tests plus five subtests across public account discovery, workspace search, gap discovery, voting and publication; TypeScript; and 14 frontend tests covering public-page guards/deduplication, gap reading, gap ranking, navigation and votes. [Public discovery regressions](../services/backend/tests/test_public_account_discovery.py) verify owner/private-edit isolation, signed-out reads, invalid credentials, search before pagination, actual vote/publication ranking, canonical deduplication and cursor invalidation after voting or unpublication. OpenAPI validation passed for 54 operations, 308 response examples and 758 local references. Browser acceptance and the coordinated production build remain separate checks; these tests did not publish content or launch a research agent.

Coordinated verification passed: all 127 frontend tests, TypeScript, and production Docker builds for frontend/API. The local services were rebuilt and restarted only. A signed-out API request for public CAD accounts returned the published snapshot with no owner controls/job ID and `private, no-store` caching. In the browser, public account search returned the canonical account; public gap search returned related coronary questions; ordinary account navigation and a return after choosing Research Statement opened Overview. Conclusions measured the same 1094-pixel width as its panel. The Research Statement uses the same full-width CSS rule; the canonical example has no generated statement, and verification did not launch one.

Browser checks also confirmed the C1 icon selects the SHH claim, full text remains available on focus, and dataset/file icons select the matching source records with their recorded S3 locations. A mouse-selected native dropdown had no outline; Tab navigation restored its 2-pixel keyboard ring. Landing and About layouts fit a 390-pixel viewport without horizontal overflow. The About illustration and official CFDE logo loaded locally. Proof: `.runtime/workflow/issue-7-community-home.png`, `issue-7-about.png`, `issue-7-full-width-conclusions.png`, and `issue-7-circle-icons.png`. No publication changes, research submissions, commits, pushes or QA deployment were performed.

### Draft section spacing

The four disclosure rows (Find more mechanisms, Related DisMech evidence, Additional knowledge graphs and Additional context) share 48-pixel minimum heights, 12-pixel vertical padding and matching dividers. Removed the stacked padding and margin before Additional context and normalized its plus icon's line height. Source: `components/composer.css` and `components/draft-editor.css`. Local browser measurements confirmed all four headers are 48 pixels high, 49 pixels apart including dividers, at 1280- and 390-pixel viewport widths. Additional context still expands to show notes and document attachments. The production frontend build passed; proof: `.runtime/workflow/issue-7-section-spacing.png`. No QA deployment.

### Community leaderboard

The shared navigation now includes **About | Leaderboard**. The user approved both an overall contribution score and separate metric rankings. The [leaderboard design plan](leaderboard-design-plan.md) records the implemented public page and API, researcher contribution/vote/gap measures, scientific-account rankings, dataset reuse, versioned scoring, public attribution, canonical counting rules and exact implementation locations. This remains local development; QA deployment is deferred.

### Interactive About graphics — October 1 refinement

The user rejected the centered CFDE logo and the long About prose. Remove the logo from the shared header and restore its 60-pixel layout, keeping the CFDE Knowledge Center text links. Replace the slide-1 bitmap with an interactive HTML/SVG scientific-method diagram and a second diagram showing the DAPPER/community vision. The original slide asset remains a design reference.

Design: preserve the white background, ink `#20293a`, navy `#183653`, blue `#4a79aa` and fine dividers; use warm `#a45d34` for data in the scientific-method diagram and teal `#397d78` for researcher context. Keep system typography and left-aligned headings. Each diagram is the explanation: labeled relationships and short node titles are visible; selecting a node reveals a concise explanation below. Native buttons provide keyboard and touch access, visible focus and expanded state. Respect reduced motion and fit 390-, 748- and 1280-pixel viewports.

- **Scientific method:** preserve slide 1's triangular loop. Researcher context poses a falsifiable hypothesis in the science layer; predictions are tested against new or existing data; evidence returns to the researcher for evaluation. Agents assist with finding and assembling evidence; the researcher supplies context and judgment.
- **Beyond FAIR:** show a community connecting knowledge gaps to scientific accounts, branching claims, evidence interpretations, provenance and datasets/files. A reuse path returns that work to future questions, with contributor attribution retained. Context describes which questions, methods and assumptions make data relevant. This builds on [FAIR principles](https://www.gofair.foundation/fair-principles), which already include rich metadata and provenance; it does not claim that FAIR excludes them. Source paths are not automatic proof of support. Broader community recognition remains a vision, separate from the proposed leaderboard.

Implementation locations: `src/app/about/page.tsx` and `about.css` for the shortened page; new `components/about/ScientificMethod.tsx` / `scientific-method.css` and `ReusableKnowledge.tsx` / `reusable-knowledge.css` for the diagrams; `Session.tsx`, `session-menu.css`, and `gap-browser.css` for removal of the logo and consistent header sizing. Scope remains local development.

Verification: TypeScript and the production frontend build pass. Local browser checks exercised the three scientific-method layers and all DAPPER explanation types (gap, account, claim, dataset, file, evidence, provenance, reuse and contributor roles). Enter activates a focused control, visible focus is retained, and selecting an open explanation again closes it. Both diagrams were inspected at 1280, 748 and 390 pixels; 748- and 390-pixel pages had no horizontal overflow. Mobile evidence/provenance controls have 44-pixel targets. No raster image remains in the About page; the header logo is absent on About and the landing page, whose text CFDE link remains. No browser warnings or errors were recorded. Proof: `.runtime/workflow/issue-7-about-method-interactive.png`, `issue-7-about-dapper-interactive.png`, and `issue-7-about-mobile.png`. No research jobs, publication changes, commits, pushes or QA deployment.

### Beyond FAIR story — animated About walkthrough

Remove the REVEAL Mechanisms eyebrow and lead with **Beyond FAIR**, followed by the interactive scientific-method diagram. Replace the static provenance graph with one evolving HTML/SVG story:

1. Research context begins at the center.
2. Context informs a knowledge gap; DisMech enters as the source of curated questions.
3. The community votes gaps up or down to prioritize exploration.
4. A researcher chooses a gap and guides an agent to discover claims.
5. Related claims are synthesized into a scientific account, published and ranked by the community.
6. Trace claims through recorded evidence and provenance to datasets and files.
7. Other researchers bring new contexts and discover reusable data, preserving credit for research, data and analysis contributions.

The final scene remains as a complete overview. A seven-step navigator, play/pause and replay make the sequence controllable. Selecting a node pauses playback and opens its explanation. Autoplay begins once the diagram enters view, suspends offscreen or in a hidden tab, and does not catch up through missed steps. Reduced-motion users begin with the full overview and can navigate manually; explicit playback respects their motion preference. Hidden future nodes are excluded from keyboard navigation and accessibility output. A readable story transcript complements the diagram.

The sequence explains discovery and inspection, not the generation of data from claims: source associations are non-directional provenance links. Community votes express priority and reception, not scientific validity. Illustrative publication and voting in the diagram never change actual records.

Code: `src/components/about/ReusableKnowledge.tsx` and `reusable-knowledge.css` own the scene and explanations; `useResearchStory.ts` and `src/lib/research-story.ts` own visibility, motion preferences, playback and manual control. `src/app/about/page.tsx` and `about.css` own the section order. `tests/research-story.test.ts` covers playback boundaries, completion, manual pause, offscreen/background behavior and reduced-motion preferences.

Verification: all 141 frontend tests pass, including eight playback-state tests; TypeScript and the production frontend build pass. Browser checks exercised every stage, automatic advancement, the DisMech entrance, pause/replay, keyboard activation with a visible focus ring, clickable dataset/provenance explanations, the full transcript, and suspension while the diagram is offscreen. The source and final reuse stages fit a 390-pixel viewport without intersecting button hit areas or horizontal overflow. Desktop and the 748-pixel review layout were also inspected. The scientific-method layers remain interactive below Beyond FAIR, and the project eyebrow is gone. Browser logs recorded no warnings or errors. Reduced-motion defaults and preference changes are covered by the playback tests and CSS; system reduced-motion emulation was not available in this browser session. Proof: `.runtime/workflow/issue-7-about-story-desktop.png` and `issue-7-about-story-mobile.png`. This remains local development; no commits, pushes or QA deployment.

### Scientific-method wording refinement

The diagram now says **Uses data to test predictions**, **Poses a knowledge gap**, and **Judges if the evidence actually warrants closing the gap**. The science layer contains **Linked biological concepts**; the data layer contains **Existing data for analysis**. Matching layer explanations and the accessible diagram description use the same framing. Explicit line breaks, a lower compact prediction arc, and a wider mobile judgment label keep the longer text clear of the researcher illustration. Code: `components/about/ScientificMethod.tsx` and `scientific-method.css`. Beyond FAIR remains the first section.

Wording verification: production build and TypeScript checks passed. The 748-pixel review and 390-pixel mobile layouts show all five labels, with the compact prediction arrow and longer judgment text clear of surrounding content. Updated context/data explanations open correctly; no horizontal overflow or browser warnings/errors were observed. Proof: `.runtime/workflow/issue-7-method-wording-review.png` and `issue-7-method-wording-mobile.png`. Local only; QA unchanged.

### Restrained research walkthrough and simpler leaderboard

The earlier moving scene is superseded by a fixed research schematic. Keep the existing white, blue and teal visual language, with small line icons, consistent rectangular nodes and thin connectors. Research context, DisMech, community priorities, researcher-directed agents, claims, accounts and reusable sources retain stable positions throughout the walkthrough. Only node/line emphasis changes, with a short color fade; no characters, flying labels, resizing or layout motion. A single caption carries the explanation. Previous, play/pause, next and full-map controls replace the seven numbered stage buttons. Each node remains selectable, and supporting provenance/credit explanations live in a collapsed transcript. Source relationships remain non-directional and distinct from the research sequence. All nodes stay keyboard-accessible; reduced-motion removes fades and opens the full map. Manual explanations announce politely; automatic playback does not.

Leaderboard presentation is reduced to its heading, three view tabs and relevant results. An empty researcher view says **No researchers yet.** Empty views omit metric controls, descriptions, score explanations and calls to action. Populated tables show rank, name and the selected metric, with other metrics available on request. Existing overall and individual rankings, public-record drilldowns, URL state and loading/error behavior remain; methodology is a single disclosure beneath populated results. The dataset evidence filter stays available when an empty filtered view needs a way back to all evidence.

Refinement verification: production build, TypeScript and all 141 frontend tests pass. Local browser checks confirmed automatic stage advancement with fixed node positions, manual full-map navigation, dataset explanations and keyboard activation of file explanations. The 1280- and 748-pixel desktop/tablet layouts and 390-pixel mobile map were inspected; the mobile map has no overlapping node hit areas or horizontal overflow. Researcher/account leaderboard tabs show concise empty states, and the 390-pixel leaderboard has no horizontal overflow. No browser warnings or errors were recorded. Small caption text contrast was strengthened during review; automatic narration remains silent while selected/manual explanations announce politely. Proof: `.runtime/workflow/issue-7-about-refined.png`, `issue-7-about-refined-mobile.png` and `issue-7-leaderboard-simple-mobile.png`. Local frontend preview only; no publishing, repository commits or remote deployment.

About and Leaderboard now include a matching **← Knowledge gaps** link before the page heading. Both use the existing top-navigation style with a 44-pixel link target, keyboard focus treatment and a direct `/` destination so fresh visits have a reliable route into the public knowledge-gap flow.

Navigation verification: TypeScript and the production build pass. Local browser checks followed the About link with a click and the Leaderboard link with Enter; both returned to `/` and the public knowledge-gap discovery flow. Proof: `.runtime/workflow/issue-7-about-back-link.png`.
