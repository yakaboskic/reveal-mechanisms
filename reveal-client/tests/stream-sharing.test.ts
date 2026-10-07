import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { alwaysVisible, followJob, followWorkspace, pageVisibility, parkHiddenAfterMs, type StreamSharing, type Visibility } from "../src/lib/events";
import type { JobEvent, WorkspaceEvent } from "../src/lib/types";

const settle = async (rounds = 6) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setTimeout(resolve, 2)); };
const change = (position: number) => ({ schema_version: 1, event_id: `scope:${position}`, cursor: String(position), scope: "workspace", event_type: "draft.changed",
  entity_id: `draft-${position}`, entity_revision: 1, operation: "upsert", collections: ["drafts"], committed_at: "2026-10-07T00:00:00Z" } as WorkspaceEvent);
const frame = (event: string, data: unknown, id?: string) => `${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const item = (id: string, status = "running") => ({ id, job_id: "job-1", occurred_at: "2026-10-07T00:00:00Z", event_type: "progress", status,
  stage: "preparing_evidence", message: "Evidence", result: null, detail: null } as unknown as JobEvent);
let owners = 0;
const owner = () => `user-${process.pid}-${++owners}`;

/** fetch replacement: each request gets `answer(index)` or an SSE body that stays open until it is aborted. */
function server(context: { mock: { method: (object: object, name: string, fn: unknown) => unknown } }, answer?: (index: number) => Response | undefined) {
  const opened: { url: string; cursor: string | null; signal: AbortSignal; push: (text: string) => void }[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    const fixed = answer?.(opened.length), signal = options.signal!;
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start: controller => { stream = controller; } });
    opened.push({ url, cursor: new Headers(options.headers).get("Last-Event-ID"), signal, push: text => stream.enqueue(new TextEncoder().encode(text)) });
    if (fixed) return fixed;
    signal.addEventListener("abort", () => { try { stream.error(new DOMException("Aborted", "AbortError")); } catch { /* closed */ } });
    return new Response(body, { headers: { "content-type": "text/event-stream" } });
  });
  return opened;
}

function locks() {
  const held = new Set<string>(), queues = new Map<string, (() => void)[]>();
  const release = (name: string) => { const next = queues.get(name)?.shift(); if (next) next(); else held.delete(name); };
  const request = (name: string, options: { signal?: AbortSignal }, callback: () => Promise<unknown>) => new Promise((resolve, reject) => {
    const run = () => queueMicrotask(() => { void Promise.resolve().then(callback).then(resolve, reject).finally(() => release(name)); });
    if (options.signal?.aborted) return reject(new DOMException("Aborted", "AbortError"));
    if (!held.has(name)) { held.add(name); return run(); }
    const queue = queues.get(name) || []; queues.set(name, queue);
    const cancel = () => { queue.splice(queue.indexOf(granted), 1); reject(new DOMException("Aborted", "AbortError")); };
    const granted = () => { options.signal?.removeEventListener("abort", cancel); run(); };
    queue.push(granted); options.signal?.addEventListener("abort", cancel, { once: true });
  });
  return { request } as unknown as NonNullable<StreamSharing["locks"]>;
}
const sharing = (lockManager: NonNullable<StreamSharing["locks"]>): StreamSharing => ({ locks: lockManager, channel: name => new BroadcastChannel(name) });

function page(hidden = false) {
  const waiting = new Set<() => void>(), timers = new Set<() => void>();
  const value: Visibility & { park(): void; show(): void } = {
    hidden: () => hidden,
    untilVisible: signal => new Promise(resolve => { if (!hidden) return resolve(); waiting.add(() => resolve()); signal.addEventListener("abort", () => resolve(), { once: true }); }),
    onHiddenFor: (_ms, callback) => { timers.add(callback); return () => { timers.delete(callback); }; },
    park: () => { hidden = true; for (const callback of [...timers]) callback(); },
    show: () => { hidden = false; for (const done of [...waiting]) done(); waiting.clear(); },
  };
  return value;
}

test("tabs of one user share one workspace stream and the next tab resumes from the last broadcast cursor", async context => {
  const opened = server(context), user = owner(), shared = locks();
  const a: (string | undefined)[] = [], b: (string | undefined)[] = [], states: string[] = [];
  const closeA = new AbortController(), closeB = new AbortController();
  const leading = followWorkspace({ signal: closeA.signal, owner: user, sharing: sharing(shared), visibility: alwaysVisible, onState() {}, onChange: event => a.push(event?.entity_id) }); await settle();
  const following = followWorkspace({ signal: closeB.signal, owner: user, sharing: sharing(shared), visibility: alwaysVisible, onState: state => states.push(state), onChange: event => b.push(event?.entity_id) }); await settle();
  assert.equal(opened.length, 1);
  opened[0].push(frame("workspace_change", change(1), "c1") + frame("ready", {}, "c2")); await settle();
  assert.deepEqual(a, ["draft-1"]); assert.deepEqual(b, ["draft-1"]); assert.equal(states.at(-1), "Workspace live");
  closeA.abort(); await leading; await settle();
  assert.equal(opened.length, 2); assert.equal(opened[1].cursor, "c2");
  closeB.abort(); await following;
});

test("a revoked shared stream rejects in every tab and none of them reconnects", async context => {
  const opened = server(context, index => index === 0 ? new Response(JSON.stringify({ code: "SESSION_EXPIRED", detail: "Reconnect your workspace" }), { status: 401 }) : undefined);
  const user = owner(), shared = locks(), closeA = new AbortController(), closeB = new AbortController();
  const options = (signal: AbortSignal) => ({ signal, owner: user, sharing: sharing(shared), visibility: alwaysVisible, onState() {}, onChange() {} });
  const results = await Promise.allSettled([followWorkspace(options(closeA.signal)), followWorkspace(options(closeB.signal))]);
  assert.deepEqual(results.map(result => result.status), ["rejected", "rejected"]);
  assert.match(String((results[1] as PromiseRejectedResult).reason?.message), /Reconnect your workspace/);
  closeA.abort(); await settle();
  assert.equal(opened.length, 1);
  closeB.abort(); await settle();
});

test("a leader parked while it waits to retry hands over without failing the other tabs", async context => {
  const opened = server(context, index => index === 0 ? new Response(JSON.stringify({ code: "SERVICE_UNAVAILABLE" }), { status: 503 }) : undefined);
  const user = owner(), shared = locks(), hiddenLeader = page(), close = new AbortController(), errors: unknown[] = [];
  const runs = [followWorkspace({ signal: close.signal, owner: user, sharing: sharing(shared), visibility: hiddenLeader, onState() {}, onChange() {} }).catch(error => { errors.push(error); })];
  await settle();
  runs.push(followWorkspace({ signal: close.signal, owner: user, sharing: sharing(shared), visibility: alwaysVisible, onState() {}, onChange() {} }).catch(error => { errors.push(error); }));
  await settle(); assert.equal(opened.length, 1);
  hiddenLeader.park(); await settle();
  assert.deepEqual(errors, []); assert.equal(opened.length, 2);
  close.abort(); await Promise.all(runs);
});

test("without an owner each tab streams for itself", async context => {
  const opened = server(context), close = new AbortController();
  const runs = [followWorkspace({ signal: close.signal, sharing: sharing(locks()), visibility: alwaysVisible, onState() {}, onChange() {} }),
    followWorkspace({ signal: close.signal, sharing: sharing(locks()), visibility: alwaysVisible, onState() {}, onChange() {} })];
  await settle(); assert.equal(opened.length, 2);
  close.abort(); await Promise.all(runs);
});

test("a hidden tab parks its job stream without a failure and resumes from the cursor once shown", async context => {
  const opened = server(context), visibility = page(), close = new AbortController(), states: string[] = [], delivered: string[] = [];
  const run = followJob("job-1", { signal: close.signal, visibility, onState: state => states.push(state), onEvent: value => delivered.push(value.id),
    onResync: async () => { throw new Error("No resync expected"); } });
  await settle(); opened[0].push(`id: 4\nevent: progress\ndata: ${JSON.stringify(item("4"))}\n\n`); await settle();
  visibility.park(); await settle();
  assert.equal(opened.length, 1); assert.equal(opened[0].signal.aborted, true);
  assert.ok(!states.some(state => state.includes("interrupted")));
  visibility.show(); await settle();
  assert.equal(opened.length, 2); assert.equal(opened[1].cursor, "4"); assert.deepEqual(delivered, ["4"]);
  close.abort(); await run;
});

test("the visibility source parks only after a full minute hidden", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const doc = Object.assign(new EventTarget(), { visibilityState: "visible" as DocumentVisibilityState });
  let parked = 0; const stop = pageVisibility(doc).onHiddenFor(parkHiddenAfterMs, () => { parked++; });
  doc.visibilityState = "hidden"; doc.dispatchEvent(new Event("visibilitychange"));
  context.mock.timers.tick(parkHiddenAfterMs - 1); assert.equal(parked, 0);
  context.mock.timers.tick(1); assert.equal(parked, 1);
  stop();
});

test("the home page shares its workspace stream by user and reads a finished job only once", async () => {
  const page = await readFile(new URL("../src/app/page.tsx", import.meta.url), "utf8");
  assert.match(page, /followWorkspace\(\{ signal: controller\.signal, owner: principal\.user_id,/);
  const terminalEvent = page.slice(page.indexOf("if (!terminal(event.status)) return;"), page.indexOf("void readLatest().then(() => refresh([\"jobs\"]))"));
  assert.match(terminalEvent, /if \(known\?\.id === id && terminal\(known\.status\) && BigInt\(known\.last_event_id\) >= BigInt\(event\.id\)\) return;/);
});
