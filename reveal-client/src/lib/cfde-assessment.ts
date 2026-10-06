/** Advisory draft assessment. This client never starts a research job. */
export type AssessmentDraft = { id: string; version: number };
export type AssessmentComposer = { source_gap: unknown; eaggl_anchors: readonly unknown[]; [field: string]: unknown };
export type CfdeAssessment = {
  id: string; draft_id: string; draft_version: number; composer_sha256: string;
  status: "preparing" | "assessing" | "succeeded" | "failed" | "interrupted";
  created_at: string; updated_at: string; expires_at: string;
  model: string; rubric_version: string; reference_generation_id: string | null;
  result: null | {
    verdict: "yes" | "no"; probability_yes: number; probability_no: number; confidence: number;
    main_blocker: string;
    relationship_support: { gene_gene_set: number; gene_mechanism: number; gene_set_mechanism: number };
    calibration: "not_calibrated";
  };
  coverage: null | {
    factor_count: number; gene_loading_count: number; gene_set_loading_count: number; unique_gene_set_count: number;
    missing: string[]; truncations: { source: string; included_chars: number; total_chars: number }[]; complete: boolean;
  };
  error: null | { code: string; detail: string; retryable: boolean };
  stale: boolean;
};
export type AssessmentState = {
  binding: string; phase: "idle" | "starting" | "polling" | "complete" | "stale" | "error" | "waiting";
  resource: CfdeAssessment | null; message: string;
};
export const idleAssessment = (binding = ""): AssessmentState => ({ binding, phase: "idle", resource: null, message: "" });

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return value;
}
export const assessmentBinding = (draft: AssessmentDraft | null, composer: AssessmentComposer) => JSON.stringify(canonical({ draft: draft && { id: draft.id, version: draft.version }, composer }));
export const assessmentReady = (draft: AssessmentDraft | null, composer: AssessmentComposer, disabled: boolean) => !disabled && !!draft && !!composer.source_gap && !!composer.eaggl_anchors.length;
export const assessmentIsRunning = (value: CfdeAssessment) => value.status === "preparing" || value.status === "assessing";
export function visibleAssessment(state: AssessmentState, binding: string): AssessmentState {
  return state.binding === binding ? state : { ...idleAssessment(binding), phase: state.phase === "idle" ? "idle" : "stale" };
}
export function assessmentResult(state: AssessmentState): CfdeAssessment | null {
  return state.phase === "complete" && state.resource?.status === "succeeded" && !state.resource.stale ? state.resource : null;
}
export function assessmentSupportLabel(value: number) {
  // This is a presentation band, not a calibrated cutoff or a claim that data is absent.
  return value > 0.6 ? "Likely CFDE support" : value < 0.4 ? "CFDE support unlikely" : "CFDE support uncertain";
}

type AssessmentTimers = { schedule: (callback: () => void, ms: number) => unknown; cancel: (timer: unknown) => void };
/** One automatic attempt per stable input transition; unchanged failures never retry. */
export class AssessmentAutoCheck {
  private binding: string | null = null;
  private attempted = false;
  private timer: unknown = null;
  private sequence = 0;
  constructor(private readonly start: (binding: string) => boolean, private readonly timers: AssessmentTimers = {
    schedule: (callback, ms) => setTimeout(callback, ms), cancel: timer => clearTimeout(timer as ReturnType<typeof setTimeout>),
  }) {}
  queue(binding: string, ready: boolean) {
    this.cancel();
    if (binding !== this.binding) { this.binding = binding; this.attempted = false; }
    if (!ready || this.attempted) return;
    const sequence = this.sequence;
    this.timer = this.timers.schedule(() => {
      if (sequence !== this.sequence) return;
      this.timer = null;
      if (this.attempted) return;
      this.attempted = true;
      // A render may already have changed readiness before its effect cleanup runs.
      if (!this.start(binding)) this.attempted = false;
    }, 1500);
  }
  manual(binding: string) { this.cancel(); this.binding = binding; this.attempted = true; }
  cancel() { this.sequence++; if (this.timer !== null) this.timers.cancel(this.timer); this.timer = null; }
}

export class AssessmentRequestError extends Error {
  constructor(message: string, public readonly code = "ASSESSMENT_UNAVAILABLE", public readonly status = 0) { super(message); }
}
const fraction = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
export function readAssessment(value: unknown): CfdeAssessment {
  const item = value as CfdeAssessment | null;
  if (!item || typeof item.id !== "string" || typeof item.draft_id !== "string" || !Number.isInteger(item.draft_version)
      || typeof item.composer_sha256 !== "string" || typeof item.stale !== "boolean"
      || !["preparing", "assessing", "succeeded", "failed", "interrupted"].includes(item.status)) throw new AssessmentRequestError("The assessment response was incomplete. Check its status again.", "INVALID_ASSESSMENT_RESPONSE");
  if (item.status === "succeeded" && (!item.result || !["yes", "no"].includes(item.result.verdict)
      || !fraction(item.result.probability_yes) || !fraction(item.result.probability_no) || !fraction(item.result.confidence)
      || typeof item.result.main_blocker !== "string" || !item.result.relationship_support
      || !["gene_gene_set", "gene_mechanism", "gene_set_mechanism"].every(key => fraction(item.result!.relationship_support[key as keyof typeof item.result.relationship_support]))
      || item.result.calibration !== "not_calibrated")) throw new AssessmentRequestError("The assessment returned an invalid estimate. No score is available.", "INVALID_ASSESSMENT_RESPONSE");
  if (item.status !== "succeeded" && item.result !== null) throw new AssessmentRequestError("This assessment has not produced a completed estimate.", "INVALID_ASSESSMENT_RESPONSE");
  if (item.coverage && (!Array.isArray(item.coverage.missing) || !item.coverage.missing.every(value => typeof value === "string")
      || !Array.isArray(item.coverage.truncations) || !item.coverage.truncations.every(value => typeof value.source === "string" && Number.isInteger(value.included_chars) && Number.isInteger(value.total_chars)))) throw new AssessmentRequestError("The assessment coverage was incomplete. No score is available.", "INVALID_ASSESSMENT_RESPONSE");
  return item;
}
async function request(path: string, signal: AbortSignal, body?: { draft_version: number; composer: AssessmentComposer }, key?: string): Promise<CfdeAssessment> {
  const response = await fetch("/api/backend/v1/drafts/" + path, {
    method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
    signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "Idempotency-Key": key! } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new AssessmentRequestError(typeof problem?.detail === "string" ? problem.detail : "CFDE support could not be checked. Your draft can still start research.", typeof problem?.code === "string" ? problem.code : undefined, response.status);
  }
  return readAssessment(await response.json());
}
export const assessmentApi = {
  start: (draft: AssessmentDraft, composer: AssessmentComposer, key: string, signal: AbortSignal) => request(encodeURIComponent(draft.id) + "/cfde-assessments", signal, { draft_version: draft.version, composer }, key),
  get: (draftId: string, id: string, signal: AbortSignal) => request(encodeURIComponent(draftId) + "/cfde-assessments/" + encodeURIComponent(id), signal),
};
function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const cancel = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve(); }, ms);
    signal.addEventListener("abort", cancel, { once: true });
  });
}
type AssessmentDependencies = {
  start: typeof assessmentApi.start; get: typeof assessmentApi.get;
  wait: typeof wait; now: () => number; key: () => string; pollingMs: number;
};
/** Keeps request identity through ambiguous failures and fences late results after an edit. */
export class AssessmentController {
  state = idleAssessment();
  private input: { draft: AssessmentDraft; composer: AssessmentComposer } | null = null;
  private attempt: { key: string; resource: CfdeAssessment | null } | null = null;
  private controller: AbortController | null = null;
  private sequence = 0;
  private deps: AssessmentDependencies;
  constructor(private readonly onChange: (state: AssessmentState) => void, dependencies: Partial<AssessmentDependencies> = {}) {
    this.deps = { ...assessmentApi, wait, now: Date.now, key: () => crypto.randomUUID(), pollingMs: 120_000, ...dependencies };
  }
  private publish(state: AssessmentState) { this.state = state; this.onChange(state); }
  bind(draft: AssessmentDraft | null, composer: AssessmentComposer) {
    const binding = assessmentBinding(draft, composer);
    if (this.state.binding === binding) return;
    this.cancel();
    const stale = this.state.phase !== "idle";
    this.input = draft ? { draft: { id: draft.id, version: draft.version }, composer: structuredClone(composer) } : null;
    this.attempt = null;
    this.publish({ ...idleAssessment(binding), phase: stale ? "stale" : "idle" });
  }
  cancel() { this.sequence++; this.controller?.abort(); this.controller = null; }
  async run() {
    if (!this.input || this.controller) return;
    const { draft, composer } = this.input, binding = this.state.binding;
    if (!this.attempt || (this.attempt.resource && (!assessmentIsRunning(this.attempt.resource) || this.attempt.resource.stale))) this.attempt = { key: this.deps.key(), resource: null };
    const attempt = this.attempt, token = ++this.sequence;
    const controller = new AbortController(); this.controller = controller;
    const current = () => this.sequence === token && !controller.signal.aborted;
    const publish = (phase: AssessmentState["phase"], resource = attempt.resource, message = "") => { if (current()) this.publish({ binding, phase, resource, message }); };
    const accept = (resource: CfdeAssessment) => {
      readAssessment(resource);
      if (resource.draft_id !== draft.id || resource.draft_version !== draft.version
          || (attempt.resource && (resource.id !== attempt.resource.id || resource.composer_sha256 !== attempt.resource.composer_sha256))) throw new AssessmentRequestError("The assessment no longer matches this draft. Edit or reload the draft before checking again.", "ASSESSMENT_IDENTITY_MISMATCH");
      if (current()) attempt.resource = resource;
      return resource;
    };
    publish(attempt.resource ? "polling" : "starting");
    try {
      let resource = accept(attempt.resource
        ? await this.deps.get(draft.id, attempt.resource.id, controller.signal)
        : await this.deps.start(draft, composer, attempt.key, controller.signal));
      if (!current()) return;
      const started = this.deps.now(); let polls = 0;
      while (assessmentIsRunning(resource) && !resource.stale) {
        publish("polling", resource);
        if (this.deps.now() - started >= this.deps.pollingMs) { publish("waiting", resource, "This check is still running. Check its status again; this will not start another estimate."); return; }
        await this.deps.wait(polls++ === 0 ? 1000 : 2000, controller.signal);
        if (!current()) return;
        resource = accept(await this.deps.get(draft.id, resource.id, controller.signal));
        if (!current()) return;
      }
      if (resource.stale) publish("stale", resource, "This estimate is out of date. Check support again for the current draft.");
      else if (resource.status === "succeeded") publish("complete", resource);
      else publish("error", resource, resource.error?.detail || (resource.status === "interrupted" ? "The service restarted before this estimate finished. You can request a new check." : "No estimate is available. You can still start research."));
    } catch (error) {
      if (current()) publish("error", attempt.resource, error instanceof AssessmentRequestError ? error.message : attempt.resource
        ? "The status could not be retrieved. Check status to resume this estimate."
        : "The request could not be confirmed. Retry safely using the same request; this will not create a duplicate estimate.");
    } finally { if (current()) this.controller = null; }
  }
}
