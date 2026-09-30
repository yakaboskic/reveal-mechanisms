import assert from "node:assert/strict";
import test from "node:test";
import events from "../src/lib/events.ts";
import types from "../src/lib/types.ts";
const { createEventParser, followJob, followWorkspace, jobEvent } = events;
const { terminal } = types;

const encode = value => new TextEncoder().encode(value);
const item = (id, status = "running") => ({ id, job_id: "test-job", occurred_at: "2026-09-30T00:00:00Z", event_type: "progress", status,
  stage: status === "running" ? "preparing_evidence" : "complete", message: "Evidence β", result: null, detail: null });
const event = value => `id: ${value.id}\nevent: ${value.event_type}\ndata: ${JSON.stringify(value)}\n\n`;
const response = text => new Response(text, { headers: { "content-type": "text/event-stream" } });

test("parser preserves UTF-8 and split CRLF, ignores heartbeat comments, joins data lines", () => {
  const frames = [], parser = createEventParser(value => frames.push(value));
  const bytes = encode(": heartbeat\r\n\r\nid: 9\r\nevent: progress\r\ndata: β evidence\r\ndata: second line\r\n\r\n");
  for (const byte of bytes) parser.push(new Uint8Array([byte]));
  parser.finish();
  assert.deepEqual(frames, [{ id: "9", event: "progress", data: "β evidence\nsecond line" }]);
});

test("parser handles every split boundary, CR-only frames and colonless data", () => {
  const value = "event: ready\rid: signed-cursor\rdata: {}\r\rdata\n\n", bytes = encode(value);
  for (let boundary = 0; boundary <= bytes.length; boundary++) {
    const frames = [], parser = createEventParser(frame => frames.push(frame));
    parser.push(bytes.slice(0, boundary)); parser.push(bytes.slice(boundary)); parser.finish();
    assert.deepEqual(frames, [{ event: "ready", id: "signed-cursor", data: "{}" }, { event: "message", data: "" }]);
  }
});

test("incomplete EOF data is discarded and NUL cursor is ignored", () => {
  const frames = [], parser = createEventParser(frame => frames.push(frame));
  parser.push(encode("id: forbidden\0cursor\ndata: complete\n\ndata: incomplete")); parser.finish();
  assert.deepEqual(frames, [{ event: "message", data: "complete" }]);
});

test("oversized event is rejected before unbounded buffering", () => {
  const parser = createEventParser(() => {});
  assert.throws(() => parser.push(encode("data:" + "x".repeat(2_000_001))), /size/);
});

test("controls are distinct from job data; duplicate cursors are ignored with bigint precision", () => {
  for (const name of ["ready", "connection_degraded", "resync_required"])
    assert.equal(jobEvent({ event: name, data: "{}" }, "test-job", "0"), null);
  const value = item("9007199254740993");
  assert.equal(jobEvent({ event: "progress", id: value.id, data: JSON.stringify(value) }, "test-job", value.id), null);
  assert.deepEqual(jobEvent({ event: "progress", id: value.id, data: JSON.stringify(value) }, "test-job", "9007199254740992"), value);
});

test("foreign jobs, mismatched frame cursors and malformed events cannot advance replay", () => {
  for (const value of [null, {}, { ...item("1"), job_id: "another-job" }, { ...item("1"), id: "nonnumeric" }, { ...item("1"), status: "unknown" }])
    assert.throws(() => jobEvent({ event: "progress", data: JSON.stringify(value) }, "test-job", "0"));
  assert.throws(() => jobEvent({ event: "progress", id: "2", data: JSON.stringify(item("1")) }, "test-job", "0"));
});

test("every real terminal state stops, cancellation requests do not", () => {
  for (const status of ["succeeded", "failed", "cancelled", "insufficient_evidence"]) assert.equal(terminal(status), true);
  for (const status of ["queued", "running", "cancel_requested"]) assert.equal(terminal(status), false);
});

test("job reconnect preserves cursor across network failure, deduplicates replay and stops at terminal", async context => {
  const requests = [], delivered = [], states = [];
  const answers = [response(event(item("1"))), new TypeError("Connection lost"),
    response(event(item("1")) + event(item("2", "insufficient_evidence")) + event(item("3")))];
  context.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, cursor: options.headers["Last-Event-ID"], credentials: options.credentials });
    const value = answers.shift(); if (value instanceof Error) throw value; return value;
  });
  await followJob("test-job", { signal: new AbortController().signal, onState: state => states.push(state),
    onEvent: value => delivered.push(value.id), onResync: async () => { throw new Error("No resync expected"); } });
  assert.deepEqual(delivered, ["1", "2"]);
  assert.deepEqual(requests.map(value => value.cursor), ["0", "1", "1"]);
  assert.ok(requests.every(value => value.url.endsWith("/events") && value.credentials === "same-origin"));
  assert.ok(states.some(value => value.includes("interrupted")));
  assert.equal(answers.length, 0);
});

test("expired replay performs one explicit recovery read and stops if saved state is terminal", async context => {
  let calls = 0, recovery = 0;
  context.mock.method(globalThis, "fetch", async () => { calls++; return new Response(JSON.stringify({ code: "EVENT_CURSOR_EXPIRED" }), { status: 409 }); });
  await followJob("test-job", { signal: new AbortController().signal, onState() {}, onEvent() {},
    onResync: async () => { recovery++; return { cursor: "100", terminal: true }; } });
  assert.equal(calls, 1); assert.equal(recovery, 1);
});

test("revoked job credentials fail once without polling or reconnecting", async context => {
  let calls = 0;
  context.mock.method(globalThis, "fetch", async () => { calls++; return new Response(JSON.stringify({ detail: "Reconnect workspace" }), { status: 401 }); });
  await assert.rejects(followJob("test-job", { signal: new AbortController().signal, onState() {}, onEvent() {}, onResync: async () => ({ cursor: "0", terminal: false }) }), /Reconnect workspace/);
  assert.equal(calls, 1);
});

test("workspace controls preserve opaque cursor and never trigger job-status requests", async context => {
  const controller = new AbortController(), requests = [], changes = [];
  const answers = [response('event: ready\nid: opaque.1\ndata: {}\n\n'),
    response('event: resync_required\nid: opaque.2\ndata: {}\n\nevent: access_revoked\ndata: {}\n\n')];
  context.mock.method(globalThis, "fetch", async (url, options) => { requests.push({ url, cursor: options.headers["Last-Event-ID"] }); return answers.shift(); });
  await assert.rejects(followWorkspace({ signal: controller.signal, onState() {}, onChange: change => changes.push(change) }), /Reconnect/);
  assert.deepEqual(changes, [null]);
  assert.deepEqual(requests.map(value => value.cursor), [undefined, "opaque.1"]);
  assert.ok(requests.every(value => value.url.endsWith("/me/workspace/events")));
});
