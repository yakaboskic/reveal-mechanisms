import { test } from "node:test";
import assert from "node:assert/strict";
import type { Schema } from "../src/lib/client";
import { activityRows } from "../src/lib/activity";
import { elapsedLabel, operationalStep, stageStateLabel, timedActivitySections, toolElapsed } from "../src/lib/activity-timing";
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
      ["research", "ended", 10000], ["collection", "working", (elapsed - 12) * 1000],
    ]);
  }
  const validation = event(3, 30, "validating");
  assert.deepEqual(timedActivitySections([...events, validation], staleJob, base + 35000)
    .map(section => [section.state, section.durationMs]), [["ended", 10000], ["completed", 18000], ["working", 5000]]);
  const failed = { ...event(3, 30, "collecting_output"), status: "failed" as const };
  assert.deepEqual(timedActivitySections([...events, failed], staleJob, base + 35000)
    .map(section => [section.state, section.durationMs]), [["ended", 10000], ["failed", 18000]]);
});
test("historical failure after capture describes the original attempt without a second research timer", () => {
  const events = [event(1, 0, "authoring_account"), event(2, 900, "collecting_output"),
    event(3, 934, "collecting_output"), event(4, 955, "authoring_account", { state: "failed" }),
    { ...event(5, 959, "authoring_account"), event_type: "failure" as const, status: "failed" as const, detail: null }];
  const saved = JSON.stringify(events);
  const sections = timedActivitySections(events, job, base + 2000000);
  assert.deepEqual(sections.map(s => [s.stage, s.state, s.durationMs]), [
    ["research", "failed", 900000], ["collection", "completed", 55000], ["outcome", "failed", null],
  ]);
  assert.equal(stageStateLabel(sections[1].stage, sections[1].state), "Preserved");
  for (const row of activityRows(sections[2].events)) assert.equal(operationalStep(row, [], sections[2], base + 2000000).durationMs, null);
  assert.equal(JSON.stringify(events), saved);
});
test("an early execution failure stops the research timer while diagnostics are still being preserved", () => {
  const events = [event(1, 0, "authoring_account"), event(2, 900, "authoring_account", { state: "failed", source: "harness" })];
  const failed = timedActivitySections(events, job, base + 910000)[0];
  assert.equal(failed.state, "failed"); assert.equal(failed.durationMs, 900000);
  events.push(event(3, 905, "collecting_output"));
  const sections = timedActivitySections(events, job, base + 915000);
  assert.deepEqual(sections.map(s => [s.state, s.durationMs]), [["failed", 900000], ["working", 10000]]);
  assert.equal(stageStateLabel(sections[1].stage, sections[1].state), "Preserving");
});
test("a successful provider notice ends authoring without claiming scientific acceptance", () => {
  const events = [event(1, 0, "authoring_account"), event(2, 20, "collecting_output", { source: "harness", state: "completed" }),
    event(3, 24, "collecting_output"), event(4, 40, "validating")];
  const sections = timedActivitySections(events, job, base + 45000);
  assert.deepEqual(sections.map(s => [s.stage, s.state]), [["research", "completed"], ["collection", "completed"], ["validation", "working"]]);
  assert.equal(stageStateLabel("research", sections[0].state), "Finished");
  assert.equal(stageStateLabel("collection", sections[1].state), "Preserved");
});
test("retry attempts remain separate from earlier failure notices and elapsed idle time", () => {
  const events = [event(1, 0, "authoring_account"), event(2, 10, "collecting_output"),
    { ...event(3, 15, "authoring_account", { state: "failed" }), status: "failed" as const },
    { ...event(4, 100, "authoring_account"), status: "queued" as const }, event(5, 105, "authoring_account"),
    event(6, 120, "collecting_output", { state: "completed", source: "harness" })];
  const sections = timedActivitySections(events, job, base + 125000);
  assert.deepEqual(sections.map(s => [s.stage, s.state, s.durationMs]), [
    ["research", "failed", 10000], ["collection", "completed", 5000], ["outcome", "failed", null],
    ["research", "completed", 20000], ["collection", "working", 5000],
  ]);
});
test("partial replay with a newer failed authoring snapshot does not invent a short research stage", () => {
  const newer = { ...job, status: "failed" as const, stage: "authoring_account" as const, last_event_id: "9", completed_at: date(959) };
  const sections = timedActivitySections([event(1, 900, "collecting_output")], newer, base + 2000000);
  assert.deepEqual(sections.map(s => [s.stage, s.state, s.durationMs]), [["collection", "completed", null], ["outcome", "failed", null]]);
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
test("new and saved agent completion notices never time the wait before capture", () => {
  for (const state of ["started", "completed"] as const) {
    const notice = { event: event(1, 10, "collecting_output", { source: "harness", state }) };
    const capture = { event: event(2, 60, "collecting_output") };
    for (const next of [[], [capture]]) {
      assert.deepEqual(operationalStep(notice, next, { state: "working", end: base + 210_000 }, base + 210_000),
        { state: "completed", durationMs: null });
    }
    // Capture remains an actual worker operation, with its own forward timer.
    assert.deepEqual(operationalStep(capture, [], { state: "working", end: base + 210_000 }, base + 210_000),
      { state: "working", durationMs: 150_000 });
    const validation = event(3, 210, "validating");
    const section = timedActivitySections([notice.event, capture.event, validation], job, base + 300_000)[0];
    assert.equal(section.durationMs, 200_000);
    assert.deepEqual(operationalStep(capture, [], section, base + 300_000), { state: "completed", durationMs: 150_000 });
  }
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
