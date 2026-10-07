import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ASSESSMENT_WAIT_SECONDS, AssessmentAutoCheck, AssessmentController, AssessmentRequestError, assessmentApi, assessmentBinding, assessmentReady, assessmentResult, assessmentSupportLabel, idleAssessment, readAssessment, visibleAssessment, type AssessmentState, type CfdeAssessment } from "../src/lib/cfde-assessment";
import { CfdeAssessmentFeedback, CfdeEstimateRing } from "../src/components/CfdeAssessmentView";
import { CfdeAssessment as Assessment } from "../src/components/CfdeAssessment";

const draft = { id: "isolated-draft", version: 3 };
const composer = { source_gap: { id: "gap:fixture", source_revision: "gap-revision" }, eaggl_anchors: [{ reference: { source_id: "factor:fixture" } }], context: "Unsaved fixture context" };
const resource = (overrides: Partial<CfdeAssessment> = {}): CfdeAssessment => ({
  id: "assessment-fixture", draft_id: draft.id, draft_version: draft.version, composer_sha256: "composer-hash",
  status: "succeeded", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:01Z", expires_at: "2099-01-01T00:00:00Z",
  model: "jev-fixture", rubric_version: "fixture-v1", reference_generation_id: "reference-fixture", stale: false,
  result: { verdict: "yes", probability_yes: 0.56, probability_no: 0.44, confidence: 0.91, main_blocker: "missing_cfde_evidence", relationship_support: { gene_gene_set: 0.6, gene_mechanism: 0.4, gene_set_mechanism: 0.5 }, calibration: "not_calibrated" },
  coverage: { factor_count: 1, gene_loading_count: 50, gene_set_loading_count: 50, unique_gene_set_count: 40, complete: false, missing: ["GeneSet construction metadata unavailable"], truncations: [{ source: "DisMech attachment", included_chars: 1200, total_chars: 5000 }] }, error: null, ...overrides,
});
const pending = () => resource({ status: "preparing", result: null, coverage: null });
const render = (state: AssessmentState, disabled = false) => renderToStaticMarkup(React.createElement(CfdeAssessmentFeedback, { state, disabled, onCheck() {} }));
function harness(overrides: Partial<ConstructorParameters<typeof AssessmentController>[1]> = {}) {
  const states: AssessmentState[] = [], calls: { key: string; context: unknown }[] = [];
  let now = 0, keys = 0;
  const controller = new AssessmentController(state => states.push(state), {
    start: async (_draft, value, key) => { calls.push({ key, context: value }); return resource(); },
    get: async () => resource(), wait: async ms => { now += ms; }, now: () => now, key: () => `key-${++keys}`, ...overrides,
  });
  controller.bind(draft, composer);
  return { controller, states, calls };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { resolve, promise }; }
function clock() {
  let now = 0, next = 0;
  const pending = new Map<number, { at: number; callback: () => void }>();
  return {
    timers: { schedule(callback: () => void, ms: number) { const id = ++next; pending.set(id, { at: now + ms, callback }); return id; }, cancel(id: unknown) { pending.delete(id as number); } },
    advance(ms: number) { now += ms; for (const [id, entry] of pending) if (entry.at <= now) { pending.delete(id); entry.callback(); } },
    pending,
  };
}

test("automatic checks make no request until draft, gap, anchors and editor readiness are present", () => {
  const time = clock(), calls: string[] = [];
  const automatic = new AssessmentAutoCheck(binding => { calls.push(binding); return true; }, time.timers);
  for (const [value, inputs, disabled] of [[null, composer, false], [draft, { ...composer, source_gap: null }, false], [draft, { ...composer, eaggl_anchors: [] }, false], [draft, composer, true]] as const) {
    automatic.queue("input", assessmentReady(value, inputs, disabled)); time.advance(2000); assert.equal(calls.length, 0);
  }
  automatic.queue("input", assessmentReady(draft, composer, false)); time.advance(1499); assert.equal(calls.length, 0);
  time.advance(1); assert.deepEqual(calls, ["input"]);
  automatic.queue("input", true); time.advance(5000); assert.equal(calls.length, 1);
});

test("automatic checks debounce edits and wait again when saving or attachments block readiness", () => {
  const time = clock(), calls: string[] = [];
  const automatic = new AssessmentAutoCheck(binding => { calls.push(binding); return true; }, time.timers);
  automatic.queue("first edit", true); time.advance(1000);
  automatic.queue("second edit", true); time.advance(1000); assert.equal(calls.length, 0);
  automatic.queue("second edit", false); time.advance(5000); assert.equal(calls.length, 0);
  automatic.queue("second edit", true); time.advance(1499); assert.equal(calls.length, 0);
  time.advance(1); assert.deepEqual(calls, ["second edit"]);
});

test("unmount cleanup cancels the timer and invalidates a callback already queued for delivery", () => {
  const time = clock(), calls: string[] = [];
  const automatic = new AssessmentAutoCheck(binding => { calls.push(binding); return true; }, time.timers);
  automatic.queue("first", true);
  const pending = [...time.pending.values()][0].callback;
  automatic.cancel(); time.advance(2000); pending(); assert.equal(calls.length, 0);
});

test("a manual check cancels the automatic timer and unchanged failures never restart automatically", async () => {
  for (const failure of ["failed", "interrupted", "ambiguous"] as const) {
    const time = clock(); let starts = 0;
    const { controller } = harness({ start: async () => { starts++; if (failure === "ambiguous") throw new TypeError("lost response"); return resource({ status: failure, result: null, error: { code: "fixture", detail: "Unavailable", retryable: true } }); } });
    const automatic = new AssessmentAutoCheck(() => { void controller.run(); return true; }, time.timers);
    automatic.queue("input", true); const queued = [...time.pending.values()][0].callback;
    automatic.manual("input"); await controller.run(); queued(); time.advance(5000);
    automatic.queue("input", false); automatic.queue("input", true); time.advance(5000);
    assert.equal(starts, 1, `${failure} requires an explicit retry, even after readiness toggles`);
    await controller.run(); assert.equal(starts, 2);
  }
});

test("returning to a successful selection after an edit automatically retrieves its cached assessment again", async () => {
  const time = clock(), running: Promise<void>[] = [];
  const { controller, calls } = harness();
  const automatic = new AssessmentAutoCheck(() => { running.push(controller.run()); return true; }, time.timers);
  const first = assessmentBinding(draft, composer);
  automatic.queue(first, true); time.advance(1500); await Promise.all(running);
  assert.equal(controller.state.phase, "complete");
  const other = { ...composer, context: "another selection" };
  controller.bind(draft, other); automatic.queue(assessmentBinding(draft, other), true); time.advance(100);
  controller.bind(draft, composer); automatic.queue(first, true); time.advance(1500); await Promise.all(running);
  assert.equal(calls.length, 2); assert.equal(controller.state.phase, "complete");
  assert.deepEqual(calls[0].context, calls[1].context, "The server can reuse the exact original cached inputs");
});

test("the latest-input guard prevents a timer firing against a render that is no longer ready", () => {
  const time = clock(), calls: string[] = []; let ready = true;
  const automatic = new AssessmentAutoCheck(binding => { if (!ready) return false; calls.push(binding); return true; }, time.timers);
  automatic.queue("input", true); ready = false; time.advance(1500); assert.equal(calls.length, 0);
  ready = true; automatic.queue("input", true); time.advance(1500); assert.deepEqual(calls, ["input"]);
});

test("binding and server rendering never initiate a model request or save the draft", async () => {
  const { controller, calls } = harness();
  controller.bind(draft, { ...composer, context: "changed, still unsaved" });
  assert.equal(calls.length, 0);
  const html = renderToStaticMarkup(React.createElement(Assessment, { draft, composer, disabled: false, children: value => React.createElement("button", { type: "button" }, value ? "scored" : "Launch research") }));
  assert.doesNotMatch(html, /Check CFDE support|Check again|TypeSafe/);
  assert.equal((html.match(/<button/g) || []).length, 1, "Only the research launch control is rendered before the automatic check");
  assert.match(html, /Launch research/);
  assert.doesNotMatch(html, /cfde-estimate-ring|Jev estimate ·/);
  assert.equal(calls.length, 0);
});

test("POST snapshots all unsaved inputs and reuses the original key after an ambiguous failure", async () => {
  const calls: { key: string; value: unknown }[] = [];
  const { controller } = harness({ start: async (_draft, value, key) => { calls.push({ key, value }); if (calls.length === 1) throw new TypeError("network lost"); return resource(); } });
  await controller.run();
  assert.equal(controller.state.phase, "error");
  await controller.run();
  assert.deepEqual(calls.map(value => value.key), ["key-1", "key-1"]);
  assert.deepEqual(calls[1].value, composer);
  assert.equal(controller.state.phase, "complete");
});

test("double clicks share one request and a completed explicit check gets a new key", async () => {
  const start = deferred<CfdeAssessment>(); let calls = 0;
  const { controller } = harness({ start: async () => { calls++; return start.promise; } });
  const first = controller.run(); await controller.run();
  assert.equal(calls, 1); start.resolve(resource()); await first;
  const other = harness(); await other.controller.run(); await other.controller.run();
  assert.deepEqual(other.calls.map(value => value.key), ["key-1", "key-2"]);
});

test("a polling failure resumes GET only and never posts another paid request", async () => {
  let starts = 0, gets = 0;
  const { controller } = harness({ start: async () => { starts++; return pending(); }, get: async () => { if (++gets === 1) throw new TypeError("offline"); return resource(); } });
  await controller.run();
  assert.equal(controller.state.phase, "error");
  assert.match(render(controller.state), />Check status<\/button>/);
  await controller.run();
  assert.equal(starts, 1); assert.equal(gets, 2); assert.equal(controller.state.phase, "complete");
});

test("polling stops after the bounded window and status can be resumed without another POST", async () => {
  let starts = 0, gets = 0;
  const { controller } = harness({ pollingMs: 2500, start: async () => { starts++; return pending(); }, get: async () => { gets++; return pending(); } });
  await controller.run();
  assert.equal(controller.state.phase, "waiting"); assert.equal(gets, 2);
  assert.equal(render(controller.state), "", "A still-running check remains visually silent");
  await controller.run(); assert.equal(starts, 1);
});

test("status reads long-poll and the next read follows at once after a wait or a status change", async () => {
  const waits: number[] = [], sleeps: number[] = []; let now = 0;
  const answers = [{ took: 15_000, status: "preparing" }, { took: 400, status: "assessing" }, { took: 9_000, status: "succeeded" }] as const;
  const { controller } = harness({ now: () => now, wait: async ms => { sleeps.push(ms); now += ms; }, start: async () => pending(),
    get: async (_draft, _id, _signal, wait) => {
      waits.push(wait ?? 0); const answer = answers[waits.length - 1]; now += answer.took;
      return answer.status === "succeeded" ? resource() : resource({ status: answer.status, result: null, coverage: null });
    } });
  await controller.run();
  assert.equal(controller.state.phase, "complete");
  assert.deepEqual(waits, [ASSESSMENT_WAIT_SECONDS, ASSESSMENT_WAIT_SECONDS, ASSESSMENT_WAIT_SECONDS]);
  assert.deepEqual(sleeps, [], "A read that waited, or saw a new status, is followed at once");
});

test("an API that answers status reads at once is still read at a backed-off cadence", async () => {
  const sleeps: number[] = []; let now = 0, gets = 0;
  const { controller } = harness({ now: () => now, wait: async ms => { sleeps.push(ms); now += ms; }, start: async () => pending(),
    get: async () => { now += 100; return ++gets < 6 ? pending() : resource(); } });
  await controller.run();
  assert.equal(controller.state.phase, "complete");
  assert.deepEqual(sleeps, [900, 1900, 2900, 3900, 3900], "1, 2, 3 then 4 s between unchanged answers, counting the read itself");
});

test("the long-poll never waits past the polling bound and a resumed check reads its status at once", async () => {
  const waits: number[] = []; let now = 0;
  const { controller } = harness({ pollingMs: 20_000, now: () => now, wait: async ms => { now += ms; }, start: async () => pending(),
    get: async (_draft, _id, _signal, wait) => { waits.push(wait ?? 0); now += (wait ?? 0) * 1000; return pending(); } });
  await controller.run();
  assert.equal(controller.state.phase, "waiting");
  assert.deepEqual(waits, [15, 5], "The last read waits only for the time left");
  await controller.run();
  assert.equal(waits[2], 0, "Check status answers immediately, then long-polls");
  assert.equal(waits[3], 15);
});

test("the assessed subject alone binds a check: editor-only fields never start, cancel or hide one", async () => {
  const anchored = { ...composer, context: "Anchored context", eaggl_anchors: [{ reference: { source_id: "factor:fixture" }, origin: "automatic", suggestion_id: "s1" }], mechanism_subquery: "", dismissed_source_ids: [] as string[] };
  const binding = assessmentBinding(draft, anchored);
  for (const edit of [{ mechanism_subquery: "insulin" }, { dismissed_source_ids: ["factor:other"] }, { eaggl_anchors: [{ reference: { source_id: "factor:fixture" }, origin: "manual", suggestion_id: null }] }])
    assert.equal(assessmentBinding(draft, { ...anchored, ...edit }), binding, JSON.stringify(edit));
  for (const edit of [{ context: "new" }, { research_direction: "new" }, { hypotheses: "new" }, { model: "other-model" }, { selected_kgs: ["prokn"] }, { upload_ids: ["upload"] },
    { source_gap: { id: "gap:other" } }, { eaggl_anchors: [{ reference: { source_id: "factor:other" }, origin: "automatic", suggestion_id: "s1" }] }])
    assert.notEqual(assessmentBinding(draft, { ...anchored, ...edit }), binding, JSON.stringify(edit));
  assert.notEqual(assessmentBinding({ ...draft, version: 4 }, anchored), binding);
  const time = clock(), calls: string[] = [];
  const automatic = new AssessmentAutoCheck(value => { calls.push(value); return true; }, time.timers);
  automatic.queue(binding, true); time.advance(1500);
  automatic.queue(assessmentBinding(draft, { ...anchored, mechanism_subquery: "insulin" }), true); time.advance(5000);
  assert.equal(calls.length, 1, "Typing a mechanism search does not post another check");
  const old = deferred<CfdeAssessment>(); let signal: AbortSignal | undefined; const posted: unknown[] = [];
  const { controller } = harness({ start: async (_draft, value, _key, requestSignal) => { posted.push(value); signal = requestSignal; return old.promise; } });
  controller.bind(draft, anchored); const running = controller.run();
  controller.bind(draft, { ...anchored, mechanism_subquery: "insulin", dismissed_source_ids: ["factor:other"] });
  assert.equal(signal?.aborted, false);
  old.resolve(resource()); await running;
  assert.equal(controller.state.phase, "complete");
  assert.equal(assessmentResult(visibleAssessment(controller.state, assessmentBinding(draft, { ...anchored, mechanism_subquery: "insulin" })))?.id, "assessment-fixture");
  assert.deepEqual(posted, [anchored], "The check keeps the inputs it posted, so a retry replays its request exactly");
});

test("failed and interrupted operations do not automatically retry; an explicit retry has a new key", async () => {
  for (const status of ["failed", "interrupted"] as const) {
    const keys: string[] = [];
    const { controller } = harness({ start: async (_draft, _value, key) => { keys.push(key); return resource({ status, result: null, error: { code: "fixture", detail: "Provider unavailable", retryable: true } }); } });
    await controller.run(); assert.deepEqual(keys, ["key-1"]); assert.equal(assessmentResult(controller.state), null);
    await controller.run(); assert.deepEqual(keys, ["key-1", "key-2"]);
  }
});

test("editing any input aborts and discards an old response even if its transport ignores abort", async () => {
  const old = deferred<CfdeAssessment>(); let signal: AbortSignal | undefined;
  const { controller } = harness({ start: async (_draft, _value, _key, requestSignal) => { signal = requestSignal; return old.promise; } });
  const running = controller.run();
  controller.bind(draft, { ...composer, context: "new context" });
  assert.equal(signal?.aborted, true);
  old.resolve(resource()); await running;
  assert.equal(controller.state.phase, "stale"); assert.equal(controller.state.resource, null);
  assert.doesNotMatch(render(controller.state), /56%|cfde-estimate-ring/);
});

test("late polling completion cannot overwrite a newer assessment", async () => {
  const old = deferred<CfdeAssessment>(); let gets = 0;
  const { controller } = harness({ start: async (_draft, value) => (value as typeof composer).context === composer.context ? pending() : resource({ id: "new-assessment" }), get: async () => { gets++; return old.promise; } });
  const first = controller.run();
  while (!gets) await Promise.resolve();
  controller.bind(draft, { ...composer, context: "new context" }); await controller.run();
  old.resolve(resource()); await first;
  assert.equal(controller.state.resource?.id, "new-assessment");
});

test("render fence removes a score before effects run, including version and anchor changes", () => {
  const state: AssessmentState = { binding: assessmentBinding(draft, composer), phase: "complete", resource: resource(), message: "" };
  for (const binding of [assessmentBinding({ ...draft, version: 4 }, composer), assessmentBinding(draft, { ...composer, eaggl_anchors: [] }), assessmentBinding(draft, { ...composer, context: "edited" })]) {
    assert.equal(assessmentResult(visibleAssessment(state, binding)), null);
    assert.equal(render(visibleAssessment(state, binding)), "", "Normal edits clear feedback while the automatic check settles");
  }
  assert.equal(assessmentBinding(draft, composer), assessmentBinding({ version: 3, id: draft.id }, { context: composer.context, eaggl_anchors: composer.eaggl_anchors, source_gap: composer.source_gap }));
});

test("server-stale results and mismatched identities never become an estimate", async () => {
  for (const value of [resource({ stale: true }), resource({ draft_id: "other-owner-draft" }), resource({ draft_version: 1 })]) {
    const { controller } = harness({ start: async () => value }); await controller.run();
    assert.equal(assessmentResult(controller.state), null);
    assert.doesNotMatch(render(controller.state), /Jev estimate · 56%/);
  }
});

test("invalid probabilities and missing results cannot display a fabricated zero or NaN score", () => {
  for (const value of [NaN, Infinity, -1, 1.1]) assert.throws(() => readAssessment(resource({ result: { ...resource().result!, probability_yes: value } })), AssessmentRequestError);
  assert.throws(() => readAssessment(resource({ result: null })), AssessmentRequestError);
  assert.equal(readAssessment(resource({ result: { ...resource().result!, probability_no: 0.43 } })).result?.probability_no, 0.43, "Rounded provider likelihoods are preserved rather than normalized");
  assert.equal(readAssessment(resource({ result: { ...resource().result!, probability_yes: 0, probability_no: 1, verdict: "no" } })).result?.probability_yes, 0);
});

test("success displays only the support sentence while the ring retains the estimate internally", () => {
  const state: AssessmentState = { ...idleAssessment(), phase: "complete", resource: resource() };
  const html = render(state);
  assert.match(html, /CFDE support uncertain/);
  assert.equal(html.replace(/<[^>]+>/g, ""), "CFDE support uncertain");
  assert.doesNotMatch(html, /%|Jev|confidence|coverage|details|button|GeneSets/);
  assert.equal(assessmentSupportLabel(0.8), "Likely CFDE support"); assert.equal(assessmentSupportLabel(0.2), "CFDE support unlikely");
  assert.doesNotMatch(html, /no CFDE data|no data exists|91% Yes/);
  const ring = renderToStaticMarkup(React.createElement(CfdeEstimateRing, { probability: 0.56 }));
  assert.ok(Math.abs(Number(ring.match(/stroke-dasharray="([^ ]+) 100"/)?.[1]) - 56) < 0.000001); assert.match(ring, /aria-hidden="true"/);
});

test("active checks show one subtle live label with no percentage, extra details or manual check", () => {
  assert.equal(render(idleAssessment()), "");
  assert.equal(render({ ...idleAssessment(), phase: "stale" }), "");
  const success = render({ ...idleAssessment(), phase: "complete", resource: resource() });
  assert.match(success, /CFDE support uncertain/);
  assert.doesNotMatch(success, /<button|details|%|Jev|Check again|Check CFDE support|TypeSafe/);
  for (const state of [{ ...idleAssessment(), phase: "starting" as const }, { ...idleAssessment(), phase: "polling" as const, resource: pending() }, { ...idleAssessment(), phase: "polling" as const, resource: resource({ status: "assessing", result: null }) }]) {
    const html = render(state);
    assert.equal(html.replace(/<[^>]+>/g, ""), "Assessing likely CFDE support");
    assert.match(html, /role="status" aria-live="polite"/);
    assert.doesNotMatch(html, /<button|details|%|Jev|TypeSafe/);
  }
  for (const message of ["Service unavailable", "These inputs exceed the assessment size limit"]) {
    const html = render({ ...idleAssessment(), phase: "error", message }); assert.match(html, /Retry support check/); assert.doesNotMatch(html, /CFDE support unlikely|0% Yes/);
  }
  assert.equal(render({ ...idleAssessment(), phase: "stale", resource: resource({ stale: true }) }), "");
  assert.equal(render({ ...idleAssessment(), phase: "waiting", resource: pending() }), "");
});

test("the loading ring is decorative, indeterminate, and stops animating for reduced motion", () => {
  const html = renderToStaticMarkup(React.createElement(CfdeEstimateRing, { loading: true }));
  assert.match(html, /class="cfde-estimate-ring is-assessing"/);
  assert.match(html, /stroke-dasharray="24 100"/);
  assert.match(html, /aria-hidden="true"/); assert.match(html, /focusable="false"/);
  assert.doesNotMatch(html, /role="progressbar"|aria-valuenow|%/);
  const completed = renderToStaticMarkup(React.createElement(CfdeEstimateRing, { probability: 0.8 }));
  assert.doesNotMatch(completed, /is-assessing/);
  const css = readFileSync(new URL("../src/components/cfde-assessment.css", import.meta.url), "utf8");
  assert.match(css, /\.cfde-estimate-ring\.is-assessing\{animation:cfde-assessing-spin /);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{\.cfde-estimate-ring\.is-assessing\{animation:none\}\}/);
});

test("wire requests stay on the same-origin gateway, send exact inputs and have independent GET polling", async () => {
  const old = globalThis.fetch, calls: { url: string; init: RequestInit }[] = [];
  globalThis.fetch = async (url, init) => { calls.push({ url: String(url), init: init! }); return new Response(JSON.stringify(resource()), { status: 202 }); };
  try {
    const signal = new AbortController().signal;
    await assessmentApi.start(draft, composer, "request-fixture", signal);
    await assessmentApi.get(draft.id, "assessment-fixture", signal);
    await assessmentApi.get(draft.id, "assessment-fixture", signal, ASSESSMENT_WAIT_SECONDS);
    assert.equal(calls[0].url, "/api/backend/v1/drafts/isolated-draft/cfde-assessments");
    assert.equal(calls[1].url, "/api/backend/v1/drafts/isolated-draft/cfde-assessments/assessment-fixture");
    assert.equal(calls[2].url, "/api/backend/v1/drafts/isolated-draft/cfde-assessments/assessment-fixture?wait=15");
    assert.deepEqual(JSON.parse(calls[0].init.body as string), { draft_version: 3, composer });
    assert.equal(new Headers(calls[0].init.headers).get("Idempotency-Key"), "request-fixture");
    assert.equal(calls[0].init.credentials, "same-origin"); assert.equal(calls[0].init.cache, "no-store");
    assert.equal(calls[1].init.method, "GET"); assert.equal(calls[1].init.body, undefined);
    assert.equal(new Headers(calls[1].init.headers).has("Idempotency-Key"), false);
  } finally { globalThis.fetch = old; }
});
