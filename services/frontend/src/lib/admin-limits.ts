// Pure logic of the admin console's Limits tab: the form's text fields, their validation and the change it sends.
// The backend (agent_limits.change) stays the authority; this mirrors it so most mistakes show beside the field.

export const LIMITS_FORMAT = "reveal.agent-limits/1";
export const LIMIT_FIELDS = ["default", "ceiling"] as const;
export type LimitField = typeof LIMIT_FIELDS[number];
export type ResolvedLimit = {
  value: number; source: "console" | "environment" | "code"; environment: string;
  lowered_to_ceiling?: boolean; ignored_environment_value?: boolean;
};
export type AgentLimit = {
  key: string; label: string; unit: string; description: string; type: "integer" | "number";
  minimum: number; exclusive_minimum: boolean; maximum: number;
  code: Record<LimitField, number>; console: Record<LimitField, number | null>;
  default: ResolvedLimit; ceiling: ResolvedLimit;
};
export type LimitsHistoryEntry = { at: string; by: string; changes: Record<string, Partial<Record<LimitField, [number | null, number | null]>>> };
export type AgentLimitsView = {
  format: string; version: number | null; updated_at: string | null; updated_by: string | null;
  limits: AgentLimit[]; history: LimitsHistoryEntry[];
};
/** The text of every input, by limit key. An empty string means "not set in the console". */
export type LimitFields = Record<string, Record<LimitField, string>>;
export type LimitValues = Record<string, Partial<Record<LimitField, number>>>;
export type LimitsChange = { expected_version: number | null; values: Record<string, unknown> };

export function isLimitsView(value: unknown): value is AgentLimitsView {
  const view = value as AgentLimitsView | null;
  return !!view && typeof view === "object" && view.format === LIMITS_FORMAT && Array.isArray(view.limits) && Array.isArray(view.history);
}

export const limitFieldId = (key: string, field: LimitField) => `${key}.${field}`;
// Inputs take plain digits with a decimal point, so displayed values use that convention in every locale.
export const formatLimitValue = (value: number) => value.toLocaleString("en-US", { maximumFractionDigits: 6 });
export function supportedRange(limit: AgentLimit) {
  return `${limit.exclusive_minimum ? `above ${formatLimitValue(limit.minimum)}, up to` : `${formatLimitValue(limit.minimum)} to`} ${formatLimitValue(limit.maximum)}`;
}

/** The form as last saved: only what the console has set. Effective values are never seeded, or saving would pin them. */
export function limitFields(view: AgentLimitsView): LimitFields {
  const text = (value: number | null | undefined) => value == null ? "" : String(value);
  return Object.fromEntries(view.limits.map(limit => [limit.key, { default: text(limit.console.default), ceiling: text(limit.console.ceiling) }]));
}

/** One field's text as a value in the supported range; null when the field is empty. */
export function parseLimit(limit: AgentLimit, raw: string): { value: number | null } | { error: string } {
  const text = raw.trim();
  if (!text) return { value: null };
  const whole = limit.type === "integer";
  if (!(whole ? /^-?\d+$/ : /^-?(?:\d+\.?\d*|\.\d+)$/).test(text)) return { error: whole ? "Enter a whole number, digits only." : "Enter a number, such as 2 or 2.5." };
  const value = Number(text);
  if (!(limit.exclusive_minimum ? value > limit.minimum : value >= limit.minimum) || value > limit.maximum)
    return { error: `Enter a ${whole ? "whole " : ""}number ${limit.exclusive_minimum ? "" : "from "}${supportedRange(limit)}.` };
  return { value };
}

/** What an empty field resolves to. Under a console-set value the view does not say whether the environment
 *  variable is set, so only the built-in value is certain to exist and `known` is false. */
export function fallbackLimit(limit: AgentLimit, field: LimitField): { value: number; known: boolean } {
  const resolved = limit[field];
  return resolved.source === "console" ? { value: limit.code[field], known: false } : { value: resolved.value, known: true };
}

/** The whole form: `values` for the PUT (every console-set field, because the PUT replaces them all), an error
 *  per invalid field, and the fields that differ from what is saved. */
export function limitsForm(view: AgentLimitsView, fields: LimitFields) {
  const values: LimitValues = {}, errors: Record<string, string> = {}, changed: string[] = [];
  for (const limit of view.limits) {
    const parsed: Partial<Record<LimitField, number | null>> = {}; // A field with unusable text has no entry.
    for (const field of LIMIT_FIELDS) {
      const id = limitFieldId(limit.key, field), result = parseLimit(limit, fields[limit.key]?.[field] ?? "");
      if ("error" in result) errors[id] = result.error; else parsed[field] = result.value;
      if (parsed[field] !== (limit.console[field] ?? null)) changed.push(id);
    }
    const chosen = parsed.default, ceiling = parsed.ceiling;
    // As the server does, only a default set here is checked: an empty one is lowered to the ceiling instead.
    if (typeof chosen === "number" && ceiling !== undefined) {
      const fallback = fallbackLimit(limit, "ceiling");
      const top = ceiling ?? (fallback.known ? fallback.value : null); // Unknown fallback: the server decides.
      if (top !== null && chosen > top) errors[limitFieldId(limit.key, "default")] = `The default ${formatLimitValue(chosen)} is above the ceiling ${formatLimitValue(top)}.`;
    }
    const kept: Partial<Record<LimitField, number>> = {};
    for (const field of LIMIT_FIELDS) { const value = parsed[field]; if (typeof value === "number") kept[field] = value; }
    if (Object.keys(kept).length) values[limit.key] = kept;
  }
  return { values, errors, changed };
}

export const MAX_CHANGE_BYTES = 8192;
type ReadChange = { ok: true; change: LimitsChange } | { ok: false; status: number; error: string };
/** A limits change from a request body: at most maxBytes actually read, parsed, and reduced to its two members. */
export async function readLimitsChange(request: Request, maxBytes = MAX_CHANGE_BYTES): Promise<ReadChange> {
  const large: ReadChange = { ok: false, status: 413, error: "The limits change is too large." };
  const invalid: ReadChange = { ok: false, status: 400, error: "Send the limits change as JSON with expected_version and values." };
  if (Number(request.headers.get("content-length")) > maxBytes) return large;
  const bytes = new Uint8Array(maxBytes);
  let size = 0, body: unknown;
  try {
    const reader = request.body?.getReader();
    for (let chunk = await reader?.read(); chunk && !chunk.done; chunk = await reader!.read()) {
      if (size + chunk.value.byteLength > maxBytes) { await reader!.cancel().catch(() => {}); return large; }
      bytes.set(chunk.value, size); size += chunk.value.byteLength;
    }
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, size)));
  } catch { return invalid; } // Unreadable, not UTF-8 or not JSON.
  const plain = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
  if (!plain(body) || !plain(body.values) || !(body.expected_version === null || Number.isSafeInteger(body.expected_version))) return invalid;
  return { ok: true, change: { expected_version: body.expected_version as number | null, values: body.values } };
}
