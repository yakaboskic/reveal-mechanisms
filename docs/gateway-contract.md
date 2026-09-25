# Authentication gateway contract

**v12 implementation specification, not an implemented service.** This supplements [authentication](authentication.md) and the [scientific OpenAPI](../api/openapi.json). Next.js owns browser sessions; EC2 owns application principals and research authorization. Names below reserve the boundary for implementation; NextAuth retains its own provider/callback routes.

## Browser routes

| Route | Input | Output / effect |
|---|---|---|
| `POST /api/session/anonymous` | Empty JSON body, CSRF protection and idempotency key | 201 `Me` (OpenAPI schema), Secure/HttpOnly/SameSite session cookie. Server creates an opaque principal; no caller-selected owner ID. Existing valid session reuses its principal. |
| NextAuth ORCID / Google sign-in and callback | Provider authorization-code flow, state/nonce/PKCE as supported by the chosen configuration | Verified issuer/subject resolves to stable application UUID; registered session established, composer preserved. Never send provider tokens to the scientific API. |
| `POST /api/session/claim` | `{ "consent": true }`, valid source anonymous session and freshly verified target login held by gateway | `WorkspaceClaimResult` from [gateway schema](../schema/gateway.schema.json). New identity upgrades same UUID; existing target requires explicit consent and atomic ownership transfer. |
| Session logout / expiry | Framework session operation | Clear browser session/cache partition; do not cancel jobs or delete scientific content. No recovery merely from an old UUID. |

No login is required for public gap discovery. Anonymous **writes** require the trusted anonymous session. Before that, gap visits/edits remain local; after session creation the frontend saves selections and records visits via the research API. Browser cookies/CSRF tokens are not research-table columns.

## Trusted gateway → EC2 boundary

All research requests use `Authorization: Bearer <short-lived gateway assertion>` validated for signature, allowed algorithm, trusted issuer, audience, expiry and active principal. Registered assertions identify a verified provider mapping; anonymous assertions refer to an existing server-provisioned principal with `principal_kind=anonymous`. Names/emails are profile data, never identity proofs or ownership keys. The exact claim names/JWKS configuration are deployment configuration, not browser-controlled JSON.

Reserve service-only operations, separately authenticated from research-user assertions:

- `POST /internal/v1/principals/anonymous`: `AnonymousProvisionInput` → 201 `AnonymousProvisionResult`. Empty input, service-issued idempotency key. Returns principal and expiry to the gateway; no reusable session secret is returned by the research database.
- `POST /internal/v1/principals/resolve`: `VerifiedIdentityInput` → `PrincipalResult`. Gateway provides freshly verified issuer/subject and explicitly verified profile attributes. Unique issuer/subject mapping; never automatic email matching.
- `POST /internal/v1/principals/claim`: `WorkspaceClaimInput` → `WorkspaceClaimResult`. Valid source-session and fresh target-login proofs are short-lived signed assertions; verify both and service authority. `consent=true` for an existing-account transfer. Reject replay/conflicting ownership; same idempotent operation returns its committed result. Do not log/store raw proofs.

Input/output definitions are in [gateway.schema.json](../schema/gateway.schema.json); these operations are not exposed as browser research endpoints. Service identity cannot be substituted with an ordinary user bearer. Research routes derive ownership from validated assertions, never from `owner_user_id`, `anonymous: true`, ORCID text or an email in a body.

## Invariants and errors

- Proposed initial anonymous session lifetime: 30 days, visible through `Me.workspace_expires_at`; expiry/renewal and rate/concurrency/cost quotas must be configured before launch. Quotas return 429 `ANONYMOUS_QUOTA_EXCEEDED` with Retry-After and preserve drafts.
- Claim/upgrade transaction covers drafts, explorations, requests, active jobs and object grants; retire source anonymous credentials after transfer. Deduplicate exploration projections and leave scientific IDs, original submitting actor and citation bylines unchanged.
- Internal UUID is private ownership. Scientific/citation anonymous actor identifiers are separate. A validated pseudonymous DAPPER Person/citation fixture is still an implementation acceptance item; the current API anonymous identity example uses `person: null` rather than inventing a schema extension.
- Return shared OpenAPI `Problem` semantics: 401 `SESSION_EXPIRED` / `INVALID_IDENTITY_PROOF`; 403 `SERVICE_IDENTITY_REQUIRED` / `CLAIM_CONSENT_REQUIRED`; 409 `PRINCIPAL_ALREADY_CLAIMED` / `IDEMPOTENCY_CONFLICT`; 429 quota. Do not expose another account's details on conflict.
- Missing/expired anonymous credential means no guaranteed recovery. Sign-in without that proof cannot claim a named old workspace. Replacing NextAuth preserves app UUID/mappings; migration of still-valid anonymous sessions needs an explicit time-bounded gateway trust transition.

Example claim result (illustrative UUIDs, no credentials):

```json
{
  "mode": "upgraded",
  "user_id": "11111111-1111-4111-8111-111111111111",
  "principal_kind": "registered",
  "source_retired": false,
  "historical_attribution_unchanged": true
}
```

This boundary is a specification. OAuth applications, JWT/JWKS rotation, session/CSRF handling, anonymous actor fixtures, signed-proof validation and atomic transfer tests remain implementation work.
