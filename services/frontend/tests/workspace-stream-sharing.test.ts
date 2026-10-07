import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { announceSignOut, connectWorkspaceEvents, shareWorkspaceEvents, type StreamSharing, type WorkspaceConnection, type WorkspaceEvent } from "../src/lib/workspace-events";
import { alwaysVisible, documentVisibility, parkHiddenAfterMs, type PageVisibility } from "../src/lib/page-visibility";

const sample: WorkspaceEvent = { schema_version: 1, event_id: "scope:1", cursor: "1", scope: "workspace", event_type: "draft.changed", entity_id: "draft", entity_revision: 1, operation: "upsert", collections: ["drafts", "gaps"], committed_at: "2026-09-30T00:00:00Z" };
const change = (position: number) => ({ ...sample, event_id: `scope:${position}`, cursor: String(position), entity_id: `draft-${position}` });
const frame = (event: string, data: unknown, id = "cursor") => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const settle = async (rounds = 6) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setTimeout(resolve, 2)); };
let owners = 0;
const owner = () => `user-${process.pid}-${++owners}`;

/** Streaming responses that stay open until the test ends them or the request is aborted. */
function server(answer?: (index: number) => Response | undefined) {
  const opened: { cursor: string | null; signal: AbortSignal; push: (text: string) => void; end: () => void }[] = [];
  const fetcher = (async (_url: unknown, options: RequestInit) => {
    const fixed = answer?.(opened.length);
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start: controller => { stream = controller; } });
    const signal = options.signal!;
    opened.push({ cursor: new Headers(options.headers).get("Last-Event-ID"), signal, push: text => stream.enqueue(new TextEncoder().encode(text)), end: () => stream.close() });
    if (fixed) return fixed;
    signal.addEventListener("abort", () => { try { stream.error(new DOMException("Aborted", "AbortError")); } catch { /* closed */ } });
    return new Response(body, { headers: { "content-type": "text/event-stream" } });
  }) as typeof fetch;
  return { fetcher, opened };
}

/** Web Locks with exclusive, queued, abortable requests and asynchronous grants. */
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

/** A page whose minute-hidden timer the test fires. */
function page(hidden = false) {
  const waiting = new Set<() => void>(), timers = new Set<() => void>();
  const value: PageVisibility & { hide(): void; park(): void; show(): void } = {
    hidden: () => hidden,
    untilVisible: signal => new Promise(resolve => {
      if (!hidden || signal.aborted) return resolve();
      const done = () => { waiting.delete(done); resolve(); };
      waiting.add(done); signal.addEventListener("abort", done, { once: true });
    }),
    onHiddenFor: (_ms, callback) => { timers.add(callback); return () => { timers.delete(callback); }; },
    hide: () => { hidden = true; },
    park: () => { for (const callback of [...timers]) callback(); },
    show: () => { hidden = false; for (const done of [...waiting]) done(); },
  };
  return value;
}

function tab() {
  const seen = { changes: [] as (string | undefined)[], states: [] as WorkspaceConnection[], revoked: 0 };
  return { seen, handlers: { change: (event?: WorkspaceEvent) => { seen.changes.push(event?.entity_id); }, status: (state: WorkspaceConnection) => { seen.states.push(state); }, revoked: () => { seen.revoked++; } } };
}

function open(fetcher: typeof fetch, visibility: PageVisibility = alwaysVisible, lockManager = locks(), posted?: unknown[]): StreamSharing {
  return { locks: lockManager, fetcher, page: visibility, channel: name => {
    const channel = new BroadcastChannel(name);
    if (!posted) return channel;
    return { postMessage: (message: unknown) => { posted.push(message); channel.postMessage(message); }, close: () => channel.close(),
      set onmessage(listener) { channel.onmessage = listener; }, get onmessage() { return channel.onmessage; } };
  } };
}

test("two tabs of one user hold one stream; the follower mirrors it and resumes from its cursor when the leader closes", async () => {
  const user = owner(), { fetcher, opened } = server(), shared = locks();
  const a = tab(), b = tab(), closeA = new AbortController(), closeB = new AbortController();
  const leading = shareWorkspaceEvents(closeA.signal, user, a.handlers, open(fetcher, alwaysVisible, shared)); await settle();
  const following = shareWorkspaceEvents(closeB.signal, user, b.handlers, open(fetcher, alwaysVisible, shared)); await settle();
  assert.equal(opened.length, 1);
  opened[0].push(frame("workspace_change", change(1), "c1") + frame("ready", {}, "c2")); await settle();
  assert.deepEqual(a.seen.changes, ["draft-1"]); assert.deepEqual(b.seen.changes, ["draft-1"]);
  assert.equal(b.seen.states.at(-1), "live");
  // A tab opened later learns the live status from the leader at once.
  const c = tab(), closeC = new AbortController();
  const late = shareWorkspaceEvents(closeC.signal, user, c.handlers, open(fetcher, alwaysVisible, shared)); await settle();
  assert.equal(c.seen.states.at(-1), "live"); assert.equal(opened.length, 1);
  closeA.abort(); await leading; await settle();
  assert.equal(opened.length, 2); assert.equal(opened[1].cursor, "c2");
  opened[1].push(frame("workspace_change", change(2), "c3")); await settle();
  assert.deepEqual(b.seen.changes, ["draft-1", "draft-2"]); assert.deepEqual(c.seen.changes, ["draft-2"]);
  assert.deepEqual(a.seen.changes, ["draft-1"]);
  closeB.abort(); closeC.abort(); await Promise.all([following, late]);
});

test("another user's tab streams separately and never applies this user's changes", async () => {
  const { fetcher, opened } = server(), shared = locks();
  const a = tab(), other = tab(), close = new AbortController();
  const runs = [shareWorkspaceEvents(close.signal, owner(), a.handlers, open(fetcher, alwaysVisible, shared)),
    shareWorkspaceEvents(close.signal, owner(), other.handlers, open(fetcher, alwaysVisible, shared))];
  await settle(); assert.equal(opened.length, 2);
  opened[0].push(frame("workspace_change", change(1), "c1")); await settle();
  assert.equal(a.seen.changes.length + other.seen.changes.length, 1);
  close.abort(); await Promise.all(runs);
});

test("a replay burst reaches the other tabs as one message", async () => {
  const user = owner(), { fetcher, opened } = server(), shared = locks(), posted: unknown[] = [];
  const a = tab(), b = tab(), close = new AbortController();
  const runs = [shareWorkspaceEvents(close.signal, user, a.handlers, open(fetcher, alwaysVisible, shared, posted))]; await settle();
  runs.push(shareWorkspaceEvents(close.signal, user, b.handlers, open(fetcher, alwaysVisible, shared))); await settle();
  opened[0].push(frame("workspace_change", change(1), "c1") + frame("workspace_change", change(2), "c2") + frame("workspace_change", change(3), "c3")); await settle();
  const batches = posted.filter(message => (message as { type: string }).type === "changes") as { events: WorkspaceEvent[]; cursor: string }[];
  assert.equal(batches.length, 1); assert.equal(batches[0].events.length, 3); assert.equal(batches[0].cursor, "c3");
  assert.deepEqual(b.seen.changes, ["draft-1", "draft-2", "draft-3"]);
  close.abort(); await Promise.all(runs);
});

test("revocation reaches every tab of the user; none of them reconnects, a tab started later may", async () => {
  const user = owner(), { fetcher, opened } = server(index => index === 0 ? new Response(null, { status: 401 }) : undefined), shared = locks();
  const a = tab(), b = tab(), closeA = new AbortController(), closeB = new AbortController();
  const leading = shareWorkspaceEvents(closeA.signal, user, a.handlers, open(fetcher, alwaysVisible, shared));
  const following = shareWorkspaceEvents(closeB.signal, user, b.handlers, open(fetcher, alwaysVisible, shared)); await settle();
  assert.equal(a.seen.revoked, 1); assert.equal(b.seen.revoked, 1); assert.equal(b.seen.states.at(-1), "expired");
  closeA.abort(); await leading; await settle();
  assert.equal(opened.length, 1);
  const c = tab(), closeC = new AbortController();
  const later = shareWorkspaceEvents(closeC.signal, user, c.handlers, open(fetcher, alwaysVisible, shared)); await settle();
  assert.equal(opened.length, 2);
  opened[1].push(frame("ready", {}, "c1")); await settle();
  assert.equal(c.seen.states.at(-1), "live"); assert.equal(b.seen.revoked, 1);
  closeB.abort(); closeC.abort(); await Promise.all([following, later]);
});

test("a tab started after a revocation takes the stream from the tab still holding it", async () => {
  const user = owner(), { fetcher, opened } = server(index => index === 0 ? new Response(null, { status: 401 }) : undefined), shared = locks();
  const close = new AbortController();
  const revoked = shareWorkspaceEvents(close.signal, user, tab().handlers, open(fetcher, alwaysVisible, shared)); await settle();
  const later = shareWorkspaceEvents(close.signal, user, tab().handlers, open(fetcher, alwaysVisible, shared)); await settle();
  assert.equal(opened.length, 2); assert.equal(opened[1].signal.aborted, false);
  close.abort(); await Promise.all([revoked, later]);
});

test("signing out in one tab revokes the user's other tabs and closes their stream", async () => {
  const user = owner(), { fetcher, opened } = server(), shared = locks();
  const a = tab(), b = tab(), close = new AbortController();
  const runs = [shareWorkspaceEvents(close.signal, user, a.handlers, open(fetcher, alwaysVisible, shared))]; await settle();
  runs.push(shareWorkspaceEvents(close.signal, user, b.handlers, open(fetcher, alwaysVisible, shared))); await settle();
  announceSignOut(user, open(fetcher)); await settle();
  assert.equal(a.seen.revoked, 1); assert.equal(b.seen.revoked, 1);
  assert.equal(opened[0].signal.aborted, true); assert.equal(opened.length, 1);
  close.abort(); await Promise.all(runs);
});

test("a leader hidden for a minute hands the stream to a visible tab; hidden tabs neither queue nor lead", async () => {
  const user = owner(), { fetcher, opened } = server(), shared = locks();
  const pageA = page(), pageB = page(), pageC = page(true);
  const a = tab(), b = tab(), c = tab(), close = new AbortController();
  const runs = [shareWorkspaceEvents(close.signal, user, a.handlers, open(fetcher, pageA, shared))]; await settle();
  runs.push(shareWorkspaceEvents(close.signal, user, b.handlers, open(fetcher, pageB, shared)));
  runs.push(shareWorkspaceEvents(close.signal, user, c.handlers, open(fetcher, pageC, shared))); await settle();
  opened[0].push(frame("ready", {}, "c1")); await settle();
  assert.equal(c.seen.states.at(-1), "live");  // a hidden follower still applies broadcasts
  pageA.hide(); pageA.park(); await settle();
  assert.equal(opened.length, 2); assert.equal(opened[0].signal.aborted, true); assert.equal(opened[1].cursor, "c1");
  pageA.show(); await settle();
  assert.equal(opened.length, 2);  // A follows again behind B
  pageB.hide(); pageB.park(); await settle();
  assert.equal(opened.length, 3);  // A takes over; C never queued while hidden
  pageA.hide(); pageA.park(); await settle();
  assert.equal(opened.length, 3); assert.equal(opened[2].signal.aborted, true);  // every tab hidden: no stream
  pageC.show(); await settle();
  assert.equal(opened.length, 4); assert.equal(opened[3].cursor, "c1");
  close.abort(); await Promise.all(runs);
});

test("without Web Locks each tab streams for itself", async () => {
  const user = owner(), { fetcher, opened } = server(), close = new AbortController();
  const runs = [shareWorkspaceEvents(close.signal, user, tab().handlers, { ...open(fetcher), locks: null }),
    shareWorkspaceEvents(close.signal, user, tab().handlers, { ...open(fetcher), locks: null })];
  await settle(); assert.equal(opened.length, 2);
  close.abort(); await Promise.all(runs);
});

test("a hidden tab parks its stream without a failure and resumes from its cursor once shown", async () => {
  const { fetcher, opened } = server(), visibility = page(), close = new AbortController(), seen = tab();
  const run = connectWorkspaceEvents(close.signal, seen.handlers, fetcher, visibility); await settle();
  opened[0].push(frame("ready", {}, "c1")); await settle();
  visibility.hide(); await settle(); assert.equal(opened[0].signal.aborted, false);  // briefly hidden: still streaming
  visibility.park(); await settle(20);
  assert.equal(opened[0].signal.aborted, true); assert.equal(opened.length, 1);
  assert.deepEqual(seen.seen.states, ["connecting", "live"]);
  visibility.show(); await settle();
  assert.equal(opened.length, 2); assert.equal(opened[1].cursor, "c1");
  close.abort(); await run;
});

test("the document visibility source parks only after a full minute hidden", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const doc = Object.assign(new EventTarget(), { visibilityState: "visible" as DocumentVisibilityState });
  const visibility = documentVisibility(doc); let parked = 0;
  const stop = visibility.onHiddenFor(parkHiddenAfterMs, () => { parked++; });
  doc.visibilityState = "hidden"; doc.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(parkHiddenAfterMs - 1); doc.visibilityState = "visible"; doc.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(parkHiddenAfterMs); assert.equal(parked, 0);
  doc.visibilityState = "hidden"; doc.dispatchEvent(new Event("visibilitychange"));
  t.mock.timers.tick(parkHiddenAfterMs); assert.equal(parked, 1);
  let shown = false; const waiting = visibility.untilVisible(new AbortController().signal).then(() => { shown = true; });
  await Promise.resolve(); assert.equal(shown, false);
  doc.visibilityState = "visible"; doc.dispatchEvent(new Event("visibilitychange")); await waiting; assert.equal(shown, true);
  stop();
});

test("the workspace provider shares the stream by user and sign-out announces it", async () => {
  const provider = await readFile(new URL("../src/components/WorkspaceCache.tsx", import.meta.url), "utf8");
  assert.match(provider, /shareWorkspaceEvents\(controller\.signal, owner \|\| scope,/);
  assert.doesNotMatch(provider, /connectWorkspaceEvents/);
  const session = await readFile(new URL("../src/components/Session.tsx", import.meta.url), "utf8");
  assert.match(session, /owner=\{identity\.principal\?\.user_id\}/);
  assert.match(session, /await signOut\(\{ callbackUrl: "\/" \}\);\n.*\n\s*if \(owner\) announceSignOut\(owner\);/);
});
