import createClient from "openapi-fetch";
import type { paths, components } from "./api.generated";
import { withRequestDeadline } from "./request-deadline";
import { changesWorkspace, invalidateWorkspace } from "./workspace-events";
export type Schema<K extends keyof components["schemas"]> = components["schemas"][K];
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
const client = createClient<paths>({ baseUrl: "/api/backend", credentials: "same-origin" });
client.use({ onResponse({ request, response }) {
  if (response.ok && changesWorkspace(request.method, new URL(request.url).pathname)) invalidateWorkspace();
  return response;
} });
const listSignal = (signal: AbortSignal, caller?: AbortSignal) => caller ? AbortSignal.any([signal, caller]) : signal;
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok) {
    const e = result.error as { code?: string; detail?: string; title?: string } | undefined;
    throw new ApiError(result.response.status, e?.code || "API_ERROR", e?.detail || e?.title || `Request failed (${result.response.status})`);
  }
  return result.data as T;
}
export const keyHeaders = (key = crypto.randomUUID()) => ({ "Idempotency-Key": key });
export const api = {
  me: () => withRequestDeadline(async signal => unwrap(await client.GET("/v1/me", { signal }))),
  leaderboard: (query: { view: Schema<"LeaderboardView">; sort: Schema<"LeaderboardSort">; evidence: Schema<"LeaderboardEvidence">; limit?: number; cursor?: string }, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/leaderboard", { params: { query }, signal: listSignal(signal, caller), cache: "no-store" })), "The leaderboard is taking longer than expected to load. Please retry."),
  leaderboardRecords: (view: Schema<"LeaderboardView">, id: string, query: { metric: Schema<"LeaderboardMetric">; evidence: Schema<"LeaderboardEvidence">; limit?: number; cursor?: string }, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/leaderboard/{view}/{entry_id}/records", { params: { path: { view, entry_id: id }, query }, signal: listSignal(signal, caller), cache: "no-store" })), "The contributing records are taking longer than expected to load. Please retry."),
  gaps: (cursor?: string, scope: "public" | "workspace" = "public", callerSignal?: AbortSignal, sort: "accounts" | "votes" = "accounts") => withRequestDeadline(async signal => unwrap(await client.GET("/v1/knowledge-gaps", { params: { query: { limit: 20, cursor, scope, sort } }, signal: callerSignal ? AbortSignal.any([signal, callerSignal]) : signal })), "The knowledge gaps are taking longer than expected to load. Please retry."),
  searchGaps: async (q: string, signal?: AbortSignal, scope: "public" | "workspace" = "public") => unwrap(await client.GET("/v1/knowledge-gaps/search", { params: { query: { q, mode: "fuzzy", limit: 20, scope } }, signal })),
  gap: (gap_id: string, callerSignal?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/knowledge-gaps/{gap_id}", { params: { path: { gap_id } }, signal: listSignal(signal, callerSignal) }))),
  gapVote: (gap_id: string, callerSignal?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/knowledge-gaps/{gap_id}/vote", { params: { path: { gap_id } }, signal: listSignal(signal, callerSignal) }))),
  accountVote: (account_id: string, callerSignal?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts/{account_id}/vote", { params: { path: { account_id } }, signal: listSignal(signal, callerSignal) }))),
  setGapVote: (gap_id: string, vote: -1 | 0 | 1, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/knowledge-gaps/{gap_id}/vote", { params: { path: { gap_id }, header: keyHeaders(key) }, body: { vote }, signal }))),
  setAccountVote: (account_id: string, vote: -1 | 0 | 1, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/accounts/{account_id}/vote", { params: { path: { account_id }, header: keyHeaders(key) }, body: { vote }, signal }))),
  gapAccounts: (gap_id: string, source_revision: string, cursor?: string, callerSignal?: AbortSignal, scope: "public" | "workspace" = "public") => withRequestDeadline(async signal => unwrap(await client.GET("/v1/knowledge-gaps/{gap_id}/accounts", { params: { path: { gap_id }, query: { source_revision, limit: 20, cursor, scope } }, signal: callerSignal ? AbortSignal.any([signal, callerSignal]) : signal })), "The scientific accounts are taking longer than expected to load. Please retry."),
  mechanisms: async (q: string, signal?: AbortSignal, mode: "lexical" | "hybrid" = "hybrid") => unwrap(await client.GET("/v1/mechanisms/search", { params: { query: { q, source: "eaggl", model: "cfde-inc-v2", limit: 20, mode } }, signal })),
  mechanism: async (source_id: string, source_revision: string) => unwrap(await client.GET("/v1/mechanisms/{source_id}", { params: { path: { source_id }, query: { source_revision } } })),
  suggest: async (body: Schema<"SuggestInput">, signal?: AbortSignal) => unwrap(await client.POST("/v1/mechanisms/suggest", { body, signal })),
  drafts: (cursor?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/drafts", { params: { query: { cursor, limit: 100 } }, signal: listSignal(signal, caller) }))),
  draft: (draft_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/drafts/{draft_id}", { params: { path: { draft_id } }, signal }))),
  createDraft: (composer: Schema<"Composer">, key: string, name?: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/drafts", { body: { composer, ...(name ? { name } : {}) }, params: { header: keyHeaders(key) }, signal }))),
  createWorkingDraft: (composer: Schema<"Composer">, key: string, source_draft_id?: string, source_draft_version?: number) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/drafts", { body: { composer, lifecycle: "temporary", ...(source_draft_id ? { source_draft_id, ...(source_draft_version ? { source_draft_version } : {}) } : {}) }, params: { header: keyHeaders(key) }, signal }))),
  renameDraft: (draft: Schema<"Draft">, name: string, key: string) => withRequestDeadline(async signal => unwrap(await client.PATCH("/v1/drafts/{draft_id}", { params: { path: { draft_id: draft.id }, header: keyHeaders(key) }, body: { expected_version: draft.version, name }, signal }))),
  deleteDraft: (draft: Schema<"Draft">, key: string) => withRequestDeadline(async signal => unwrap(await client.DELETE("/v1/drafts/{draft_id}", { params: { path: { draft_id: draft.id }, header: keyHeaders(key) }, body: { expected_version: draft.version }, signal }))),
  saveDraft: (draft: Schema<"Draft">, composer: Schema<"Composer">, key: string, name?: string) => withRequestDeadline(async signal => unwrap(await client.PATCH("/v1/drafts/{draft_id}", { params: { path: { draft_id: draft.id }, header: keyHeaders(key) }, body: { expected_version: draft.version, composer, lifecycle: "saved", ...(name ? { name } : {}) }, headers: keyHeaders(key), signal }))),
  job: (job_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/jobs/{job_id}", { params: { path: { job_id } }, signal })), "The job status is taking longer than expected to load. Please retry."),
  requests: (cursor?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/research-requests", { params: { query: { cursor, limit: 100 } }, signal: listSignal(signal, caller) }))),
  request: (request_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/research-requests/{request_id}", { params: { path: { request_id } }, signal }))),
  jobs: (cursor?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/jobs", { params: { query: { cursor, limit: 100 } }, signal: listSignal(signal, caller) }))),
  submit: (body: Schema<"JobCreate">, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/jobs", { body, params: { header: keyHeaders(key) }, signal })), "We haven’t received confirmation yet. Retry to check this submission; it won’t create a second job."),
  cancel: async (job_id: string) => unwrap(await client.POST("/v1/jobs/{job_id}/cancel", { params: { path: { job_id } } })),
  retryReview: (job_id: string, expected_last_event_id: string, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/jobs/{job_id}/retry-review", { body: { expected_last_event_id }, params: { path: { job_id }, header: keyHeaders(key) }, signal })), "Review retry has not been confirmed yet. Retry to check the same request; it will not start a second review."),
  account: (dapper_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts/{dapper_id}", { params: { path: { dapper_id } }, signal })), "The account is taking longer than expected to load. Please retry."),
  accountPage: (dapper_id: string, cursor: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts/{dapper_id}", { params: { path: { dapper_id }, query: { cursor } }, signal: listSignal(signal, caller) }))),
  publication: (dapper_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts/{dapper_id}/publication", { params: { path: { dapper_id } }, signal }))),
  setPublication: (dapper_id: string, body: Schema<"PublicationInput">, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/accounts/{dapper_id}/publication", { params: { path: { dapper_id }, header: keyHeaders(key) }, body, signal })), "Publication could not be confirmed yet. Retry to check the same change."),
  analysisOutcome: (outcome_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/analysis-outcomes/{outcome_id}", { params: { path: { outcome_id } }, signal }))),
  jobOutcome: (job_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/jobs/{job_id}/outcome", { params: { path: { job_id } }, signal }))),
  gapOutcomes: (gap_id: string, source_revision: string, scope: "public" | "workspace", cursor?: string, callerSignal?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/knowledge-gaps/{gap_id}/outcomes", { params: { path: { gap_id }, query: { source_revision, scope, limit: 20, cursor } }, signal: callerSignal ? AbortSignal.any([signal, callerSignal]) : signal }))),
  outcomePublication: (outcome_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/analysis-outcomes/{outcome_id}/publication", { params: { path: { outcome_id } }, signal }))),
  setOutcomePublication: (outcome_id: string, body: Schema<"PublicationInput">, key: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/analysis-outcomes/{outcome_id}/publication", { params: { path: { outcome_id }, header: keyHeaders(key) }, body, signal })), "Publication could not be confirmed yet. Retry to check the same change."),
  claim: (dapper_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/claims/{dapper_id}", { params: { path: { dapper_id } }, signal })), "The claim is taking longer than expected to load. Please retry."),
  paragraph: (dapper_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/paragraphs/{dapper_id}", { params: { path: { dapper_id } }, signal })), "The research statement is taking longer than expected to load. Please retry."),
  object: (dapper_id: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/objects/{dapper_id}", { params: { path: { dapper_id } }, signal })), "The record is taking longer than expected to load. Please retry."),
  accounts: (cursor?: string, caller?: AbortSignal, q?: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts", { params: { query: { cursor, q } }, signal: listSignal(signal, caller) }))),
  publicAccounts: (cursor?: string, caller?: AbortSignal, q?: string, sort: "votes" | "recent" = "votes") => withRequestDeadline(async signal => unwrap(await client.GET("/v1/accounts", { params: { query: { cursor, q, scope: "public", sort, limit: 20 } }, signal: listSignal(signal, caller) })), "The published scientific accounts are taking longer than expected to load. Please retry."),
  outcomes: (cursor?: string, caller?: AbortSignal, q?: string) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/analysis-outcomes", { params: { query: { cursor, q } }, signal: listSignal(signal, caller) }))),
  explorations: (cursor?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/me/explorations", { params: { query: { cursor } }, signal: listSignal(signal, caller) }))),
  explore: async (body: Schema<"ExplorationInput">) => unwrap(await client.POST("/v1/me/explorations", { body, params: { header: keyHeaders() } })),
  render: (paragraph_id: string) => withRequestDeadline(async signal => unwrap(await client.POST("/v1/citations/render", { body: { paragraph_id, style: "apa", locale: "en-US" }, signal })), "The statement’s references are taking longer than expected to load. Please retry."),
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
