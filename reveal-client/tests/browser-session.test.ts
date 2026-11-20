import assert from "node:assert/strict";
import test from "node:test";
import { errorMessage } from "../src/lib/api";
import { BrowserSessionError, browserSessions, changedWorkspace, sessionChangedKey, sessionLock, type SessionLocks } from "../src/lib/browser-session";

test("two delayed first-connect tabs recheck under the shared navigator lock and provision once", async () => {
  let tail = Promise.resolve();
  const navigator = { locks: {
    request<T>(name: string, operation: () => Promise<T>): Promise<T> {
      assert.equal(name, sessionLock);
      const result = tail.then(operation); tail = result.then(() => {}, () => {}); return result;
    },
  } satisfies SessionLocks };
  let cookie: { user_id: string } | null = null, posts = 0, reads = 0;
  const observed: (string | null)[] = [], changes: (string | null)[] = [];
  const tab = () => browserSessions({
    read: async () => { reads++; return { principal: cookie }; },
    create: async () => { posts++; await new Promise(resolve => setTimeout(resolve, 20)); cookie = { user_id: "guest-" + posts }; return { principal: cookie }; },
    remove: async () => { cookie = null; return { principal: null }; },
    locks: () => navigator.locks,
    remember: principal => observed.push(principal?.user_id || null),
    announce: principal => changes.push(principal?.user_id || null),
  });
  const first = tab(), second = tab();
  const results = await Promise.all([first.connect(), second.connect()]);
  assert.deepEqual(results, [{ principal: { user_id: "guest-1" } }, { principal: { user_id: "guest-1" } }]);
  assert.equal(posts, 1); assert.equal(reads, 2); assert.deepEqual(observed, ["guest-1", "guest-1"]); assert.deepEqual(changes, ["guest-1"]);
  await second.connect(); assert.equal(posts, 1);
  await first.disconnect(); assert.deepEqual(changes, ["guest-1", null]);
  assert.deepEqual(await second.connect(), { principal: { user_id: "guest-2" } }); assert.equal(posts, 2);
});

test("unsupported lock APIs resume existing sessions but cannot race first provisioning or disconnect", async () => {
  let existing: { user_id: string } | null = null;
  const sessions = browserSessions({
    read: async () => ({ principal: existing }),
    create: async () => { throw new Error("must not provision"); },
    remove: async () => { throw new Error("must not remove"); },
    locks: () => undefined, remember: () => {}, announce: () => {},
  });
  await assert.rejects(sessions.connect(), /Web Locks/);
  await assert.rejects(sessions.disconnect(), /Web Locks/);
  existing = { user_id: "guest-existing" };
  assert.deepEqual(await sessions.connect(), { principal: existing });
});

test("cross-tab notifications reload only a changed established workspace", () => {
  const event = (principal: unknown) => ({ key: sessionChangedKey, newValue: JSON.stringify({ principal }) });
  assert.equal(changedWorkspace(event("guest-b"), "guest-a"), true);
  assert.equal(changedWorkspace(event(null), "guest-a"), true);
  assert.equal(changedWorkspace(event("guest-a"), "guest-a"), false);
  assert.equal(changedWorkspace(event("guest-a"), null), false);
  assert.equal(changedWorkspace({ key: "unrelated", newValue: event(null).newValue }, "guest-a"), false);
  assert.equal(changedWorkspace({ key: sessionChangedKey, newValue: "invalid" }, "guest-a"), false);
  assert.equal(changedWorkspace(event({ user_id: "guest-b" }), "guest-a"), false);
});

test("unsupported-browser guidance is visible while arbitrary errors stay sanitized", () => {
  assert.match(errorMessage(new BrowserSessionError("Opening a workspace requires Web Locks support.")), /Web Locks/);
  assert.doesNotMatch(errorMessage(new Error("private transport details")), /private transport/);
});
