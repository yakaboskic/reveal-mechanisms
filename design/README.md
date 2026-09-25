# Frontend design study 01

Scope: a local HTML simulation for reviewing the REVEAL interaction and data contract. No application server, login, model invocation, database write, or research publication runs here.

## Current account example — multiple biological claims

Open [the account preview](http://127.0.0.1:8767/#example). The [YAML packet](data/cad-account/scientific-account.yaml) now contains 12 biological Claims and 16 EvidenceItems around three genes, two gene sets and the CAD-in-T2D Factor1 mechanism. It uses an actual DisMech CAD gap as its framing object. The two gene-set membership claims and KG assertions are explicitly illustrative; the numerical CFDE observations are captured. See the [packet guide](data/cad-account/README.md).

The account opens on **Conclusions** (the ScientificAccount closing remarks). A **Research statement** switch reveals the rendered DAPPER Paragraph, numbered citations and a references section. **Associated claims** is collapsed by default; opening it shows the compact list with text search and relationship filter chips. No claim opens automatically. Cards begin with the proposition text; selecting one expands an assessment, evidence-source summary and **Open claim page** link directly below it. Selecting it again collapses it; opening another collapses the previous card. Filtering out an expanded claim closes it. Each `#claim/<encoded DAPPER ID>` page is directly addressable and contains the assessment, proposition, evidence and provenance. Returning to the account preserves the search and expansion.

Paragraph preparation starts automatically when the account appears, including on the job result card. The HTML simulates an independent background task; changing views does not restart it and conclusions stay readable throughout. No agent or API request is sent. The example disclosure contains **Replay paragraph preparation** for reviewing the loading state. A future live client would receive each account's paragraph status independently.

The authored [Paragraph object](data/cad-account/paragraph-object.json) has 19 citation occurrences resolving to 13 unique target/revision pairs: the exact KnowledgeGap and all 12 biological Claims. Shared spans render once, and repeated references reuse the same number. Its identity and citations validate against the pinned DAPPER schema. [The JSON packet](data/cad-account/paragraph-packet.json) bundles the Paragraph with its citation metadata records; [the full DAPPER document](data/cad-account/paragraph.json) also retains the supporting scientific and provenance objects.

The research view offers **Copy for Word** with HTML and plain-text clipboard formats, including the complete paragraph, numbered citations and hyperlinked references. Use **Keep Source Formatting** when pasting. It falls back to selected rich text if automatic clipboard access is unavailable. Downloads include [Markdown with references](data/cad-account/research-statement.md), [LaTeX](data/cad-account/research-statement.tex), [BibTeX](data/cad-account/references.bib), and the JSON packet. LaTeX uses `\cite{…}` and `\bibliography{references}`; a visible reminder asks the user to download `references.bib` alongside the `.tex` file. Compile with pdfLaTeX → BibTeX → pdfLaTeX twice (or latexmk). Exports use the same Paragraph and pinned registry records as the UI. They are numbered DAPPER references, not an APA/MLA rendering. The records, local resolver links, dates and example byline remain unpublished design fixtures; no DOI or real agent execution is asserted. Illustrative KG membership stays labeled in the paragraph and referenced Claims.

The same account appears after simulated job completion; when a different gap was selected it remains labeled as a reference account for a different gap. The current API handoff now uses this same account/gap/Paragraph fixture. Historical iteration notes below describe earlier versions and are superseded by the [v12 stage map](../docs/design-plan.md#4-user-journey--endpoints--schemas--implementation-assets). The existing standalone HTML is a visual simulation, not a live API client.

UI sources: `prototype.html`, `account-view.js/css`, `workspace-view.js/css`, and the shared `polish.css` finishing layer, bundled by `build_prototype.py`. The layout keeps the approved white background, neutral text and blue controls. It prioritizes reading the account, with claims available on demand. Inline expansion uses the same full-width layout on narrow screens; deeper inspection has its own claim page. The Paragraph JSON download sits in the example/record disclosure; Word, Markdown, LaTeX and BibTeX remain beside the research statement.

## Current visual system — September 25 polish

Keep the question as the entry point, the closing remarks as the result, and deeper claims/evidence available on demand. White `#ffffff`, ink `#20293a`, reading text `#4d5e76`, secondary text `#69798e`, boundaries `#e3e9f1`, blue actions `#2563eb`. Use the native sans-serif stack; reserve monospace for source records and agent tool activity. Accounts, claims and the workspace share a 960px outer column with 80-character reading measures. The question/activity flow stays narrower. Type follows content hierarchy: 19–21px question/proposition, 18px account title, 14px body, 12–13px controls and secondary metadata.

The pass aligns the formerly offset account question with its conclusions/tabs, increases secondary-text legibility, standardizes disclosures/focus/hover states, simplifies sign-in copy and puts technical JSON exports in the existing disclosure. Scientific text, IDs, source values, and uncertainty labels are preserved. Completed activity returns to ordinary page scrolling; live activity remains bounded. Mobile layouts wrap source paths and chips, preserve the four claim tabs, and keep long provenance records inside the viewport. Reduced motion and keyboard tab navigation are retained. No new app services or scientific objects are introduced.

## Original design direction (historical)

- **Purpose:** help a researcher move from a precise question to inspectable evidence, keeping the question visible throughout.
- **Palette:** paper `#f7f6f2`, white `#ffffff`, ink `#233630`, action green `#2d5c4f`, muted `#68736b`, amber `#986c35`. Hairline borders; no gradients or decorative dashboard tiles.
- **Type:** Georgia for inquiry/account headings; the native sans-serif stack for controls and reading; monospace only for source IDs and records.
- **Spacing:** 4/8/12/16/24/32/48/64 px. Content up to 1160 px; homepage composer 760 px. 44 px minimum primary targets.
- **Shape:** modest 8 px corners. Chips distinguish source types by text as well as color. Details use a side inspector, full width on small screens.
- **Motion:** submission folds the question card upward and starts scripted activity automatically. Events append below the question; reduced motion disables movement. No elapsed-time, token or cost measurements are fabricated.

```
REVEAL                                             session
                 inquiry / activity / accounts

Home:       a single question composer + search results
Context:    inquiry + rationale     | attached context / EAGGL
Activity:   short event narrative   | captured graph / coverage
Account:    reading column          | selected claim / proposition
Provenance: GeneSet → collection    | selected object / source file
```

**Design critique:** a metric dashboard would distract from the scientific question, and a force-directed hairball would make provenance hard to follow. Use an ordered evidence trail and a small, labeled graph. Full records remain available on demand. Keep the homepage quiet. Following the second visual review, the homepage has only a question box and a curated preview of trending gaps; no logo, sign-in button, study banner or footer. Review-only views are reached by direct URLs.

## Run and share

From the repository root:

```bash
python3 -m http.server 8767 --bind 127.0.0.1 --directory design
```

Open http://127.0.0.1:8767/ . Share `index.html` as a standalone HTML design; its styles, scripts and sample data are embedded. The companion README and [contract review](contract-review.md) explain the boundaries. Serving it over HTTP is the tested path.

For the account's YAML and source-download links, share/serve the entire `design` directory including `data/cad-account`. Inspection content is embedded in the HTML; downloads use those companion files.

Source: `prototype.html` and `build_prototype.py`; regenerate with a Python environment containing PyYAML and the existing DAPPER/LinkML dependencies:

```bash
python3 design/build_prototype.py
```

The build uses frozen repository data. Only refreshing the HuBMAP snapshot requires the sibling DAPPER export; its source path and checksum are recorded in `data/manifest.json`.

## Walkthroughs

1. Search for **AIP**, **diabetes**, **Alzheimer**, or a typo such as **alzhiemer**. Select a DisMech gap, inspect its source rationale and attachments, and edit the five EAGGL defaults. Their ranking is explicitly illustrative.
2. Open `/#example` to inspect the existing saved account example. It is an inspection route, not a new Question submission path. Its account is an authored contract fixture grounded in an actual CADinT2D graph capture. No agent ran.
3. Inspect a claim, its Proposition, EvidenceItems, captured source files and catalog GeneSet. Unknown membership/construction history stays unknown.
4. Open `/#hubmap` for the separate corrected 358-set bundle. Select a GeneSet and trace its collection, GMT representation, processing activities and recorded source files. It is not linked as evidence for the CADinT2D account.

Selected DisMech gaps exercise context selection, identity choice and automatic job playback. At the user’s request, playback finishes with the existing ScientificAccount example, explicitly labeled when unrelated to the selected gap. Workspace history and last anchor selections now survive same-tab reloads in browser storage; agent playback remains an in-memory simulation. No server drafts are saved.

## Iteration 02 — minimal landing page

Removed the logo, persistent sign-in button, study banner, study number and footer links at the user’s request. The question box now reads “What would you like to understand?” Trending gaps are labeled **Curated preview**; no usage-based popularity ranking is claimed. Sign-in/anonymous continuation stays at submission. Direct review routes: `/#example` (captured question), `/#hubmap` (provenance), and `/#notes` (contract notes).

## Iteration 03 — one question screen

User-directed design change: white canvas, native sans-serif type, compact rounded composer, neutral ink with blue actions. Replace the warm paper/green/serif direction. Keep selection and context on the same question screen: the exact natural-language question fills the composer, with DisMech mechanism chips and EAGGL anchor chips directly below. Rationale, source details and KG settings are progressive disclosures. Trending entries display full source questions and explicitly sample counts of user-generated scientific accounts. Counts are allowed mock application metadata, not scientific records or observed usage.

Layout: `question field → source detail action → DisMech chips → EAGGL chips → Go`, within one centered composer. The empty state is just the small question field and trending questions. No separate two-column compose screen.

## Iteration 04 — DisMech-gap-only interaction

The tool searches existing DisMech knowledge gaps only. Fuzzy matching tolerates typos and partial terms; no result means search again, not create a question. The idle search hint types/backspaces randomly selected real gap questions, pauses on focus/input/page hiding, and respects reduced motion. Examples are decorative and never populate the actual input value.

Selection fixes the source question on the same screen. EAGGL anchors remain editable. Linked DisMech mechanisms are read-only in a collapsed disclosure beside Find more mechanisms and Additional knowledge graphs. The subtle prompt is “Anchor on possible genetic mechanisms to explore evidence for answering this gap.” The earlier free-text composer behavior is superseded. `/#example` retains read-only inspection of the earlier account fixture, not a backdoor to new free-text analysis jobs.

### Latest wording and context behavior

Use **Find more mechanisms**, **Linked DisMech mechanisms**, and “Anchor on possible genetic mechanisms to explore evidence for answering this gap.” Linked DisMech mechanisms can be inspected, but not unchecked/unpinned. User-editable EAGGL anchors are possible mechanistic leads, not established causal mechanisms. The goal is evidence about how to address the gap, not evidence that the gap itself exists.

### Shared arrow and card transition

Search and the selected gap card use the same circular up-arrow control. In search, the arrow reveals/focuses matching DisMech results; it cannot submit a new question. After gap selection, the arrow opens the existing identity/analysis flow and remains disabled without EAGGL anchors. The card morphs between search and selected-gap states, with a short fade fallback and reduced-motion handling.

The user-facing anchor heading is **Mechanism anchors**. Add-anchor controls and the empty-selection prompt use the same wording; source records and API payloads retain EAGGL/CFDE identifiers.

The selected-gap submit area contains only the circular up arrow, without helper text or a divider line.

Trending gaps hide as soon as search text is entered and return when the input is cleared. Animated hint text does not hide them.

### Search field refinement

The input uses the card’s rounded outer border for its focus indicator, with a light blue halo, instead of a rectangular outline inside the box. Search text, animated examples, and the selected question use 17 px type on desktop and 16 px on mobile. Keyboard focus remains visible, including in forced-colors mode.

Above the search box, a small muted invitation reads “Help us close these knowledge gaps.” The linked words “knowledge gaps” open the DisMech discussions browser in a new tab, preserving the current search. The invitation is absent once a gap is selected.

After selecting a gap, the submit control pairs “Let’s close this gap” with the existing circular up arrow. Both form one button and keep the existing identity choice and minimum-anchor requirement.

## Iteration 05 — question → anchors → activity

Keep the white background, native sans-serif, neutral ink `#20293a`, secondary text `#8190a3`, borders `#e0e5ec`, and blue `#2563eb` already approved for the question screen. The job is one column at the same width. The same question card contracts upward, keeping its exact question and selected anchor chips. Controls and disclosures disappear; a quiet activity panel unfolds below. There is no separate hero, step navigator, graph sidebar, or account-construction explainer.

```text
[ Exact selected knowledge gap                  ]
[ read-only mechanism chips                     ]

Agent activity                              Stop
Simulated activity
◌ Current action
    CFDE expansion → nodes/edges → evidence package
    Agent start → tool/activity messages → validation
```

Submission starts playback immediately after the existing identity choice (or immediately for a simulated session). The history appends one entry at a time. It follows new entries only while the reader is near the bottom; scrolling up preserves their place. The question and controls remain above the independently scrolling history. The Stop button transitions through “Stopping…” to “Stopped,” preserves the gap and chips, and prevents subsequent steps. Replay and return-to-question actions are available after stopping/finishing. A paused preview restored by browser navigation offers Resume.

Graph counts are computed from distinct IDs in the real reference CFDE capture: **9 nodes and 8 edges**. The log explicitly identifies them as reference counts with unverified coverage for the current gap. They are neither newly retrieved nor measurements of a five-anchor expansion. “About this preview” identifies their source and the simulated activity. Public progress text illustrates tool actions and short summaries; it contains no private model reasoning. Iteration 06 below supersedes the earlier missing-evidence terminal preview: the saved account is now shown as a clearly labeled example at the end of the flow. No data-level link to the selected gap is asserted.

The playback objects are presentation fixtures, **not** new valid `JobEvent` wire payloads. The machine-readable API is unchanged. Proposed telemetry and lifecycle semantics are recorded in [the contract review](contract-review.md).

## Iteration 06 — agent workspace and rendered account

Keep the first three infrastructure steps as a small preparation checklist: expand CFDE, report captured node/edge counts, prepare the evidence package. Collapse that checklist to an inspectable “Evidence preparation · Ready” row when the agent starts.

Agent work has a different visual language: an animated working indicator, a current-task summary, a compact tool feed (Start / Read / Inspect / Query / Draft / Validate), and an in-progress ScientificAccount excerpt. Agent messages do not acquire checkmarks. The working state stops on cancellation or completion, and reduced motion disables animation. Both the preparation details and agent history remain inspectable.

On completion, render **one existing example ScientificAccount** directly beneath the activity, with its real source question, component claim and closing remarks. The user explicitly authorized using an account unrelated to the selected gap for this UI iteration. Mark it “Example” and “unrelated to the selected gap”; do not rewrite its question, IDs, evidence or attribution. It is a successful *preview outcome*, not a live inference result or a newly saved account. Clicking Inspect account, Proposition, Evidence or Provenance opens the existing full inspector. Back to activity preserves the original selected gap, anchor chips and completed playback. Paragraph/citation inspection remains available in the account view.

The API contract and production relevance/evidence requirements remain unchanged. This revision allows an explicit fixture transition for UI review; it does not relax the scientific validation rules of real jobs.

## Iteration 07 — transcript-style research activity

Evidence preparation remains the first stage with completion marks. A single three-bar pulse in the active stage header replaces all rotating per-row circles. The same indicator is reused in the research-agent header; preparation stops animating when the agent starts. Stop and terminal states halt that indicator. Reduced motion disables it.

The research-agent surface now follows the supplied transcript reference: a quiet gray panel, monospaced public messages, concrete tool names with arguments, colored status dots, and short result lines. Entries arrive in order and keep their natural length instead of becoming standardized task tiles or badges. There is no duplicated high-level current-task summary and no draft/example account inside the running transcript. The finished ScientificAccount renders below the activity only after completion.

All tool calls are **scripted presentation examples**, not executed actions or confirmed MCP tool schemas. `Read`, `Write`, `Validate`, and `QueryKnowledgeGraphs` illustrate adapter-level operations; the evidence-package path is a proposed workspace path. Calls do not execute. Graph counts, GeneSet/ScientificAccount IDs, and final account content still come from captured fixtures. Public action statements and tool results are shown; private model reasoning is not part of the event contract. Existing final Example labeling and source-question provenance stay intact.

## Iteration 08 — finished activity becomes one line

Successful playback hides the activity heading, simulation subtitle, preparation row and research-agent panel together behind a single “Gap analysis complete” disclosure. Scientific accounts appear immediately below it. Expanding the disclosure reveals the preparation and transcript history; collapsing it restores the results-first layout. The question and anchor chips stay above both. Completion scrolls the result area to its beginning rather than leaving it at the end of the transcript. Stopped jobs keep their distinct stopped state.

## Personal workspace — avatar and history

Keep the approved white canvas, native sans-serif, ink `#20293a`, secondary `#8190a3`, borders `#e0e5ec` and blue `#2563eb`. A 36px avatar sits in the upper-right. Its popover leads to **Your knowledge gaps** and **Your scientific accounts**, with an anonymous sign-in prompt. The dashboard uses the same line-integrated tabs as the account page. Rows show natural-language questions or account conclusions, without a tile grid or separate jobs section.

```text
                                                    (avatar)
Your workspace
[ Anonymous workspace                 Sign in to keep your work ]
Knowledge gaps  2    Scientific accounts  1
───────────────────────────────────────────────────────────────
The full knowledge gap question / account conclusion
```

Routes: `#dashboard/gaps` and `#dashboard/accounts`. Anonymous exploration gets a temporary local ID without requiring submission. Gap selections and last anchors are retained. Viewing the account adds its exact CAD gap and the labeled example account; simulated completion deduplicates that same account and never associates it with an unrelated selected gap. Empty lists offer exploration actions.

Only design history is cached: anonymous in `sessionStorage`, simulated signed-in profiles in `localStorage`. The existing ORCID/Google preview dialog preserves history; sign-out starts a new anonymous workspace. Selecting the same simulated provider again restores its browser-local history. No OAuth/DB/API call occurs. Production registered history comes from owner-authorized backend listings. Anonymous access may be lost with its session; scientific record retention is separate. See [authentication](../docs/authentication.md#10-avatar-and-personal-dashboard).
