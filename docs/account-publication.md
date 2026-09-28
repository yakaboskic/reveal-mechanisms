# Account publication and gap discovery

Accounts remain private until an owner explicitly publishes a snapshot. This is
mutable application metadata; publishing does not edit the immutable scientific
account, claims, original authorship, or pinned citation revisions.

## Reading and publishing

An owner opens a scientific account and chooses **Publish…**. The disclosure
explains what will become publicly viewable and downloadable: the accepted
account, its claims, original author attribution, supporting evidence and
provenance, and its currently accepted research statement and citations.
**Publish account** confirms that choice. Public readers do not need to sign in.
Job activity, drafts, queue records, research requests and unrelated workspace
artifacts remain private. Public scientific reads are limited to the published
snapshot's authorized dependency closure.

The snapshot is fixed at publication time. A statement accepted later, including
a regenerated statement, remains private until the owner chooses **Update
published snapshot** and confirms **Update public snapshot**. The owner is told
when a newer accepted statement is not included. **Unpublish** removes this
workspace's public snapshot. An independently published copy of the same
scientific content may remain available.

The account endpoint returns `publication.can_manage`, which is true only for an
owner. The frontend requires that permission for publication changes, statement
generation and private statement activity. Signed-in readers of someone else's
published account have the same read-only controls as visitors. Account state is
bound to both its identity and the current principal; an identity change hides
previously loaded private content before fetching the new authorized view.

Publication changes use `POST /v1/accounts/{dapper_id}/publication`, a visibility
choice, `expected_version`, and an idempotency key. A timed-out or uncertain
response offers Retry with the same body and key. A version conflict refreshes
publication metadata before a new explicit choice. No publishing happens on
page load, account acceptance, browsing, or automated UI verification.

## Discovery scopes and pagination

The default selector is **Trending knowledge gaps**. Its counts and associated
accounts come from explicitly published accounts across all users. **Top
questions in your workspace** selects the authenticated workspace's saved
accounts; that option is unavailable without a session. Selecting a question
shows the matching account list in the chosen scope, including original author,
account title, a brief synthesis, claim count, creation date and account link.
The list count shows loaded records, with `+` when further pages remain.

The question input and scope selector stay stationary while the gap list scrolls
within the remaining viewport. An IntersectionObserver rooted in that list loads
subsequent pages; **Load more questions** provides a keyboard-accessible fallback.
The frontend preserves the backend's count ordering and opaque continuation
cursor. Equal-count gaps can shuffle for each new browse; the signed cursor's
seed keeps their order stable across that browse's pages. No frontend editorial
selection or first-three limit remains.

Changing scope or principal starts a new collection. Late responses from an old
binding cannot append records into the new list. An expired gap or account cursor
clears its old snapshot and Retry requests the first page, rather than replaying
the expired cursor. Ordinary page failures keep existing cards and retry the
same continuation. All discovery reads remain separate from research jobs.

## Verification scope

The durable browser scripts are
[`check-gap-accounts.mjs`](../services/frontend/scripts/check-gap-accounts.mjs) and
[`check-account-publication.mjs`](../services/frontend/scripts/check-account-publication.mjs).
They intercept every API call and use explicit invented fixtures. Their checks
cover public/workspace scope, 40-row infinite scrolling at desktop and 390px,
stationary controls, pagination/fallback/expiry, slow loading, stale response and
identity fencing, publication disclosure, identical-key retry, version conflict,
unpublishing, published scientific reading, and owner-only controls. No real
account is published and no research job or model call is started by these tests.
Backend tests separately verify publication authorization and snapshot boundaries.
