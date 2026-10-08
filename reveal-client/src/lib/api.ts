import type { AnalysisInput, Composer, Draft, Factor, Gap, Job, Me, Page, Schema } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
// Reference reloads (docs/reference-reload.md) replace the EAGGL factors the API serves.
const referenceCopy: Record<string, string> = {
  REFERENCE_GENERATION_SUPERSEDED: "These mechanism anchors come from an outdated EAGGL reference. Select a knowledge gap again to use current factors.",
  REFERENCE_RELOAD_IN_PROGRESS: "EAGGL reference data is being updated. Your selections are kept; please try again in a few minutes.",
};
export async function responseError(response: Response): Promise<ApiError> {
  const value = await response.json().catch(() => null);
  const code = value?.code || "API_UNAVAILABLE";
  return new ApiError(response.status, code, referenceCopy[code] || value?.detail || `Request failed (${response.status}).`);
}
export async function request<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
  const headers = new Headers({ Accept: "application/json" });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.key) headers.set("Idempotency-Key", options.key);
  const timeout = AbortSignal.timeout(30000);
  const response = await fetch(path, { method: options.method || "GET", credentials: "same-origin", cache: "no-store", headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal ? AbortSignal.any([options.signal, timeout]) : timeout });
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const backend = (path: string) => "/api/backend/v1/" + path;
export type CfdeAssessment = {
  id: string;
  draft_id: string;
  draft_version: number;
  status: "preparing" | "assessing" | "succeeded" | "failed" | "interrupted";
  result: { verdict: "yes" | "no" } | null;
  error: { code: string; detail: string; retryable: boolean } | null;
  stale: boolean;
};
export type FactorLoading = { id: string; label: string; loading: number; rank: number; gene_set_id?: string | null; library?: string | null; joint_loading?: number | null; marginal_loading?: number | null };
export type CatalogGeneSet = { id: string; object?: { members?: unknown[] | null } | null };
export type FactorLoadings = { items: FactorLoading[]; total: number; offset: number; limit: number; next_offset: number | null };
export const api = {
  session: () => request<{ principal: Me | null }>("/api/session"),
  connect: () => request<{ principal: Me }>("/api/session", { method: "POST", body: {} }),
  disconnect: () => request<{ principal: null }>("/api/session", { method: "DELETE" }),
  drafts: () => request<Page<Draft>>(backend("drafts?limit=100")),
  draft: (id: string) => request<Draft>(backend("drafts/" + encodeURIComponent(id))),
  jobs: () => request<Page<Job>>(backend("jobs?limit=100")),
  job: (id: string) => request<Job>(backend("jobs/" + encodeURIComponent(id))),
  requests: (signal?: AbortSignal) => request<Page<Schema<"ResearchRequest">>>(backend("research-requests?limit=100"), { signal }),
  gap: (id: string, signal?: AbortSignal) => request<Gap>(backend("knowledge-gaps/" + encodeURIComponent(id)), { signal }),
  factor: (id: string, signal?: AbortSignal) => request<Factor>(backend("mechanisms/" + encodeURIComponent(id)), { signal }),
  factorLoadings: (sourceId: string, kind: "gene" | "gene_set", offset: number, limit: number, signal?: AbortSignal) => {
    const params = new URLSearchParams({ source_id: sourceId, kind, limit: String(limit), offset: String(offset), sort: "loading" });
    if (kind === "gene_set") params.set("metric", "joint");
    return request<FactorLoadings>(backend("factor-loadings?" + params), { signal });
  },
  catalogGeneSet: (id: string, signal?: AbortSignal) => request<CatalogGeneSet>(backend("catalog/gene-sets/" + encodeURIComponent(id)), { signal }),
  factorLoadingsAll: async (sourceId: string, kind: "gene" | "gene_set", signal?: AbortSignal) => {
    const items: FactorLoading[] = [];
    let offset = 0;
    for (;;) {
      const page = await api.factorLoadings(sourceId, kind, offset, 500, signal);
      items.push(...page.items);
      if (page.next_offset == null || items.length >= page.total) return items;
      offset = page.next_offset;
    }
  },
  gaps: async (query: string, signal?: AbortSignal): Promise<Gap[]> => {
    if (!query.trim()) return (await request<Schema<"GapList">>(backend("knowledge-gaps?limit=12"), { signal })).items;
    return (await request<Schema<"GapSearchResults">>(backend("knowledge-gaps/search?mode=fuzzy&limit=12&q=" + encodeURIComponent(query)), { signal })).items.map(hit => hit.gap);
  },
  createCfdeAssessment: (draftId: string, body: { draft_version: number; composer: Composer }, key: string) => request<CfdeAssessment>(backend("drafts/" + encodeURIComponent(draftId) + "/cfde-assessments"), {
    method: "POST", key, body,
  }),
  cfdeAssessment: (draftId: string, assessmentId: string, signal?: AbortSignal) => request<CfdeAssessment>(backend("drafts/" + encodeURIComponent(draftId) + "/cfde-assessments/" + encodeURIComponent(assessmentId)), { signal }),
  suggest: (composer: Composer, signal?: AbortSignal) => request<Schema<"Suggestions">>(backend("mechanisms/suggest"), {
    method: "POST", signal, body: { source_gap: composer.source_gap, manual_eaggl_anchors: [], dismissed_source_ids: [],
      subquery: composer.mechanism_subquery, mode: "semantic", model: composer.model },
  }),
  save: (draft: Draft | null, composer: Composer, name: string, key: string) => request<Draft>(backend("drafts" + (draft ? "/" + encodeURIComponent(draft.id) : "")), {
    method: draft ? "PATCH" : "POST", key, body: { composer, ...(name.trim() ? { name: name.trim() } : {}), ...(draft ? { expected_version: draft.version } : {}) },
  }),
  deleteDraft: (draft: Draft, key: string) => request<{ id: string; deleted: true }>(backend("drafts/" + encodeURIComponent(draft.id)), {
    method: "DELETE", key, body: { expected_version: draft.version },
  }),
  submit: (body: AnalysisInput, key: string) => request<Job>(backend("jobs"), { method: "POST", body, key }),
  cancel: (id: string) => request<Job>(backend("jobs/" + encodeURIComponent(id) + "/cancel"), { method: "POST" }),
  retryReview: (job: Job, key: string) => request<Job>(backend("jobs/" + encodeURIComponent(job.id) + "/retry-review"), {
    method: "POST", key, body: { expected_last_event_id: job.last_event_id },
  }),
};
export function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && ["TimeoutError", "AbortError"].includes(error.name)) return "The request timed out. Its outcome may be unknown; retry the same action to recover its result.";
  return "The API could not be reached. Your selections are still here; retry the same action.";
}
