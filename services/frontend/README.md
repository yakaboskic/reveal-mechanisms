# REVEAL frontend

Next.js 15.5.26, React 19.1.9, NextAuth 4.24.15. The approved HTML interaction is implemented as a responsive React application. All scientific operations use the actual API through a same-origin server gateway. No scientific fixture is imported by a production page.

## Local startup

Use [README.local.md](../../README.local.md) for clone-and-run setup. Normal startup is `.venv/bin/python scripts/local_deployment.py up --build`; shutdown is `down`. Next.js runs as a production Docker container alongside the API, dispatcher, Redis and workers. No host Node installation is needed for that path. For source-level frontend work, install Node 22 and run `npm --prefix services/frontend ci`. The older `dev-up.sh` workflow is documented separately in [legacy development](../../docs/local-development.md); do not run both stacks together.

For isolated frontend development, with the backend already listening:

```bash
cd services/frontend
node --env-file=../../.env node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3000
```

`/api/health` reports frontend readiness. Docker Compose waits for backend `/readyz`. The container gateway uses `REVEAL_API_URL=http://api:8000`; direct host API access is on port 18000. The legacy host workflow uses port 8000. No database connection or database credentials are used by frontend code.

Required server environment:

| Name | Meaning |
| --- | --- |
| `NEXTAUTH_URL` | Browser origin, normally `http://localhost:3000` |
| `AUTH_SECRET` | At least 32 random characters; encrypted NextAuth sessions and signed anonymous browser cookies |
| `REVEAL_API_URL` | Backend URL reachable from the host |
| `REVEAL_GATEWAY_SECRET` | Separate HS256 signing key, at least 32 characters; must match backend |
| `REVEAL_GATEWAY_SERVICE_TOKEN` | Separate service-only bearer shared with backend |
| `REVEAL_GATEWAY_ISSUER` | Default `reveal-nextjs` |
| `REVEAL_GATEWAY_AUDIENCE` | Default `reveal-api` |
| `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | Google OAuth application; missing credentials disable Google continuation |
| `AUTH_ORCID_ID`, `AUTH_ORCID_SECRET` | ORCID OAuth application; missing credentials disable ORCID continuation |
| `AUTH_ORCID_ISSUER` | Default `https://orcid.org`; use sandbox issuer with sandbox credentials |

Register callbacks at `{NEXTAUTH_URL}/api/auth/callback/google` and `{NEXTAUTH_URL}/api/auth/callback/orcid`. ORCID uses its official OIDC discovery, `openid`, `client_secret_post`, state, and nonce. Current ORCID discovery does not advertise PKCE. Google uses the built-in provider's authorization-code checks. OAuth sessions use JWT strategy with no database adapter. Provider subjects and tokens never enter browser scientific requests. Configure HTTPS in production; anonymous cookies become Secure there and are always HttpOnly/SameSite=Lax.

The gateway provisions anonymous principals via separate service authority, then signs five-minute user assertions. Origin checks protect all browser mutations. OAuth callbacks resolve only verified issuer/subject identities. An existing anonymous proof is sent in `X-Reveal-Anonymous-Session` to permit a first login to upgrade the same application UUID. An identity already mapped to a different account requires explicit workspace-transfer consent and a fresh login. Scientific attribution is never rewritten. Local composer state is stored in sessionStorage; autosaves are serialized, version checked, and acknowledged before submission. Conflicts retain local edits and offer loading the saved version or creating a separate draft. Logging out clears the browser's composer partition, not server research records or jobs.

## Checks and generated data

Workspace lists use a private, in-memory cache owned by the mounted session.
Each tab retains its loaded pages across client navigation. Data is fresh for
30 seconds; stale data stays visible during background refresh on return,
window focus, reconnection, or the visible page's refresh timer. The Refresh
button bypasses the freshness interval. Active work polls every 10 seconds;
known frozen research requests are reused between job-status updates.

Successful workspace mutations, completed jobs, and anonymous-work transfers
invalidate the lists. Revalidation follows all previously loaded pages, so it
does not reset pagination. Failed refreshes retain existing rows and offer a
retry. Logout, identity changes, and authorization denial clear private cached
data; late responses cannot restore it. Cached data is not persisted to browser
storage or shared in the server's HTTP cache. A full reload starts a fresh cache.

```bash
npm --prefix services/frontend run generate
npm --prefix services/frontend run fixtures
npm --prefix services/frontend run typecheck
npm --prefix services/frontend test
npm --prefix services/frontend run build
```

`src/lib/api.generated.ts` is generated directly from `api/openapi.json`; do not hand edit it. `src/lib/client.ts` uses typed paths, bodies, headers, and responses. SSE streams pass through without buffering, replay from the last observed event ID, preserve already displayed history, and recover current job state if replay expires. A disconnected browser does not cancel a job. Stop explicitly calls the cancellation endpoint.

Every tab of one user in a browser shares a single workspace event stream: the tab holding the user's Web Lock streams and rebroadcasts on a BroadcastChannel, and when it closes the next visible tab resumes from the last cursor. Revocation and sign-out reach every tab at once. A tab hidden for a minute disconnects its streams (workspace and job activity) and resumes from their cursors when shown, so plain-HTTP/1.1 local development stays under the browser's six connections per origin. Without Web Locks (for example an insecure LAN origin) each tab streams for itself.

`fixture-adapter.ts` is an explicitly selected read-only development/test adapter. `scripts/generate-fixtures.mjs` extracts exact existing OpenAPI examples and pins their contract checksum. Fixtures retain the source scientific IDs and explicit invented-KG notice; they cannot create jobs or provide evidence to the backend. Default application pages use the real backend client only. The fixture schema check uses the repository's Python jsonschema environment; the frontend tests check pinning, identity preservation, anchor limits and dismissals, arbitrary UTF-8 SSE boundaries, malformed events, and expired cursors.

Scientific accounts foreground closing synthesis, leave claims collapsed, and provide dedicated assessment/proposition/evidence/provenance inspection. Paragraph status is independent of accepted account status. UI citations use Unicode code-point spans and target+metadata-revision pins. Numbered references follow first occurrence order across UI and exports. Rich-text copy requests both HTML and plain text for Word; exports use the backend's Markdown, LaTeX, and BibTeX content. Canonical `/id/{id}` links resolve to authorized records.

## Factors and gene sets

Mechanism chips open `/factors/{source_id}?source_revision=...` in a new tab, preserving the research editor. The factor page places metadata in collapsed **Factor details**, with KPN trait links to their catalog pages. Gene and gene-set tabs retain their loaded rows and search when switching. Heatmap and table views default to global alphabetical order; **Strongest first** remains available, and **Load more** appends the next page (200 genes or 50 gene sets). Heatmap colors use the full factor's numeric range even while filtering. Gene-set projections offer joint and marginal metrics and retain their original projection ranks. The catalog includes all stored nonzero gene weights and the union of the top 50 gene sets by either projection; missing observations are never displayed as zero.

The gene view can overlay a gene set from this factor, or **Overlay on genes** can be selected from a gene-set tile or table row. Exact `HGNC.SYMBOL:` members (or recorded bare symbols) are outlined without changing their loading colors. Unsupported identifier namespaces remain unknown; missing membership is explicitly unavailable. Membership requests pin the same reference generation, and members absent from this factor's stored loadings are never invented as zero-weight genes.

`/gene-sets/{id}?generation_id=...` shows the exact imported gene set, collection, source datasets, construction activities, files, attribution, and searchable members. Unknown membership and provenance remain explicitly unavailable. Requests carry the factor revision and reference generation so a catalog reload cannot silently substitute another fit. Archived factor links render only the retained snapshot. Unpinned gene sets missing from the reference catalog may resolve through the existing authorized scientific-object API; pinned reference requests never do.

Component validation does not certify OAuth login, Aurora persistence, a live Box result, or a full-stack browser journey. Those require the configured providers and the backend/integration acceptance gates.

## Trending knowledge gaps

Homepage trending knowledge gaps retain loaded pages, the Accounts/Votes selection, and each sort's scroll position in session memory across client navigation. Returning to the homepage does not refetch or reorder the list. Relevant events show an update notice; **Refresh rankings** loads a new first page while retaining existing rows until it succeeds. A full reload or identity change clears the cached rows. Tied account counts or vote scores favor more distinct, resolved DisMech mechanisms (Votes uses account count as the next tie-breaker).

## Workspace drafts

Each knowledge gap has a collapsed **Drafts (count)** panel. Expanding it reveals
the draft selector showing one draft and its run at a time.
Users can create a named draft (fresh or copying the selected settings), open it
with **Open draft** in the autosaving editor, rename it, and delete it after confirmation.
The editor displays the draft name and save status, with a link back to the
workspace. Explicit draft links take priority over unfinished submission state
and never resume or launch research merely by opening the draft.
Unnamed older drafts receive a stable short-ID label. Draft names are private
workspace metadata; they do not change the scientific question.

Rename and delete use optimistic versions and idempotency keys. Running drafts
link to their activity instead of the editor, and the API rejects deletion while
an analysis is active. Deleting a draft keeps the gap, frozen requests, runs,
scientific accounts, and exploration results. Completed runs can still restore
their submitted selection after the editable draft is deleted. Changes invalidate
the workspace cache automatically.
