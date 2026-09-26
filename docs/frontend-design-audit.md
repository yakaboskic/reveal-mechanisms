# Frontend design fidelity audit — 26 September 2026

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
