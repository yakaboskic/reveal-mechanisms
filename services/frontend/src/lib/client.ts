import createClient from "openapi-fetch";
import type { paths, components } from "./api.generated";
export type Schema<K extends keyof components["schemas"]> = components["schemas"][K];
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
const client = createClient<paths>({ baseUrl: "/api/backend", credentials: "same-origin" });
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok) {
    const e = result.error as { code?: string; detail?: string; title?: string } | undefined;
    throw new ApiError(result.response.status, e?.code || "API_ERROR", e?.detail || e?.title || `Request failed (${result.response.status})`);
  }
  return result.data as T;
}
export const keyHeaders = (key = crypto.randomUUID()) => ({ "Idempotency-Key": key });
export const api = {
  me: async () => unwrap(await client.GET("/v1/me")),
  gaps: async (cursor?: string) => unwrap(await client.GET("/v1/knowledge-gaps", { params: { query: { limit: 10, cursor } } })),
  searchGaps: async (q: string, signal?: AbortSignal) => unwrap(await client.GET("/v1/knowledge-gaps/search", { params: { query: { q, mode: "fuzzy", limit: 20 } }, signal })),
  gap: async (gap_id: string) => unwrap(await client.GET("/v1/knowledge-gaps/{gap_id}", { params: { path: { gap_id } } })),
  mechanisms: async (q: string, signal?: AbortSignal) => unwrap(await client.GET("/v1/mechanisms/search", { params: { query: { q, source: "eaggl", model: "cfde-inc-v2", limit: 20 } }, signal })),
  mechanism: async (source_id: string, source_revision: string) => unwrap(await client.GET("/v1/mechanisms/{source_id}", { params: { path: { source_id }, query: { source_revision } } })),
  suggest: async (body: Schema<"SuggestInput">) => unwrap(await client.POST("/v1/mechanisms/suggest", { body })),
  drafts: async () => unwrap(await client.GET("/v1/drafts")),
  draft: async (draft_id: string) => unwrap(await client.GET("/v1/drafts/{draft_id}", { params: { path: { draft_id } } })),
  createDraft: async (composer: Schema<"Composer">, key: string) => unwrap(await client.POST("/v1/drafts", { body: { composer }, params: { header: keyHeaders(key) } })),
  saveDraft: async (draft: Schema<"Draft">, composer: Schema<"Composer">, key: string) => unwrap(await client.PATCH("/v1/drafts/{draft_id}", { params: { path: { draft_id: draft.id }, header: keyHeaders(key) }, body: { expected_version: draft.version, composer }, headers: keyHeaders(key) })),
  job: async (job_id: string) => unwrap(await client.GET("/v1/jobs/{job_id}", { params: { path: { job_id } } })),
  requests: async () => unwrap(await client.GET("/v1/research-requests")),
  jobs: async () => unwrap(await client.GET("/v1/jobs")),
  submit: async (body: Schema<"JobCreate">, key: string) => unwrap(await client.POST("/v1/jobs", { body, params: { header: keyHeaders(key) } })),
  cancel: async (job_id: string) => unwrap(await client.POST("/v1/jobs/{job_id}/cancel", { params: { path: { job_id } } })),
  account: async (dapper_id: string) => unwrap(await client.GET("/v1/accounts/{dapper_id}", { params: { path: { dapper_id } } })),
  claim: async (dapper_id: string) => unwrap(await client.GET("/v1/claims/{dapper_id}", { params: { path: { dapper_id } } })),
  paragraph: async (dapper_id: string) => unwrap(await client.GET("/v1/paragraphs/{dapper_id}", { params: { path: { dapper_id } } })),
  object: async (dapper_id: string) => unwrap(await client.GET("/v1/objects/{dapper_id}", { params: { path: { dapper_id } } })),
  accounts: async (cursor?: string) => unwrap(await client.GET("/v1/accounts", { params: { query: { cursor } } })),
  explorations: async (cursor?: string) => unwrap(await client.GET("/v1/me/explorations", { params: { query: { cursor } } })),
  explore: async (body: Schema<"ExplorationInput">) => unwrap(await client.POST("/v1/me/explorations", { body, params: { header: keyHeaders() } })),
  render: async (paragraph_id: string) => unwrap(await client.POST("/v1/citations/render", { body: { paragraph_id, style: "apa", locale: "en-US" } })),
  export: async (dapper_id: string, format: Schema<"ParagraphExport">["format"]) => unwrap(await client.GET("/v1/paragraphs/{dapper_id}/export", { params: { path: { dapper_id }, query: { format, style: "apa" } } })),
};
export type ResearchClient = typeof api;
export const terminal = (status: Schema<"Job">["status"]) => ["succeeded", "failed", "insufficient_evidence", "cancelled"].includes(status);
export const messageOf = (error: unknown) => error instanceof Error ? error.message : "The request could not be completed.";

// The parser handles arbitrary network boundaries, multi-line data and SSE heartbeats.
export async function readEvents(response: Response, onEvent: (event: Schema<"JobEvent">) => void) {
  if (!response.ok) {
    const problem = await response.json();
    throw new ApiError(response.status, problem.code, problem.detail || "Activity is unavailable");
  }
  const reader = response.body?.getReader(); if (!reader) throw new Error("No activity stream available");
  const decoder = new TextDecoder(); let pending = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      pending = (pending + decoder.decode(value, { stream: !done })).replace(/\r\n/g, "\n");
      let end: number;
      while ((end = pending.indexOf("\n\n")) >= 0) {
        const block = pending.slice(0, end); pending = pending.slice(end + 2);
        const data = block.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const event = JSON.parse(data) as Schema<"JobEvent">;
        if (typeof event.id !== "string" || !/^\d+$/.test(event.id) || typeof event.job_id !== "string" || typeof event.message !== "string") throw new Error("Malformed activity event");
        onEvent(event);
      }
      if (done) break;
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
