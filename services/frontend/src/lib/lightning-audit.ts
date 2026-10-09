/** Private, single-completion audits. Reading an audit never starts or retries inference. */
import { invalidateWorkspace, type WorkspaceEvent } from "./workspace-events";

export const lightningEnabled = process.env.NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED === "true";
export type ResearchMode = "online" | "local" | "lightning";
export type LightningContinuation = { mode: "online" | "local"; id: string; research_request_id: string; created_at: string };
export type LightningAudit = {
  id: string; kind: "lightning_audit"; status: "preparing" | "assessing" | "succeeded" | "failed" | "interrupted";
  research_request_id: string; source_draft_id: string; source_draft_version: number;
  question: { id: string; text: string }; created_at: string; updated_at: string; completed_at: string | null;
  continuation_expires_at: string; reference_generation_id: string;
  result: null | {
    assessment: "promising" | "partial" | "unsupported"; summary: string;
    observations: { text: string; evidence_refs: string[] }[]; recommended_direction: string;
    missing_evidence: string[]; next_steps: string[]; limitations: string[];
  };
  coverage: Record<string, unknown> | null;
  evidence_references: { id: string; pointer: string; label: string; value: unknown; source: Record<string, unknown> }[];
  provenance: { model: string; prompt_version: string; source_state_sha256?: string; request_sha256?: string; response_sha256?: string };
  usage: { input_tokens: number; output_tokens: number } | null;
  error: { code: string; detail: string; retryable: boolean } | null;
  continuations: LightningContinuation[];
};
export type LightningAuditList = { items: LightningAudit[]; page: { next_cursor: string | null; has_more: boolean } };
export const lightningAuditHref = (id: string) => `/lightning-audits/${encodeURIComponent(id)}`;
export const lightningContinuationHref = (run: LightningContinuation) => `/${run.mode === "local" ? "local-runs" : "runs"}/${encodeURIComponent(run.id)}`;
export const lightningStatusLabel: Record<LightningAudit["status"], string> = {
  preparing: "Preparing evidence", assessing: "Assessing evidence", succeeded: "Audit complete", failed: "Audit failed", interrupted: "Audit interrupted",
};
export const lightningAssessmentLabel = { promising: "Promising direction", partial: "Limited or partial direction", unsupported: "No supported direction in this package" };
export const lightningPending = (audit: LightningAudit) => audit.status === "preparing" || audit.status === "assessing";
export const lightningBrief = (audit: LightningAudit) => audit.result
  ? [audit.result.recommended_direction, audit.result.next_steps.length ? `Next steps:\n${audit.result.next_steps.map(step => `- ${step}`).join("\n")}` : ""].filter(Boolean).join("\n\n").slice(0, 6000) : "";

export class LightningError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export const lightningAccessLost = (error: unknown) => error instanceof LightningError && [401, 403, 404].includes(error.status);
/** These admission errors precede creation; transport/server failures may hide a committed run. */
export const lightningDispatchRejected = (error: unknown) => error instanceof LightningError && (
  [400, 422, 429].includes(error.status)
  || ["AUDIT_NOT_COMPLETE", "AUDIT_CONTINUATION_EXPIRED", "SOURCE_UNAVAILABLE", "AUDIT_CONTEXT_UNAVAILABLE",
    "LIGHTNING_DISABLED", "LIGHTNING_UNAVAILABLE", "SOURCE_NOT_READY", "VERSION_CONFLICT", "REFERENCE_RELOAD_IN_PROGRESS", "MCP_NOT_CONFIGURED"].includes(error.code));
export function readLightningAudit(value: unknown): LightningAudit {
  const audit = value as LightningAudit | null;
  if (!audit || typeof audit.id !== "string" || audit.kind !== "lightning_audit" || !Object.hasOwn(lightningStatusLabel, audit.status)
    || !audit.question || typeof audit.question.text !== "string" || typeof audit.question.id !== "string"
    || !audit.provenance || typeof audit.provenance.model !== "string" || typeof audit.provenance.prompt_version !== "string"
    || !Array.isArray(audit.continuations) || !audit.continuations.every(run => ["online", "local"].includes(run.mode) && typeof run.id === "string" && typeof run.created_at === "string")
    || !Array.isArray(audit.evidence_references) || !audit.evidence_references.every(ref => ref && typeof ref.id === "string" && typeof ref.pointer === "string" && typeof ref.label === "string" && ref.source && typeof ref.source === "object")
    || (audit.usage !== null && (!audit.usage || !Number.isInteger(audit.usage.input_tokens) || !Number.isInteger(audit.usage.output_tokens))))
    throw new LightningError(502, "INVALID_AUDIT_RESPONSE", "The audit response was incomplete. Refresh to check its saved status.");
  if (audit.status === "succeeded") {
    const result = audit.result;
    if (!result || !Object.hasOwn(lightningAssessmentLabel, result.assessment) || typeof result.summary !== "string" || typeof result.recommended_direction !== "string"
      || !Array.isArray(result.observations) || ![result.missing_evidence, result.next_steps, result.limitations].every(items => Array.isArray(items) && items.every(item => typeof item === "string")))
      throw new LightningError(502, "INVALID_AUDIT_RESPONSE", "The completed audit response was incomplete. Refresh to retrieve it again.");
    const refs = new Set(audit.evidence_references.map(ref => ref.id));
    if (!result.observations.every(item => typeof item.text === "string" && Array.isArray(item.evidence_refs) && item.evidence_refs.every(ref => refs.has(ref))))
      throw new LightningError(502, "INVALID_AUDIT_RESPONSE", "The audit's evidence references could not be verified. Refresh to retrieve it again.");
  } else if (audit.result !== null) throw new LightningError(502, "INVALID_AUDIT_RESPONSE", "This audit has not produced a complete assessment.");
  return audit;
}

function changed(id: string, continuation = false) {
  // An invalidation contains no evidence or private audit text. The durable stream follows it.
  invalidateWorkspace({ schema_version: 1, event_id: "", cursor: "0", scope: "workspace", event_type: "audit.changed", entity_id: id,
    entity_revision: 0, operation: "upsert", committed_at: new Date().toISOString(), collections: continuation ? ["audits", "jobs", "requests"] : ["audits", "requests"] } as WorkspaceEvent, true);
}
export function createLightningClient(fetcher: typeof fetch = (...args) => fetch(...args)) {
  const base = "/api/backend/v1/lightning-audits";
  async function request<T>(path: string, options: { body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
    const response = await fetcher(path, { method: options.body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json", "Idempotency-Key": options.key! } : {}) },
      ...(options.body ? { body: JSON.stringify(options.body) } : {}), signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(30_000)]) : AbortSignal.timeout(30_000) });
    if (!response.ok) {
      const problem = await response.json().catch(() => null);
      throw new LightningError(response.status, problem?.code || "AUDIT_UNAVAILABLE", problem?.detail || `The audit request could not be completed (${response.status}).`);
    }
    return response.json();
  }
  return {
    create: async (body: { draft_id: string; draft_version: number }, key: string) => { const audit = readLightningAudit(await request(base, { body, key })); changed(audit.id); return audit; },
    get: async (id: string, signal?: AbortSignal, wait = 0) => {
      const audit = readLightningAudit(await request(`${base}/${encodeURIComponent(id)}${wait ? `?wait=${wait}` : ""}`, { signal }));
      if (audit.id !== id) throw new LightningError(502, "AUDIT_IDENTITY_MISMATCH", "The response did not match this audit.");
      return audit;
    },
    list: (cursor?: string, signal?: AbortSignal) => request<LightningAuditList>(base + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""), { signal }),
    continue: async (id: string, body: { mode: "online" | "local"; research_direction: string }, key: string) => {
      const result = await request<LightningContinuation>(`${base}/${encodeURIComponent(id)}/continue`, { body, key });
      if (!result || result.mode !== body.mode || typeof result.id !== "string") throw new LightningError(502, "INVALID_CONTINUATION_RESPONSE", "The run was not confirmed. Check the same submission again.");
      changed(id, true); return result;
    },
  };
}
export const lightningApi = createLightningClient();

/** The exact paid-run dispatch survives refresh; only an acknowledged receipt clears it. */
export type LightningHandoff = { mode: "online" | "local"; research_direction: string; key: string };
const handoffKey = (owner: string, id: string) => `reveal:lightning-handoff:${owner}:${id}`;
export function restoreLightningHandoff(owner: string, id: string): LightningHandoff | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(handoffKey(owner, id)) || "null") as LightningHandoff | null;
    return value && ["online", "local"].includes(value.mode) && typeof value.research_direction === "string" && typeof value.key === "string" && value.key ? value : null;
  } catch { return null; }
}
export function rememberLightningHandoff(owner: string, id: string, value: LightningHandoff | null) {
  try { if (value) sessionStorage.setItem(handoffKey(owner, id), JSON.stringify(value)); else sessionStorage.removeItem(handoffKey(owner, id)); }
  catch { /* The mounted page retains its exact receipt when browser storage is restricted. */ }
}
const briefKey = (owner: string, id: string) => `reveal:lightning-brief:${owner}:${id}`;
export function restoreLightningBrief(owner: string, id: string): string | null {
  try { return sessionStorage.getItem(briefKey(owner, id)); } catch { return null; }
}
export function rememberLightningBrief(owner: string, id: string, value: string) {
  try { sessionStorage.setItem(briefKey(owner, id), value); } catch { /* Editing still works without storage. */ }
}
