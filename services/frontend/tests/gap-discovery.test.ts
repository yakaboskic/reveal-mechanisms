import assert from "node:assert/strict";
import test from "node:test";
import { api, ApiError, type Schema } from "../src/lib/client";
import { GapDiscoveryCache, loadGapCollection, type GapCollection } from "../src/lib/gap-discovery";

const collection = (id: string, more = false): GapCollection => ({
  items: [{ object: { id } } as Schema<"GapRecord">],
  page: { has_more: more, next_cursor: more ? "opaque-next" : null, snapshot_id: "snapshot" },
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}

test("navigation retains each sort, pagination and scroll even beyond the workspace freshness window", async t => {
  let calls = 0;
  const cache = new GapDiscoveryCache(async key => { calls++; return collection(key, true); });
  cache.bind("alice");
  await cache.ensure("alice", "public:accounts"); cache.rememberPosition("alice", "public:accounts", 480);
  await cache.ensure("alice", "public:votes"); cache.rememberPosition("alice", "public:votes", 210);
  const before = cache.read("alice", "public:accounts").data;
  const later = Date.now() + 120_000; t.mock.method(Date, "now", () => later);
  await cache.ensure("alice", "public:accounts"); await cache.ensure("alice", "public:votes");
  assert.equal(calls, 2); assert.equal(cache.read("alice", "public:accounts").data, before);
  assert.equal(before?.page.next_cursor, "opaque-next");
  assert.equal(cache.position("alice", "public:accounts"), 480); assert.equal(cache.position("alice", "public:votes"), 210);
});

test("invalidation while away keeps rows stable until explicit refresh", async () => {
  let calls = 0;
  const cache = new GapDiscoveryCache(async () => collection(String(++calls)));
  cache.bind("alice"); await cache.ensure("alice", "public:accounts");
  cache.invalidate(); await cache.ensure("alice", "public:accounts");
  assert.equal(calls, 1); assert.equal(cache.read("alice", "public:accounts").stale, true);
  await cache.revalidate("alice", "public:accounts", true);
  assert.equal(cache.read("alice", "public:accounts").data?.items[0].object.id, "2");
  assert.equal(cache.read("alice", "public:accounts").stale, false);
});

test("navigation shares a pending first page instead of restarting it", async () => {
  const pending = deferred<GapCollection>(); let calls = 0;
  const cache = new GapDiscoveryCache(async () => { calls++; return pending.promise; });
  cache.bind("visitor");
  const first = cache.ensure("visitor", "public:accounts"), second = cache.ensure("visitor", "public:accounts");
  assert.equal(first, second); pending.resolve(collection("one")); await second;
  assert.equal(calls, 1);
});

test("identity reset erases scroll and rows and rejects late responses even for the same returning viewer", async () => {
  const pending = deferred<GapCollection>(); let signal!: AbortSignal;
  const cache = new GapDiscoveryCache(async (_key, _previous, _mode, abort) => { signal = abort; return pending.promise; });
  cache.bind("alice"); cache.rememberPosition("alice", "public:accounts", 500);
  const loading = cache.ensure("alice", "public:accounts"); await Promise.resolve();
  cache.bind(null); cache.bind("alice");
  assert.equal(signal.aborted, true); assert.equal(cache.position("alice", "public:accounts"), 0);
  pending.resolve(collection("old-private-workspace")); await loading;
  assert.equal(cache.read("alice", "public:accounts").data, undefined);
  assert.equal(cache.read("bob", "public:accounts").data, undefined);
});

test("page loading preserves server order, deduplicates boundaries and passes the sort and opaque cursor", async t => {
  const calls: unknown[] = [];
  t.mock.method(api, "gaps", async (cursor?: string, scope?: string, _signal?: AbortSignal, sort?: string) => {
    calls.push({ cursor, scope, sort });
    return cursor ? { ...collection("second"), items: [...collection("first").items, ...collection("second").items] } : collection("first", true);
  });
  const signal = new AbortController().signal;
  let data = await loadGapCollection("public:votes", undefined, "refresh", signal);
  data = await loadGapCollection("public:votes", data, "append", signal);
  assert.deepEqual(data.items.map(gap => gap.object.id), ["first", "second"]);
  assert.deepEqual(calls, [{ cursor: undefined, scope: "public", sort: "votes" }, { cursor: "opaque-next", scope: "public", sort: "votes" }]);
});

test("expired continuation retains rows; explicit refresh starts from the first page", async t => {
  let revision = 1; const calls: (string | undefined)[] = [];
  t.mock.method(api, "gaps", async (cursor?: string) => {
    calls.push(cursor);
    if (cursor) throw new ApiError(409, "CURSOR_EXPIRED", "Changed");
    return collection(`revision-${revision}`, true);
  });
  const cache = new GapDiscoveryCache(); cache.bind("alice"); await cache.ensure("alice", "public:accounts");
  await cache.revalidate("alice", "public:accounts", true, "append");
  assert.equal(cache.read("alice", "public:accounts").data?.items[0].object.id, "revision-1");
  assert.equal((cache.read("alice", "public:accounts").error as ApiError).code, "CURSOR_EXPIRED");
  revision = 2; await cache.ensure("alice", "public:accounts"); assert.equal(calls.length, 2);
  await cache.revalidate("alice", "public:accounts", true);
  assert.deepEqual(calls, [undefined, "opaque-next", undefined]);
  assert.equal(cache.read("alice", "public:accounts").data?.items[0].object.id, "revision-2");
});

test("committed votes update both cached sorts in place and defeat older in-flight responses", async () => {
  let next = Promise.resolve(collection("gap"));
  const cache = new GapDiscoveryCache(async () => next); cache.bind("alice");
  await cache.ensure("alice", "public:accounts"); await cache.ensure("alice", "public:votes");
  const old = deferred<GapCollection>(); next = old.promise;
  const pending = cache.revalidate("alice", "public:accounts", true);
  const votes = { score: 1, upvotes: 1, downvotes: 0, user_vote: 1 } as Schema<"VoteState">;
  cache.vote("alice", "gap", votes); old.resolve(collection("gap")); await pending;
  for (const key of ["public:accounts", "public:votes"] as const) {
    assert.deepEqual(cache.read("alice", key).data?.items[0].votes, votes);
    assert.equal(cache.read("alice", key).stale, true);
  }
});
