// Pure logic of the admin console's API keys tab: the view it reads, the mint form's checks and the bounded body the
// proxy forwards. The backend (api_keys.mint) stays the authority; this mirrors it so most mistakes show beside the field.

export const KEYS_FORMAT = "reveal.api-keys/1";
export const LABEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_MINT_BYTES = 4096;

export type ApiKeySummary = {
  key_id: string; label: string; prefix: string; user_id: string; agent_jobs: boolean;
  workspace: { principal_kind: string | null; display_name: string | null; workspace_expires_at: string | null; retired: boolean } | null;
  created_at: string; created_by: string; revoked_at: string | null; revoked_by: string | null;
};
export type ConfiguredKey = { fingerprint: string; user_id: string; agent_jobs: boolean };
export type ApiKeysView = { format: string; keys: ApiKeySummary[]; environment: { keys: ConfiguredKey[]; problem: string | null } };
export type Minted = { key: ApiKeySummary; api_key: string };
export type MintRequest = { label: string; user_id?: string; agent_jobs: boolean };
export type MintFields = { label: string; workspace: "new" | "existing"; userId: string; agentJobs: boolean };

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
  if (Object.keys(errors).length) return { errors };
  return { request: { label: fields.label, agent_jobs: fields.agentJobs, ...(fields.workspace === "existing" ? { user_id: userId } : {}) } };
}

type ReadMint = { ok: true; request: Record<string, unknown> } | { ok: false; status: number; error: string };
/** The mint body, read to at most 4 KB and passed on only as a plain JSON object with the three known fields. */
export async function readMintRequest(request: Request, maxBytes = MAX_MINT_BYTES): Promise<ReadMint> {
  const large: ReadMint = { ok: false, status: 413, error: "The key request is too large." };
  const invalid: ReadMint = { ok: false, status: 400, error: "Send the key request as JSON with a label." };
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
  const allowed = new Set(["label", "user_id", "agent_jobs"]);
  if (Object.keys(body).some(key => !allowed.has(key))) return invalid;
  return { ok: true, request: body as Record<string, unknown> };
}
