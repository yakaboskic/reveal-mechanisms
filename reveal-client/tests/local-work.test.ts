import { test } from "node:test";
import assert from "node:assert/strict";
import { downloadLocalBlob, localClientPreference, localLaunchCommand, analysisAccountResults, activeLocalGrants, createLocalWorkClient, localConnectionConfig, localResearchPrompt, LocalWorkError, localWorkHref, submissionAccounts, type LocalWork } from "../src/lib/local-work";

test("manual remote configurations begin anonymously without bearer credentials", () => {
  const url = "https://research.example/mcp", id = "1234-5678";
  const codex = localConnectionConfig("codex", url, id);
  assert.doesNotMatch(codex, /bearer|token|Authorization/i);
  assert.match(codex, /mcp_servers.reveal_1234_5678/);
  const claude = JSON.parse(localConnectionConfig("claude_code", url, id));
  assert.equal(claude.mcpServers.reveal_1234_5678.headers, undefined);
  assert.equal(claude.mcpServers.reveal_1234_5678.url, url);
  const prompt = localResearchPrompt(id);
  assert.match(prompt, /1234-5678/);
  assert.match(prompt, /Begin with anonymous Reveal MCP access/);
  assert.match(prompt, /user chooses to sign in and approves access through connect_reveal/);
  assert.match(prompt, /Do not publish or request hosted statement generation automatically/);
  assert.doesNotMatch(prompt, /Bearer|REVEAL_MCP_TOKEN/);
});

test("credential-bearing or insecure MCP endpoint URLs are rejected before configuration is shown", () => {
  for (const url of ["https://user:secret@research.example/mcp", "https://research.example/mcp?token=secret", "https://research.example/mcp#secret", "http://research.example/mcp", "javascript:alert(1)"]) {
    assert.throws(() => localConnectionConfig("codex", url, "work"), /invalid MCP endpoint/);
  }
  assert.match(localConnectionConfig("claude_code", "http://127.0.0.1:8000/mcp", "work"), /127.0.0.1/);
});

test("local creation and retries use the local endpoint and exact original revision/key without launching a job", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = createLocalWorkClient(async (input, init) => { calls.push({ path: String(input), init }); return Response.json({ id: "work", state: "ready" }); });
  const body = { draft_id: "draft", draft_version: 3 };
  await client.create(body, "same-key"); await client.create(body, "same-key");
  for (const call of calls) {
    assert.equal(call.path, "/api/backend/v1/local-work");
    assert.equal(call.init?.method, "POST");
    assert.equal(call.init?.credentials, "same-origin");
    assert.equal(call.init?.cache, "no-store");
    assert.equal(new Headers(call.init?.headers).get("Idempotency-Key"), "same-key");
    assert.deepEqual(JSON.parse(String(call.init?.body)), body);
    assert.equal(new Headers(call.init?.headers).get("Authorization"), null);
  }
});

test("grant, revocation, close and history requests stay within the authenticated browser gateway", async () => {
  const paths: string[] = [], methods: string[] = [];
  const client = createLocalWorkClient(async (input, init) => { paths.push(String(input)); methods.push(init?.method || "GET"); return init?.method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({}); });
  await client.grant("work/id", "grant-key"); await client.revoke("work/id", "grant/id"); await client.close("work/id", "close-key"); await client.list("cursor+one"); await client.package("work/id");
  assert.deepEqual(paths, ["/api/backend/v1/local-work/work%2Fid/grants", "/api/backend/v1/local-work/work%2Fid/grants/grant%2Fid", "/api/backend/v1/local-work/work%2Fid/close", "/api/backend/v1/local-work?cursor=cursor%2Bone", "/api/backend/v1/local-work/work%2Fid/package"]);
  assert.deepEqual(methods, ["POST", "DELETE", "POST", "GET", "GET"]);
  assert.equal(localWorkHref("work/id"), "/local-runs/work%2Fid");
});

test("authorization failures retain status for clearing private research and credentials", async () => {
  const client = createLocalWorkClient(async () => Response.json({ code: "FORBIDDEN", detail: "Connection no longer authorized" }, { status: 403 }));
  await assert.rejects(client.get("work"), (error: unknown) => error instanceof LocalWorkError && error.status === 403 && error.code === "FORBIDDEN" && error.message === "Connection no longer authorized");
  await assert.rejects(client.package("work"), (error: unknown) => error instanceof LocalWorkError && error.status === 403);
});

test("revoked and expired grants cannot be presented as usable and reuse-only batches show their accounts", () => {
  const work = { grants: [
    { grant_id: "active", expires_at: "2030-01-01T00:00:00Z" },
    { grant_id: "revoked", expires_at: "2030-01-01T00:00:00Z", revoked_at: "2026-01-01T00:00:00Z" },
    { grant_id: "expired", expires_at: "2020-01-01T00:00:00Z" },
  ] } as LocalWork;
  assert.deepEqual(activeLocalGrants(work, Date.parse("2026-10-05T00:00:00Z")).map(grant => grant.grant_id), ["active"]);
  assert.deepEqual(submissionAccounts({ id: "batch", state: "accepted", account_ids: [], reused_account_ids: ["dapper:ScientificAccount.existing"] }), ["dapper:ScientificAccount.existing"]);
  assert.deepEqual(submissionAccounts({ id: "batch", state: "accepted", account_ids: ["same"], reused_account_ids: ["same"] }), ["same"]);
});

test("validation candidates and unaccepted submissions never become saved account links", () => {
  const candidates = { id: "batch", account_ids: ["candidate"], reused_account_ids: ["prior"] };
  assert.deepEqual(submissionAccounts({ ...candidates, state: "succeeded", validation_only: true }), []);
  assert.deepEqual(submissionAccounts({ ...candidates, state: "accepted", validation_only: true }), []);
  for (const state of [undefined, "received", "running", "succeeded", "rejected", "failed", "cancelled"]) {
    assert.deepEqual(submissionAccounts({ ...candidates, state }), [], `state=${state}`);
    assert.deepEqual(submissionAccounts({ id: "batch", status: state, result: { account_ids: ["candidate"] } }), [], `status=${state}`);
  }
  assert.deepEqual(submissionAccounts({ id: "batch", status: "accepted", result: { account_ids: ["saved"] } }), ["saved"]);
  assert.deepEqual(submissionAccounts({ ...candidates, state: "failed", status: "accepted" }), []);
});


test("online analysis results retain reuse-only findings and original authorship labels", () => {
  assert.deepEqual(analysisAccountResults({ account_ids: [], reused_account_ids: ["prior"] }), [{ id: "prior", reused: true }]);
  assert.deepEqual(analysisAccountResults({ account_ids: ["new"], reused_account_ids: ["prior", "new"] }), [{ id: "new", reused: false }, { id: "prior", reused: true }]);
});


test("workspace setup downloads the selected client ZIP privately with a bounded request", async t => {
  const timeouts: number[] = [], calls: { path: string; init?: RequestInit }[] = [];
  t.mock.method(AbortSignal, "timeout", (milliseconds: number) => { timeouts.push(milliseconds); return new AbortController().signal; });
  const bytes = new Uint8Array([80, 75, 3, 4, 0, 1]);
  const client = createLocalWorkClient(async (input, init) => { calls.push({ path: String(input), init }); return new Response(bytes, { headers: { "Content-Type": "application/zip", "Cache-Control": "private, no-store" } }); });
  for (const selected of ["codex", "claude_code"] as const) {
    const result = await client.setupKit("work/id", selected, new AbortController().signal);
    assert.deepEqual(new Uint8Array(await result.arrayBuffer()), bytes);
  }
  assert.deepEqual(timeouts, [120_000, 120_000]);
  for (const [index, call] of calls.entries()) {
    assert.equal(call.path, "/api/backend/v1/local-work/work%2Fid/setup-kit");
    assert.equal(call.init?.method, "POST"); assert.equal(call.init?.cache, "no-store");
    assert.equal(call.init?.credentials, "same-origin"); assert.equal(call.init?.redirect, "error");
    assert.deepEqual(JSON.parse(String(call.init?.body)), { client: index ? "claude_code" : "codex" });
    assert.equal(new Headers(call.init?.headers).get("Accept"), "application/zip");
    assert.equal(new Headers(call.init?.headers).get("Authorization"), null);
  }
});

test("setup failures never produce a workspace file and cancellation reaches the fetch", async () => {
  for (const [response, expected] of [[Response.json({ detail: "Access revoked", code: "FORBIDDEN" }, { status: 403 }), /Access revoked/], [Response.json({}), /workspace ZIP/], [new Response(null, { headers: { "Content-Type": "application/zip" } }), /empty/]] as const) {
    const client = createLocalWorkClient(async () => response);
    await assert.rejects(client.setupKit("work", "codex"), expected);
  }
  const controller = new AbortController(); let observed: AbortSignal | null | undefined;
  const client = createLocalWorkClient(async (_input, init) => { observed = init?.signal; return new Promise<Response>((_resolve, reject) => { init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true }); }); });
  const pending = client.setupKit("work", "claude_code", controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(observed?.aborted, true);
  let called = false;
  const cancelled = createLocalWorkClient(async () => { called = true; return new Response(); });
  await assert.rejects(cancelled.setupKit("work", "codex", controller.signal), { name: "AbortError" });
  assert.equal(called, false);
});

test("agent preferences contain only supported client names and launch commands", () => {
  assert.equal(localClientPreference("claude_code"), "claude_code");
  for (const value of [null, "codex", "rvls_secret", "invalid"]) assert.equal(localClientPreference(value), "codex");
  assert.equal(localLaunchCommand("codex"), "python3 start.py codex");
  assert.equal(localLaunchCommand("claude_code"), "python3 start.py claude");
  for (const mode of ["--login", "--logout", "--check-only", "--offline"] as const) {
    assert.equal(localLaunchCommand("codex", mode), `python3 start.py codex ${mode}`);
    assert.equal(localLaunchCommand("claude_code", mode), `python3 start.py claude ${mode}`);
  }
});

test("download URLs are released on abort and cannot be created after navigation cancellation", t => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "document");
  let clicked = 0, removed = 0, created = 0; const revoked: string[] = [];
  const anchor = { href: "", download: "", click: () => { clicked++; }, remove: () => { removed++; } };
  Object.defineProperty(globalThis, "document", { configurable: true, value: { createElement: () => anchor, body: { appendChild: () => {} } } });
  t.after(() => { if (original) Object.defineProperty(globalThis, "document", original); else Reflect.deleteProperty(globalThis, "document"); });
  t.mock.method(URL, "createObjectURL", () => { created++; return "blob:fixture"; });
  t.mock.method(URL, "revokeObjectURL", (url: string) => { revoked.push(url); });
  const controller = new AbortController();
  downloadLocalBlob(new Blob(["archive"]), "reveal-work.zip", controller.signal);
  assert.equal(clicked, 1); assert.equal(removed, 1); assert.equal(anchor.download, "reveal-work.zip");
  controller.abort(); assert.deepEqual(revoked, ["blob:fixture"]);
  assert.throws(() => downloadLocalBlob(new Blob(), "never.zip", controller.signal), { name: "AbortError" });
  assert.equal(created, 1);
});

test("workspace download reports real preparation and transfer stages without invented percentages", async () => {
  const progress: { phase: string; receivedBytes: number; totalBytes?: number }[] = [];
  let completeHeaders: (response: Response) => void = () => {};
  let body: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(controller) { body = controller; } });
  const client = createLocalWorkClient(async () => new Promise<Response>(resolve => { completeHeaders = resolve; }));
  const pending = client.setupKit("work", "codex", undefined, update => progress.push(update));
  assert.deepEqual(progress, [{ phase: "preparing", receivedBytes: 0 }]);
  completeHeaders(new Response(stream, { headers: { "Content-Type": "application/zip" } }));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(progress.at(-1), { phase: "downloading", receivedBytes: 0, totalBytes: undefined });
  body!.enqueue(new Uint8Array([80, 75]));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(progress.at(-1)?.receivedBytes, 2);
  body!.enqueue(new Uint8Array([3, 4])); body!.close();
  assert.deepEqual(new Uint8Array(await (await pending).arrayBuffer()), new Uint8Array([80, 75, 3, 4]));
  assert.equal(progress.at(-1)?.receivedBytes, 4);
});

test("a failed ZIP transfer can be retried without retaining partial bytes", async () => {
  let attempts = 0;
  const client = createLocalWorkClient(async () => {
    attempts++;
    if (attempts === 1) return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array([80, 75])); controller.error(new Error("Connection lost")); } }), { headers: { "Content-Type": "application/zip" } });
    return new Response(new Uint8Array([80, 75, 3, 4]), { headers: { "Content-Type": "application/zip", "Content-Length": "4" } });
  });
  await assert.rejects(client.setupKit("work", "codex"), /Connection lost/);
  const progress: { phase: string; receivedBytes: number; totalBytes?: number }[] = [];
  const result = await client.setupKit("work", "codex", undefined, value => progress.push(value));
  assert.equal(result.size, 4);
  assert.deepEqual(progress[0], { phase: "preparing", receivedBytes: 0 });
  assert.deepEqual(progress.at(-1), { phase: "downloading", receivedBytes: 4, totalBytes: 4 });
});

test("cancelling a stalled ZIP body releases the read and never returns a partial download", async () => {
  const controller = new AbortController(); let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({ start(body) { body.enqueue(new Uint8Array([80, 75])); }, cancel() { cancelled = true; } });
  const client = createLocalWorkClient(async () => new Response(stream, { headers: { "Content-Type": "application/zip" } }));
  const pending = client.setupKit("work", "codex", controller.signal);
  await new Promise(resolve => setImmediate(resolve));
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(cancelled, true);
});
