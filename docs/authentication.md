# Login and researcher identity

Direct API clients can use an [operator-issued workspace API key](api-keys.md)
for drafts, jobs, results and event streams. Keys resolve an existing principal
and retain its ownership, expiry and quotas. The backend stores a hash/owner pair;
gateway provisioning, administrator access and Workflow callbacks retain their
separate credentials. The browser login flows below continue to use gateway JWTs.

**Status, September 30, 2026:** anonymous sessions, Google/ORCID integrations, gateway assertions, durable principal mapping, workspace claims and publication sign-in are implemented. See [colleague startup](../README.local.md), [API walkthrough](api-quickstart.md) and [gateway contract](gateway-contract.md). Sections below retain design rationale and explicit future features such as provider linking; the HTML study is a historical simulation, not the running application. Live callback validity depends on the configured provider and origin.

## 1. Initial scope

At the first **Go / Submit**, offer **Continue with ORCID**, **Continue with Google**, and **Continue anonymously**. Researchers may search/select an existing DisMech gap and choose anchors/context beforehand; arbitrary Question creation is outside the product. ORCID/Google use NextAuth/Auth.js; anonymous continuation establishes a trusted first-party session without an OAuth login. Both session types can save drafts, submit analysis/paragraph jobs and access their own results. Preserve the exact draft through the choice or provider redirect. Closing the choice returns to editing and launches nothing. An established valid session skips the choice on later submissions. Browsing public content remains possible without any session.

Use `session: { strategy: "jwt" }` with **no database adapter**. Authentication therefore needs no NextAuth User/Account/Session/VerificationToken tables. Auth.js keeps the encrypted session in an HttpOnly cookie. Provider credentials and the application secret live in the Next.js server environment. This follows the documented [JWT session strategy](https://authjs.dev/concepts/session-strategies).

We persist stable ownership and attribution with drafts, requests, runs, and scientific/citation records. To support handing the database to another implementation, add small **application-owned user and login-identity records** from the start. These contain durable IDs/profile metadata and identity mappings, not credentials or sessions. This supersedes the earlier proposal to use provider-derived ownership without a user table. NextAuth still needs no database adapter or auth session tables.

## 2. Providers and available fields

**Google:** use NextAuth's [built-in Google provider](https://authjs.dev/getting-started/providers/google), requesting only sign-in identity/profile/email information. Capture the validated provider subject, available name/given/family name, email, and its verification claim. Google does not supply an ORCID iD; leave it unset unless separately supplied or authenticated. No Google service access or offline refresh token is needed for this login-only feature.

**ORCID:** configure a custom OIDC provider with ID `orcid`, ORCID discovery, and the authorization-code flow. Use `openid` for authentication, following [ORCID's authentication/OIDC documentation](https://info.orcid.org/documentation/api-tutorials/api-tutorial-get-and-authenticated-orcid-id/); do not assume Google scopes or an unverified built-in `next-auth/providers/orcid` import. Keep state/nonce and supported PKCE checks in the pinned integration. Verify discovery, token validation, profile mapping, and sandbox callbacks before production.

An authenticated ORCID subject supplies the ORCID iD. Capture name fields when disclosed. Email is nullable: ORCID record fields have visibility controls and email is private by default. Missing email/name must not prevent an otherwise valid login. An optional user-entered contact email is marked unverified until separately verified. See [ORCID visibility settings](https://support.orcid.org/hc/en-us/articles/360006897614-Visibility-settings) and [email visibility defaults](https://support.orcid.org/hc/en-us/articles/360006894494-Visibility-preferences).

The ORCID login proves control of that ORCID account; it does not independently verify every biographical statement or scientific claim. Distinguish `orcid_authenticated` from a manually entered ORCID string. Keep ORCID sandbox and production identities separate.

## 3. Stable identity and attribution snapshots

Allocate an immutable application **`user_id` (UUID)** independent of NextAuth, provider credentials, session secrets, email/name, and DAPPER Person digests. Every draft and submitted research request uses this key for ownership. Resolve a validated **provider issuer + subject** through a `login_identities` mapping to that user ID; a first verified login creates the application user and mapping transactionally. A uniqueness constraint prevents duplicate identity mappings during concurrent first logins. Configure canonical issuer handling explicitly. Google specifies `sub` as its stable account identifier and warns against using email as the user key. [Google OIDC reference](https://developers.google.com/identity/openid-connect/openid-connect).

The server creates a normalized snapshot containing:

- Application `user_id` and `principal_kind=registered|anonymous`. A registered principal also carries provider, canonical issuer and exact provider subject resolved from verified identity; anonymous principals have no invented OAuth identity. The API's application principal is always the durable user ID.
- Nullable `display_name`, `given_name`, `family_name`, `email`, and `email_verified`; record provider-supplied versus user-supplied values.
- Nullable normalized ORCID URL, `orcid_authenticated`, provider/environment, and authentication observation time.
- An explicit `authenticated_at` only if supported by verified provider data, otherwise separately named login-observation time.
- A DAPPER Person/attribution reference when created for a scientific object, with the specific profile snapshot used. The authorization identity and the scientific content digest have different lifecycles.

At draft save/job submission the backend saves the resolved `user_id` as owner/operator and the relevant attribution snapshot. Ownership checks use that stable ID; changing a name/email does not lose prior work. At publication/citation issuance, pin the actual credited name/ORCID/role snapshot. Later profile changes do not silently rewrite existing citations or source authorship. A login identifies the operator; it does not automatically make them the original author of imported DisMech content.

Keep contact email and provider subjects in private application metadata. Public citation exports use credited names and appropriate ORCID links, and do not automatically publish login email. Agents receive the attribution subset they need; provider tokens, session cookies, and contact email are not part of the scientific evidence package.

## 4. Two providers and optional account linking

The schema supports **multiple verified login identities for one application user**. However, an unlinked Google login and an unlinked ORCID login are not automatically the same person. Matching names/emails never combine histories. Without a linking flow, a first login using another provider creates a separate user; the UI must make that behavior clear.

When a “Connect ORCID/Google” flow is implemented, require an authenticated session plus successful fresh authorization with the second provider. Add its verified identity to the existing `user_id`, with uniqueness/conflict checks and link provenance. An identity already owned by another user requires a deliberate account-reconciliation process; do not steal its mapping. JWT sessions alone cannot remember account links across devices or logins. The application mapping supports this without adopting a NextAuth database adapter.

### Auth replacement and database handoff

Keep `app_users.user_id` and all ownership foreign keys unchanged when replacing NextAuth or the frontend. A new trusted auth integration validates the caller and resolves its external identity to the same application user. If it preserves the original issuer/subject, existing mappings work directly. If it changes identity namespaces or subject IDs, add a verified migration/link mapping to the existing user instead of assigning their work a new owner. This correspondence cannot safely be inferred from a name/email match alone.

Export schema/migrations, immutable user IDs, identity mappings, source/verification metadata, drafts, requests, and provenance together. Configure new signing keys/provider secrets in the replacement deployment; those secrets and old sessions do not belong in the research database handoff. Historical citation snapshots and scientific digests remain unchanged.

## 5. Separate EC2 REST API

NextAuth manages registered browser login. A separate first-party anonymous session provides continuity for anonymous users. In both cases EC2 receives and verifies a trusted caller assertion; it never trusts a `user_id`, email, ORCID, or `anonymous: true` submitted by browser JavaScript. Section 8 defines anonymous provisioning and recovery.

Proposed first integration:

1. The browser calls same-origin Next.js routes using its session cookie.
2. The Next.js server validates the session, takes identity only from validated server-side provider/session data, and proxies the request to the independently deployed REST API.
3. The server signs a separate, short-lived API assertion (initial lifetime: five minutes) with trusted gateway `iss`, API-specific `aud`, a namespaced authenticated external subject, original identity issuer/subject, `iat`, `exp`, and required profile/permission claims. Use a separate signing key and versioned public-key trust configuration.
4. EC2 validates signature, allowed algorithm/key, issuer, audience, and expiry, resolves the asserted external identity through its application mapping to `user_id`, and enforces resource ownership/visibility and operation permissions. It ignores conflicting ownership/verified-identity claims in the request body. The Next.js server needs no direct database access. A successful sign-in is not permission to read another user's private run or cancel their job.
5. Save the actor snapshot with the accepted job. Workers can finish independently of a browser disconnect or logout; subsequent reads, events, cancellation, and paragraph requests are authorized again.

The Auth.js encrypted cookie is not the API's bearer-token contract. Do not expose it to browser JavaScript or forward provider access tokens as application authorization. Server callbacks must not allow client session-update payloads to replace the trusted external identity, resolved `user_id`, roles, or verified ORCID status. Proxy mutations retain appropriate CSRF/origin protections; event streams validate identity/ownership at connection and reconnect.

Other frontends can call the same EC2 API after obtaining an equivalent assertion through an approved authentication integration. The API remains independently deployed; a second frontend cannot simply assert user identity. The detailed token exchange for additional clients is a later integration contract.

JWT logout clears the current browser session. Immediate cross-device invalidation requires additional server-side revocation state; it is not part of the initial database-free session design. Short-lived API assertions bound their remaining lifetime.

## 6. Configuration and acceptance

Publishing a scientific account or exploration (including updating a public snapshot) requires a registered signed-in owner. The API rejects anonymous publication with `403 SIGN_IN_REQUIRED` before idempotency replay. Anonymous work remains usable privately; its owner can still unpublish an existing snapshot. The publication panels offer configured providers and return to the same record after login; sign-in never publishes automatically. Existing-account claims refresh record access without changing historical attribution. See [publication behavior](account-publication.md).

Implementation needs registered Google and ORCID clients, exact local/production callback URLs, a high-entropy Auth.js secret, the Next.js deployment origin, and API signing/verification configuration. Proposed callback paths are `/api/auth/callback/google` and `/api/auth/callback/orcid`. Pin a compatible Next.js/NextAuth version and verify the custom ORCID integration; a configured provider is not a completed live login test.

Suggested configuration names: `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`, and application-defined `AUTH_ORCID_ID`/`AUTH_ORCID_SECRET` explicitly wired into the custom provider. Provider credentials stay server-side. Deployment and callback registration are setup tasks, not a request for users' Google/ORCID passwords.

Acceptance before launch:

- Google and ORCID each complete sign-in/sign-out with JWT sessions and no auth adapter tables.
- A returning login with the same issuer/subject retains ownership after email/name changes; a missing ORCID email is supported.
- Google-only, ORCID-only, and user-entered profile values retain correct verification/source labels.
- Draft questions and chips survive the submit-time identity choice. ORCID, Google and valid anonymous sessions can submit; requests without a valid application session/assertion cannot launch agent work. Anonymous budgets and rate limits are enforced on the server.
- Forged, expired, wrong-audience, or wrong-owner requests fail at EC2; client session updates cannot change trusted identity or authorization claims.
- Runs, citation credits, and mint/publication attribution remain traceable after logout; public citations omit private contact details.
- Provider switching does not silently merge accounts. Any future linking flow proves both identities and persists the mapping explicitly.
- A replacement auth integration can resolve the same `user_id` and recover its drafts/query history without rewriting ownership or scientific/citation records. New identity namespaces require a verified mapping.

## 7. Draft autosave and query history

**Drafts autosave to the backend after either a registered or anonymous session is established.** Start with a one-second debounce after text/chip changes, serialize saves per draft, and show Saving/Saved/Save failed state. Mark Saved only after the backend confirms a committed write. Persist the exact selected source-gap text and revision (resolved from DisMech), DisMech/EAGGL chips, automatic/manual selection origins and dismissals, model, mechanism-search subquery, selected graphs, and relevant composer settings. Autosaving does not call CFDE, start an agent, or mint a new DAPPER KnowledgeGap on each keystroke.

Before any session is chosen, keep the draft locally. After either continuation, save it to the resolved owner's workspace and wait for confirmation before submitting. Registered sign-in enables cross-device recovery; anonymous server-saved work is recoverable only while the browser possesses its valid session. On expiry/failure, retain pending edits locally and show their actual save state. Do not silently assign an expired anonymous owner's private work to a new session. Bind pending saves to their original user and draft; never flush one user's queued edits into another user's account after a switch.

Use a monotonically increasing draft `version` with compare-and-swap updates. A stale client/tab receives a conflict and keeps its pending edit for reconciliation instead of overwriting a newer version. The create operation has an idempotency key to avoid duplicate drafts on retries.

**Submitted queries are immutable snapshots.** Submission specifies a saved draft ID and version; the backend verifies ownership/current version and freezes that exact question/context plus attribution in `research_requests`, then queues the analysis job transactionally. If edits are still pending, finish saving first; if the revision changed, return a conflict rather than submitting stale text. Retries use a submission idempotency key. Continuing to edit the draft never changes an already-submitted query, agent input, result, or citation. A submit does not automatically delete the draft.

Proposed application records, not applied migrations:

- `app_users`: immutable UUID, `principal_kind=anonymous|registered`, current display/contact metadata and field provenance, created/updated times, application status, and optional audited merge target. No password hashes, OAuth tokens, client secrets, or session secrets. Anonymous rows may have no login-identity mappings.
- `login_identities`: application `user_id`, provider/environment, canonical original issuer, exact subject, verified-link provenance and timestamps; unique issuer/subject identity. These identifiers permit lookup but are not accepted as proof of login by themselves.
- `research_drafts`: UUID, `owner_user_id` foreign key, version, composer payload, schema version, created/updated times, optional archived state; index owner and update time.
- `research_requests`: existing planned immutable query records, with `owner_user_id`, originating draft/version, submitted payload and attribution snapshot, submission time, and idempotency key. Runs/results link back to the request.

Proposed REST additions: `GET/POST /v1/drafts`, `GET/PATCH /v1/drafts/{id}`, and `GET /v1/research-requests` for the caller's query history. Derive the owner on the server; do not accept arbitrary owner IDs in create/update bodies. `PATCH` requires `expected_version` and returns the committed version; a mismatch returns `409 Conflict` with enough version information to reconcile. Other frontends implement the same contract.

Acceptance includes reload/cross-device draft recovery, logged-out pending edits, account switching, out-of-order writes, two-tab conflicts, duplicate-submit retries, unchanged submitted snapshots after later edits, and recovery through a replacement auth adapter using the same application user IDs.

The [OpenAPI contract](../api/openapi.json) creates both analysis and paragraph work through `POST /v1/jobs`, with shared status/events/cancel endpoints. Draft create, draft PATCH and job submission require `Idempotency-Key`; check replay before optimistic version validation. The assertion resolves to the same durable `user_id` for every endpoint.

## 8. Anonymous workspace lifecycle

### Provision and authorize

1. The user explicitly chooses **Continue anonymously**. The browser calls a same-origin, CSRF-protected session bootstrap route (proposed `POST /api/session/anonymous`). The trusted gateway calls a service-authenticated backend provisioning operation, which creates an anonymous `app_users` UUID. This internal operation accepts no browser-selected owner and is not an open principal lookup.
2. The gateway sets a signed/encrypted, Secure, HttpOnly, SameSite cookie carrying the server-created anonymous principal and expiry. Session secrets stay in deployment configuration, not the research database. Proposed initial lifetime: 30 days from creation; expiry must be visible in the workspace, with sign-in offered to retain access. No recovery promise after cookie loss. Final lifetime and renewal policy remain deployment settings.
3. Before each API call the gateway validates that cookie and signs an API-specific short-lived assertion containing `principal_kind=anonymous` and the server-created principal reference. EC2 validates gateway trust and resolves/checks the existing active anonymous row, including merge/revocation status. OAuth subject fields are absent. Knowledge of a UUID alone grants no access.
4. The same ownership, idempotency, expected-version, job/event/cancel, artifact and paragraph checks apply to both kinds. Save the draft, freeze the request and queue the job in the same order as registered submission. Missing session is distinct from a valid anonymous session.
5. Anonymous results are private to their owner by default. A scientific digest is not a sharing capability; a public resolver must not expose private jobs, request text, attribution or artifacts because the ID is known. Anonymous means no identified login, not untracked or automatically public.

Enforce bounded job budgets, concurrency and submission rate limits server-side for anonymous principals. Add creation/network limits so repeated cookie resets do not bypass paid-work limits; use minimal operational metadata with an explicit retention policy. The precise quotas are deployment decisions. Return a typed, recoverable limit error while preserving the draft. No provider sign-in is required solely because the user chose anonymous mode within its allowed budget.

The session proves possession of a browser workspace, not a verified human identity. The implementation must protect bootstrap and claim operations against CSRF, rotate the anonymous session at establishment, and never reuse a credential from a URL or client-selected ID. Auth.js adapter/session tables remain unnecessary; application principal, audit and rate-limit state are separate concerns.

### Attribution

Record the job operator as `principal_kind=anonymous` with the durable private `user_id`, null verified name/email/ORCID and the actual generating software/harness provenance. Display **Anonymous researcher** in the workspace and citation byline, alongside the generating AI where appropriate; this label does not claim an identified person. Do not assign a real-looking ORCID or invent a personal name.

Before implementation, add a DAPPER validation fixture for anonymous attribution. Proposed representation: a pseudonymous Person with an opaque, non-credential actor identifier separate from the private ownership UUID; the name is explicitly an anonymous display label. Confirm the hashable identifier and scientific/citation profile requirements against the adopted schema. `Agent` is a mixin in the current pin, so do not mint a made-up standalone `dapper:Agent`. The application snapshot records anonymity even if DAPPER has no dedicated anonymity field. Preserve imported DisMech source authorship.

### Sign in later

- If the provider identity is new, prove the current anonymous session and complete fresh provider authorization, then transactionally attach the verified login identity to the **same user UUID** and mark it registered. Rotate/clear the old anonymous cookie and issue the registered session. Drafts, requests and jobs retain ownership.
- If the provider identity already belongs to an account, show an explicit **Keep this anonymous work in my account** action. Require both the current anonymous session and the newly verified existing account. Transactionally transfer ownership/grants for drafts, history, active jobs and results to that account; record source/target UUIDs and the event. Retire the anonymous principal and reject replay of its credentials. Handle uniqueness/concurrent-claim conflicts and make the claim idempotent. Never merge on email/name matching.
- The historical request's operator/attribution snapshot remains anonymous. Ownership can change independently of scientific identity. Do not remint Claims or rewrite old citation metadata/bylines simply because the owner signs in. Explicit later publication/credit correction would create a new metadata revision with its own provenance.
- Keep pending local edits bound to their original owner until the claim transaction succeeds. Existing workers retain the original submitting actor; newly authorized reads/cancel/paragraph operations follow the resulting owner grant.
- Loss or expiry of the only anonymous credential means no guaranteed recovery. Signing in afresh cannot prove ownership of a lost anonymous workspace. Downloading a DAPPER document preserves its contents, but does not transfer private access. A recovery-code/share-link design is outside this iteration.

### Handoff and acceptance

The database handoff retains anonymous UUIDs, ownership/grants, historical actor snapshots and upgrade/merge audit records just like registered work. Replacing auth must not require rewriting research rows. Preserving an outstanding anonymous browser session additionally requires a trusted, time-bounded session migration or overlap with the old gateway; the database alone cannot reauthenticate an anonymous visitor after the old session system is removed. Keep old signing secrets out of the research export.

Acceptance: three submit choices; dismissal launches nothing; zero anchors still blocks every choice; anonymous create/autosave/reload/history; private reads and cancellation across two anonymous browsers; forged/expired/retired cookie rejection; first-login upgrade; existing-account claim; concurrent claim and submission retries; active-job ownership; preserved anonymous scientific/citation attribution; cookie-loss messaging; quota exhaustion; no credentials in research tables.

## 9. Current API and gateway contracts

The [v12 OpenAPI](../api/openapi.json) now includes registered/anonymous `principal_kind`, nullable identity fields and workspace expiry, owner-scoped exploration/account listings and the current source-selected composer. The same bearer/owner/idempotency rules govern both session kinds. No token is not anonymous write permission.

[Gateway contract](gateway-contract.md) and [gateway schema](../schema/gateway.schema.json) specify browser bootstrap, service-only provisioning/identity resolution and upgrade/claim with both proofs. The gateway and backend implement these contracts with session/CSRF checks, quotas and owner transfer transactions. See the backend/frontend tests and the dated validation report for exercised behavior. Provider-to-provider account linking remains a separate feature.

## 10. Avatar and personal dashboard

Keep an avatar in the upper-right corner on every page, even before login. Anonymous visitors see an outline person; registered users may see initials or their available profile image. Its popover contains **Your knowledge gaps** and **Your scientific accounts**, plus sign-in for anonymous visitors. Both destinations open one dashboard with line-integrated tabs. Jobs remain underlying history linked to their gaps and accounts, rather than a third primary menu destination.

- **Knowledge gaps:** exact DisMech questions explored, most recent exploration time, saved mechanism selection and counts/links to that owner's related scientific accounts. Clicking a question restores its composer. A visit does not create a new scientific object or imply an analysis ran.
- **Scientific accounts:** accounts generated by that user's jobs, newest first, with closing remarks, claim count and exact associated gap. Open the existing account, claims and cited statement. Exclude other users' accounts merely sharing the same gap; deduplicate repeated deliveries by account identity. Implementation needs pagination and loading, empty, unavailable and error states.
- **Anonymous continuity:** before submission, a temporary opaque local ID may index browsing history without establishing server authorization. Submitted anonymous work still uses the trusted principal/session in section 8. Losing the local cache or anonymous session may lose access. Offer **Sign in to keep your work**. Cookie/cache loss does not delete durable scientific records; retention is a separate decision.
- **Registered continuity:** retrieve owner-authorized history from the backend using the durable application user ID, across sessions and devices. Sign-in from an anonymous session follows section 8's proof/upgrade/claim rules and retains original scientific attribution. Never recover ownership merely from a browser-supplied temporary ID.

The HTML prototype stores explored gap IDs, last anchor selections and the existing example account under a random `preview:` ID in `sessionStorage`. Same-tab reloads retain it; tab closure/clearing storage may discard it (browser session restoration varies). Simulated ORCID/Google choices merge that history into a browser-local preview profile in `localStorage`; signing out creates an empty temporary workspace. Choosing the same simulated provider can restore its local preview history. The signed-in state is labeled as a preview: no provider, credentials, server save or cross-device sync is involved. These caches are not the production authorization model. The example account stays labeled and retains its exact gap, even when reached from unrelated-gap playback.
