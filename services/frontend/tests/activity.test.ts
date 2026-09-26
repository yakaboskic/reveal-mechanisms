import { test } from "node:test";
import assert from "node:assert/strict";
import { coalesceMessageDeltas } from "../src/lib/activity";
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

test("full and legacy messages, tool activity, stages and jobs remain separate", () => {
  const tool = event("4", "Using an authorized tool"); tool.detail!.kind = "tool_call";
  const stage = {...event("6", "Other stage", true), stage: "authoring_paragraph" as const};
  const other = {...event("7", "Other job", true), job_id: "job-2"};
  const input = [event("1", "Starting."), event("2", "Complete sentence.", false), event("3", "I", true),
    tool, event("5", "Next", true), stage, other];
  assert.deepEqual(coalesceMessageDeltas(input), input);
});
