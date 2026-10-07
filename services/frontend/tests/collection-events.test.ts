import assert from "node:assert/strict";
import test from "node:test";
import { onCollectionInvalidation } from "../src/lib/collection-events";
import { invalidateWorkspace, onWorkspaceChange, resetWorkspaceCache, type WorkspaceEvent } from "../src/lib/workspace-events";
import { createLocalWorkClient } from "../src/lib/local-work";
import { readFileSync } from "node:fs";

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

test("a view that skips local echoes refreshes on matching events, resyncs and resets but not on its own tab's mutations", async () => {
  const calls: boolean[] = [], all: boolean[] = [];
  const remove = onCollectionInvalidation(["catalog"], reset => calls.push(reset), { local: false });
  const removeAll = onCollectionInvalidation(["catalog"], reset => all.push(reset));
  try {
    invalidateWorkspace(undefined, true); await Promise.resolve();
    assert.deepEqual(calls, [], "A draft or exploration write in this tab cannot change a vote tally");
    assert.deepEqual(all, [false], "Other views keep refreshing on local writes");
    invalidateWorkspace(event(["drafts", "gaps"])); invalidateWorkspace(event(["gaps", "explorations"])); await Promise.resolve();
    assert.deepEqual(calls, [], "Draft and exploration echoes name no catalog change");
    invalidateWorkspace(event(["catalog"])); await Promise.resolve();
    invalidateWorkspace(); await Promise.resolve();
    invalidateWorkspace(event(["identity"])); await Promise.resolve();
    resetWorkspaceCache(); await Promise.resolve();
    assert.deepEqual(calls, [false, false, false, true], "Catalog events, a stream resync, identity changes and resets still refresh");
  } finally { remove(); removeAll(); }
});

test("this tab's own API and local-work mutations are marked local; stream events are not", async () => {
  const seen: (boolean | undefined)[] = [];
  const remove = onWorkspaceChange((_reset, _event, local) => seen.push(local));
  const fetcher = (async () => new Response("{}", { status: 200 })) as typeof fetch;
  try {
    await createLocalWorkClient(fetcher).close("work-id", "close-key");
    invalidateWorkspace(event(["catalog"]));
    assert.deepEqual(seen, [true, false]);
    assert.match(readFileSync(new URL("../src/lib/client.ts", import.meta.url), "utf8"), /\.pathname\)\) invalidateWorkspace\(undefined, true\);/);
    assert.match(readFileSync(new URL("../src/components/Composer.tsx", import.meta.url), "utf8"), /onCollectionInvalidation\(\["catalog"\], refreshVote, \{ local: false \}\)/);
  } finally { remove(); }
});
