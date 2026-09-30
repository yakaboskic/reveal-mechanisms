import { test } from "node:test";
import assert from "node:assert/strict";
import type { Schema } from "../src/lib/client";
import { activityRows } from "../src/lib/activity";
import { elapsedLabel, operationalStep, timedActivitySections, toolElapsed } from "../src/lib/activity-timing";
const base = Date.parse("2026-09-28T12:00:00Z");
const date = (seconds: number) => new Date(base + seconds * 1000).toISOString();
const job = { status: "running", stage: "preparing_evidence", last_event_id: "0", created_at: date(0), completed_at: null } as Schema<"Job">;
function event(id: number, seconds: number, stage: Schema<"Job">["stage"] = "preparing_evidence", detail: Partial<NonNullable<Schema<"JobEvent">["detail"]>> = {}): Schema<"JobEvent"> {
  return { id: String(id), job_id: "job", occurred_at: date(seconds), stage, status: "running", event_type: "activity", message: "step", result: null,
    detail: { kind: "preparation", state: "started", source: "worker", call_id: null, tool_name: null, selected_kg: null, display_arguments: null, output_excerpt: null, artifact_sha256: null, duration_ms: null, counts: null, ...detail } };
}
test("recorded stage boundaries freeze prior stages while the current timer advances", () => {
  const events = [event(1, 2), event(2, 6, "starting_agent"), event(3, 10, "authoring_account")];
  assert.deepEqual(timedActivitySections(events, job, base + 15_000).map(s => s.durationMs), [4000, 4000, 5000]);
  const later = timedActivitySections(events, job, base + 19_000);
  assert.deepEqual(later.map(s => s.durationMs), [4000, 4000, 9000]);
  assert.deepEqual(later.map(s => s.state), ["completed", "completed", "working"]);
});
test("agent completion freezes research timing while output collection continues before acceptance", () => {
  const staleJob = { ...job, stage: "authoring_account" as const, last_event_id: "1" };
  const events = [event(1, 2, "authoring_account"), event(2, 12, "collecting_output")];
  for (const elapsed of [15, 25]) {
    const sections = timedActivitySections(events, staleJob, base + elapsed * 1000);
    assert.deepEqual(sections.map(section => [section.stage, section.state, section.durationMs]), [
      ["research", "completed", 10000], ["collection", "working", (elapsed - 12) * 1000],
    ]);
  }
  const validation = event(3, 30, "validating");
  assert.deepEqual(timedActivitySections([...events, validation], staleJob, base + 35000)
    .map(section => [section.state, section.durationMs]), [["completed", 10000], ["completed", 18000], ["working", 5000]]);
  const failed = { ...event(3, 30, "collecting_output"), status: "failed" as const };
  assert.deepEqual(timedActivitySections([...events, failed], staleJob, base + 35000)
    .map(section => [section.state, section.durationMs]), [["completed", 10000], ["failed", 18000]]);
});
test("terminal SSE freezes failure and cancellation before the job snapshot catches up", () => {
  for (const status of ["failed", "cancelled"] as const) {
    const events = [event(1, 2), { ...event(2, 8), status }];
    const section = timedActivitySections(events, job, base + 100_000)[0];
    assert.equal(section.durationMs, 6000);
    assert.equal(section.state, status === "failed" ? "failed" : "stopped");
  }
});
test("review retry keeps the failed review separate and excludes idle time from its duration", () => {
  const events = [event(1, 2, "validating"), { ...event(2, 8, "validating"), status: "failed" as const },
    { ...event(3, 100, "validating"), status: "queued" as const }, event(4, 101, "validating")];
  const sections = timedActivitySections(events, job, base + 105_000);
  assert.deepEqual(sections.map(section => section.state), ["failed", "working"]);
  assert.deepEqual(sections.map(section => section.durationMs), [6000, 5000]);
});
test("partial replay does not fabricate start/end times and repeated stages remain separate", () => {
  const newer = { ...job, stage: "validating" as const, last_event_id: "9" };
  assert.deepEqual(timedActivitySections([event(1, 1)], newer, base + 10_000).map(s => s.durationMs), [null, null]);
  assert.equal(timedActivitySections([], job, base + 10_000)[0].durationMs, null);
  assert.equal(timedActivitySections([], { ...job, stage: "queued" }, base + 10_000)[0].durationMs, 10000);
  const events = [event(1, 1, "authoring_account"), event(2, 4, "validating"), event(3, 8, "authoring_account")];
  assert.deepEqual(timedActivitySections(events, job, base + 10_000).map(s => s.durationMs), [3000, 4000, 2000]);
});
test("operational dots complete on the next worker step while errors remain errors", () => {
  const row = { event: event(1, 1) }, next = { event: event(2, 4) };
  assert.deepEqual(operationalStep(row, [], { state: "working", end: base + 5000 }, base + 5000), { state: "working", durationMs: 4000 });
  assert.deepEqual(operationalStep(row, [next], { state: "working", end: base + 5000 }, base + 5000), { state: "completed", durationMs: 3000 });
  const failed = { event: event(1, 1, "preparing_evidence", { state: "failed" }) };
  assert.deepEqual(operationalStep(failed, [next], { state: "completed", end: base + 5000 }, base + 5000), { state: "failed", durationMs: null });
});
test("parallel tool timers use their own results and stopped calls do not keep ticking", () => {
  const call = (id: number, time: number, callId: string) => event(id, time, "authoring_account", { kind: "tool_call", call_id: callId });
  const result = (id: number, time: number, callId: string) => event(id, time, "authoring_account", { kind: "tool_result", call_id: callId, state: "completed" });
  const rows = activityRows([call(1, 1, "a"), call(2, 2, "b"), result(3, 4, "b"), result(4, 9, "a")]);
  assert.deepEqual(rows.map(row => toolElapsed(row, true, base + 10_000)), [8000, 2000]);
  assert.equal(toolElapsed({ event: call(1, 1, "a") }, false, base + 100_000), null);
  assert.equal(toolElapsed({ event: call(1, 1, "a") }, true, base + 3000), 2000);
  assert.equal(toolElapsed({ event: call(1, 5, "a") }, true, base + 3000), 0);
  assert.equal(elapsedLabel(65000), "1m 5s");
  assert.equal(elapsedLabel(3661000), "1h 1m 1s");
});
