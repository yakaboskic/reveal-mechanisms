import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createSessionIdentity, identityFreshMs, provisionalMe, sessionMe, sessionScope, type IdentitySnapshot, type SessionPrincipal, type SessionStatus } from "../src/lib/session-identity";
import { ApiError, type Schema } from "../src/lib/client";

type Me = Schema<"Me">;
const deferred = <T>() => {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0));
const alice: SessionPrincipal = { user_id: "alice", principal_kind: "registered" };
const bob: SessionPrincipal = { user_id: "bob", principal_kind: "anonymous", workspace_expires_at: "2026-10-08T00:00:00.000Z" };
const verified = (principal: SessionPrincipal, display_name = "Alice"): Me => ({ user_id: principal.user_id, principal_kind: principal.principal_kind,
  workspace_expires_at: principal.workspace_expires_at ? principal.workspace_expires_at.replace(".000Z", ".123456Z") : null,
  display_name, email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null });
const status = (principal: SessionPrincipal | null): SessionStatus => ({ principal, canClaim: false, providers: { google: true, orcid: true } });

function harness(principal: SessionPrincipal | null = alice) {
  let clock = 1_000, current = principal, statusCalls = 0, resets = 0;
  const meCalls: ReturnType<typeof deferred<Me>>[] = [], states: IdentitySnapshot[] = [];
  const session = createSessionIdentity({
    status: async () => { statusCalls++; return status(current); },
    me: () => { const call = deferred<Me>(); meCalls.push(call); return call.promise; },
    onStatus: () => {}, onChange: state => states.push(state), reset: () => { resets++; },
    denied: error => error instanceof ApiError && [401, 403].includes(error.status), now: () => clock,
  });
  return { session, meCalls, states, statusCalls: () => statusCalls, resets: () => resets,
    advance: (ms: number) => { clock += ms; }, signIn: (next: SessionPrincipal | null) => { current = next; } };
}

test("page data starts once the gateway principal is known, before GET /v1/me answers", async () => {
  const h = harness(alice);
  const refresh = h.session.refresh(); await tick();
  const state = h.session.snapshot();
  assert.equal(h.meCalls.length, 1, "verification runs in parallel");
  assert.equal(state.ready, true);
  assert.equal(sessionMe(state)?.user_id, "alice");
  const scope = sessionScope(state, false);
  assert.equal(scope, "alice:registered::false");
  h.meCalls[0].resolve(verified(alice)); assert.equal((await refresh)?.display_name, "Alice");
  assert.equal(sessionScope(h.session.snapshot(), false), scope, "verification never re-keys or clears the caches");
  assert.equal(sessionMe(h.session.snapshot()), h.session.snapshot().verified);
});

test("anonymous visitors are ready after the status hop and never call GET /v1/me", async () => {
  const h = harness(null);
  assert.equal(await h.session.refresh(), null);
  assert.equal(h.meCalls.length, 0);
  assert.equal(h.session.snapshot().ready, true);
  assert.equal(sessionScope(h.session.snapshot(), false), null);
});

test("focus checks reuse a recent verification; resync and claims force one", async () => {
  const h = harness(alice);
  const first = h.session.refresh(); await tick(); h.meCalls[0].resolve(verified(alice)); await first;
  h.advance(identityFreshMs - 1);
  assert.equal((await h.session.refresh())?.display_name, "Alice");
  assert.equal(h.statusCalls(), 2); assert.equal(h.meCalls.length, 1);
  const forced = h.session.refresh({ force: true }); await tick();
  assert.equal(h.meCalls.length, 2); h.meCalls[1].resolve(verified(alice, "Alice Claimed")); assert.equal((await forced)?.display_name, "Alice Claimed");
  h.advance(identityFreshMs);
  const stale = h.session.refresh(); await tick(); assert.equal(h.meCalls.length, 3);
  h.meCalls[2].resolve(verified(alice)); await stale;
});

test("concurrent checks share one request and one verification", async () => {
  const h = harness(alice);
  const a = h.session.refresh(), b = h.session.refresh();
  assert.equal(a, b); await tick();
  assert.equal(h.statusCalls(), 1); assert.equal(h.meCalls.length, 1);
  h.meCalls[0].resolve(verified(alice)); await a;
});

test("a just-provisioned anonymous Me is adopted without /v1/me and bypasses a stale pending check", async () => {
  const h = harness(null);
  const focus = h.session.refresh();               // started before provisioning; would answer null
  h.signIn(bob);
  const created = provisionalMe(bob), adopted = await h.session.refresh({ adopt: created });
  assert.equal(adopted, created);
  assert.equal((await focus)?.user_id, "bob", "the superseded focus check answers with the adopted identity");
  assert.equal(h.meCalls.length, 0);
  assert.equal(h.session.snapshot().verified, created);
  assert.equal(sessionScope(h.session.snapshot(), false), `bob:anonymous:${bob.workspace_expires_at}:false`);
});

test("an adopted identity for another principal is ignored and verified normally", async () => {
  const h = harness(alice);
  const request = h.session.refresh({ adopt: provisionalMe(bob) }); await tick();
  assert.equal(h.meCalls.length, 1);
  h.meCalls[0].resolve(verified(alice)); assert.equal((await request)?.user_id, "alice");
});

test("the backend's microsecond expiry never re-keys the cookie-derived scope", async () => {
  const h = harness(bob);
  const request = h.session.refresh(); await tick();
  const scope = sessionScope(h.session.snapshot(), false);
  h.meCalls[0].resolve(verified(bob, "")); await request;
  assert.notEqual(h.session.snapshot().verified?.workspace_expires_at, bob.workspace_expires_at);
  assert.equal(sessionScope(h.session.snapshot(), false), scope);
});

test("a superseded check answers with the newer identity, not null", async () => {
  const h = harness(null);
  const old = h.session.refresh(); h.signIn(bob);
  const adopted = h.session.refresh({ adopt: provisionalMe(bob) });
  assert.equal((await old)?.user_id, "bob"); assert.equal((await adopted)?.user_id, "bob");
});

test("a full identity is adopted without another /v1/me", async () => {
  const h = harness(alice);
  const me = verified(alice);
  assert.equal(await h.session.refresh({ adopt: me }), me);
  assert.equal(h.meCalls.length, 0); assert.equal(h.session.snapshot().verified, me);
});

test("a rejected principal purges cached rows and drops the workspace scope; outages keep it", async () => {
  const outage = harness(alice);
  const pending = outage.session.refresh(); await tick();
  outage.meCalls[0].reject(new Error("Network unavailable"));
  assert.equal((await pending)?.user_id, "alice");
  assert.equal(outage.resets(), 1, "only the initial principal binding resets");
  assert.ok(sessionScope(outage.session.snapshot(), false));

  const denied = harness(alice);
  const request = denied.session.refresh(); await tick();
  denied.meCalls[0].reject(new ApiError(401, "UNAUTHENTICATED", "Sign in again."));
  assert.equal(await request, null);
  assert.equal(denied.resets(), 2);
  assert.equal(sessionMe(denied.session.snapshot()), null);
  assert.equal(sessionScope(denied.session.snapshot(), false), null);
});

test("a principal change resets caches and discards the old principal's late verification", async () => {
  const h = harness(alice);
  const first = h.session.refresh(); await tick();
  h.signIn(bob);
  const second = h.session.refresh({ force: true }); await tick();
  assert.equal(h.resets(), 2);
  assert.equal(sessionMe(h.session.snapshot())?.user_id, "bob");
  h.meCalls[0].resolve(verified(alice)); await first;
  assert.equal(h.session.snapshot().verified, null, "alice's answer never labels bob");
  h.meCalls[1].resolve(verified(bob)); await second;
  assert.equal(h.session.snapshot().verified?.user_id, "bob");
});

test("logout supersedes an in-flight check", async () => {
  const h = harness(alice);
  const request = h.session.refresh(); await tick();
  h.session.clear();
  h.meCalls[0].resolve(verified(alice)); await request;
  assert.equal(sessionMe(h.session.snapshot()), null);
  assert.equal(h.session.snapshot().verified, null);
});

test("the session provider no longer waits for /v1/me, and a new anonymous workspace needs no /v1/me", async () => {
  const provider = await readFile(new URL("../src/components/Session.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(provider, /await api\.me\(\)/);
  assert.match(provider, /live=\{verified\}/);
  const route = await readFile(new URL("../src/app/api/session/anonymous/route.ts", import.meta.url), "utf8");
  const provisioning = route.slice(route.indexOf("if (!principal)"), route.indexOf("const me = await fetch"));
  assert.match(provisioning, /return Response\.json\(provisionalMe\(created\), \{ status: 201/);
  const composer = await readFile(new URL("../src/components/Composer.tsx", import.meta.url), "utf8");
  assert.equal(composer.match(/refresh\(\{ adopt: created \}\)/g)?.length, 2);
  const cache = await readFile(new URL("../src/components/WorkspaceCache.tsx", import.meta.url), "utf8");
  assert.match(cache, /if \(!scope \|\| !live\) return;/);
  assert.match(cache, /force: event\.operation === "resync"/);
});

test("a just-provisioned anonymous Me keeps the 201 Me gateway contract field for field", async () => {
  const openapi = JSON.parse(await readFile(new URL("../../../api/openapi.json", import.meta.url), "utf8"));
  const me = provisionalMe(bob);
  assert.deepEqual(Object.keys(me).sort(), [...openapi.components.schemas.Me.required].sort());
  // services/backend fresh_principal('anonymous'): no profile, 30-day workspace expiry from provisioning.
  assert.deepEqual(me, { user_id: "bob", principal_kind: "anonymous", workspace_expires_at: bob.workspace_expires_at, display_name: null,
    email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null });
});
