import assert from "node:assert/strict";
import test from "node:test";
import { RevalidationCache, workspaceFreshMs } from "../src/lib/revalidation-cache";
import { changesWorkspace } from "../src/lib/workspace-events";
import { loadWorkspaceData } from "../src/lib/workspace-data";
import { api, ApiError, type Schema } from "../src/lib/client";

const deferred = <T>() => {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("fresh navigation reuses data; stale and concurrent refreshes keep it visible and share one request", async () => {
  let now = 100, calls = 0, next = Promise.resolve(["saved"]);
  const cache = new RevalidationCache<string, string[]>(async () => { calls++; return next; }, () => now);
  cache.bind("owner"); await cache.revalidate("owner", "gaps");
  await cache.revalidate("owner", "gaps"); assert.equal(calls, 1);
  now += workspaceFreshMs;
  const refresh = deferred<string[]>(); next = refresh.promise;
  const first = cache.revalidate("owner", "gaps"), second = cache.revalidate("owner", "gaps");
  assert.equal(first, second); assert.deepEqual(cache.read("owner", "gaps").data, ["saved"]);
  assert.equal(cache.read("owner", "gaps").loading, true);
  refresh.resolve(["updated"]); await first;
  assert.deepEqual(cache.read("owner", "gaps").data, ["updated"]); assert.equal(calls, 2);
});

test("refresh errors preserve saved rows; explicit retry replaces them", async () => {
  let fail = false;
  const cache = new RevalidationCache<string, string[]>(async () => { if (fail) throw new Error("Offline"); return ["saved"]; });
  cache.bind("owner"); await cache.revalidate("owner", "accounts");
  fail = true; await cache.revalidate("owner", "accounts", true);
  assert.deepEqual(cache.read("owner", "accounts").data, ["saved"]);
  assert.equal((cache.read("owner", "accounts").error as Error).message, "Offline");
  fail = false; await cache.revalidate("owner", "accounts", true);
  assert.equal(cache.read("owner", "accounts").error, null);
});

test("identity changes and logout discard late results even when the same owner signs back in", async () => {
  const old = deferred<string[]>(); let signal!: AbortSignal;
  const cache = new RevalidationCache<string, string[]>(async (_key, _data, _mode, current) => { signal = current; return old.promise; });
  cache.bind("alice"); const request = cache.revalidate("alice", "gaps"); await Promise.resolve();
  assert.equal(cache.read("bob", "gaps").data, undefined);
  cache.bind(null); assert.equal(signal.aborted, true); cache.bind("alice");
  old.resolve(["private old session"]); await request;
  assert.equal(cache.read("alice", "gaps").data, undefined);
});

test("a mutation during a pending read cannot make the old response fresh or replace retained data", async () => {
  let next = Promise.resolve(["retained"]);
  const cache = new RevalidationCache<string, string[]>(async () => next);
  cache.bind("owner"); await cache.revalidate("owner", "gaps");
  const old = deferred<string[]>(); next = old.promise;
  const request = cache.revalidate("owner", "gaps", true);
  cache.invalidate(); old.resolve(["before save"]); await request;
  assert.deepEqual(cache.read("owner", "gaps").data, ["retained"]);
  assert.equal(cache.read("owner", "gaps").stale, true);
  next = Promise.resolve(["after save"]); await cache.revalidate("owner", "gaps");
  assert.deepEqual(cache.read("owner", "gaps").data, ["after save"]);
});

test("revoked access clears all cached private tabs", async () => {
  let denied = false;
  const cache = new RevalidationCache<string, string[]>(async () => { if (denied) throw new ApiError(401, "SESSION_EXPIRED", "Expired"); return ["private"]; });
  cache.bind("owner"); await cache.revalidate("owner", "gaps"); await cache.revalidate("owner", "accounts");
  denied = true; await cache.revalidate("owner", "gaps", true);
  assert.equal(cache.read("owner", "gaps").data, undefined); assert.equal(cache.read("owner", "accounts").data, undefined);
});

test("workspace changes invalidate while searches, rendering and private reads do not", () => {
  for (const [method, path] of [["POST", "/v1/jobs"], ["PATCH", "/v1/drafts/id"], ["POST", "/v1/jobs/id/retry-review"], ["POST", "/v1/me/explorations"], ["POST", "/v1/accounts/id/publication"], ["POST", "/v1/analysis-outcomes/id/publication"]]) {
    assert.equal(changesWorkspace(method, "/api/backend" + path), true, path);
  }
  for (const [method, path] of [["GET", "/v1/accounts"], ["POST", "/v1/citations/render"], ["POST", "/v1/mechanisms/suggest"]]) assert.equal(changesWorkspace(method, path), false);
});

test("revalidation refreshes every previously loaded page without duplicating rows or resetting pagination", async t => {
  let changed = false; const requests: (string | undefined)[] = [];
  t.mock.method(api, "outcomes", async (cursor?: string) => {
    requests.push(cursor);
    const ids = cursor ? changed ? ["a", "c"] : ["b"] : changed ? ["new", "a"] : ["a"];
    return { items: ids.map(id => ({ id })), page: { next_cursor: cursor ? null : "page2", has_more: !cursor, snapshot_id: "test" } };
  });
  const signal = new AbortController().signal;
  let data = await loadWorkspaceData("explorations", undefined, "refresh", signal);
  data = await loadWorkspaceData("explorations", data, "append", signal);
  assert.equal(data.pages, 2); changed = true; requests.length = 0;
  data = await loadWorkspaceData("explorations", data, "refresh", signal);
  assert.deepEqual(requests, [undefined, "page2"]); assert.deepEqual(data.outcomes.map(item => item.id), ["new", "a", "c"]);
  assert.equal(data.cursor, null); assert.equal(data.pages, 2);
});

test("running-job refresh reuses frozen requests until it discovers an unknown research request", async t => {
  let requestId = "known", frozenReads = 0;
  t.mock.method(api, "jobs", async () => ({ items: [{ id: "job", research_request_id: requestId }], page: { next_cursor: null, has_more: false, snapshot_id: "test" } }));
  t.mock.method(api, "requests", async () => { frozenReads++; return { items: [{ id: "new" }], page: { next_cursor: null, has_more: false, snapshot_id: "test" } }; });
  const initial = { gaps: [], accounts: [], outcomes: [], drafts: [], jobs: [], requests: [{ id: "known" }] as Schema<"ResearchRequest">[], cursor: null, pages: 1 };
  const signal = new AbortController().signal;
  await loadWorkspaceData("gaps", initial, "activity", signal); assert.equal(frozenReads, 0);
  requestId = "new"; await loadWorkspaceData("gaps", initial, "activity", signal); assert.equal(frozenReads, 1);
});

test("server events only invalidate affected tabs and leave unrelated cached accounts fresh", async () => {
  const { affectedWorkspaceTabs } = await import("../src/lib/workspace-events");
  const cache = new RevalidationCache<string, string[]>(async key => [key]);
  cache.bind("owner"); await cache.revalidate("owner", "gaps"); await cache.revalidate("owner", "accounts");
  cache.invalidate(affectedWorkspaceTabs({ collections: ["jobs", "gaps"] } as never));
  assert.equal(cache.read("owner", "gaps").stale, true);
  assert.equal(cache.read("owner", "accounts").stale, false);
});

test("multiple committed events during a fetch retain the newer invalidation until a fresh read", async () => {
  let next = Promise.resolve(["retained"]), calls = 0;
  const cache = new RevalidationCache<string, string[]>(async () => { calls++; return next; });
  cache.bind("owner"); await cache.revalidate("owner", "gaps");
  const delayed = deferred<string[]>(); next = delayed.promise;
  const pending = cache.revalidate("owner", "gaps", true);
  cache.invalidate(["gaps"]); cache.invalidate(["gaps"]);
  assert.equal(cache.revalidate("owner", "gaps"), pending);
  delayed.resolve(["older response"]); await pending;
  assert.deepEqual(cache.read("owner", "gaps").data, ["retained"]);
  assert.equal(cache.read("owner", "gaps").stale, true);
  next = Promise.resolve(["current"]); await cache.revalidate("owner", "gaps");
  assert.equal(calls, 3); assert.deepEqual(cache.read("owner", "gaps").data, ["current"]);
});
