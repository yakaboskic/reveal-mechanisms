import assert from "node:assert/strict";
import test from "node:test";
import { jwtVerify, SignJWT } from "jose";
import { configuration, createAnonymousPrincipal, proxyRequest } from "../src/lib/gateway";
import type { Fetcher, Me } from "../src/lib/gateway";
import { handleSession, readSession, sessionConfiguration, sessionCookie } from "../src/lib/session";
const env = {
  REVEAL_API_URL: "https://api.example.test/api/reveal", REVEAL_GATEWAY_SECRET: "g".repeat(40), REVEAL_GATEWAY_SERVICE_TOKEN: "s".repeat(40),
  REVEAL_GATEWAY_ISSUER: "reveal-nextjs", REVEAL_GATEWAY_AUDIENCE: "reveal-api", APP_ORIGIN: "https://reveal-client.example.test",
  AUTH_SECRET: "a".repeat(40), REVEAL_SESSION_MODE: "guest",
};
const request = (method = "GET", options: RequestInit = {}) => new Request(env.APP_ORIGIN + "/api/session", { method, ...options });
const connect = (cookie?: string, body = "{}") => request("POST", { headers: { Origin: env.APP_ORIGIN, "Idempotency-Key": "browser-key", ...(cookie ? { Cookie: cookie } : {}) }, body });
const cookieFrom = (response: Response) => response.headers.get("set-cookie")!.split(";")[0];
const noFetch: Fetcher = async () => { throw new Error("Unexpected upstream call"); };
const guest = (id: string): Me => ({ user_id: id, principal_kind: "anonymous", display_name: null, email: null,
  email_verified: false, orcid: null, orcid_authenticated: false, person: null, workspace_expires_at: new Date(Date.now() + 30 * 86400000).toISOString() });
const first = guest("11111111-1111-4111-8111-111111111111"), second = guest("22222222-2222-4222-8222-222222222222");
const decodeAssertion = async (init?: RequestInit) => (await jwtVerify(new Headers(init?.headers).get("authorization")!.slice(7), new TextEncoder().encode(env.REVEAL_GATEWAY_SECRET), {
  algorithms: ["HS256"], issuer: env.REVEAL_GATEWAY_ISSUER, audience: env.REVEAL_GATEWAY_AUDIENCE,
})).payload;
function backend() {
  const created: { id: string; key: string }[] = [];
  const fetcher: Fetcher = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/internal/v1/principals/anonymous")) {
      const headers = new Headers(init?.headers);
      assert.equal(headers.get("authorization"), "Bearer " + env.REVEAL_GATEWAY_SERVICE_TOKEN);
      assert.equal(init?.body, "{}"); assert.equal(init?.redirect, "error");
      const principal = created.length ? second : first;
      created.push({ id: principal.user_id, key: headers.get("idempotency-key")! });
      return Response.json(principal, { status: 201 });
    }
    assert.ok(url.endsWith("/v1/me"));
    const payload = await decodeAssertion(init);
    assert.equal(payload.principal_kind, "anonymous"); assert.equal(payload.purpose, undefined);
    return Response.json(payload.sub === first.user_id ? first : second);
  };
  return { fetcher, created };
}
test("hosted guests get isolated anonymous workspaces, server retry keys and cookies bounded by backend expiry", async () => {
  const { fetcher, created } = backend();
  const a = await handleSession(connect(), env, fetcher), b = await handleSession(connect(), env, fetcher);
  assert.equal(a.status, 200); assert.equal(b.status, 200);
  assert.deepEqual(await a.json(), { principal: first }); assert.deepEqual(await b.json(), { principal: second });
  assert.notEqual(created[0].id, created[1].id); assert.notEqual(created[0].key, created[1].key);
  for (const creation of created) assert.match(creation.key, /^[a-f0-9-]{36}$/);
  const cookie = a.headers.get("set-cookie")!;
  assert.match(cookie, /HttpOnly; SameSite=Strict; Max-Age=\d+; Secure$/);
  assert.equal(cookie.includes(env.REVEAL_GATEWAY_SECRET), false); assert.equal(cookie.includes(env.REVEAL_GATEWAY_SERVICE_TOKEN), false);
  assert.match(a.headers.get("cache-control")!, /private, no-store/);
  const incoming = request("GET", { headers: { Cookie: cookieFrom(a) } });
  assert.deepEqual(await readSession(incoming, sessionConfiguration(incoming, env)), { user_id: first.user_id, principal_kind: "anonymous" });
  const { payload } = await jwtVerify(cookieFrom(a).slice(sessionCookie.length + 1), new TextEncoder().encode(env.AUTH_SECRET));
  assert.equal(payload.exp, Math.floor(Date.parse(first.workspace_expires_at!) / 1000)); assert.ok(payload.exp! - payload.iat! <= 30 * 86400);
});
test("reload and repeated Connect preserve guest identity and deadline; proxy asserts only this anonymous identity", async () => {
  const { fetcher, created } = backend();
  const a = await handleSession(connect(), env, fetcher), cookie = cookieFrom(a);
  const reconnect = await handleSession(connect(cookie), env, fetcher);
  assert.equal(reconnect.status, 200); assert.deepEqual(await reconnect.json(), { principal: first });
  const incoming = request("GET", { headers: { Cookie: cookie } });
  assert.deepEqual(await (await handleSession(incoming, env, fetcher)).json(), { principal: first }); assert.equal(created.length, 1);
  const original = await jwtVerify(cookie.slice(sessionCookie.length + 1), new TextEncoder().encode(env.AUTH_SECRET));
  const renewed = await jwtVerify(cookieFrom(reconnect).slice(sessionCookie.length + 1), new TextEncoder().encode(env.AUTH_SECRET));
  assert.equal(renewed.payload.exp, original.payload.exp);
  const config = sessionConfiguration(incoming, env), principal = await readSession(incoming, config);
  const result = await proxyRequest(new Request(env.APP_ORIGIN + "/api/backend/v1/drafts"), ["v1", "drafts"], principal, config, async (_, init) => {
    const payload = await decodeAssertion(init);
    assert.equal(payload.sub, first.user_id); assert.equal(payload.principal_kind, "anonymous"); return Response.json({ items: [] });
  });
  assert.equal(result.status, 200);
});
test("guest cookies cannot cross apps, backends, modes or signing authority; browser identity fields are rejected", async () => {
  const { fetcher } = backend(), cookie = cookieFrom(await handleSession(connect(), env, fetcher));
  for (const settings of [{ ...env, APP_ORIGIN: "https://other.example.test" }, { ...env, REVEAL_API_URL: "https://other-api.example.test" }, { ...env, AUTH_SECRET: "other".repeat(10) }]) {
    const incoming = new Request(settings.APP_ORIGIN + "/api/session", { headers: { Cookie: cookie } });
    assert.equal(await readSession(incoming, sessionConfiguration(incoming, settings)), null);
  }
  const demo = { ...env, REVEAL_SESSION_MODE: "demo", APP_ORIGIN: "http://localhost:3200", REVEAL_DEMO_MODE: "true", DEMO_IDENTITY_ISSUER: "urn:demo", DEMO_IDENTITY_SUBJECT: "developer" };
  const local = new Request(demo.APP_ORIGIN + "/api/session", { headers: { Cookie: cookie } });
  assert.equal(await readSession(local, sessionConfiguration(local, demo)), null);
  for (const body of ['{"user_id":"other"}', '{"subject":"developer"}', '{"principal_kind":"registered"}', '{"idempotency_key":"known-key"}']) {
    assert.equal((await handleSession(connect(undefined, body), env, noFetch)).status, 422);
  }
  const forged = await new SignJWT({ principal_kind: "anonymous" }).setProtectedHeader({ alg: "HS256" }).setSubject(first.user_id)
    .setIssuer("reveal-client-guest").setAudience(JSON.stringify([env.APP_ORIGIN, env.REVEAL_API_URL, "guest"]))
    .setIssuedAt().setExpirationTime("1d").sign(new TextEncoder().encode("wrong".repeat(10)));
  const invalid = await handleSession(request("GET", { headers: { Cookie: sessionCookie + "=" + forged } }), env, noFetch);
  assert.deepEqual(await invalid.json(), { principal: null });
});
test("guest endpoints require configured HTTPS origin and reject cross-origin writes before provisioning", async () => {
  for (const method of ["POST", "DELETE"]) for (const origin of [undefined, "https://attacker.example.test"]) {
    assert.equal((await handleSession(request(method, { headers: origin ? { Origin: origin } : {} }), env, noFetch)).status, 403);
  }
  for (const [settings, incoming] of [
    [{ ...env, APP_ORIGIN: "http://localhost:3200" }, new Request("http://localhost:3200/api/session")],
    [env, new Request("https://attacker.example.test/api/session")],
    [env, request("GET", { headers: { Host: "attacker.example.test" } })],
  ] as const) assert.equal((await handleSession(incoming, settings, noFetch)).status, 503);
  assert.equal((await handleSession(request(), { ...env, REVEAL_SESSION_MODE: "typo" }, noFetch)).status, 503);
  assert.equal((await handleSession(request(), { ...env, REVEAL_SESSION_MODE: undefined, REVEAL_DEMO_MODE: "true" }, noFetch)).status, 503);
});
test("disconnect clears access without backend mutation; expired backend sessions retire and reconnect independently", async () => {
  const { fetcher } = backend(), cookie = cookieFrom(await handleSession(connect(), env, fetcher));
  const disconnected = await handleSession(request("DELETE", { headers: { Cookie: cookie, Origin: env.APP_ORIGIN } }), env, noFetch);
  assert.deepEqual(await disconnected.json(), { principal: null }); assert.match(disconnected.headers.get("set-cookie")!, /Max-Age=0; Secure/);
  const expired = await handleSession(request("GET", { headers: { Cookie: cookie } }), env, async () => Response.json({}, { status: 401 }));
  assert.deepEqual(await expired.json(), { principal: null }); assert.match(expired.headers.get("set-cookie")!, /Max-Age=0/);
  let count = 0;
  const renewed = await handleSession(connect(cookie), env, async (input, init) => ++count === 1 ? Response.json({}, { status: 401 }) : fetcher(input, init));
  assert.deepEqual(await renewed.json(), { principal: second });
});
test("invalid provision or expiry cannot establish guests; transient failures preserve existing sessions", async () => {
  await assert.rejects(createAnonymousPrincipal(configuration(env), async () => Response.json({ ...first, principal_kind: "registered" })));
  for (const expiry of [null, "invalid", new Date(Date.now() - 10000).toISOString()]) {
    const response = await handleSession(connect(), env, async () => Response.json({ ...first, workspace_expires_at: expiry }));
    assert.equal(response.status, 502); assert.equal(response.headers.get("set-cookie"), null);
  }
  const { fetcher } = backend(), connected = await handleSession(connect(), env, fetcher);
  let calls = 0;
  const failure = await handleSession(connect(cookieFrom(connected)), env, async input => {
    calls++; assert.ok(String(input).endsWith("/v1/me")); return Response.json({}, { status: 503 });
  });
  assert.equal(failure.status, 502); assert.equal(failure.headers.get("set-cookie"), null); assert.equal(calls, 1);
});

test("a stale tab cannot write under the replacement cookie's guest identity", async () => {
  const config = configuration(env);
  for (const workspace of [undefined, first.user_id]) {
    const incoming = new Request(env.APP_ORIGIN + "/api/backend/v1/drafts", { method: "POST", body: "{}", headers: {
      Origin: env.APP_ORIGIN, ...(workspace ? { "X-Reveal-Workspace-ID": workspace } : {}),
    } });
    const rejected = await proxyRequest(incoming, ["v1", "drafts"], second, config, noFetch);
    assert.equal(rejected.status, 409); assert.equal((await rejected.json()).code, "WORKSPACE_CHANGED");
  }
  const incoming = new Request(env.APP_ORIGIN + "/api/backend/v1/drafts", { method: "POST", body: "{}", headers: {
    Origin: env.APP_ORIGIN, "X-Reveal-Workspace-ID": second.user_id,
  } });
  const accepted = await proxyRequest(incoming, ["v1", "drafts"], second, config, async (_, init) => {
    assert.equal((await decodeAssertion(init)).sub, second.user_id);
    assert.equal(new Headers(init?.headers).get("x-reveal-workspace-id"), null);
    return Response.json({});
  });
  assert.equal(accepted.status, 200);
});
