import { test } from "node:test";
import assert from "node:assert/strict";
import { activityMessage, activityProgress, activityRows, activitySections, coalesceMessageDeltas } from "../src/lib/activity";
import type { Schema } from "../src/lib/client";

function event(id: string, message: string, delta?: boolean): Schema<"JobEvent"> {
  return { id, job_id: "job-1", occurred_at: "2026-09-25T12:00:00Z", event_type: "activity", status: "running",
    stage: "authoring_account", message, result: null, detail: { kind: "agent_message", state: "started", source: "harness",
      call_id: null, tool_name: null, selected_kg: null, display_arguments: null, output_excerpt: null,
      artifact_sha256: null, duration_ms: null, counts: null, ...(delta === undefined ? {} : {message_delta: delta}) } };
}

test("explicit text deltas form one readable row without changing persisted events or replay IDs", () => {
  const input = [event("11", "I", true), event("12", "'ll read α", true), event("13", "β evidence.", true)];
  const original = JSON.stringify(input);
  assert.deepEqual(coalesceMessageDeltas(input).map(e => [e.id, e.message]), [["11", "I'll read αβ evidence."]]);
  assert.equal(JSON.stringify(input), original);
  assert.equal(input.at(-1)?.id, "13");
});

test("legacy capture wording is presented neutrally without changing historical records or agent prose", () => {
  const capture = { ...event("1", "Capturing completed output and evidence."), stage: "collecting_output" as const,
    detail: { ...event("1", "").detail!, kind: "preparation" as const, source: "worker" as const } };
  const saved = JSON.stringify(capture);
  assert.equal(activityMessage(capture), "Preserving available output and evidence.");
  for (const message of ["Restoring the saved execution result and evidence.", "Restoring saved execution result and evidence."])
    assert.equal(activityMessage({ ...capture, message }), "Restoring saved output and evidence.");
  assert.equal(JSON.stringify(capture), saved);
  assert.equal(activityMessage({ ...capture, detail: { ...capture.detail, source: "harness", kind: "agent_message" } }), capture.message);
});

test("full and legacy messages, tool activity, stages and jobs remain separate", () => {
  const tool = event("4", "Using an authorized tool"); tool.detail!.kind = "tool_call";
  const stage = {...event("6", "Other stage", true), stage: "authoring_paragraph" as const};
  const other = {...event("7", "Other job", true), job_id: "job-2"};
  const input = [event("1", "Starting."), event("2", "Complete sentence.", false), event("3", "I", true),
    tool, event("5", "Next", true), stage, other];
  assert.deepEqual(coalesceMessageDeltas(input), input);
});

test("parallel tools pair only by call ID; narrative and unmatched legacy results stay distinct", () => {
  const call = (id: string, callId: string) => ({...event(id, "Using a tool"), detail: {...event(id, "").detail!, kind: "tool_call" as const, call_id: callId, tool_name: "Read"}});
  const result = (id: string, callId: string | null) => ({...event(id, "Tool result"), detail: {...event(id, "").detail!, kind: "tool_result" as const, call_id: callId, state: "completed" as const}});
  const a = call("1", "a"), b = call("2", "b");
  const resultB = result("4", "b"), resultA = result("5", "a"), legacy = result("6", null);
  const events = [a, b, event("3", "Checking the next source."), resultB, resultA, legacy];
  const original = JSON.stringify(events);
  assert.deepEqual(activityRows(events), [{event:a, result:resultA}, {event:b, result:resultB}, {event:events[2]}, {event:legacy}]);
  assert.equal(JSON.stringify(events), original);
  assert.equal(activityRows([a, event("3", "Done reading")])[0].result, undefined);
});

test("setup and validation remain distinct and repeated authoring stages preserve chronology", () => {
  const stages = ["preparing_evidence", "starting_agent", "authoring_account", "validating", "authoring_account", "validating", "persisting"] as const;
  const events = stages.map((stage, i) => ({...event(String(i + 1), stage), stage}));
  assert.deepEqual(activitySections(events).map(section => section.stage), ["preparation", "setup", "research", "validation", "research", "validation", "saving"]);
});

test("live SSE advances stage and terminal status before refresh; stale replay cannot regress fetched job", () => {
  const job = {status:"running", stage:"authoring_account", last_event_id:"10"} as Schema<"Job">;
  const validation = {...event("11", "Validating"), stage:"validating" as const};
  assert.deepEqual(activityProgress(job, [validation]), {status:"running", stage:"validating"});
  assert.deepEqual(activityProgress(job, [{...validation, status:"failed"}]), {status:"failed", stage:"validating"});
  assert.deepEqual(activityProgress({...job, status:"succeeded", stage:"complete", last_event_id:"12"}, [validation]), {status:"succeeded", stage:"complete"});
});

test("output collection follows final agent prose without waiting for a job refresh or claiming acceptance", () => {
  for (const stage of ["authoring_account", "authoring_paragraph"] as const) {
    const job = { status: "running", stage, last_event_id: "10" } as Schema<"Job">;
    const prose = { ...event("11", "The causal question remains open.", true), stage };
    const collection = { ...event("12", "Collecting the finished output."), stage: "collecting_output" as const,
      detail: { ...event("12", "").detail!, kind: "preparation" as const, source: "worker" as const } };
    assert.deepEqual(activityProgress(job, [prose, collection]), { status: "running", stage: "collecting_output" });
    const sections = activitySections([prose, collection]);
    assert.deepEqual(sections.map(section => section.stage), ["research", "collection"]);
    assert.equal(activityRows(sections[0].events)[0].event.message, prose.message);
    const validation = { ...event("13", "Checking source fidelity."), stage: "validating" as const };
    assert.deepEqual(activitySections([prose, collection, validation]).map(section => section.stage), ["research", "collection", "validation"]);
    assert.deepEqual(activityProgress({ ...job, last_event_id: "13", stage: "validating" }, [prose, collection]), { status: "running", stage: "validating" });
  }
});
