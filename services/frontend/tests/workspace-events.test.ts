import assert from "node:assert/strict";
import test from "node:test";
import { connectWorkspaceEvents, readWorkspaceFrames, type WorkspaceEvent } from "../src/lib/workspace-events";

const sample: WorkspaceEvent = { schema_version: 1, event_id: "scope:1", cursor: "1", scope: "workspace", event_type: "draft.changed", entity_id: "draft", entity_revision: 1, operation: "upsert", collections: ["drafts", "gaps"], committed_at: "2026-09-30T00:00:00Z" };
const frame = (event: string, data: unknown, id = "cursor") => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test("workspace SSE parser handles split UTF8 and CRLF frames without treating heartbeats as changes", async () => {
  const encoded = new TextEncoder().encode(frame("workspace_change", { ...sample, entity_id: "🧬" }).replaceAll("\n", "\r\n") + ": heartbeat\r\n\r\n");
  const response = new Response(new ReadableStream({ start(controller) {
    for (const byte of encoded) controller.enqueue(new Uint8Array([byte])); controller.close();
  } }));
  const received: unknown[] = [];
  await readWorkspaceFrames(response, value => received.push({ ...value, data: JSON.parse(value.data) }));
  assert.deepEqual(received, [{ event: "workspace_change", id: "cursor", data: { ...sample, entity_id: "🧬" } }]);
});

test("duplicate and reordered committed events apply once; ready does not refresh workspace lists", async () => {
  const abort = new AbortController(); const received: (WorkspaceEvent | undefined)[] = [], states: string[] = [];
  const body = frame("workspace_change", sample) + frame("workspace_change", sample) + frame("ready", { schema_version: 1 });
  const fake = (async () => new Response(body)) as typeof fetch;
  await connectWorkspaceEvents(abort.signal, { change: event => received.push(event), status: status => {
    states.push(status); if (status === "live") abort.abort();
  }, revoked: () => assert.fail("unexpected revocation") }, fake);
  assert.equal(received.length, 1); assert.equal(states.at(-1), "live");
});

test("expired identity clears private cache and does not reconnect", async () => {
  const controller = new AbortController(); let calls = 0, revoked = 0;
  const fake = (async () => { calls++; return new Response(null, { status: 401 }); }) as typeof fetch;
  await connectWorkspaceEvents(controller.signal, { change: () => assert.fail("unauthorized data"), status: () => {}, revoked: () => { revoked++; } }, fake);
  assert.equal(calls, 1); assert.equal(revoked, 1);
});

test("expired replay cursor triggers one explicit resync", async () => {
  const abort = new AbortController(); let resyncs = 0;
  const fake = (async () => new Response(frame("resync_required", { reason: "cursor_expired" }) + frame("ready", {}))) as typeof fetch;
  await connectWorkspaceEvents(abort.signal, { change: event => { assert.equal(event, undefined); resyncs++; }, status: status => {
    if (status === "live") abort.abort();
  }, revoked: () => {} }, fake);
  assert.equal(resyncs, 1);
});

test("workspace and account components contain no periodic data refresh loops", async () => {
  const { readFile } = await import("node:fs/promises");
  const provider = await readFile(new URL("../src/components/WorkspaceCache.tsx", import.meta.url), "utf8");
  const scientific = await readFile(new URL("../src/components/Scientific.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(provider, /setInterval/);
  assert.doesNotMatch(scientific, /setTimeout\(load/);
});

test("reconnect forwards the committed cursor without recurring list refresh", async () => {
  const abort = new AbortController(); let calls = 0, changes = 0;
  const fake = (async (_url: unknown, options: RequestInit) => {
    calls++;
    if (calls === 1) return new Response(frame("workspace_change", sample, "durable-cursor"));
    assert.equal(new Headers(options.headers).get("Last-Event-ID"), "durable-cursor");
    return new Response(frame("ready", {}, "durable-cursor"));
  }) as typeof fetch;
  await connectWorkspaceEvents(abort.signal, { change: () => { changes++; }, status: status => {
    if (status === "live") abort.abort();
  }, revoked: () => {} }, fake);
  assert.equal(calls, 2); assert.equal(changes, 1);
});

test("rotated cursor namespace resets once and resumes without an invalid-cursor reconnect loop", async () => {
  const abort = new AbortController(); let calls = 0, resyncs = 0;
  const fake = (async (_url: unknown, options: RequestInit) => {
    calls++;
    if (calls === 1) return new Response(frame("ready", {}, "old-cursor"));
    if (calls === 2) return Response.json({ code: "INVALID_CURSOR" }, { status: 400 });
    assert.equal(new Headers(options.headers).get("Last-Event-ID"), null);
    return new Response(frame("ready", {}, "new-cursor"));
  }) as typeof fetch;
  await connectWorkspaceEvents(abort.signal, { change: () => { resyncs++; }, status: status => {
    if (status === "live" && calls === 3) abort.abort();
  }, revoked: () => {} }, fake);
  assert.equal(calls, 3); assert.equal(resyncs, 1);
});
