import { test } from "node:test";
import assert from "node:assert/strict";
import { paragraphStateFromJob } from "../src/lib/paragraph-state";
import type { Schema } from "../src/lib/client";
import fixture from "../src/lib/fixtures/contract.json";

const accountId = fixture.account.root_id;
const paragraphId = fixture.paragraph.root_id;
const job: Schema<"Job"> = { id: "55555555-5555-4555-8555-555555555555", kind: "paragraph", input_account_id: accountId,
  owner_user_id: "11111111-1111-4111-8111-111111111111", research_request_id: null,
  status: "running", stage: "authoring_paragraph", result: null, failure: null, warnings: [], last_event_id: "1",
  created_at: "2026-09-28T12:00:00Z", updated_at: "2026-09-28T12:00:00Z", completed_at: null,
  links: { self: "/job", events: "/job/events", cancel: "/job/cancel" } };

test("paragraph status stays active during cancellation and carries only its own accepted result", () => {
  assert.deepEqual(paragraphStateFromJob({ ...job, status: "cancel_requested" }, accountId), { job_id: job.id, status: "running", paragraph_id: null });
  assert.deepEqual(paragraphStateFromJob({ ...job, status: "succeeded", result: { kind: "paragraph", account_id: accountId, paragraph_id: paragraphId } }, accountId), { job_id: job.id, status: "succeeded", paragraph_id: paragraphId });
});

test("analysis jobs, cross-account results and malformed successes cannot select a statement", () => {
  assert.equal(paragraphStateFromJob({ ...job, kind: "analysis" }, accountId), null);
  assert.equal(paragraphStateFromJob(job, "different-account"), null);
  assert.equal(paragraphStateFromJob({ ...job, status: "succeeded" }, accountId), null);
  assert.equal(paragraphStateFromJob({ ...job, status: "succeeded", result: { kind: "paragraph", account_id: "different-account", paragraph_id: paragraphId } }, accountId), null);
});
