# Community leaderboard

Local implementation · October 1, 2026 · follow-up to [issue #7](https://github.com/yakaboskic/reveal-mechanisms/issues/7)

This extends the [UI design plan](issue-7-ui-design-plan.md). The user approved **About | Leaderboard** in the top-left navigation, rankings for researchers and datasets, and **both an overall score and separate metric rankings**. The first version is implemented locally on `codex/issue-7-ui-improvements`. The contribution formula is versioned as `public-contribution-v1`; it is not a scientific evaluation method. Deployment to QA remains a separate step.

## Purpose and page

Recognize people who contribute useful public research and make the reuse of biomedical datasets visible. Reward breadth, community response and contributions that other researchers can inspect. Keep the raw measures visible so an overall rank is explainable.

Use the existing typography, white background, blue links and fine dividers. Add a `/leaderboard` page, reachable from the shared header. Keep returning to Knowledge gaps available from the page itself. The decorative navigation separator is hidden from assistive technology; each link has an active state.

```text
About | Leaderboard                                  Workspace

Leaderboard
Recognizing the researchers and data behind our shared progress.

Researchers        Scientific accounts        Datasets

Rank by: Overall contribution                 All time

Rank  Researcher       Score  Public accounts  Gaps covered  Community votes
...

How rankings work
```

Use a quiet ranked table, not a wall of score cards. On narrow screens, retain rank, name and selected metric; expose the other measures beneath each row. Sort controls update the URL. Opening a metric explanation shows its definition, included records, observation time and score version. Each number links to the public records that contribute to it, rather than an inaccessible private workspace. No invented names or counts in production; a new community gets an honest empty state.

## Researcher rankings

| Ranking | Exact measure | Why it belongs |
| --- | --- | --- |
| Overall contribution | Proposed blend of public accounts, community votes and distinct gaps covered, below | A starting overview with an inspectable breakdown |
| Most scientific accounts | Count distinct currently public, accepted scientific-account IDs attributed to the researcher | Contribution volume |
| Community recognition | Sum upvotes minus downvotes across those accounts; exclude the researcher's own ballots | How the community responds to their contributions |
| Broadest gap coverage | Count distinct canonical knowledge-gap IDs represented by their public accounts | Breadth without rewarding repeated runs on one question |
| Gaps explored | Count distinct gaps with either a public account or a published exploration by that researcher | Recognize useful evidence searches even when no account can be supported |

Show both **gaps covered by accounts** and **gaps explored**. The latter includes published insufficient-evidence findings; opening a draft or starting a job does not count. These are contributions toward a question, not proof that a gap has been closed. Do not label a ranking “gaps closed” until the product has an explicit scientific resolution/review model.

### Proposed overall score

Start with **30% published accounts + 40% community recognition + 30% gap coverage**. Use percentile components among eligible contributors so accounts, votes and gaps can be combined without raw vote volume overwhelming the other units. Every row exposes the raw numbers and the three component scores.

An implementation-ready initial definition is:

- Eligible contributors have at least one qualifying public account or published exploration; absence of a measure is zero.
- For each metric `x`, define `P(x) = 0` when `x <= 0`. Otherwise, `P(x) = 100 × (number of eligible contributors below x + 0.5 × number tied at x) / number of eligible contributors`. Equal values receive equal points. A sole contributor's nonzero component receives 50, with the small cohort disclosed; zero votes earn zero recognition points.
- `overall = 0.30 × P(account_count) + 0.40 × P(net_community_votes) + 0.30 × P(account_gap_count)`.
- Display one decimal place; rank by the unrounded score. Exact score ties share rank; an opaque stable contributor key provides deterministic display ordering only.
- Preserve negative net votes in the separate votes ranking and displayed counts; their overall recognition component is zero.
- Version the formula and return the observation time and cohort size. Explain that the score is relative to the current community, so it can change when other researchers contribute.

This is a **contribution score**, not scientific validity, expertise or a calibrated quality rating. Cumulative votes also tend to favor prolific contributors; the separate account ranking lets visitors inspect the strongest individual accounts. During a small pilot, show a short “Early community rankings” note and the number of voting participants. Do not silently change weights to make early rankings look better.

Public explorations earn visibility in their own ranking and in the “gaps explored” column. A later revision could include reviewed explorations in the overall score; do not inflate it through repeated insufficient-evidence runs.

## Scientific-account rankings

- **Highest rated:** net votes on each currently public canonical account, with upvotes, downvotes and voter count visible. Treat this as community reception. An account with no votes is unrated, not negatively rated.
- **Most reused:** distinct later public accounts that explicitly use a component claim from this account as evidence. This is a follow-up metric requiring attribution of shared claims to their originating accounts; do not infer reuse from similar prose or shared datasets.
- **Recently contributed:** first-publication order when durable first-publication history is available. Current `published_at` can reset after unpublish/republish, so it is not sufficient for a persistent “new contribution” award.

Default account links open Overview. Associated gaps and credited researchers are visible alongside the title. Keep “most claims” as context rather than a headline award: splitting an argument into more claims should not automatically improve its rank.

## Dataset rankings

| Ranking | Count | Counting rule |
| --- | --- | --- |
| Most claims informed | Distinct component claims using the dataset as evidence | Deduplicate `(dataset_id, claim_id)` across all public accounts |
| Most accounts informed | Distinct public accounts using the dataset as evidence | Deduplicate `(dataset_id, account_id)` |
| Broadest gap coverage | Distinct knowledge gaps whose public accounts use it as evidence | Deduplicate `(dataset_id, gap_id)` |
| Widest researcher adoption | Distinct eligible attributed researchers using it in public accounts | Deduplicate `(dataset_id, researcher_id)` |

Default to **Most accounts informed**, with claims, gaps and researchers as adjacent measures. This recognizes reuse across research outputs while avoiding a reward based only on how finely claims were split. The user can sort by any measure.

“Claims informed” includes supporting and disputing evidence. A detail view breaks evidence uses down into `SUPPORTS`, `DISPUTES`, `MIXED`, `NEUTRAL` and `UNKNOWN`. A **Supporting evidence** filter counts only an explicit `SUPPORTS` interpretation targeting that claim's proposition. Direction concerns the proposition, not an endorsement of the claim's assessment: disputing evidence may agree with a skeptical claim. If the same dataset has supporting and disputing evidence for one claim, count the claim once overall and retain both directions in the breakdown; direction counts need not sum to the unique total.

Only follow evidence paths attached to the component claim and targeting its proposition. Resolve source claims and their recorded derivations to datasets or files, then use explicit file membership to associate a dataset. Preserve which evidence item licensed the relation and its direction relative to the target; do not multiply directions along a chain of scientific claims. Direct computational provenance without an evidence interpretation belongs in a separate **Analysis input uses** measure, not the support count. Missing, ambiguous or truncated paths must not become inferred support.

Aggregate complete frozen publication graphs, not the bubble display hierarchy: the latter repeats sources under multiple claims, includes computational inputs and shows all recorded member files. Count a concrete file only when a qualifying path reaches it, not merely because another member of its dataset was used. Show files inside dataset detail first; a file leaderboard can follow if useful.

Use canonical dataset IDs initially. Different observations/versions of one study remain separate until an authoritative identifier/version mapping exists. Never merge datasets based on their name, S3 prefix or similar file path. A later “study family” view can roll versions up with the rule made explicit. Locations are navigation/provenance metadata, not ranking identity.

## Eligibility and attribution

- Use currently public accepted account snapshots and explicitly published exploration snapshots. Private drafts, user uploads, research runs and unpublished results contribute no public counts.
- Attribute researcher contributions to the frozen original registered request actor. Current workspace ownership and publication ownership do not establish authorship. Moving anonymous work into a registered workspace does not retroactively make it an authored registered contribution.
- Existing public attribution supplies the display name and authenticated ORCID where available. Return an opaque public contributor key, never private email, OAuth details or job/request IDs. Do not assemble a roster from all `Person` nodes: it would include upstream authors and dataset creators. Software agents are not researchers on this board.
- A public profile with a chosen name, optional verified ORCID and ranking visibility is a useful follow-up. Current OAuth providers can resolve to separate application identities; do not merge them by name or email. Account linking needs an explicit verified process and must preserve historical attribution.
- Missing or anonymous attribution stays unranked by researcher; its eligible public scientific records can still contribute to account/dataset totals. Conflicting frozen attribution for the same canonical account must be reconciled or left uncredited, not assigned according to whichever publication row is read first.
- Exclude the canonical UI fixture and all records flagged as demo/test origin from live rankings. Preserve that exclusion before public-summary projection. Use invented data only in clearly labeled local test fixtures.
- Deduplicate canonical accounts globally across copies, republishing and publication snapshots; multiple paths to the same claim/dataset pair count once. Distinct content-addressed accounts are not automatically distinct scientific discoveries; near-duplicate/revision grouping is a later explicit policy.
- Current votes allow owners to vote. For researcher recognition, subtract the original credited researcher's ballot from their credited account totals. Preserve current account voting behavior and make this leaderboard rule visible. Account ranking can show the existing canonical community vote totals.
- Only count a record while at least one eligible public copy remains. Unpublishing the last copy removes its contributions. An independently published copy can keep it public.
- Start with **All time**. Rolling awards require durable first-publication and vote-event history; the existing mutable timestamps should not be presented as event history. Account versions and retries must not earn repeat credit.

## Code locations and implementation order

| Area | Existing code / proposed change |
| --- | --- |
| Shared navigation | [Session.tsx](../services/frontend/src/components/Session.tsx) and [session-menu.css](../services/frontend/src/components/session-menu.css): About and Leaderboard, with no logo, including at mobile widths |
| Public page | New `services/frontend/src/app/leaderboard/page.tsx` and scoped CSS; use the existing loading, empty, error and pagination patterns |
| Frozen public accounts | [publication.py](../services/backend/src/reveal_backend/publication.py): `published`, `public_accounts`, `publication_snapshot.document` and `.summary` |
| Original contributor | [account_discovery.py](../services/backend/src/reveal_backend/account_discovery.py): `visible_accounts(..., attribution=True)`; [app.py](../services/backend/src/reveal_backend/app.py): request attribution snapshot; [acceptance.py](../services/backend/src/reveal_backend/acceptance.py): scientific actor provenance |
| Published explorations | [analysis_outcomes.py](../services/backend/src/reveal_backend/analysis_outcomes.py): `published`, `listing`, frozen outcome attribution |
| Votes | [votes.py](../services/backend/src/reveal_backend/votes.py): `states`, registered ballots and canonical `vote_total`; read privately server-side when removing own ballots, expose aggregate counts only |
| Evidence semantics | [evidence-package.schema.json](../schema/evidence-package.schema.json): `Claim.has_evidence`, `EvidenceItem.target_proposition`, `direction`, `source_claims`, derivation and dataset membership; [account-graph.ts](../services/frontend/src/lib/account-graph.ts) is a display reference, not the ranking implementation |
| Demo exclusion | [fixture_seed.py](../services/backend/src/reveal_backend/fixture_seed.py): persisted `fixture_origin` |
| New aggregation | Proposed `services/backend/src/reveal_backend/leaderboard.py`: pure projections, explicit eligibility and pair deduplication; server-side sorting/pagination over the entire public population |
| API and client | New public read endpoint in [app.py](../services/backend/src/reveal_backend/app.py), then [OpenAPI source](../scripts/openapi_current.py), generated clients and [client.ts](../services/frontend/src/lib/client.ts) |

1. Implement an all-time public projection and transparent definitions before adding scores to the UI. Reuse existing public-read boundaries. At larger scale materialize a projection keyed by publication versions and update it on publication/vote events; browser-side counting of a fetched page is never the authoritative total.
2. Add researcher separate rankings and proposed overall score; account vote ranking; dataset account/claim/gap rankings. Include `as_of`, `score_version`, eligibility/exclusion counts and definition identifiers. Keep drilldowns on the same eligibility scope as displayed totals.
3. Build the page and shared navigation, with visible methodology and sortable raw counts. Do not rank a UI fixture as a real researcher or dataset contribution to avoid an empty table.
4. Add reuse, reviewed-contribution awards, verified collaborators and rolling windows once their underlying attribution/events exist. Future reviewer credit should reward substantive accepted review, not the number of comments or clicks.

Acceptance checks: private-result isolation; current publication eligibility and unpublication; copies and republish deduplication; fixture exclusion; original attribution after transfer; missing/anonymous/conflicting attribution; self-vote handling; exact ties and all-zero/single-contributor cohorts; dataset/file/provenance distinctions; support versus dispute; repeated evidence paths; partial graphs; stable full-population pagination; mobile navigation and keyboard sorting.

## Implemented first version

- `/leaderboard` provides researcher, scientific-account and dataset tabs, URL-bound ranking controls, all-time measures, supporting-evidence filtering, contribution details and a methodology disclosure. The shared header links About and Leaderboard.
- `GET /v1/leaderboard` computes the complete current public population before ranking and pagination. `GET /v1/leaderboard/{view}/{entry_id}/records` returns the distinct public records behind each measure. Both use revocable public reads with no-store responses and cursors bound to the projection, view and filters.
- [leaderboard.py](../services/backend/src/reveal_backend/leaderboard.py) computes eligibility, attribution, vote corrections, rational percentile scores and canonical deduplication. [leaderboard_sources.py](../services/backend/src/reveal_backend/leaderboard_sources.py) traverses the full frozen evidence graph, separate from the bubble presentation.
- [Leaderboard.tsx](../services/frontend/src/components/leaderboard/Leaderboard.tsx) implements tables and drilldowns. [leaderboard.ts](../services/frontend/src/lib/leaderboard.ts) validates navigation state and pagination consistency. The contract is generated from `scripts/openapi_current.py` into both clients and the API handoff.
- The canonical example remains excluded. Its source-tracing test yields 12 claims, four datasets and five files reached by evidence, rather than crediting all eight files displayed by the bubble hierarchy. Populated test data stays isolated from the normal development database.

Account reuse, first-publication awards, rolling windows, researcher profile controls and dataset-family grouping remain deferred until the required attribution or history is available. No publication, research run, or QA deployment is needed to compute these read-only rankings.

### Local validation — October 1, 2026

- 133 frontend tests, including six leaderboard navigation/pagination tests; frontend and integration-client type checks passed.
- 61 backend tests across leaderboard projection, evidence traversal, publication, votes and public discovery passed. OpenAPI validation, generated clients, examples and API handoff were refreshed.
- Production builds succeeded; the local API and frontend are healthy. All three live ranking endpoints exclude the canonical demo account and use `private, no-store` responses.
- Browser checks used a separate disposable SQLite API for populated tables. Verified researcher score components, negative vote ranking, account attribution, supporting-evidence filtering, source-file drilldowns and evidence links. At 1280px and 390px widths there was no horizontal overflow; keyboard tab navigation, dialog dismissal and mobile metric expansion worked without console warnings/errors. The normal development database received no test contributions.

### Presentation simplification

The current layout shows only rank, identity and the chosen metric; an optional **All metrics** disclosure retains the other measures and their contributing-record drilldowns. Filters appear when they are relevant to existing results, with an escape from an empty supporting-evidence filter. Empty Researchers displays only **No researchers yet.** beneath the view tabs. Other empty views use the same concise pattern. Introductory copy, cohort banners, timestamps, footer calls to action and redundant scoring controls are removed from the primary view; methodology and observation metadata remain in one collapsed disclosure for populated boards. Fetching, canonical public-data rules, score formulas, refresh behavior, query parameters and record dialogs are unchanged.
