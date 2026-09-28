# Frontend design fidelity audit — 26–28 September 2026

## Current activity behavior — 28 September 2026

The user requested a bounded activity log throughout a job, with automatic tail
following, visible progress across stages, and readable tool activity. This
supersedes the September 26 activity-table entry and verification of unrestricted
completed-history height below. The earlier prototype comparison remains a dated
record of that implementation.

The activity history now uses one bounded scroll region for running and terminal
jobs, including expanded completed history. It follows appended events while the
reader is at the end. Scrolling up pauses following and exposes **Jump to latest**;
that button returns to the end and resumes following. The viewport adapts when the
browser resizes, a delayed question changes the panel position, or a disclosed
tool excerpt changes the content height.

Chronological sections distinguish **Evidence preparation**, **Runtime setup**,
**Research agent**, **Scientific validation**, and **Saving results**. Only the
active section has an animated progress pulse; completed, failed and stopped
sections have explicit states. Incoming stream events advance the presentation
without waiting for the connection to close. Reduced-motion preferences disable
the animation.

Public assistant messages are labeled **Agent update**. Tool calls and their
explicit results are paired by call ID in collapsed invocation disclosures.
Summaries use readable names such as `QueryGraph`, bound the visible arguments,
and shorten long filenames while retaining their recognizable beginning and end.
The recorded result state and duration remain visible where available. Expanding
an invocation reveals the full recorded arguments, raw tool name, call ID and
**Result excerpt**; JSON formatting does not add output that was never recorded.
Opening a tool pauses auto-follow for inspection; **Jump to latest** resumes it.
Repeated identical saved warnings share one card with an occurrence count.
Tool success does not imply scientific validation or account acceptance. Original
event IDs and replay data remain unchanged.

The durable [activity browser regression](../services/frontend/scripts/check-activity.mjs)
passed at 1280×900 and 390×844 with 194 synthetic events per scenario. It checked
bounded long logs, marked text-delta coalescing, append/follow behavior, real wheel
scroll-up pause, jump/resume, paired tool disclosures, long-identifier wrapping,
resizing, delayed question layout, stage transitions, reduced motion, and terminal
validation failure without a success claim. Both scenarios had no page errors or
horizontal overflow. The visible log adjusted from 504 to 401 pixels on desktop
and from 390 to 186 pixels on mobile after the delayed question grew.

The compact-invocation refinement passed two further scenarios at the same
desktop/mobile sizes, with eight synthetic events each. They verified shortened
`Read` filenames, bounded summary arguments, friendly `QueryGraph` and
`GetGraphSchema` names, initially hidden results, full recorded arguments/result
and raw identities after expansion, and pause/resume while inspecting details.
Recorded large-integer and high-precision-decimal digits remained unchanged.
Three identical saved warnings rendered one card labeled **3 occurrences**.
There were no page errors, console errors, duplicate-key warnings, unmocked
requests or horizontal overflow. Screenshots and reports are under
`.runtime/activity-audit/compact-tools/`. Both 194-event long-log scenarios were
rerun with the new disclosure behavior and passed; their current evidence is in
`.runtime/activity-audit/compact-long-logs/`.

A separate replay regression passed after correcting same-job snapshot updates:
an older cached job is replaced by the fetched validation-stage snapshot at
cursor 50, while an earlier research event at cursor 1 replays. Validation keeps
the only active pulse and the older research section stays completed. This case
used one synthetic event, with no page errors or unmocked requests; its report and
screenshot are under `.runtime/activity-audit/replay-ahead/`.

These were **intercepted UI tests**, with a controlled browser SSE stream and all
other API responses mocked. They did not authenticate, write drafts, run research,
count tokens or establish scientific correctness. Local evidence is saved under
`.runtime/activity-audit/` (`checks.json` and desktop/mobile screenshots). Run the
regression against a local frontend with:

```sh
node services/frontend/scripts/check-activity.mjs
```

## Historical prototype comparison — 26 September 2026

The initial implementation was a partial port of the approved HTML design. The
stylesheet loaded correctly; missing fonts or broken CSS imports did not explain
the differences. Functional validation had not established visual fidelity.

## Reference

The assembled `design/index.html` is authoritative, generated from
`design/prototype.html`, `account-view.css/js`, `workspace-view.css/js`, and finally
`design/polish.css`. The final white canvas, native font, blue controls, and quiet
navigation supersede earlier green/serif iterations.

## Findings and corrections

| Area | Drift found | Correction |
| --- | --- | --- |
| Navigation | Added wordmark and 70px header | Avatar-only 60px navigation, prototype icon and menu spacing |
| Discovery | Oversized field, generic hints, ten alphabetical catalog entries, no arrow | Compact field, real-question typing hints, three curated live-source topics, circular search arrow, accurate accessible account counts |
| Selected gap | Expanded to 960px, question 23px, rectangular Go button | Same 800px outer flow / 752px desktop card, 17px question (16px mobile), circular up-arrow with prototype label |
| Mechanisms | Missing hierarchy; search behind plus only; source chips not inspectable | Mechanism heading, inspectable pinned records, Find more / Linked DisMech / Additional graphs disclosures |
| Activity | Borderless question, permanently bounded completion transcript, full embedded account | Retained question card, collapsed completion line, normal completed-history scrolling, compact saved-account preview |
| Account and claim | Direction dropdown; generic record dumps; lost reader state | Relationship filter chips, source/evidence/metric views with disclosed records, persistent filters and claim expansion |
| Statement and workspace | Missing continuity/reading details | Statement progress and record disclosure, export companion note and safe clipboard fallback, workspace topbar/panel/counts and account-name rows |

The port retains the existing API, authentication, draft ownership, source
identities, event replay, evidence provenance, citation revisions, and exports.
Prototype sample counts and illustrative scientific findings were not copied.
Missing account names, relationship fields, or mechanism records remain explicit
rather than being inferred from generated prose.

## Verification

- Compared the assembled prototype and running app in the browser. At
  1280×900, selected cards have matching x=264, width=752, y=123 and 17px
  question text. Different source-backed anchors legitimately change card height.
- Checked 390px mobile composer, account, claim, statement, and workspace layouts;
  document width remains 390px without horizontal overflow. All four claim tabs fit.
- Checked search arrow/Enter/ArrowDown, ArrowUp back to search, clear-gap focus,
  source modal, mechanism picker, and disclosure behavior. Fixed stale results
  during query changes and cached anchor records from a different source revision.
- Verified account-to-claim return preserves reading state. Filtering an expanded
  claim out clears its expansion; clearing the filter does not reopen it.
- Verified the real completed analysis uses a compact account preview. Expanded
  history has visible overflow and no maximum height; technical run notices stay
  inside that history disclosure.
- Inspected the real saved statement and all four export controls. No new paid
  analysis was launched for this visual audit.
- TypeScript and all 9 frontend tests pass. An isolated production build passes
  with the final source/configuration, without replacing the running dev output.

The real saved account is intentionally less elaborate than the illustrative
prototype: its generated title and structured relationship/anchor fields are
absent. This audit corrects presentation without fabricating those fields.

## Mechanism labels and responsive search

Anchor chips and mechanism-picker rows now show the EAGGL mechanism label (for
example, “Beta Cell Dysfunction and Diabetes”). The trait/factor identifier stays
in the tooltip and source record; selections retain their exact native IDs and
source revisions.

The picker shows lexical label matches after a short debounce, then incorporates
semantic matches once typing settles. Loading is explicit, obsolete requests are
cancelled, and late responses cannot replace a newer query or gap. Draft autosave
waits for automatic suggestions to finish, avoiding a redundant empty-anchor
write while the suggestion request is using Aurora. Browser-local recovery
continues throughout.

An isolated browser regression checked desktop and 390px mobile layouts, labels,
identifier tooltips, early lexical results, loading feedback, clearing a pending
suggestion, and authenticated autosave after suggestions. It used a real catalog
record with intercepted suggestion/authentication/write responses; it did not
launch research jobs or mutate user drafts.
