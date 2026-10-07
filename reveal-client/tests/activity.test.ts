import test from "node:test";
import assert from "node:assert/strict";
import { activityMessage, activityStageLabel } from "../src/lib/activity";
import type { JobEvent } from "../src/lib/types";

test("capture is evidence preservation, never an accepted scientific result", () => {
  assert.equal(activityStageLabel("collecting_output"), "Preserving output and evidence");
  const event = { stage: "collecting_output", message: "Capturing completed output and evidence.", detail: { source: "worker" } } as JobEvent;
  const before = JSON.stringify(event);
  assert.equal(activityMessage(event), "Preserving available output and evidence.");
  for (const message of ["Restoring the saved execution result and evidence.", "Restoring saved execution result and evidence."])
    assert.equal(activityMessage({ ...event, message }), "Restoring saved output and evidence.");
  assert.equal(JSON.stringify(event), before);
  assert.equal(activityMessage({ ...event, detail: { ...event.detail!, source: "harness", kind: "agent_message" } }), event.message);
});
