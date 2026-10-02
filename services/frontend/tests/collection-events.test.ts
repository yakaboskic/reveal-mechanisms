import assert from "node:assert/strict";
import test from "node:test";
import { onCollectionInvalidation } from "../src/lib/collection-events";
import { invalidateWorkspace, resetWorkspaceCache, type WorkspaceEvent } from "../src/lib/workspace-events";

const event = (collections: WorkspaceEvent["collections"]) => ({ collections }) as WorkspaceEvent;

test("collection notifications coalesce matching events without refreshing unrelated or idle views", async () => {
  const calls: boolean[] = [];
  const remove = onCollectionInvalidation(["accounts", "catalog"], reset => calls.push(reset));
  try {
    invalidateWorkspace(event(["jobs"]));
    await Promise.resolve();
    assert.deepEqual(calls, []);
    invalidateWorkspace(event(["accounts"])); invalidateWorkspace(event(["catalog"])); invalidateWorkspace();
    assert.deepEqual(calls, []);
    await Promise.resolve();
    assert.deepEqual(calls, [false]);
    await Promise.resolve();
    assert.deepEqual(calls, [false]);
    invalidateWorkspace(event(["identity"]));
    await Promise.resolve();
    assert.deepEqual(calls, [false, false]);
  } finally { remove(); }
});

test("identity reset takes precedence over a queued refresh and disposal drops late delivery", async () => {
  const calls: boolean[] = [];
  const remove = onCollectionInvalidation(["accounts"], reset => calls.push(reset));
  invalidateWorkspace(event(["accounts"])); resetWorkspaceCache();
  await Promise.resolve();
  assert.deepEqual(calls, [true]);
  invalidateWorkspace(event(["accounts"])); remove();
  await Promise.resolve();
  invalidateWorkspace();
  await Promise.resolve();
  assert.deepEqual(calls, [true]);
});
