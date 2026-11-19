import type { Schema } from "./client";

/** deliverable: what an agent job was asked to return ("answer" or "scientific_account"); null for other kinds of
 * job, and absent from backends older than this field. */
export type AdminJob = { id: string; owner: string; kind: string; deliverable?: string | null; status: string; stage: string; created_at: string; started_at: string | null; attempt_started_at: string | null; completed_at: string | null; updated_at: string; wall_seconds: number | null; queue_seconds: number | null; attempt_seconds: number | null; attempt: number; worker: string | null; lease_until: string | null; recoveries: number; delivery_recoveries?: number; handoffs?: number; lease_expired: boolean; box_id: string | null; box_phase: string | null; box_seconds: number | null; setup_seconds: number | null; agent_seconds: number | null; capture_seconds: number | null; cleanup_seconds: number | null; failure_code: string | null } & AdminJobMetrics;

/** Recorded agent cost and failure telemetry; every field is null until the job records metrics. */
export type AdminJobMetrics = { cost_usd?: number | null; estimated_cost_usd?: number | null; cost_source?: "provider" | "estimated" | "unreported" | null; budget_usd?: number | null; budget_used?: number | null; turns?: number | null; tokens?: { input: number | null; output: number | null; cache_write: number | null; cache_read: number | null } | null; model?: string | null; agent_status?: string | null; agent_subtype?: string | null; lint_checks?: number | null; draft_writes?: number | null; tool_calls?: number | null; tool_failures?: number | null; turn_limit?: number | null; time_limit_seconds?: number | null; error_phase?: string | null; error_type?: string | null; parent_job_id?: string | null };

/** How an agent job was asked to run and what it returned: settings, limits, sizes and outcomes. The prompt,
 * question, claims and response are not part of it: only the admin content read (AgentRunContent) returns them.
 * null means the input or result has no such value. */
export type AgentRun = {
  deliverable: string | null; input_format: string | null; model: string | null; effort: string | null;
  tools: string[] | null; graphs: string[] | null; network: string | null;
  allowed_domains: string[] | null; claims_repository: boolean | null; limits: Record<string, number> | null;
  attribute_requester: boolean | null; prompt_characters: number | null; system_prompt_characters: number | null;
  question_characters: number | null; claims_requested: number | null; claims_with_triple: number | null;
  /** Size of the requester's output_schema, when the answer job asked for structured output. */
  output_schema_characters?: number | null;
  result: {
    response_truncated: boolean | null; response_characters: number | null; files: number | null;
    structured_output_characters?: number | null;
    account_id: string | null; objects: number | null; claims: number | null; quotes: number | null; papers: number | null;
    lint_warnings: number | null; dapper_version: string | null;
    deposit_status: string | null; deposit_id: string | null; deposit_reason: string | null; deposit_http_status: number | null;
    /** The deposit screen's verdict: passed or withheld, the check that withheld the account, the classifier's highest
     * (or flagging) score against its threshold, and where the text was: a source file and lines, a ledger call or an
     * account field. Never the text. Absent from backends older than the screen. */
    screen_outcome?: string | null; screen_check?: string | null; screen_score?: number | null; screen_threshold?: number | null;
    screen_location?: string | null; screen_classifier?: string | null; screen_windows?: number | null; screen_seconds?: number | null;
    /** How many texts (source files, tool results, account fields) the classifier scored. */
    screen_texts?: number | null;
  };
  /** The files an account job saved under output/sources/, in the job detail only (never the job list); `total` counts
   * them all when the list is capped. null when the job returned no account; absent from older backends. */
  sources?: { items: AgentSource[]; total: number } | null;
};
/** One saved source file: what the agent recorded about it (path, URL, its command, the Bash call REVEAL saw run, the file
 * it was converted from) and what REVEAL measured (size, sha256, media type), with how many quotes cite it. Never its text. */
export type AgentSource = {
  path: string | null; url: string | null; command: string | null; observed_command: string | null; derived_from: string | null;
  sha256: string | null; media_type: string | null; size_bytes: number | null; evidence_items: number | null;
};
export type AgentJob = AdminJob & { agent: AgentRun };

/** One limit as an agent job asked for it, was frozen with and used it (GET /api/admin/jobs/{id}/run).
 * requested: whether the request named it; null when the job predates that record (requested_basis "not_recorded"),
 * when differs_from_current_default compares the frozen value with today's default only. ceiling_basis "current"
 * means the job did not record the ceiling in force when it was queued. applied: what the run was given, which the
 * dispatch clamp may lower for the budget. used: null where nothing counts it (REVEAL tool calls on their own). */
export type AgentLimitUse = {
  key: string; requested: boolean | null; requested_basis: "recorded" | "not_recorded"; differs_from_current_default: boolean | null;
  frozen: number | null; ceiling: number | null; ceiling_basis: "queue_time" | "current"; applied: number | null; used: number | null; hit: boolean;
  used_estimated?: boolean; all_tool_calls?: number | null; reached?: boolean | null;
};
/** One agent job's content for admins: the frozen request in full, its limits and why it stopped, and its response.
 * Each read is recorded by the backend under the admin's email. */
export type AgentRunContent = {
  job: { id: string; owner: string; status: string; stage: string; created_at: string | null; updated_at: string | null; completed_at: string | null;
    failure: { code: string | null; message: string | null } | null };
  input: {
    format: string | null; prompt: string | null; system_prompt: string | null; tools: string[] | null; graphs: string[] | null; network: string | null;
    allowed_domains: string[] | null; effort: string | null; model: string | null; deliverable: string | null; claims_repository: boolean | null;
    output_schema: unknown; limits: Record<string, number | null> | null;
    account: { question: string | null; attribute_requester: boolean | null;
      claims: { statement: string; proposition_id?: string; subject_entity?: string; relation?: string; object_entity?: string }[] } | null;
  };
  limits: AgentLimitUse[];
  /** by: who set the limit that was hit: the request, the default, the admin ceiling, or unknown for older jobs. */
  stop: { limit: string | null; by: "request" | "default" | "admin_ceiling" | "unknown" | null; text: string };
  run: { subtype: string | null; status: string | null; reason: string | null; tool_calls: number | null; tool_failures: number | null };
  /** source "captured": the agent's full final message from its response record; "job_result": the job result's copy,
   * cut at 50,000 characters (truncated says whether it was); null: no response. */
  response: { source: "captured" | "job_result" | null; attempt?: number | null; text: string | null; truncated: boolean; characters: number | null;
    status: string | null; subtype: string | null; structured_output: unknown; captured_unavailable: string | null;
    files: { path: string | null; size_bytes: number | null; sha256: string | null }[] };
  /** How the newest attempt's Claude Code was started, from its runtime.json. Absent from backends before Phase 2. */
  configuration?: RunConfiguration | null;
  read: { by: string; at: string };
};

/** How one attempt's Claude Code was started (docs/admin-telemetry.md, "The run response, field by field"). Every part is
 * null when runtime.json is missing; `missing` says why: not_captured, not_recorded (a run from before the configuration
 * was recorded; observed and transcript may still be set), too_large, unreadable or unavailable. */
export type RunConfiguration = {
  attempt: number | null; missing: string | null; format: string | null;
  claude_code: { pinned_version?: string | null; observed_version?: string | null } | null;
  cli: { argv?: string[] | null; wrapper?: string[] | string | null; environment?: Record<string, string> | null; prompt?: string | null } | null;
  tools: { builtin?: string[] | null; mcp?: string[] | null; mcp_servers?: Record<string, unknown> | null } | null;
  network: { mode?: string | null; allowed_domains?: string[] | null } | null;
  toolchain: { stamp?: string | null } | null;
  account_kit: { skill?: string | null; skill_sha256?: string | null; schema_excerpt_sha256?: string | null } | null;
  limits: { execution?: Record<string, number | null> | null; agent?: Record<string, number | null> | null } | null;
  source_tools: { packages?: Record<string, string> | null; programs?: Record<string, string> | null } | null;
  observed: { claude_code_version: string | null; model: string | null; permission_mode: string | null; tools: string[] | null;
    mcp_servers: { name: string; status: string }[] | null } | null;
  transcript: Record<string, { stored_bytes?: number | null; original_bytes?: number | null; cut?: boolean | null; omitted_bytes?: number | null; omitted_lines?: number | null }> | null;
};

/** One turn of an agent job's transcript (GET /api/admin/jobs/{id}/transcript). A tool_use and its tool_result share
 * call_id; tool_input is compact JSON text that a partial call may leave unparseable. */
export type TranscriptTurn = {
  index: number; role: "assistant" | "user" | "tool" | "system" | "result"; kind: "text" | "thinking" | "tool_use" | "tool_result" | "result";
  text: string | null; truncated: boolean; tool_name: string | null; tool_input: string | null; tool_input_truncated: boolean;
  call_id: string | null; is_error: boolean; partial: boolean; detail: Record<string, unknown> | null; at: string | null;
};
/** One page of a transcript. missing: why there is none (not_captured, too_large, unreadable, unavailable). */
export type TranscriptPage = {
  job: { id: string; status: string | null }; attempt: number | null; latest_attempt: number | null; missing: string | null;
  turns: TranscriptTurn[]; total: number; next_after: number | null; has_more: boolean; cut: boolean; complete: boolean | null; skipped: number;
  stderr: { text: string | null; truncated: boolean; cut: boolean; unreadable: boolean } | null;
  read: { by: string; at: string };
};

export type JobTokens = { input: number | null; output: number | null; cache_write: number | null; cache_read: number | null };
/** The job_metrics row an agent run records, and the recorded cause of a failed workflow phase. */
export type JobRunMetrics = {
  parent_job_id?: string | null; recorded_at?: string;
  agent?: { model: string | null; status: string | null; subtype: string | null; reason: string | null; cost_usd: number | null; estimated_cost_usd?: number | null; cost_source: string; budget_usd: number | null; budget_used: number | null; turns: number | null; turn_limit: number | null; elapsed_seconds: number | null; time_limit_seconds: number | null; api_seconds: number | null; tokens: JobTokens | null; tool_calls: number | null; tool_failures: number | null; lint_checks: number | null; draft_writes: number | null };
  error?: { phase: string | null; error_type: string; message: string | null; recorded_at: string };
};
export type JobDiagnostic = { path: string; available: boolean; data?: unknown; reason?: string };
export type JobAttempt = { attempt: number; started_at: string | null; worker_id: string | null; "remote_handle.box_id": string | null };
/** One job's logs and diagnostics (GET /api/admin/jobs/{id}), read under the telemetry assertion and refreshed while the
 * job runs. `agent` is how an agent job was asked to run; null for other kinds. */
export type JobDetail = {
  job: AdminJob & { failure: { code: string; message: string; retryable: boolean; budget?: { limit_usd: number; spent_usd: number | null } } | null; warnings: string[]; research_request_id: string | null; input_account_id: string | null };
  events: Schema<"JobEvent">[]; next_before: string | null;
  attempts: JobAttempt[]; attempts_limited: boolean; diagnostics: JobDiagnostic[]; diagnostics_limited: boolean;
  metrics?: JobRunMetrics | null; agent?: AgentRun | null;
};

export type AgentJobType = {
  deliverable: string; jobs: number; succeeded: number; failed: number; cancelled: number; active: number;
  deposited: number; deposit_not_made: number; spend_usd: number; owners: number; latest_at: string | null;
  /** Accounts the deposit screen withheld from the claims repository, in all and by the check that withheld them. */
  withheld_unsafe?: number; withheld_by_check?: Record<string, number>;
  typical_cost_usd: number | null; typical_turns: number | null; typical_seconds: number | null;
};
export type AgentJobs = { window_limit: number; jobs: AgentJob[]; types: AgentJobType[] };

const DELIVERABLES: Record<string, string> = { scientific_account: "Scientific account", answer: "Answer" };
/** The reader's name for what an agent job returns. An unrecorded one is "Unknown type", never a guess. */
export const deliverableLabel = (value: string | null | undefined) => value ? DELIVERABLES[value] ?? value.replaceAll("_", " ") : "Unknown type";
const KINDS: Record<string, string> = { analysis: "research run", paragraph: "research statement", agent: "agent job", deployment_probe: "deployment probe" };
export const jobKindLabel = (kind: string) => KINDS[kind] ?? kind.replaceAll("_", " ");
/** "agent · scientific account" for an agent job whose type is known; the bare kind otherwise. */
export const jobLabel = (job: Pick<AdminJob, "kind" | "deliverable">) => job.kind === "agent" && job.deliverable ? `agent · ${deliverableLabel(job.deliverable).toLowerCase()}` : job.kind;

/** Where the owner's GET /v1/jobs/{id}/account reads the account from: rebuilt from the claims repository when it was
 * deposited, else the copy kept with the job. */
export function accountSource(result: Pick<AgentJob["agent"]["result"], "account_id" | "deposit_status">): string | null {
  if (!result.account_id) return null;
  return result.deposit_status === "deposited" ? "served from the claims repository" : "served from the copy kept with the job";
}

/** Whether an account reached the claims repository. A job can succeed while its deposit does not. */
export function depositOutcome(job: Pick<AgentJob, "status" | "agent">): { text: string; tone: "good" | "bad" | "" } | null {
  const { deliverable, result } = job.agent;
  if (deliverable !== "scientific_account") return null;
  if (result.deposit_status === "deposited") return { text: "Deposited", tone: "good" };
  if (result.deposit_status === "failed" && result.deposit_reason === "inconsistent") return { text: "Deposit inconsistent: the claims repository holds part of the account otherwise than sent", tone: "bad" };
  if (result.deposit_status === "failed") return { text: `Deposit failed${result.deposit_reason ? `: ${result.deposit_reason.replaceAll("_", " ")}` : ""}${result.deposit_http_status ? ` (HTTP ${result.deposit_http_status})` : ""}`, tone: "bad" };
  if (result.deposit_status === "not_configured") return { text: "Not deposited: no claims repository is configured", tone: "bad" };
  if (result.deposit_status === "withheld_unsafe") return { text: `Withheld as unsafe${result.screen_check || result.deposit_reason ? `: ${(result.screen_check || result.deposit_reason)!.replaceAll("_", " ")}` : ""}`, tone: "bad" };
  if (job.status === "succeeded") return { text: "No deposit recorded", tone: "bad" };
  return { text: job.status === "failed" || job.status === "cancelled" ? "No account" : "Pending", tone: "" };
}

/** Which deposit filter an agent job falls under on the Agent jobs page: deposited, withheld by the deposit screen
 * (withheld_unsafe), failed or missing (the tones depositOutcome marks bad), or none (answers, pending, no account). */
export type DepositFilter = "deposited" | "withheld" | "failed" | "none";
export function depositFilter(job: Pick<AgentJob, "status" | "agent">): DepositFilter {
  if (job.agent.deliverable !== "scientific_account") return "none";
  const status = job.agent.result.deposit_status;
  if (status === "deposited") return "deposited";
  if (status === "withheld_unsafe") return "withheld";
  return depositOutcome(job)?.tone === "bad" ? "failed" : "none";
}

const score = (value: number | null | undefined) => value == null ? "—" : value.toFixed(2);
/** The deposit screen's verdict in one line: what it found, the classifier's score against its threshold, and where. */
export function screenSummary(result: AgentRun["result"]): string | null {
  if (!result.screen_outcome) return null;
  // "disabled": the classifier's lock (prompt-guard-release.json) is not pinned, so only the fixed checks ran.
  const scored = result.screen_classifier === "disabled" ? "classifier off — lock not pinned"
    : result.screen_classifier === "not_loaded" ? "classifier not loaded"
    : result.screen_score == null ? null : `score ${score(result.screen_score)} (threshold ${score(result.screen_threshold)})`;
  const head = result.screen_outcome === "withheld" ? `Withheld: ${(result.screen_check || "unknown check").replaceAll("_", " ")}` : "Passed";
  const where = result.screen_location ? `${result.screen_outcome === "withheld" ? "at" : "highest at"} ${result.screen_location}` : null;
  const effort = result.screen_windows ? `${result.screen_windows.toLocaleString()} windows${result.screen_texts ? ` from ${result.screen_texts.toLocaleString()} text${result.screen_texts === 1 ? "" : "s"}` : ""}${result.screen_seconds != null ? ` in ${result.screen_seconds.toFixed(1)} s` : ""}` : null;
  return [head, scored, where, effort].filter(Boolean).join(" · ");
}

/** The first 12 hex digits of a checksum, enough to tell files apart on screen. */
export const shortSha = (value: string | null | undefined) => value ? value.slice(0, 12) : "—";
/** A file size in bytes, kB or MB (decimal, as the capture limits are stated). */
export function fileSize(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1000) return `${bytes.toLocaleString()} byte${bytes === 1 ? "" : "s"}`;
  return bytes < 1_000_000 ? `${(bytes / 1000).toFixed(1)} kB` : `${(bytes / 1_000_000).toFixed(1)} MB`;
}
