import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { followJobEvents } from "../src/lib/job-events";
import type { Schema } from "../src/lib/client";
import type { PageVisibility } from "../src/lib/page-visibility";

type Job = Schema<"Job">; type JobEvent = Schema<"JobEvent">;
const job = (status: Job["status"], last: string) => ({ id: "job-1", status, last_event_id: last } as Job);
const event = (id: string, status: Job["status"] = "running") => ({ id, job_id: "job-1", status, event_type: "progress", message: `step ${id}`, occurred_at: "2026-10-07T00:00:00Z" } as JobEvent);
const sse = (...events: JobEvent[]) => events.map(value => `id: ${value.id}\nevent: ${value.event_type}\ndata: ${JSON.stringify(value)}\n\n`).join("");
const settle = async (rounds = 6) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setTimeout(resolve, 2)); };

/** Each request answers with `bodies[n]`; an absent body stays open until aborted. */
function server(bodies: (string | undefined)[]) {
  const requests: { cursor: string | null; signal: AbortSignal }[] = [];
  const fetcher = (async (_url: unknown, options: RequestInit) => {
    const text = bodies[requests.length], signal = options.signal!;
    requests.push({ cursor: new Headers(options.headers).get("Last-Event-ID"), signal });
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      if (text !== undefined) { controller.enqueue(new TextEncoder().encode(text)); controller.close(); return; }
      signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
    } });
    return new Response(body, { headers: { "content-type": "text/event-stream" } });
  }) as typeof fetch;
  return { fetcher, requests };
}

function view(known: Job, refreshed: Job = known) {
  const seen = { events: [] as string[], labels: [] as string[], errors: [] as string[], refreshes: 0 };
  return { seen, view: { event: (value: JobEvent) => { seen.events.push(value.id); }, connection: (label: string) => { seen.labels.push(label); },
    error: (message: string) => { seen.errors.push(message); }, refresh: async () => { seen.refreshes++; return refreshed; }, known: () => known } };
}

test("a finished job's activity is one stream and no job read", async () => {
  const { fetcher, requests } = server([sse(event("1"), event("2"), event("3", "succeeded"))]);
  const { seen, view: activity } = view(job("succeeded", "3"));
  await followJobEvents(new AbortController().signal, "job-1", { current: "0" }, activity, fetcher);
  assert.equal(requests.length, 1); assert.equal(seen.refreshes, 0);
  assert.deepEqual(seen.events, ["1", "2", "3"]); assert.equal(seen.labels.at(-1), "");
});

test("a job finishing while watched is read once, not again when its stream ends", async () => {
  const { fetcher, requests } = server([sse(event("2"), event("3", "succeeded"))]);
  const { seen, view: activity } = view(job("running", "1"), job("succeeded", "3"));
  await followJobEvents(new AbortController().signal, "job-1", { current: "1" }, activity, fetcher);
  assert.equal(requests.length, 1); assert.equal(seen.refreshes, 1); assert.deepEqual(seen.events, ["2", "3"]);
});

test("a renewed stream of a running job checks the job once and resumes from its cursor", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { fetcher, requests } = server([sse(event("2")), sse(event("3", "failed"))]);
  const { seen, view: activity } = view(job("running", "1"), job("running", "2"));
  const cursor = { current: "1" };
  const run = followJobEvents(new AbortController().signal, "job-1", cursor, activity, fetcher);
  for (let i = 0; i < 20 && seen.refreshes < 1; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(seen.refreshes, 1);
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  t.mock.timers.tick(1_000);
  activity.refresh = async () => { seen.refreshes++; return job("failed", "3"); };
  await run;
  assert.deepEqual(requests.map(value => value.cursor), ["1", "2"]); assert.equal(seen.refreshes, 2); assert.equal(cursor.current, "3");
});

test("a hidden tab parks the job stream without a failure or job read and resumes from its cursor once shown", async () => {
  let hidden = false; const waiting = new Set<() => void>(), timers = new Set<() => void>();
  const page: PageVisibility = {
    hidden: () => hidden,
    untilVisible: signal => new Promise(resolve => { if (!hidden) return resolve(); waiting.add(() => resolve()); signal.addEventListener("abort", () => resolve(), { once: true }); }),
    onHiddenFor: (_ms, callback) => { timers.add(callback); return () => { timers.delete(callback); }; },
  };
  const { fetcher, requests } = server([undefined, undefined]);
  const { seen, view: activity } = view(job("running", "4"));
  const close = new AbortController();
  const run = followJobEvents(close.signal, "job-1", { current: "4" }, activity, fetcher, page); await settle();
  hidden = true; for (const park of [...timers]) park(); await settle();
  assert.equal(requests.length, 1); assert.equal(requests[0].signal.aborted, true);
  assert.equal(seen.refreshes, 0); assert.ok(!seen.labels.some(label => label.includes("interrupted")));
  hidden = false; for (const show of [...waiting]) show(); await settle();
  assert.equal(requests.length, 2); assert.equal(requests[1].cursor, "4");
  close.abort(); await run;
});

test("activity streams through the shared job follower", async () => {
  const activity = await readFile(new URL("../src/components/Activity.tsx", import.meta.url), "utf8");
  assert.match(activity, /followJobEvents\(controller\.signal, initial\.id, cursor,/);
  assert.match(activity, /known: \(\) => jobRef\.current/);
  assert.doesNotMatch(activity, /events\?after=/);
});
