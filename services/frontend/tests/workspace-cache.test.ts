import assert from "node:assert/strict";
import test from "node:test";
import { RevalidationCache, workspaceFreshMs } from "../src/lib/revalidation-cache";
import { affectedWorkspaceTabs, changesWorkspace } from "../src/lib/workspace-events";
import { loadWorkspaceData, workspaceKey, workspaceKeyParts, workspaceSize, type WorkspaceKey, type WorkspaceData } from "../src/lib/workspace-data";
import { api, ApiError, type Schema } from "../src/lib/client";
import { localWorkApi, LocalWorkError, type LocalWork } from "../src/lib/local-work";

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
  for (const [method, path] of [["POST", "/v1/jobs"], ["PATCH", "/v1/drafts/id"], ["POST", "/v1/drafts/id/save"], ["POST", "/v1/jobs/id/retry-review"], ["POST", "/v1/me/explorations"], ["POST", "/v1/accounts/id/publication"], ["POST", "/v1/analysis-outcomes/id/publication"], ["POST", "/v1/local-work"], ["POST", "/v1/local-work/id/close"], ["POST", "/v1/local-work/id/grants"], ["DELETE", "/v1/local-work/id/grants/grant"]]) {
    assert.equal(changesWorkspace(method, "/api/backend" + path), true, path);
  }
  for (const [method, path] of [["GET", "/v1/accounts"], ["POST", "/v1/citations/render"], ["POST", "/v1/mechanisms/suggest"], ["GET", "/v1/local-work"], ["GET", "/v1/local-work/id/package"]]) assert.equal(changesWorkspace(method, path), false);
});

test("saved draft loading excludes temporary work without fetching jobs or frozen requests", async t => {
  t.mock.method(api, "drafts", async (cursor?: string) => ({
    items: cursor ? [{ id: "legacy" }] : [{ id: "saved", lifecycle: "saved" }, { id: "temporary", lifecycle: "temporary" }],
    page: { next_cursor: cursor ? null : "next", has_more: !cursor, snapshot_id: "test" },
  }));
  t.mock.method(api, "jobs", () => assert.fail("Draft display must not depend on runs"));
  t.mock.method(api, "requests", () => assert.fail("Draft display must not depend on frozen requests"));
  t.mock.method(localWorkApi, "list", () => assert.fail("Draft display must not depend on local research"));
  const signal = new AbortController().signal;
  let data = await loadWorkspaceData("drafts", undefined, "refresh", signal);
  assert.deepEqual(data.drafts.map(draft => draft.id), ["saved"]);
  assert.equal(data.cursor, "next");
  data = await loadWorkspaceData("drafts", data, "append", signal);
  assert.deepEqual(data.drafts.map(draft => draft.id), ["saved", "legacy"]);
  assert.equal(data.cursor, null);
});

test("research runs load frozen inputs independently of deleted drafts and exclude paragraph jobs", async t => {
  const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
  t.mock.method(api, "drafts", () => assert.fail("Run history must not read mutable drafts"));
  t.mock.method(api, "jobs", async () => ({ items: [{ id: "research", kind: "analysis", research_request_id: "frozen" }, { id: "paragraph", kind: "paragraph" }], page }));
  t.mock.method(api, "requests", async () => ({ items: [{ id: "frozen", source_draft_id: "deleted-draft" }], page }));
  let localState = "preparing";
  t.mock.method(localWorkApi, "list", async () => ({ items: [{ id: "local", state: localState, request: { id: "local-frozen" } }] }));
  const signal = new AbortController().signal;
  let data = await loadWorkspaceData("runs", undefined, "refresh", signal);
  assert.deepEqual(data.jobs.map(job => job.id), ["research"]);
  assert.equal(data.requests[0].source_draft_id, "deleted-draft");
  assert.deepEqual(data.localWorks.map(work => work.id), ["local"]);
  assert.equal(workspaceSize("runs", data), 2);
  localState = "ready";
  data = await loadWorkspaceData("runs", data, "activity", signal);
  assert.deepEqual(data.jobs.map(job => job.id), ["research"]);
  assert.equal(data.localWorks[0].state, "ready");
});

test("local run pagination preserves the caller signal and deduplicates frozen work", async t => {
  const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
  t.mock.method(api, "jobs", async () => ({ items: [], page }));
  t.mock.method(api, "requests", async () => ({ items: [], page }));
  const cursors: (string | undefined)[] = [], signal = new AbortController().signal;
  t.mock.method(localWorkApi, "list", async (cursor?: string, caller?: AbortSignal) => {
    assert.equal(caller, signal); cursors.push(cursor);
    return { items: (cursor ? ["first", "second"] : ["first"]).map(id => ({ id })), page: { next_cursor: cursor ? null : "next" } };
  });
  const data = await loadWorkspaceData("runs", undefined, "refresh", signal);
  assert.deepEqual(cursors, [undefined, "next"]);
  assert.deepEqual(data.localWorks.map(work => work.id), ["first", "second"]);
  assert.equal(workspaceSize("runs", data), 2);
  assert.equal(data.cursor, null);
});

test("local run cursor loops fail instead of silently presenting incomplete history", async t => {
  const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
  t.mock.method(api, "jobs", async () => ({ items: [], page }));
  t.mock.method(api, "requests", async () => ({ items: [], page }));
  t.mock.method(localWorkApi, "list", async () => ({ items: [], page: { next_cursor: "repeated" } }));
  await assert.rejects(loadWorkspaceData("runs", undefined, "refresh", new AbortController().signal), /Local research history changed/);
});

test("local research read failures retain mixed history while denied access clears private caches", async t => {
  const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
  let failure: Error | null = null;
  t.mock.method(api, "jobs", async () => ({ items: [{ id: "online", kind: "analysis" }], page }));
  t.mock.method(api, "requests", async () => ({ items: [], page }));
  t.mock.method(api, "accounts", async () => ({ items: [], page }));
  t.mock.method(localWorkApi, "list", async () => { if (failure) throw failure; return { items: [{ id: "local" } as LocalWork] }; });
  const cache = new RevalidationCache<WorkspaceKey, WorkspaceData>(loadWorkspaceData);
  cache.bind("owner"); await cache.revalidate("owner", "runs"); await cache.revalidate("owner", "accounts");
  failure = new Error("Local history unavailable"); await cache.revalidate("owner", "runs", true);
  assert.equal(cache.read("owner", "runs").data?.jobs[0].id, "online");
  assert.equal(cache.read("owner", "runs").data?.localWorks[0].id, "local");
  assert.equal((cache.read("owner", "runs").error as Error).message, failure.message);
  failure = new LocalWorkError(403, "FORBIDDEN", "Access revoked"); await cache.revalidate("owner", "runs", true);
  assert.equal(cache.read("owner", "runs").data, undefined);
  assert.equal(cache.read("owner", "accounts").data, undefined);
});

test("draft and run events invalidate their independent tabs and gap history", () => {
  assert.deepEqual(affectedWorkspaceTabs({ collections: ["drafts"] } as never), ["drafts", "gaps"]);
  assert.deepEqual(affectedWorkspaceTabs({ collections: ["jobs"] } as never), ["runs", "gaps"]);
  assert.deepEqual(affectedWorkspaceTabs({ collections: ["requests"] } as never), ["runs", "gaps"]);
  assert.deepEqual(affectedWorkspaceTabs({ collections: ["identity"] } as never), ["drafts", "runs", "gaps", "accounts", "explorations"]);
  assert.equal(workspaceKey("drafts", "ignored"), "drafts");
  assert.equal(workspaceKey("runs", "ignored"), "runs");
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
  const initial = { gaps: [], accounts: [], outcomes: [], drafts: [], jobs: [], localWorks: [], requests: [{ id: "known" }] as Schema<"ResearchRequest">[], cursor: null, pages: 1 };
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

test("workspace search keeps pagination bound to its query and clearing restores the unfiltered cache", async t => {
  const calls: { cursor?: string; query?: string }[] = [];
  t.mock.method(api, "outcomes", async (cursor?: string, _signal?: AbortSignal, query?: string) => {
    calls.push({ cursor, query });
    return { items: [{ id: `${query || "all"}-${cursor || "first"}` }], page: { next_cursor: cursor ? null : "next", has_more: !cursor, snapshot_id: "test" } };
  });
  const cache = new RevalidationCache<WorkspaceKey, WorkspaceData>(loadWorkspaceData);
  cache.bind("owner");
  await cache.revalidate("owner", "explorations");
  const key = workspaceKey("explorations", "  gene   A?B & C  ");
  await cache.revalidate("owner", key);
  await cache.revalidate("owner", key, true, "append");
  assert.deepEqual(calls, [{ cursor: undefined, query: undefined }, { cursor: undefined, query: "gene A?B & C" }, { cursor: "next", query: "gene A?B & C" }]);
  assert.deepEqual(cache.read("owner", key).data?.outcomes.map(item => item.id), ["gene A?B & C-first", "gene A?B & C-next"]);
  await cache.revalidate("owner", workspaceKey("explorations", ""));
  assert.equal(calls.length, 3);
  assert.equal(cache.read("owner", "explorations").data?.outcomes[0].id, "all-first");
});

test("a late search response cannot replace another search's results", async t => {
  const first = deferred<unknown>();
  t.mock.method(api, "accounts", async (_cursor?: string, _signal?: AbortSignal, query?: string) => {
    if (query === "old") await first.promise;
    return { items: [{ account: { id: query } }], page: { next_cursor: null, has_more: false, snapshot_id: "test" } };
  });
  const cache = new RevalidationCache<WorkspaceKey, WorkspaceData>(loadWorkspaceData);
  cache.bind("owner");
  const old = workspaceKey("accounts", "old"), current = workspaceKey("accounts", "current");
  const pending = cache.revalidate("owner", old);
  await cache.revalidate("owner", current);
  first.resolve(undefined); await pending;
  assert.equal(cache.read("owner", current).data?.accounts[0].account.id, "current");
});

test("publication events invalidate every affected search while unrelated lists stay fresh", async () => {
  const cache = new RevalidationCache<WorkspaceKey, string[]>(async key => [key]);
  cache.bind("owner");
  const keys: WorkspaceKey[] = ["accounts", workspaceKey("accounts", "BMPR2"), workspaceKey("accounts", "MTOR"), "explorations"];
  await Promise.all(keys.map(key => cache.revalidate("owner", key)));
  cache.invalidateWhere(key => workspaceKeyParts(key).tab === "accounts");
  for (const key of keys) assert.equal(cache.read("owner", key).stale, key !== "explorations");
});
