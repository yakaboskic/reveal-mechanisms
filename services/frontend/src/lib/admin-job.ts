/** deliverable: what an agent job was asked to return ("answer" or "scientific_account"); null for other kinds of
 * job, and absent from backends older than this field. */
export type AdminJob = { id: string; owner: string; kind: string; deliverable?: string | null; status: string; stage: string; created_at: string; started_at: string | null; attempt_started_at: string | null; completed_at: string | null; updated_at: string; wall_seconds: number | null; queue_seconds: number | null; attempt_seconds: number | null; attempt: number; worker: string | null; lease_until: string | null; recoveries: number; delivery_recoveries?: number; handoffs?: number; lease_expired: boolean; box_id: string | null; box_phase: string | null; box_seconds: number | null; setup_seconds: number | null; agent_seconds: number | null; capture_seconds: number | null; cleanup_seconds: number | null; failure_code: string | null } & AdminJobMetrics;

/** Recorded agent cost and failure telemetry; every field is null until the job records metrics. */
export type AdminJobMetrics = { cost_usd?: number | null; estimated_cost_usd?: number | null; cost_source?: "provider" | "estimated" | "unreported" | null; budget_usd?: number | null; budget_used?: number | null; turns?: number | null; tokens?: { input: number | null; output: number | null; cache_write: number | null; cache_read: number | null } | null; model?: string | null; agent_status?: string | null; agent_subtype?: string | null; lint_checks?: number | null; draft_writes?: number | null; tool_calls?: number | null; tool_failures?: number | null; turn_limit?: number | null; time_limit_seconds?: number | null; error_phase?: string | null; error_type?: string | null; parent_job_id?: string | null };

/** How an agent job was asked to run and what it returned: settings, limits, sizes and outcomes. The prompt,
 * question, claims and response never reach the console. null means the input or result has no such value. */
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
