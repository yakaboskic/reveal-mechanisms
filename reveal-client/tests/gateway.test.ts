import assert from "node:assert/strict";
import test from "node:test";
import { jwtVerify } from "jose";
import { allowedArtifactRedirect, assertion, configuration, proxyRequest, resolvePrincipal } from "../src/lib/gateway";
import type { Fetcher, Me } from "../src/lib/gateway";
import { demoConfiguration, handleSession, readSession, sessionCookie } from "../src/lib/session";
import { GET as sessionRoute } from "../src/app/api/session/route";

const env = { REVEAL_API_URL: "https://example.test/api/reveal", REVEAL_GATEWAY_SECRET: "s".repeat(40), REVEAL_GATEWAY_SERVICE_TOKEN: "t".repeat(40),
  REVEAL_GATEWAY_ISSUER: "reveal-nextjs", REVEAL_GATEWAY_AUDIENCE: "reveal-api", APP_ORIGIN: "http://localhost:3200", AUTH_SECRET: "a".repeat(40),
  REVEAL_DEMO_MODE: "true", DEMO_IDENTITY_ISSUER: "urn:reveal:client:dk", DEMO_IDENTITY_SUBJECT: "developer",
  REVEAL_ARTIFACT_DOWNLOAD_BASE_URLS: "https://bucket.test/qa/" };
const config = configuration(env);
const me: Me = { user_id: "11111111-1111-4111-8111-111111111111", principal_kind: "registered", display_name: null, email: null,
  email_verified: false, orcid: null, orcid_authenticated: false, person: null, workspace_expires_at: null };
const request = (method = "GET", extra: RequestInit = {}) => new Request("http://localhost:3200/api/session", { method, ...extra });
const json = (value: unknown, status = 200) => Response.json(value, { status });
const noFetch: Fetcher = async () => { throw new Error("Unexpected network request"); };
const decode = (token: string) => jwtVerify(token, new TextEncoder().encode(env.REVEAL_GATEWAY_SECRET), {
  algorithms: ["HS256"], issuer: env.REVEAL_GATEWAY_ISSUER, audience: env.REVEAL_GATEWAY_AUDIENCE });

test("gateway issues independent five-minute assertions and never purpose/admin claims", async () => {
  const first = await assertion(me, config), second = await assertion(me, config);
  const { payload } = await decode(first);
  assert.equal(payload.sub, me.user_id); assert.equal(payload.principal_kind, "registered");
  assert.equal(payload.exp! - payload.iat!, 300); assert.equal(payload.purpose, undefined); assert.notEqual(first, second);
});
test("Next session wrapper does not mistake route context for environment configuration", async () => {
  const previous = Object.fromEntries(Object.keys(env).map(name => [name, process.env[name]]));
  try {
    Object.assign(process.env, env);
    const response = await Reflect.apply(sessionRoute, null, [request(), { params: Promise.resolve({}) }]);
    assert.equal(response.status, 200); assert.deepEqual(await response.json(), { principal: null });
  } finally {
    for (const [name, value] of Object.entries(previous)) if (value === undefined) delete process.env[name]; else process.env[name] = value;
  }
});
test("missing setup and externally hosted demo fail before upstream", async () => {
  assert.equal((await handleSession(request("POST"), {}, noFetch)).status, 503);
  for (const [settings, url, host] of [
    [{ ...env, REVEAL_DEMO_MODE: "false" }, "http://localhost:3200/api/session", "localhost:3200"],
    [{ ...env, APP_ORIGIN: "https://demo.example" }, "https://demo.example/api/session", "demo.example"],
    [env, "http://127.0.0.1:3200/api/session", "127.0.0.1:3200"],
    [env, "http://localhost:3200/api/session", "attacker.example"],
  ] as const) {
    const result = await handleSession(new Request(url, { headers: { host } }), settings, noFetch);
    assert.equal(result.status, 503); assert.equal((await result.json()).code, "DEMO_DISABLED");
  }
  assert.throws(() => configuration({ ...env, REVEAL_API_URL: "https://user:password@example.test" }));
});
test("connect uses only configured identity; HttpOnly cookie is separate from gateway credentials", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fake: Fetcher = async (input, init) => {
    calls.push({ url: String(input), init });
    return calls.length === 1 ? json({ user_id: me.user_id, principal_kind: me.principal_kind }) : json(me);
  };
  const result = await handleSession(request("POST", { headers: { Origin: env.APP_ORIGIN }, body: "{}" }), env, fake);
  assert.equal(result.status, 200); assert.deepEqual(await result.json(), { principal: me });
  assert.equal(calls[0].url, config.backendUrl + "/internal/v1/principals/resolve");
  assert.deepEqual(JSON.parse(String(calls[0].init!.body)), { issuer: env.DEMO_IDENTITY_ISSUER, subject: env.DEMO_IDENTITY_SUBJECT,
    display_name: null, email: null, email_verified: false, orcid: null, orcid_authenticated: false });
  assert.equal(new Headers(calls[0].init?.headers).get("authorization"), "Bearer " + env.REVEAL_GATEWAY_SERVICE_TOKEN);
  const apiToken = new Headers(calls[1].init?.headers).get("authorization")!.slice(7);
  assert.equal((await decode(apiToken)).payload.sub, me.user_id);
  const cookie = result.headers.get("set-cookie")!;
  assert.match(cookie, /HttpOnly; SameSite=Strict/); assert.ok(!cookie.includes(apiToken)); assert.match(result.headers.get("cache-control")!, /no-store/);
  const incoming = request("GET", { headers: { Cookie: cookie.split(";")[0] } });
  assert.deepEqual(await readSession(incoming, demoConfiguration(incoming, env)), { user_id: me.user_id, principal_kind: "registered" });
  const switched = { ...env, DEMO_IDENTITY_SUBJECT: "another-user" };
  assert.equal(await readSession(incoming, demoConfiguration(incoming, switched)), null);
  const restored = await handleSession(incoming, env, async () => json(me));
  assert.deepEqual(await restored.json(), { principal: me });
  const retired = await handleSession(incoming, env, async () => json({}, 401));
  assert.deepEqual(await retired.json(), { principal: null }); assert.match(retired.headers.get("set-cookie")!, /Max-Age=0/);
});
test("session rejects browser identity fields, bad JSON and missing or foreign origins", async () => {
  for (const body of ['{"subject":"attacker"}', '{"user_id":"owner"}', 'null', '[]', '"string"', '{']) {
    assert.equal((await handleSession(request("POST", { headers: { Origin: env.APP_ORIGIN }, body }), env, noFetch)).status, 422);
  }
  for (const method of ["POST", "DELETE"]) for (const origin of [undefined, "https://attacker.example"]) {
    assert.equal((await handleSession(request(method, { headers: origin ? { Origin: origin } : {} }), env, noFetch)).status, 403);
  }
  const logout = await handleSession(request("DELETE", { headers: { Origin: env.APP_ORIGIN } }), env, noFetch);
  assert.equal(logout.status, 200); assert.match(logout.headers.get("set-cookie")!, /Max-Age=0/);
  const invalid = await handleSession(request("GET", { headers: { cookie: sessionCookie + "=forged" } }), env, noFetch);
  assert.deepEqual(await invalid.json(), { principal: null });
});
test("resolve rejects identity redirects and mismatched me response", async () => {
  await assert.rejects(resolvePrincipal({ issuer: "urn:test", subject: "a" }, config, async () => new Response(null, { status: 307, headers: { Location: "https://attacker.example" } })));
  let count = 0;
  await assert.rejects(resolvePrincipal({ issuer: "urn:test", subject: "a" }, config, async () => json(++count === 1 ? me : { ...me, user_id: "other" })));
});
test("proxy strips browser authority and cookies, preserves query/idempotency, and streams without buffering", async () => {
  let push!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(controller) { push = controller; } });
  let observed!: { url: string; init: RequestInit };
  const incoming = new Request("http://localhost:3200/api/backend/v1/jobs/job/events?after=3", { headers: {
    accept: "text/event-stream", authorization: "Bearer browser-secret", cookie: "private-cookie", "last-event-id": "3", "x-reveal-admin-assertion": "forged" } });
  const response = await proxyRequest(incoming, ["v1", "jobs", "job", "events"], me, config, async (input, init) => {
    observed = { url: String(input), init: init! }; return new Response(stream, { headers: { "Content-Type": "text/event-stream", "Set-Cookie": "bad" } });
  });
  assert.equal(observed.url, config.backendUrl + "/v1/jobs/job/events?after=3");
  const forwarded = new Headers(observed.init.headers);
  assert.equal(forwarded.get("cookie"), null); assert.equal(forwarded.get("x-reveal-admin-assertion"), null); assert.equal(forwarded.get("last-event-id"), "3");
  assert.equal((await decode(forwarded.get("authorization")!.slice(7))).payload.sub, me.user_id);
  assert.equal(observed.init.redirect, "manual"); assert.equal(response.headers.get("set-cookie"), null);
  push.enqueue(new TextEncoder().encode("id: 4\ndata: {}\n\n")); push.close();
  assert.equal(await response.text(), "id: 4\ndata: {}\n\n");
  let body!: string;
  await proxyRequest(new Request("http://localhost:3200/api/backend/v1/jobs", { method: "POST", headers: { Origin: env.APP_ORIGIN, "Idempotency-Key": "retry-1" }, body: "{}" }), ["v1", "jobs"], me, config, async (_, init) => {
    assert.equal(new Headers(init!.headers).get("idempotency-key"), "retry-1"); body = String(init!.body); return json({});
  });
  assert.equal(body, "{}");
});
test("proxy rejects non-v1 paths, path traversal and cross-origin writes before upstream", async () => {
  for (const path of [["internal", "v1", "principals"], ["v1", ".."], ["v1", "x/y"], ["v1", "x\\y"]]) assert.equal((await proxyRequest(request(), path, me, config, noFetch)).status, 404);
  for (const method of ["POST", "PATCH", "DELETE"]) assert.equal((await proxyRequest(request(method), ["v1", "jobs"], me, config, noFetch)).status, 403);
});
test("artifact redirects require exact route, configured HTTPS host and key prefix", async () => {
  const path = ["v1", "artifacts", "a".repeat(64)], good = "https://bucket.test/qa/objects/file?signature=private";
  assert.ok(allowedArtifactRedirect(path, good, config.artifactBaseUrls));
  for (const bad of ["https://bucket.test/prod/file", "https://bucket.test/qa/../prod/file", "https://bucket.test.evil/qa/file", "https://user@bucket.test/qa/file", "http://bucket.test/qa/file", good + "#fragment"]) {
    assert.equal(allowedArtifactRedirect(path, bad, config.artifactBaseUrls), false);
  }
  for (const [route, status, location, expected] of [[path, 307, good, 307], [["v1", "jobs"], 307, good, 502], [path, 302, good, 502], [path, 307, "https://attacker.test/qa/file", 502]] as const) {
    const result = await proxyRequest(request(), [...route], me, config, async () => new Response(null, { status, headers: { Location: location } }));
    assert.equal(result.status, expected);
  }
});
