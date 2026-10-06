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
**Research agent**, **Checking account and sources**, and **Saving results**. Only the
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
Tool success does not imply that final account and source checks have passed. Original
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

## Account loading — 28 September 2026

The account route now shows the reading-page navigation, an **Opening scientific
account** status panel, and a decorative skeleton shaped like the account's
question, mechanisms, tabs, conclusions and claims. Placeholder shapes are hidden
from assistive technology and do not invent scientific content. After eight
seconds the status becomes **Still loading your account**. Reduced motion disables
the progress and skeleton animation.

A failed account read replaces the skeleton and pulse with **Couldn’t open this
account**, the actual error and **Retry**. The account request has a 30-second
deadline; Retry reads the same account again. A late response after the deadline
cannot overwrite the error. Successful loading replaces the shell with the saved
account and its existing conclusions/statement controls.

The [account-loading browser regression](../services/frontend/scripts/check-account-loading.mjs)
passed five mocked scenarios: delayed loading at 1280×900, delayed loading with
reduced motion at 390×844, an explicit service error followed by successful Retry,
a mobile timeout followed by a late response and successful Retry, and a failed
background status refresh on an already loaded account. Browser
clock advancement tested the eight- and thirty-second boundaries without those
real waits. It verified immediate loading/navigation, exact contract-fixture
account rendering, one new read per Retry, hidden decorative placeholders,
unchanged account path and no horizontal overflow, page errors, console errors
or unmatched API requests. Desktop opening, mobile slow-loading and mobile timeout
screenshots were visually inspected.

An account with a running research-statement job remained readable while its next
status poll stalled and hit the deadline. The refresh error offered **Retry**;
the existing conclusions stayed visible during that retry, and a successful final
status cleared the error and stopped polling. The case had no page/console errors
or unmatched requests. Its separate report and screenshots are under
`.runtime/account-loading-audit/polling/`.

These checks intercepted every account/API response and created no jobs or
database writes; they do not measure live backend latency. Reports and screenshots
are under `.runtime/account-loading-audit/`. Run with:

```sh
node services/frontend/scripts/check-account-loading.mjs
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


## Research Statement activity — 28 September 2026

The account's **Research Statement** tab now follows its existing paragraph
`job_id`. It shows elapsed time and the shared bounded activity stream, with
paragraph-specific preparation, setup, writing, claim/citation checking and
saving labels. Public agent updates and compact tool invocations remain
inspectable while generation runs. Completion loads the saved paragraph and its
citation rendering automatically. Scientific checks still precede acceptance.

Stopping remains visible until the job reaches a terminal state. An explicit
retry follows the returned replacement job immediately, preserving its request
key after an uncertain submission response. A late response from an older job
cannot replace the current attempt. Opening an already saved statement does not
fetch its completed job/history until **View statement activity** is clicked.

The [paragraph-activity browser regression](../services/frontend/scripts/check-paragraph-activity.mjs)
passed six mocked scenarios: desktop/mobile live completion with 33 synthetic
events each and bounded history; mobile cancellation/retry; failed submission
retry with the same key and a delayed old-job response; deferred saved-statement
telemetry; and expired-cursor recovery when the immediate job lookup also fails.
The recovery case used a browser clock for reconnect backoff, then verified a
successful job read and automatic paragraph rendering. All scenarios had no page
or console errors, unexpected API requests, or horizontal overflow. Desktop and
mobile live-activity screenshots were visually inspected. Evidence is under
`.runtime/paragraph-activity-audit/browser/`, with separate `deferred/` and
`recovery/` reports. Every browser account/job/event/render response and
submission/cancellation request was mocked; no real jobs or writes were made.

A separate read-only investigation of the latest saved paragraph,
`28efa60a-d9dd-4480-bb4b-7a0d8ac059e1`, found **165.082 seconds** from creation to
acceptance on September 28. Its recorded timeline was:

| Interval | Seconds |
|---|---:|
| Job created to worker started | 19.345 |
| Worker started to remote authoring started | 46.713 |
| Remote authoring | 46.315 |
| Remote completion to validation-stage event | 31.559 |
| Validation-stage event to faithfulness check completed | 11.380 |
| Faithfulness check to accepted paragraph | 9.770 |

This is one observed run, using durable event, provider and review timestamps;
it is not a controlled performance benchmark. About 119 seconds occurred outside
remote authoring. The frozen paragraph input was 8,504 bytes. Setup, result
capture, validation and persistence therefore explain material portions of the
visible wait; the new telemetry exposes their recorded stages rather than
promising faster generation. The sanitized read-only audit is
`.runtime/paragraph-activity-audit/real-paragraph-timing.json`; it contains stage
metadata for 78 public events and no credentials or source prose.


## Shared public loading surfaces — 28 September 2026

Workspace lists, scientific claims and generic records, featured questions,
knowledge-gap search, manual mechanism search and automatic anchors now use
consistent loading surfaces in the existing account-loading visual language.
The shared component provides contextual messages, restrained skeletons and a
three-dot pulse. Skeletons are hidden from assistive technology; status messages
are announced politely, errors use alerts, and reduced-motion preferences disable
animation. There are no invented percentages or completion estimates. The
existing submission progress screen remains intact.

Workspace distinguishes an unresolved response from a genuinely empty list and
uses a smaller status panel when refreshing existing results. Claim and record
failures replace skeletons with a readable error and Retry. Generic record
responses are bound to their requested identity so a late response cannot display
under a different record URL. Composer provides separate feedback for restoring
saved state, retrieving featured questions, searching and retrieving anchors.
The same surface is used by the account preview and paragraph-loading branches.

The [loading-surface browser regression](../services/frontend/scripts/check-loading-surfaces.mjs)
passed eight scenarios at 1280 × 900 and 390 × 844: workspace pending/empty/tab
change/content, claim error/retry, generic record error/retry, and Composer
featured/search/automatic-anchor loading. The checks verified no premature empty
state, no horizontal overflow, no page or console errors, visible retries,
decorative skeleton accessibility and disabled animation under reduced motion.
Desktop workspace and mobile search/record screenshots were visually inspected.
All API calls were replaced by fixtures in the browser, and unexpected API or
external requests were blocked; no saved research or real jobs were accessed.
Results and screenshots are in `.runtime/loading-surfaces-audit/`, including
`checks.json`. Frontend type checking and all 27 unit tests passed before this
browser gate. Route fallback components are included; the browser scenarios
exercise the corresponding complete routes and client loading/error states.

## Ranked knowledge gaps and proposed accounts — 28 September 2026

Trending now preserves the API's account-count ranking and displays the first
three distinct exact gap identities. The former editorial topic searches no
longer reorder this list. Ranking and count labels reflect the signed-in
workspace: saved accounts have no public publication model, so a visitor sees
zero private-account counts. Changing identity refreshes ranking and clears the
previous principal's results before new responses arrive.

Selecting a gap shows its authorized proposed scientific accounts beneath the
question. Each card uses the saved account title and closing remarks, claim
count, creation date and original ResearchRequest attribution; transferring
workspace ownership does not substitute the current owner as author. Missing
attribution is explicit. Account links open their exact saved identity. The list
is paginated, deduplicates page boundaries and retains existing cards during a
page retry. Its count shows loaded records with `+` while more pages remain;
it does not reuse potentially stale account counts from a browser-stored gap.
The exact selected gap and source revision are sent to the list endpoint.
Mismatched gap records and late responses from an old selection are not rendered.

The [gap-account browser regression](../services/frontend/scripts/check-gap-accounts.mjs)
passed seven mocked scenarios: ranked top three without editorial requests;
author/title/synthesis/count/date/link rendering; pagination with a failed page
and safe retry; 390px layout, slow feedback and reduced motion; visitor privacy;
late old-gap responses; changed identity; mismatched exact-gap response rejection;
and a 30-second deadline followed by a late response and successful retry. Some
scenarios cover several related assertions. Existing API request deadlines are
also applied to the trending read. All API responses, session transitions and
autosaves in these checks were intercepted. No real backend reads, writes,
research jobs or model calls were made. The six primary scenarios are recorded in
`.runtime/gap-accounts-audit/checks.json`; the additional deadline case is in
`.runtime/gap-accounts-audit/deadline/checks.json`. Desktop and mobile screenshots
were inspected with no horizontal overflow; all scenarios had zero unexpected
requests, page errors and console errors.

Frontend type checking and 35 unit tests passed, including server-order and
source-object preservation. Regenerated OpenAPI, flow viewer, TypeScript client
and contract fixtures include the new optional-auth gap-account endpoint and
optional original attribution. Contract validation passed 32 operations,
180 response examples and 43 exchanges. These browser checks establish UI
behavior with controlled fixtures; backend authorization/count/pagination tests
provide the separate data-access gate.

## Timed activity and immediate job recovery — 28 September 2026

Every recorded analysis/statement stage shows elapsed time while active and a
frozen duration once it finishes. Sequential worker messages use amber heartbeat
dots during work and static green dots at the next recorded step; failed and
stopped outcomes remain distinct. Tool calls use their own paired result timing.
Partial replay without a recorded boundary shows an em dash rather than inventing
a duration. The vertical bar loader is restored across activity and loading
surfaces, with motion disabled for reduced-motion preferences.

Job links render a retrieval surface on the server, before identity resolution.
Job status and saved selection load independently, so a slow source read cannot
hold back activity. Cached selections must match explicit job/draft/gap links.
Late restoration cannot clear the requested job or overwrite a newer user action.
The worker records a durable saving-stage boundary only after scientific checks,
while retaining cancellation and lease fencing.

Five timing unit tests and seven intercepted activity browser scenarios passed,
covering advancing/frozen timers, failures, partial replay, parallel tool calls,
amber/green states, desktop/mobile layout, reduced motion, tail following and
job-before-gap restoration. The final delayed-gap scenario also tests a cold
`?job&gap` link without a draft. Existing loading and eight selected submission
scenarios passed after adding mocks for the new independent account-list read.
The isolated release frontend passed 28 unit tests and its production build;
unrelated local admin changes were excluded from that build. Runtime verification
checks exact deployed file hashes; live read-only discovery confirmed one saved
account for the user's T2D gap and no private counts in public browsing.

## Public publication and infinite gap browsing — 28 September 2026

This section supersedes the earlier same-day owner-only account-count scope and
first-three trending behavior. The default selector is now **Trending knowledge
gaps**, ranked by explicitly published accounts across researchers. **Top
questions in your workspace** selects authenticated saved-account counts. The
selected question's account list follows the same scope and preserves original
authorship. Existing accounts stay private until an owner chooses to publish.

The question box and scope selector remain stationary while the remaining-height
gap list scrolls. Pages load through an IntersectionObserver rooted in that list,
with a visible Load more questions fallback. The UI preserves the backend order
and opaque cursor, including stable randomized tie ordering within one browse.
Expired gap/account cursors reset to the first page. Scope and identity changes
clear previous records; account scientific reads also bind loaded state to the
principal, preventing owner controls or private statements from surviving a
workspace change.

Scientific accounts now have explicit owner publication controls. Their disclosure
names the account, claims, original author attribution, evidence/provenance and
currently accepted statement/citations as publicly viewable and downloadable.
Publication freezes that snapshot. Later statements remain private until an
explicit update; private job activity and research write controls require
`can_manage=true`. Unknown publication responses retry with the same version,
body and key; conflicts refresh metadata before a new choice. Further behavior
and exact API operations are documented in [account-publication.md](account-publication.md).

The expanded gap browser harness passed 10 scenarios, plus two focused
cursor-expiry/fallback cases; the publication harness passed four initial
scenarios plus a focused update/version-conflict case. Evidence is in
`.runtime/gap-accounts-audit/` (including `expiry/`) and
`.runtime/publication-audit/` (including `update/`). A separate public-reader
subset additionally follows a statement export and exact claim Evidence and
Provenance routes without login; its report is in `publication-audit/reader/`.
Desktop and 390px screenshots were inspected. All requests and session changes
were intercepted; no live publishing, backend writes or research/model jobs were
performed. Assertions found no unexpected requests, page errors, console errors
or horizontal overflow. The expanded tests supersede the earlier seven-case
ranking report.

Frontend type checking and 35 unit tests passed. The regenerated contract/client,
fixtures and API viewer validate 34 operations, 193 response examples and 46
exchanges. These UI gates do not substitute for the backend's separate
publication authorization and immutable-snapshot tests.
