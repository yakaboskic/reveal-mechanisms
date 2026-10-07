# Instructions for an agent integrating REVEAL

Build the host application's connection to the existing REVEAL backend. Reuse
this client's scientific requests, ownership rules and event behavior. The
supplied gateway credentials are the authorized shared trust relationship;
do not introduce an app-key layer, a separate research database, or manual
user-token handoffs.

## 1. Establish the working reference

Run `npm ci`, `npm run setup -- --credentials /private/path/to/dk-qa.env`, then
`npm run dev` in this directory and open http://localhost:3200. Connect and run
`node scripts/check-flow.mjs` before changing adapters. This checks the gateway,
source selection and draft flow without starting scientific execution.

Read these files before porting:

| File | Responsibility |
| --- | --- |
| `src/lib/gateway.ts` | Framework-neutral server configuration, principal resolution, five-minute assertions, API/SSE proxy and artifact redirect validation |
| `src/lib/session.ts` | Loopback demo identity and HttpOnly session cookie; replace this policy for a hosted app |
| `src/app/api/session/route.ts` | Thin Next.js session route adapter |
| `src/app/api/backend/[...path]/route.ts` | Thin Next.js research proxy route adapter |
| `src/lib/api.ts` | Browser request helpers, exact mutation bodies and API errors |
| `src/lib/events.ts` | Event stream parsing, cursor replay and reconnect behavior |
| `src/lib/types.ts`, `src/lib/api.generated.ts` | Application aliases and types generated from bundled `openapi.json` |
| `scripts/check-flow.mjs` | Independent HTTP flow checker with persisted mutation keys and resumable job events |

The application server needs `REVEAL_API_URL`, `REVEAL_GATEWAY_SECRET`,
`REVEAL_GATEWAY_SERVICE_TOKEN`, `REVEAL_GATEWAY_ISSUER`, and
`REVEAL_GATEWAY_AUDIENCE`. Its own session signing key is `AUTH_SECRET`, distinct
from the shared gateway secret; `APP_ORIGIN` is the browser origin. Keep these
server-side. Setup does this automatically for the local reference. Preserve
the supplied artifact URL allowlist if present.

## 2. Port the server boundary, preserving browser routes

Keep this HTTP surface so the browser code does not depend on the host framework:

| Browser route | Contract |
| --- | --- |
| `GET /api/session` | `{ "principal": Me \| null }` |
| `POST /api/session` with `{}` | Resolve the server-authenticated user; establish the host session; return `{ "principal": Me }` |
| `DELETE /api/session` | Clear the host session; return `{ "principal": null }`; do not delete research or cancel jobs |
| `/api/backend/v1/...` | Forward allowed scientific requests with a fresh server-signed assertion |

The demonstration adapter uses a server-configured fixed issuer/subject and
rejects nonempty connect bodies. It only runs at its configured loopback origin.
For a hosted application, replace it with **your actual authentication**:

1. Authenticate the user in the host application's server session.
2. Derive a stable subject from that authenticated user. Namespace it with an
   application issuer such as `urn:reveal:application:dk`. This issuer identifies
   the user mapping and is distinct from `REVEAL_GATEWAY_ISSUER`, which identifies
   assertions. Never take a backend UUID or arbitrary subject from browser JSON.
3. Call `resolvePrincipal({ issuer, subject }, configuration())` when creating
   or renewing the host session. The backend persistently maps the exact pair
   to one registered workspace. The same pair returns the same owner; other
   subjects get isolated workspaces. Changing either value creates a different
   identity mapping.
4. Store the resolved principal in the host's protected session. For each
   browser API request, load it from that session and inject it into
   `proxyRequest`. Do not trust a browser-supplied Authorization header.
5. Enforce the host's CSRF/origin checks on mutations and use HttpOnly/SameSite
   cookies. The reference requires `Origin` to equal `APP_ORIGIN`.

`gateway.ts` uses standard `Request`, `Response`, `Headers` and injected fetch,
with no Next.js dependency. In another TypeScript server, mount thin route
adapters around those functions. In another language, reproduce this sequence:

```http
POST {REVEAL_API_URL}/internal/v1/principals/resolve
Authorization: Bearer {REVEAL_GATEWAY_SERVICE_TOKEN}
Content-Type: application/json

{
  "issuer": "urn:reveal:application:dk",
  "subject": "stable-authenticated-user-id",
  "display_name": null,
  "email": null,
  "email_verified": false,
  "orcid": null,
  "orcid_authenticated": false
}
```

Use the returned `user_id` and `principal_kind=registered` to sign HS256 JWTs
with the raw UTF-8 gateway secret. Header: `alg=HS256`, `typ=JWT`. Claims:
`sub=user_id`, `principal_kind=registered`, configured `iss` and `aud`, integer
`iat`, `exp=iat+300`, fresh UUID `jti`. Omit `purpose`. Verify the mapping through
`GET /v1/me`. Names, verified email, and ORCID are unnecessary for this flow;
leave them unset/false unless the host actually verified them.

The proxy forwards only `/v1` routes and the required headers (`Accept`,
`Content-Type`, `Idempotency-Key`, `Last-Event-ID`), supplies its own bearer,
and streams the response without buffering. Preserve status, content type,
`Retry-After`, and `Cache-Control: no-store`. Do not forward internal gateway or
administrator routes through the browser proxy.

## 3. Preserve the scientific request sequence

All paths below are backend paths; browser code prefixes `/api/backend`.
Use `encodeURIComponent` for individual IDs in path segments. Read actual
source records; do not invent scientific IDs, revision hashes or suggestion IDs.

**Discover and select a source question.**

```http
GET /v1/knowledge-gaps?limit=12
GET /v1/knowledge-gaps/search?q=coronary%20artery%20disease&mode=fuzzy&limit=12
GET /v1/knowledge-gaps/{encoded-gap-id}
```

List responses contain `items` directly; search hits contain `items[].gap`.
Keep the selected triple:

```js
const selected = {
  id: gap.object.id,
  source_id: gap.source.source_id,
  source_revision: gap.source.source_revision,
};
```

**Request anchors.** Empty `subquery` reuses the imported context embeddings.
Suggestions are retrieval candidates, not evidence that a mechanism is true.

```js
const suggestionBody = {
  source_gap: selected,
  manual_eaggl_anchors: [], dismissed_source_ids: [], subquery: "",
  mode: "semantic", model: "cfde-inc-v2",
}; // POST /v1/mechanisms/suggest
const anchors = suggestions.automatic_anchors.map(({factor}) => ({
  reference: {source: factor.source, source_id: factor.source_id,
    source_revision: factor.source_revision, dapper_id: factor.object.id},
  origin: "automatic", suggestion_id: suggestions.suggestion_id,
}));
```

Retain the user's selected anchors, with their exact references and provenance.
Changing the question invalidates previous suggestions. The backend requires a
selected gap and at least one valid current-model EAGGL anchor for an analysis.
The working example uses one anchor and `biomarkerkg`; additional selections
must come from API records and the supported knowledge-graph choices.

**Create and edit a saved draft.**

```js
const composer = {
  source_gap: selected, eaggl_anchors: selectedAnchors,
  dismissed_source_ids: [], mechanism_subquery: "", model: "cfde-inc-v2",
  selected_kgs: ["biomarkerkg"], // also supports "prokn"
};
// POST /v1/drafts -> 201 Draft
const create = {name: "My research question", composer};
// PATCH /v1/drafts/{id} -> updated Draft
const edit = {expected_version: draft.version, name: "Revised title", composer};
// DELETE /v1/drafts/{id}
const remove = {expected_version: draft.version};
```

Each operation carries a fresh `Idempotency-Key`; retain that key **with its
exact body** until the outcome is known. A retry after a timeout uses both
unchanged. Update local state from the returned draft, including `version`.
If another tab advanced the version, fetch the current draft and resolve the
conflict before creating a new edit. Do not silently overwrite it.

**Submit once.**

```js
// POST /v1/jobs -> 202 Job; persist key + body before sending.
const submission = {
  kind: "analysis", draft_id: draft.id, draft_version: draft.version,
  budgets: {max_accounts: 1},
};
```

The example asks for one account instead of the backend default of up to three.
Other model/time/compute limits are configured in the backend. A 202 means the
job was accepted; it may wait for execution capacity. Persist its ID immediately.
Do not submit again because the response was slow: replay the same idempotent
request, or resume a known job by ID. Cancel through
`POST /v1/jobs/{id}/cancel`; cancellation is a recorded transition rather than
instant disappearance.

## 4. Receive progress through events

Use `GET /v1/jobs/{id}/events` with `Accept: text/event-stream`. Job events have
numeric IDs and names `status`, `progress`, `warning`, `result`, `failure` or
`activity`. Persist each ID after applying its event, deduplicate replay, and
send `Last-Event-ID` when reconnecting. Parse multiline SSE data and ignore
heartbeat comments. A silent stream is not proof that work stopped.

The server bounds streams to at most four minutes and the gateway signs fresh
assertions on new requests. Reconnect an ordinary stream closure with the saved
cursor and bounded backoff. Authentication failure requires renewing the host
session before reconnecting. Never fall back to timer-based job-status or Redis
polling. A single `GET /v1/jobs/{id}` on initial recovery or after a terminal event
is useful for the authoritative envelope. A job loaded with that terminal event as
its `last_event_id` needs no second read; a review retry can follow an earlier
`failed` event on the same job, so any other terminal event does.

Stop job reconnection for `succeeded`, `insufficient_evidence`, `failed`, or
`cancelled`. `cancel_requested` is not terminal. For workspace collections, use
`GET /v1/me/workspace/events`; its opaque cursor is separate from job cursors.
Handle reset/access-revoked events and perform the requested one-time snapshot
refresh. Collection invalidation comes from events rather than a refresh timer.

Each open stream holds one HTTP connection. Over plain-HTTP/1.1 local development
a browser allows six per origin across all tabs, and further requests queue
behind them. `src/lib/events.ts` therefore parks any stream in a tab hidden for
a minute and resumes it from its cursor when the tab is shown. Given the
signed-in user, `followWorkspace` also shares one workspace stream across that
user's tabs: a Web Lock elects the tab that streams and a BroadcastChannel
carries its invalidations, state and cursor to the others. Without Web Locks
(an insecure origin) each tab streams for itself.

A same-origin server proxy lets browser session cookies authorize EventSource or
fetch-stream requests. Do not place bearer tokens in URLs. For another server
framework, forward the response body as a stream, disable buffering, and retain
`Last-Event-ID` rather than turning the stream into JSON or polling.

## 5. Present terminal results and artifacts

- `succeeded` analysis: inspect `job.result.account_ids` through
  `GET /v1/accounts/{id}`. Account envelopes expose scientific objects, citations,
  artifact availability and research-statement state; render the saved result,
  not text inferred from progress events.
- Follow each `job.result.paragraph_job_ids` through its own event stream. The
  backend already submitted these automatically; do not create duplicates.
  Successful paragraph jobs return `paragraph_id`; fetch `/v1/paragraphs/{id}`
  and `/v1/paragraphs/{id}/export?format=markdown` for the cited statement.
- `insufficient_evidence`: fetch `job.result.outcome_id` through
  `/v1/analysis-outcomes/{id}` (or `/v1/jobs/{job_id}/outcome`). Present its scoped
  reasons, missing evidence and limitations; it is not a ScientificAccount.
- `failed` or `cancelled`: keep the draft, job ID, diagnostic status and available
  captured results visible. A review retry uses the explicit eligibility-aware
  endpoint and `expected_last_event_id`; never retry paid execution implicitly.
- Download available artifacts via `/v1/artifacts/{sha256}`. The gateway permits
  only the configured artifact-host/prefix redirect. On a 307, the external
  download must not inherit backend Authorization or the application cookie.
  Treat an expired URL as a reason to request a new authorized artifact URL.
  Missing artifacts are shown as unavailable; do not fabricate download links.

## 6. Handle failures without duplicating work

| Response | Client action |
| --- | --- |
| 401 | Re-establish/renew the application's session; preserve pending mutation and event cursor |
| 403 | Surface permission or CSRF failure; do not invent another identity or retry in a loop |
| 404 | Show unavailable/not-owned result; never search another user's workspace |
| 409 `VERSION_CONFLICT` | Fetch current draft once; reconcile user changes and send a new edit/key |
| 409 `IDEMPOTENCY_CONFLICT` | The saved key/body pair changed; stop and recover the original action |
| 409 source/cursor conflict | Refresh the corresponding source page or snapshot and review the changed selection |
| 422 | Show validation detail and preserve selections; submitting the same invalid body does not help |
| 429 | Honor `Retry-After` where supplied; retain the intended action and expose queue/capacity state |
| 502/503/504 or lost connection | Outcome may be unknown; retry writes only with their original key/body; resume events with the last cursor |

## 7. Prove the port

Run the offline tests, typecheck and build. Run the default nonpaid checker
against your same-origin session/proxy adapters. Verify different real host users
resolve different owners and cannot read each other's private drafts/jobs.
For the full flow, submit once through the UI, then run:

```sh
node scripts/check-flow.mjs --job-id EXISTING-JOB-UUID \
  --report .runtime/integration-job.json
```

Keep and reuse that report after disconnects. It records terminal job status,
event replay, account/outcome access, automatic paragraph status and artifact
checksums separately. Smoke success is not a claim of scientific success. A
failed scientific run must remain a failed result in the report; do not launch
another run merely to make a green acceptance report.
