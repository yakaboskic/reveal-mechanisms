import assert from "node:assert/strict";
import test from "node:test";
import { createLightningRefresher } from "../src/lib/lightning-refresh";
import { LightningError, type LightningAudit } from "../src/lib/lightning-audit";

const tick = () => new Promise<void>(resolve => setTimeout(resolve, 0));
function harness(initial: LightningAudit["status"] = "preparing") {
  const timers: { run: () => void; ms: number; cleared: boolean }[] = [], reads: { wait: number; signal: AbortSignal; afterRevision?: number }[] = [];
  const accepted: LightningAudit[] = [], errors: unknown[] = [];
  let revision: number | undefined;
  let hidden = false, status = initial, gate: Promise<void> | null = null, failure: unknown = null;
  const refresher = createLightningRefresher({
    hidden: () => hidden, now: () => 0,
    load: async (signal, wait, afterRevision) => { reads.push({ signal, wait, afterRevision }); if (gate) await gate; if (failure) throw failure; return { id: "audit", status, updated_at: status, ...(revision !== undefined ? { progress: { revision } } : {}) } as LightningAudit; },
    onAudit: audit => accepted.push(audit), onError: error => errors.push(error),
    timers: { set: (run, ms) => { const timer = { run, ms, cleared: false }; timers.push(timer); return timer; }, clear: timer => { (timer as typeof timers[number]).cleared = true; } },
  });
  const pending = () => timers.filter(timer => !timer.cleared);
  return { refresher, reads, accepted, errors, pending,
    set: (value: { revision?: number; hidden?: boolean; status?: LightningAudit["status"]; gate?: Promise<void> | null; failure?: unknown }) => {
      if (value.revision !== undefined) revision = value.revision;
      if (value.hidden !== undefined) hidden = value.hidden; if (value.status) status = value.status;
      if ("gate" in value) gate = value.gate!; if ("failure" in value) failure = value.failure;
    },
    fire: async () => { const [timer] = pending(); assert.ok(timer); timer.cleared = true; timer.run(); await tick(); },
  };
}

test("pending audits use a serial 15-second long poll and terminal results stop polling", async () => {
  const h = harness();
  h.refresher.refresh(); await tick(); assert.deepEqual(h.reads.map(read => read.wait), [0]);
  await h.fire(); assert.deepEqual(h.reads.map(read => read.wait), [0, 15]);
  h.set({ status: "succeeded" }); await h.fire(); assert.equal(h.pending().length, 0);
  assert.equal(h.accepted.at(-1)?.status, "succeeded");
  h.refresher.refresh(); await tick(); assert.equal(h.reads.at(-1)?.wait, 0, "A later workspace event only reads the saved result");
  assert.equal(h.pending().length, 0);
});

test("fast unchanged reads back off instead of spinning when a server returns before long polling", async () => {
  const h = harness(); h.refresher.refresh(); await tick();
  const delays: number[] = [];
  for (let i = 0; i < 6; i++) { delays.push(h.pending()[0].ms); await h.fire(); }
  assert.deepEqual(delays, [1000, 1000, 2000, 3000, 4000, 4000]);
  h.refresher.stop();
});

test("event bursts coalesce and an inflight status request is never duplicated", async () => {
  const h = harness("succeeded");
  for (let n = 0; n < 5; n++) h.refresher.refresh();
  await tick(); assert.equal(h.reads.length, 1);
  let release!: () => void;
  h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  h.refresher.refresh(); await tick();
  for (let n = 0; n < 5; n++) h.refresher.refresh();
  await tick(); assert.equal(h.reads.length, 2);
  release(); await tick(); assert.equal(h.reads.length, 3);
  h.refresher.stop();
});

test("hidden pages abort pending reads, accept no late result and resume with one GET", async () => {
  const h = harness(); let release!: () => void;
  h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  h.refresher.refresh(); await tick();
  h.set({ hidden: true }); h.refresher.visibility();
  assert.equal(h.reads[0].signal.aborted, true);
  release(); await tick(); assert.equal(h.accepted.length, 0); assert.equal(h.pending().length, 0);
  h.refresher.refresh(); await tick(); assert.equal(h.reads.length, 1);
  h.set({ hidden: false, gate: null }); h.refresher.visibility(); await tick();
  assert.equal(h.reads.length, 2); assert.equal(h.accepted.length, 1);
  h.refresher.stop();
});

test("transport errors retry only status reads and denied access stops all automatic reads", async () => {
  const h = harness(); h.set({ failure: new LightningError(503, "UNAVAILABLE", "Unavailable") });
  h.refresher.refresh(); await tick(); assert.equal(h.pending()[0].ms, 4000);
  await h.fire(); assert.equal(h.pending()[0].ms, 8000);
  h.set({ failure: new LightningError(403, "FORBIDDEN", "Wrong owner") });
  await h.fire(); assert.equal(h.pending().length, 0);
  const count = h.reads.length; h.refresher.refresh(); h.refresher.visibility(); await tick(); assert.equal(h.reads.length, count);
});

test("unmount aborts outstanding requests and drops responses", async () => {
  const h = harness(); let release!: () => void;
  h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  h.refresher.refresh(); await tick(); h.refresher.stop(); release(); await tick();
  assert.equal(h.reads[0].signal.aborted, true); assert.equal(h.accepted.length, 0); assert.equal(h.pending().length, 0);
});

test("stream revisions advance the cursor even without a status timestamp change", async () => {
  const h = harness("assessing");
  h.refresher.refresh(); await tick();
  assert.equal(h.reads[0].afterRevision, undefined, "The initial GET retrieves the current snapshot");
  h.set({ revision: 1 }); await h.fire();
  assert.equal(h.reads.at(-1)?.afterRevision, 0);
  assert.equal(h.pending()[0].ms, 250, "New provider text is delivered promptly");
  h.set({ revision: 2 }); await h.fire();
  assert.equal(h.reads.at(-1)?.afterRevision, 1);
  assert.equal(h.accepted.at(-1)?.progress?.revision, 2);
  await h.fire();
  assert.equal(h.reads.at(-1)?.afterRevision, 2);
  assert.equal(h.pending()[0].ms, 1000, "Unchanged streaming reads cannot spin");
  h.set({ status: "failed" }); await h.fire();
  assert.equal(h.pending().length, 0);
  h.refresher.stop();
});

test("returning to a hidden stream resumes from its last revision without duplicate reads", async () => {
  const h = harness("assessing"); h.set({ revision: 3 });
  h.refresher.refresh(); await tick();
  let release!: () => void;
  h.set({ gate: new Promise<void>(resolve => { release = resolve; }) });
  await h.fire(); assert.equal(h.reads.at(-1)?.afterRevision, 3);
  h.set({ hidden: true }); h.refresher.visibility();
  assert.equal(h.reads.at(-1)?.signal.aborted, true);
  h.set({ hidden: false, revision: 5 }); h.refresher.visibility(); await tick();
  assert.equal(h.reads.length, 2, "Return waits for the aborted read to settle");
  release(); await tick();
  assert.equal(h.reads.length, 3);
  assert.equal(h.reads.at(-1)?.afterRevision, 3);
  assert.equal(h.accepted.at(-1)?.progress?.revision, 5);
  assert.equal(h.accepted.length, 2, "No late aborted preview is applied");
  h.refresher.stop();
});
