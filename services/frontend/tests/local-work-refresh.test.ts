import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createLocalWorkRefresher, localWorkFallbackMs, nextLocalWorkRefresh } from "../src/lib/local-work-refresh";
import { LocalWorkError, type LocalWork } from "../src/lib/local-work";

type State = LocalWork["state"];
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0));
function harness(initial: State = "ready") {
  const timers: { run: () => void; ms: number; cleared: boolean }[] = [];
  let hidden = false, state: State = initial, failure: unknown = null, gate: Promise<void> | null = null, loads = 0;
  const refresher = createLocalWorkRefresher({
    hidden: () => hidden, terminal: error => error instanceof LocalWorkError && [401, 403, 404].includes(error.status),
    load: async () => { loads++; if (gate) await gate; if (failure) throw failure; return { state }; },
    timers: { set: (run, ms) => { const timer = { run, ms, cleared: false }; timers.push(timer); return timer; }, clear: timer => { (timer as { cleared: boolean }).cleared = true; } },
  });
  const pending = () => timers.filter(timer => !timer.cleared);
  return {
    refresher, pending, loads: () => loads,
    set: (values: { hidden?: boolean; state?: State; failure?: unknown; gate?: Promise<void> | null }) => {
      if ("hidden" in values) hidden = values.hidden!; if (values.state) state = values.state;
      if ("failure" in values) failure = values.failure; if ("gate" in values) gate = values.gate!;
    },
    fire: async () => { const [timer] = pending(); assert.ok(timer, "a fallback timer is scheduled"); timer.cleared = true; timer.run(); await tick(); },
  };
}

test("ready local work makes one read, then only a 60 s fallback (not an 8 s poll)", async () => {
  const h = harness("ready");
  h.refresher.refresh(); await tick();
  assert.equal(h.loads(), 1);
  assert.deepEqual(h.pending().map(timer => timer.ms), [localWorkFallbackMs]);
  await h.fire(); assert.equal(h.loads(), 2);
  assert.deepEqual(h.pending().map(timer => timer.ms), [60_000]);
});

test("preparation backs off 2-4-8-16 s while events can report readiness sooner", async () => {
  const h = harness("preparing"), delays: number[] = [];
  h.refresher.refresh(); await tick();
  for (let read = 0; read < 5; read++) { delays.push(h.pending()[0].ms); await h.fire(); }
  assert.deepEqual(delays, [2_000, 4_000, 8_000, 16_000, 16_000]);
  h.set({ state: "ready" }); await h.fire();
  assert.deepEqual(h.pending().map(timer => timer.ms), [60_000]);
});

test("closed and failed preparation stop the fallback; events and page returns still refresh", async () => {
  for (const state of ["closed", "preparation_failed"] as const) {
    const h = harness(state);
    h.refresher.refresh(); await tick();
    assert.equal(h.loads(), 1); assert.equal(h.pending().length, 0);
    h.refresher.refresh(); await tick(); assert.equal(h.loads(), 2);
    h.refresher.visible(); await tick(); assert.equal(h.loads(), 3);
    assert.equal(h.pending().length, 0);
  }
});

test("hidden pages make no requests and refresh immediately when visible again", async () => {
  const h = harness("ready");
  h.refresher.refresh(); await tick(); assert.equal(h.loads(), 1);
  h.set({ hidden: true });
  await h.fire(); h.refresher.refresh(); await tick(); h.refresher.visible(); await tick();
  assert.equal(h.loads(), 1); assert.equal(h.pending().length, 0);
  h.set({ hidden: false }); h.refresher.visible(); await tick();
  assert.equal(h.loads(), 2); assert.deepEqual(h.pending().map(timer => timer.ms), [60_000]);
});

test("an event burst coalesces and an event during a read schedules exactly one more read", async () => {
  const h = harness("ready");
  for (let event = 0; event < 5; event++) h.refresher.refresh();
  await tick(); assert.equal(h.loads(), 1);
  let release!: () => void; h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  h.refresher.refresh(); await tick(); assert.equal(h.loads(), 2);
  h.refresher.refresh(); await tick(); h.refresher.refresh(); await tick();
  assert.equal(h.loads(), 2, "reads never overlap");
  h.set({ gate: null }); release(); await tick(); await tick();
  assert.equal(h.loads(), 3); assert.equal(h.pending().length, 1);
});

test("lost access stops every refresh path; transient failures retry with backoff", async () => {
  const h = harness("ready");
  h.set({ failure: new Error("Offline") });
  h.refresher.refresh(); await tick();
  assert.deepEqual(h.pending().map(timer => timer.ms), [4_000]);
  await h.fire(); assert.deepEqual(h.pending().map(timer => timer.ms), [8_000]);
  h.set({ failure: null }); await h.fire(); assert.deepEqual(h.pending().map(timer => timer.ms), [60_000]);
  h.set({ failure: new LocalWorkError(404, "NOT_FOUND", "Local research unavailable.") });
  await h.fire(); const loads = h.loads();
  assert.equal(h.pending().length, 0);
  h.refresher.refresh(); h.refresher.visible(); await tick();
  assert.equal(h.loads(), loads);
});

test("stopping discards an in-flight read and schedules nothing", async () => {
  const h = harness("ready");
  let release!: () => void; h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  h.refresher.refresh(); await tick(); h.refresher.stop(); release(); await tick();
  assert.equal(h.pending().length, 0);
  h.refresher.refresh(); await tick(); assert.equal(h.loads(), 1);
});

test("fallback policy keeps closed work idle and caps retries at the fallback", () => {
  assert.equal(nextLocalWorkRefresh("closed", 0, 0), null);
  assert.equal(nextLocalWorkRefresh("ready", 0, 0), 60_000);
  assert.equal(nextLocalWorkRefresh("preparing", 9, 0), 16_000);
  assert.equal(nextLocalWorkRefresh("closed", 0, 9), 60_000);
});

test("the local work view refreshes on workspace 'jobs' events and page returns instead of an 8 s timer", async () => {
  const view = await readFile(new URL("../src/components/LocalWork.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(view, /nextRefresh|8_000|2_000|setTimeout\(\(\) => void refresh/);
  assert.match(view, /onCollectionInvalidation\(\["jobs"\]/);
  assert.match(view, /createLocalWorkRefresher\(/);
  assert.match(view, /"visibilitychange", refresher\.visible/);
});
