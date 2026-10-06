import { test } from "node:test";
import assert from "node:assert/strict";
import { consentLookup, consentPagePath, consentRedirect, createResearchConsentClient, ResearchConsentError } from "../src/lib/research-consent";

test("consent links preserve one known request and exclude unrelated callback parameters", () => {
  assert.equal(consentLookup(new URLSearchParams()), null);
  assert.deepEqual(consentLookup(new URLSearchParams("user_code=abcd-1234&redirect_url=https://untrusted.example")), { user_code: "ABCD-1234" });
  const request = consentLookup(new URLSearchParams("request_id=request_123&client_name=Untrusted&state=other"));
  assert.equal(consentPagePath(request), "/research/connect?request_id=request_123");
  assert.equal(consentPagePath(null), "/research/connect");
  for (const query of ["request_id=one&user_code=ABCDEFGH", "request_id=request_123&request_id=request_456", "user_code=ABCD&user_code=EFGH", "request_id=", "request_id=//evil.test", "user_code=javascript:alert(1)", `request_id=${"a".repeat(201)}`]) assert.throws(() => consentLookup(new URLSearchParams(query)));
});

test("only safe server callbacks are navigable, with exact code and state bytes retained", () => {
  for (const url of ["https://client.example/callback?code=code&state=a%2Fb+%3D", "http://127.0.0.1:8765/callback?code=code&state=state", "http://[::1]:8000/callback?error=access_denied&state=state"]) assert.equal(consentRedirect(url), url);
  for (const url of ["javascript:alert(1)", "data:text/html,unsafe", "//client.example/callback", "https://user:secret@client.example/callback", "http://remote.example/callback", "/callback"]) assert.throws(() => consentRedirect(url));
});

test("reading consent never approves it, browser identity stays in same-origin gateway", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = createResearchConsentClient(async (input, init) => { calls.push({ path: String(input), init }); return Response.json({ request_id: "canonical-request" }); });
  await client.get({ user_code: "ABCD-EFGH" }); await client.get({ request_id: "opaque-request" });
  assert.deepEqual(calls.map(call => call.path), ["/api/backend/v1/research-oauth/consent?user_code=ABCD-EFGH", "/api/backend/v1/research-oauth/consent?request_id=opaque-request"]);
  for (const { init } of calls) {
    assert.equal(init?.method, "GET"); assert.equal(init?.body, undefined);
    assert.equal(init?.credentials, "same-origin"); assert.equal(init?.cache, "no-store"); assert.equal(init?.redirect, "error");
    assert.equal(new Headers(init?.headers).get("Authorization"), null);
  }
});

test("explicit approval and denial send only canonical request, selected work, and decision", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = createResearchConsentClient(async (input, init) => { calls.push({ path: String(input), init }); return Response.json({ approved: true }); });
  await client.decide("canonical-request", true, "owned-work"); await client.decide("canonical-request", false);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { request_id: "canonical-request", approve: true, local_work_id: "owned-work" });
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), { request_id: "canonical-request", approve: false });
  for (const { path, init } of calls) { assert.equal(path, "/api/backend/v1/research-oauth/consent"); assert.equal(init?.method, "POST"); assert.equal(new Headers(init?.headers).get("Authorization"), null); }
});

test("anonymous workspace promotion is separate explicit consent with recoverable request key", async () => {
  let call: { path: string; init?: RequestInit } | undefined;
  const client = createResearchConsentClient(async (input, init) => { call = { path: String(input), init }; return Response.json({}); });
  await client.claim("same-claim-key");
  assert.equal(call?.path, "/api/session/claim"); assert.equal(call?.init?.method, "POST");
  assert.deepEqual(JSON.parse(String(call?.init?.body)), { consent: true });
  assert.equal(new Headers(call?.init?.headers).get("Idempotency-Key"), "same-claim-key");
  assert.equal(new Headers(call?.init?.headers).get("Authorization"), null);
});

test("sign-in and expired-request failures stay actionable; aborted pages send no decision", async () => {
  const client = createResearchConsentClient(async () => Response.json({ code: "SIGN_IN_REQUIRED", detail: "Sign in before approval" }, { status: 403 }));
  await assert.rejects(client.get({ request_id: "request-123" }), (error: unknown) => error instanceof ResearchConsentError && error.status === 403 && error.code === "SIGN_IN_REQUIRED");
  const controller = new AbortController(); controller.abort(); let calls = 0;
  const cancelled = createResearchConsentClient(async () => { calls++; return Response.json({}); });
  await assert.rejects(cancelled.decide("request-123", true, "work", controller.signal), { name: "AbortError" });
  assert.equal(calls, 0);
});
