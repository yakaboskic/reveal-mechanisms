import assert from "node:assert/strict";
import test from "node:test";
import { createLightningClient, readLightningAudit, lightningBrief, lightningLiveProgress, lightningProgressLabel, lightningContinuationHref, lightningDispatchRejected, LightningError,
  rememberLightningHandoff, restoreLightningHandoff, rememberLightningBrief, restoreLightningBrief, type LightningAudit } from "../src/lib/lightning-audit";
import { affectedWorkspaceTabs, changesWorkspace, onWorkspaceChange } from "../src/lib/workspace-events";
import { rememberSubmission, restoreSubmission, lightningSubmissionOwnerChanged, type SubmissionAttempt } from "../src/lib/submission";

export const auditFixture = (status: LightningAudit["status"] = "succeeded"): LightningAudit => ({
  id: "audit", kind: "lightning_audit", status, research_request_id: "request", source_draft_id: "source", source_draft_version: 3,
  question: { id: "gap", text: "What could connect the observed mechanisms?" }, created_at: "2026-10-09T12:00:00Z", updated_at: "2026-10-09T12:00:01Z",
  completed_at: status === "succeeded" ? "2026-10-09T12:00:01Z" : null, continuation_expires_at: "2026-11-08T12:00:00Z", reference_generation_id: "generation",
  result: status === "succeeded" ? { assessment: "partial", summary: "A useful lead needs further evidence.", observations: [{ text: "The supplied loading is relevant.", evidence_refs: ["E1"] }],
    recommended_direction: "Investigate the proposed connection.", missing_evidence: ["Direction of the relationship"], next_steps: ["Check independent evidence"], limitations: ["Bounded source sampling"] } : null,
  coverage: { missing: [], truncations: [] }, evidence_references: [{ id: "E1", pointer: "/factors/0/genes/0", label: "Observed loading", value: { gene: "GENE1", loading: 0.7 }, source: { revision: "rev" } }],
  provenance: { model: "test-model", prompt_version: "test-v1" }, usage: { input_tokens: 100, output_tokens: 50 }, error: null, continuations: [],
});
function storage(t: { after: (fn: () => void) => void }) {
  const values = new Map<string, string>();
  const previous = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, "sessionStorage", previous); else delete (globalThis as Record<string, unknown>).sessionStorage; });
  return values;
}

test("audit POST uses an exact receipt while GET and long polling are private reads", async () => {
  const calls: { url: string; init: RequestInit }[] = [], events: string[][] = [];
  const remove = onWorkspaceChange((_reset, event) => events.push(event?.collections || []));
  const client = createLightningClient((async (url, init) => { calls.push({ url: String(url), init: init! }); return Response.json(auditFixture()); }) as typeof fetch);
  try {
    await client.create({ draft_id: "source", draft_version: 3 }, "original-key");
    await client.get("audit"); await client.get("audit", undefined, 15); await client.get("audit", undefined, 15, 7);
    assert.deepEqual(calls.map(call => call.init.method), ["POST", "GET", "GET", "GET"]);
    assert.deepEqual(JSON.parse(calls[0].init.body as string), { draft_id: "source", draft_version: 3 });
    assert.equal(new Headers(calls[0].init.headers).get("Idempotency-Key"), "original-key");
    assert.equal(calls[2].url, "/api/backend/v1/lightning-audits/audit?wait=15");
    assert.equal(calls[3].url, "/api/backend/v1/lightning-audits/audit?wait=15&after_revision=7");
    assert.ok(calls.every(call => call.init.credentials === "same-origin" && call.init.cache === "no-store"));
    assert.equal(events.length, 1, "Reads neither mutate nor invalidate workspace history");
    assert.ok(events[0].includes("audits"));
  } finally { remove(); }
});

test("completed output rejects unresolved evidence, incomplete fields and partial results", () => {
  const audit = auditFixture();
  assert.equal(readLightningAudit(audit), audit);
  assert.throws(() => readLightningAudit({ ...audit, evidence_references: [] }), /references could not be verified/);
  assert.throws(() => readLightningAudit({ ...audit, status: "assessing" }), /complete assessment/);
  assert.throws(() => readLightningAudit({ ...audit, result: { ...audit.result, next_steps: null } }), /incomplete/);
  assert.throws(() => readLightningAudit({ ...audit, status: "toString" }), /incomplete/);
  assert.throws(() => readLightningAudit({ ...audit, evidence_references: [{ id: "E1" }] }), /incomplete/);
});

test("read responses must match the requested audit and provider failures make no implicit retries", async () => {
  let calls = 0;
  const mismatch = createLightningClient((async () => Response.json(auditFixture())) as typeof fetch);
  await assert.rejects(mismatch.get("different"), /did not match/);
  const failing = createLightningClient((async () => { calls++; return Response.json({ code: "UNAVAILABLE", detail: "Service unavailable" }, { status: 503 }); }) as typeof fetch);
  await assert.rejects(failing.create({ draft_id: "source", draft_version: 3 }, "key"), LightningError);
  assert.equal(calls, 1);
});

test("edited brief and uncertain continuation retain owner-scoped exact input across reload", t => {
  storage(t);
  const pending = { mode: "online" as const, research_direction: "User-edited direction", key: "paid-run-key" };
  rememberLightningHandoff("owner", "audit", pending); rememberLightningBrief("owner", "audit", "Unsubmitted edit");
  assert.deepEqual(restoreLightningHandoff("owner", "audit"), pending);
  assert.equal(restoreLightningHandoff("other", "audit"), null);
  assert.equal(restoreLightningHandoff("owner", "other-audit"), null);
  assert.equal(restoreLightningBrief("owner", "audit"), "Unsubmitted edit");
  rememberLightningHandoff("owner", "audit", null);
  assert.equal(restoreLightningHandoff("owner", "audit"), null);
  assert.equal(restoreLightningBrief("owner", "audit"), "Unsubmitted edit");
});

test("online and local continuation send the exact reviewed direction with an idempotency key", async () => {
  for (const mode of ["online", "local"] as const) {
    let calls = 0;
    const client = createLightningClient((async (url, init) => {
      calls++; assert.equal(url, "/api/backend/v1/lightning-audits/audit/continue");
      assert.equal(new Headers(init!.headers).get("Idempotency-Key"), "same-key");
      assert.deepEqual(JSON.parse(init!.body as string), { mode, research_direction: "Reviewed direction" });
      return Response.json({ mode, id: "child", research_request_id: "frozen-child", created_at: "2026-10-09" });
    }) as typeof fetch);
    const value = await client.continue("audit", { mode, research_direction: "Reviewed direction" }, "same-key");
    assert.equal(calls, 1);
    assert.equal(lightningContinuationHref(value), mode === "local" ? "/local-runs/child" : "/runs/child");
  }
});

test("new audit events target runs and leave accounts and unrelated histories fresh", () => {
  assert.deepEqual(affectedWorkspaceTabs({ collections: ["audits"] } as never), ["runs"]);
  assert.equal(changesWorkspace("POST", "/api/backend/v1/lightning-audits"), true);
  assert.equal(changesWorkspace("POST", "/api/backend/v1/lightning-audits/id/continue"), true);
  assert.equal(changesWorkspace("GET", "/api/backend/v1/lightning-audits/id"), false);
});

test("lightning launch recovery accepts the new mode and retains exact uncertain historical receipts", t => {
  storage(t);
  const pending = { mode: "lightning", method: "session", owner: "owner", composer: { source_gap: { id: "gap" }, eaggl_anchors: [{}] },
    draft: { id: "source", version: 3 }, submitKey: { binding: "lightning:source:3", key: "audit-key" }, requestKeys: [], anonymousKey: "session-key" } as unknown as SubmissionAttempt;
  rememberSubmission(pending);
  assert.deepEqual(restoreSubmission(), pending);
  for (const mode of [undefined, "online", "local"] as const) {
    rememberSubmission({ ...pending, mode });
    assert.equal(restoreSubmission()?.mode, mode, "Historical online/local receipts still restore");
  }
});

test("the handoff brief includes the recommendation and concrete next steps", () => {
  assert.equal(lightningBrief(auditFixture()), "Investigate the proposed connection.\n\nNext steps:\n- Check independent evidence");
  assert.equal(lightningBrief(auditFixture("failed")), "");
});

test("definite admission rejections unlock edits while ambiguous deliveries retain their receipt", () => {
  for (const [status, code] of [[429, "LOCAL_WORK_LIMIT"], [503, "LIGHTNING_DISABLED"], [503, "REFERENCE_RELOAD_IN_PROGRESS"], [409, "AUDIT_CONTINUATION_EXPIRED"], [409, "SOURCE_UNAVAILABLE"], [422, "INVALID_REQUEST"]] as const)
    assert.equal(lightningDispatchRejected(new LightningError(status, code, "rejected")), true, code);
  for (const failure of [new Error("network failure"), new LightningError(502, "API_UNAVAILABLE", "unknown"), new LightningError(503, "UNKNOWN", "unknown"), new LightningError(409, "IDEMPOTENCY_CONFLICT", "conflicting receipt")])
    assert.equal(lightningDispatchRejected(failure), false);
});

test("a possibly committed Lightning dispatch cannot migrate automatically to another owner's key namespace", () => {
  const uncertain = { mode: "lightning", owner: "original", submitKey: { binding: "lightning:source:3", key: "original-key" } } as SubmissionAttempt;
  assert.equal(lightningSubmissionOwnerChanged(uncertain, "claimed"), true);
  assert.equal(lightningSubmissionOwnerChanged(uncertain, "original"), false);
  assert.equal(lightningSubmissionOwnerChanged({ ...uncertain, submitKey: null }, "claimed"), false, "Sign-in before first dispatch still works");
  for (const mode of ["online", "local"] as const) assert.equal(lightningSubmissionOwnerChanged({ ...uncertain, mode }, "claimed"), false, "Existing agent submission flow is unchanged");
});

const progressFixture = () => ({ revision: 4, phase: "writing" as const, summary: "The supplied CFDE factor offers a possible lead", recommended_direction: "", observations: ["A source observation arriving in parts"], missing_evidence: [], next_steps: [], limitations: [] });

test("live drafts display only while pending and never become a continuation brief", () => {
  const progress = progressFixture(), audit = { ...auditFixture("assessing"), progress };
  assert.equal(readLightningAudit(audit), audit);
  assert.equal(lightningLiveProgress(audit), progress);
  assert.equal(lightningProgressLabel(audit), "Writing your assessment");
  assert.equal(lightningBrief(audit), "", "A stream preview is not validated research guidance");
  assert.equal(lightningProgressLabel(auditFixture("preparing")), "Preparing evidence");
  assert.equal(lightningProgressLabel(auditFixture("assessing")), "Thinking about the CFDE evidence");
  assert.equal(lightningProgressLabel({ ...audit, progress: { ...progress, phase: "validating" } }), "Checking evidence references");
  for (const status of ["succeeded", "failed", "interrupted"] as const) {
    assert.equal(lightningLiveProgress({ ...auditFixture(status), progress }), null, `${status} hides a stale draft`);
  }
});

test("malformed live revisions and raw structured observations cannot enter the text view", () => {
  const audit = { ...auditFixture("assessing"), progress: progressFixture() };
  for (const value of [{ revision: -1 }, { revision: 1.2 }, { phase: "thinking" }, { summary: {} }, { observations: [{ text: "Raw structured content" }] }, { next_steps: null }])
    assert.throws(() => readLightningAudit({ ...audit, progress: { ...audit.progress, ...value } }), /live response was incomplete/);
  assert.equal(readLightningAudit({ ...auditFixture("assessing"), progress: null }).progress, null, "Historical responses need no stream preview");
});
