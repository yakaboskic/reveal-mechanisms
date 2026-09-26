# REVEAL frontend

Next.js 15.5.26, React 19.1.9, NextAuth 4.24.15. The approved HTML interaction is implemented as a responsive React application. All scientific operations use the actual API through a same-origin server gateway. No scientific fixture is imported by a production page.

## Local startup

Use the repository's `./scripts/dev-up.sh` and `./scripts/dev-down.sh` for normal startup and teardown. One-time dependency setup is `npm --prefix services/frontend ci`; Node 22 or newer is required. The root scripts inject ignored root `.env` configuration into the host frontend and manage the API/worker containers.

For isolated frontend development, with the backend already listening:

```bash
cd services/frontend
node --env-file=../../.env node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3000
```

`/api/health` reports frontend readiness. The root supervisor separately checks backend Aurora readiness. The host gateway calls `REVEAL_API_URL=http://127.0.0.1:8000`, whose port is published by Compose. No database connection or database credentials are used by frontend code.

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

```bash
npm --prefix services/frontend run generate
npm --prefix services/frontend run fixtures
npm --prefix services/frontend run typecheck
npm --prefix services/frontend test
npm --prefix services/frontend run build
```

`src/lib/api.generated.ts` is generated directly from `api/openapi.json`; do not hand edit it. `src/lib/client.ts` uses typed paths, bodies, headers, and responses. SSE streams pass through without buffering, replay from the last observed event ID, preserve already displayed history, and recover current job state if replay expires. A disconnected browser does not cancel a job. Stop explicitly calls the cancellation endpoint.

`fixture-adapter.ts` is an explicitly selected read-only development/test adapter. `scripts/generate-fixtures.mjs` extracts exact existing OpenAPI examples and pins their contract checksum. Fixtures retain the source scientific IDs and explicit invented-KG notice; they cannot create jobs or provide evidence to the backend. Default application pages use the real backend client only. The fixture schema check uses the repository's Python jsonschema environment; the frontend tests check pinning, identity preservation, anchor limits and dismissals, arbitrary UTF-8 SSE boundaries, malformed events, and expired cursors.

Scientific accounts foreground closing synthesis, leave claims collapsed, and provide dedicated assessment/proposition/evidence/provenance inspection. Paragraph status is independent of accepted account status. UI citations use Unicode code-point spans and target+metadata-revision pins. Numbered references follow first occurrence order across UI and exports. Rich-text copy requests both HTML and plain text for Word; exports use the backend's Markdown, LaTeX, and BibTeX content. Canonical `/id/{id}` links resolve to authorized records.

Component validation does not certify OAuth login, Aurora persistence, a live Box result, or a full-stack browser journey. Those require the configured providers and the backend/integration acceptance gates.
