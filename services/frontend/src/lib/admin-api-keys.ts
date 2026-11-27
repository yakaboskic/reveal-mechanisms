// Pure logic of the admin console's API keys tab: the view it reads, the mint form's checks and the bounded body the
// proxy forwards. The backend (api_keys.mint) stays the authority; this mirrors it so most mistakes show beside the field.

export const KEYS_FORMAT = "reveal.api-keys/1";
export const LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_MINT_BYTES = 4096;

export type Budget = { limit_usd: number; period: "month" | "total" };
export type KeyUsage = {
  spent_usd: number; in_progress_usd: number; jobs: number; period_start: string | null;
  limit_usd: number | null; period: Budget["period"] | null; remaining_usd: number | null;
};
export type ApiKeySummary = {
  key_id: string; label: string; prefix: string; user_id: string; agent_jobs: boolean;
  budget: Budget | null; budget_changed_at?: string | null; budget_changed_by?: string | null; usage?: KeyUsage;
  workspace: { principal_kind: string | null; display_name: string | null; workspace_expires_at: string | null; retired: boolean } | null;
  created_at: string; created_by: string; revoked_at: string | null; revoked_by: string | null;
};
export type ConfiguredKey = { fingerprint: string; user_id: string; agent_jobs: boolean };
export type ApiKeysView = { format: string; keys: ApiKeySummary[]; environment: { keys: ConfiguredKey[]; problem: string | null } };
export type Minted = { key: ApiKeySummary; api_key: string };
export type MintRequest = { label: string; user_id?: string; agent_jobs: boolean; budget: Budget | null };
export type BudgetFields = { period: "none" | Budget["period"]; limit: string };
export type MintFields = { label: string; workspace: "new" | "existing"; userId: string; agentJobs: boolean } & BudgetFields;
export const MAX_BUDGET_USD = 100_000;

/** The budget the fields describe: null for none, or an error to show beside the amount. */
export function budgetFields(fields: BudgetFields): { budget: Budget | null } | { error: string } {
  if (fields.period === "none") return { budget: null };
  const text = fields.limit.trim().replace(/^\$/, "").replaceAll(",", "");
  if (!/^(?:\d+\.?\d{0,2}|\.\d{1,2})$/.test(text)) return { error: "Enter an amount in dollars, such as 100 or 12.50." };
  const limit = Number(text);
  if (!(limit > 0) || limit > MAX_BUDGET_USD) return { error: `Enter more than $0 and at most $${MAX_BUDGET_USD.toLocaleString("en-US")}.` };
  return { budget: { limit_usd: limit, period: fields.period } };
}

export const dollars = (value: number) => `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** One line for a key's budget column: what it spent in its period and what is left. */
export function usageText(key: ApiKeySummary): string {
  const usage = key.usage;
  const spent = usage ? dollars(usage.spent_usd) : "—";
  if (!key.budget) return usage ? `${spent} spent, no budget` : "No budget";
  const when = key.budget.period === "month" ? "this month" : "in total";
  return `${spent} of ${dollars(key.budget.limit_usd)} ${when}` + (usage?.remaining_usd != null ? ` · ${dollars(usage.remaining_usd)} left` : "");
}

export function isKeysView(value: unknown): value is ApiKeysView {
  const view = value as ApiKeysView | null;
  return !!view && typeof view === "object" && view.format === KEYS_FORMAT && Array.isArray(view.keys)
    && !!view.environment && Array.isArray(view.environment.keys);
}

export function isMinted(value: unknown): value is Minted {
  const minted = value as Minted | null;
  return !!minted && typeof minted === "object" && typeof minted.api_key === "string" && /^rvl_[A-Za-z0-9_-]{43}$/.test(minted.api_key)
    && !!minted.key && typeof minted.key.key_id === "string";
}

/** The request to send, or an error per field ("label", "userId"). */
export function mintForm(fields: MintFields): { request: MintRequest } | { errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  if (!LABEL_PATTERN.test(fields.label)) errors.label = fields.label.trim() !== fields.label
    ? "Remove the spaces at the start or end." : "Use 1 to 64 letters, digits, spaces, dots, underscores or hyphens, starting with a letter or digit.";
  const userId = fields.userId.trim().toLowerCase();
  if (fields.workspace === "existing" && !UUID_PATTERN.test(userId)) errors.userId = "Enter a workspace id: a UUID such as 13b86465-2324-43c7-9f13-de73e1d27256.";
  const budget = budgetFields(fields);
  if ("error" in budget) errors.limit = budget.error;
  if (Object.keys(errors).length || "error" in budget) return { errors };
  return { request: { label: fields.label, agent_jobs: fields.agentJobs, budget: budget.budget, ...(fields.workspace === "existing" ? { user_id: userId } : {}) } };
}

type ReadMint = { ok: true; request: Record<string, unknown> } | { ok: false; status: number; error: string };
const MINT_FIELDS = ["label", "user_id", "agent_jobs", "budget"], BUDGET_FIELDS = ["budget"];
/** The mint body, read to at most 4 KB and passed on only as a plain JSON object with the known fields. */
export const readMintRequest = (request: Request, maxBytes = MAX_MINT_BYTES) =>
  readKeyRequest(request, MINT_FIELDS, "Send the key request as JSON with a label.", maxBytes);
/** A budget change: {"budget": {...}} or {"budget": null}; the backend checks the values. */
export const readBudgetRequest = (request: Request, maxBytes = MAX_MINT_BYTES) =>
  readKeyRequest(request, BUDGET_FIELDS, "Send the budget as JSON.", maxBytes, true);

async function readKeyRequest(request: Request, fields: string[], message: string, maxBytes: number, exact = false): Promise<ReadMint> {
  const large: ReadMint = { ok: false, status: 413, error: "The key request is too large." };
  const invalid: ReadMint = { ok: false, status: 400, error: message };
  if (Number(request.headers.get("content-length")) > maxBytes) return large;
  let text: string;
  try {
    const bytes = new Uint8Array(await request.arrayBuffer());
    if (bytes.byteLength > maxBytes) return large;
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch { return invalid; }
  let body: unknown;
  try { body = JSON.parse(text); } catch { return invalid; }
  if (!body || typeof body !== "object" || Array.isArray(body)) return invalid;
  const allowed = new Set(fields);
  if (Object.keys(body).some(key => !allowed.has(key)) || exact && Object.keys(body).length !== fields.length) return invalid;
  return { ok: true, request: body as Record<string, unknown> };
}
