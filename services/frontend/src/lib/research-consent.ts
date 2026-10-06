/** OAuth browser consent uses the app session; provider tokens never leave the auth gateway. */
export type ConsentLookup = { request_id: string } | { user_code: string };
export type ResearchConsent = {
  request_id: string; kind: "authorization_code" | "device"; client_name: string; client_id: string;
  scopes: string[]; resource: string; redirect_uri?: string | null; requested_local_work_id: string | null; expires_at: string;
  status: string; requires_registered: true;
};
export type ConsentDecision = { redirect_url: string } | { approved: boolean; local_work_id?: string };
export class ResearchConsentError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export function consentLookup(params: Pick<URLSearchParams, "getAll">): ConsentLookup | null {
  const requests = params.getAll("request_id"), codes = params.getAll("user_code");
  if (!requests.length && !codes.length) return null;
  if (requests.length + codes.length !== 1) throw new Error("Use one connection request or one device code.");
  if (requests.length) {
    if (!/^[A-Za-z0-9_-]{8,200}$/.test(requests[0])) throw new Error("This connection request is invalid. Open the link from your agent again.");
    return { request_id: requests[0] };
  }
  const code = codes[0].trim().toUpperCase();
  if (!/^[A-Z0-9-]{6,32}$/.test(code)) throw new Error("Enter the device code shown by your agent.");
  return { user_code: code };
}
export const consentPagePath = (lookup: ConsentLookup | null) => `/research/connect${lookup ? `?${new URLSearchParams(lookup)}` : ""}`;
export function consentRedirect(value: string) {
  // Callback registration and exact matching belong to the backend. Preserve its full string/state.
  const parsed = new URL(value);
  if (parsed.username || parsed.password || !(parsed.protocol === "https:" || parsed.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname))) throw new Error("Reveal returned an invalid client callback. Return to your agent and start a new connection.");
  return value;
}
export const consentScopeLabel = (scope: string) => ({
  "research:read": "Read this research’s private inputs, retained evidence and owned scientific context",
  "research:write": "Read this research, capture and import evidence, validate and submit findings",
  "research:submit": "Validate and submit scientific accounts",
  "offline_access": "Stay connected between sessions until you disconnect",
}[scope] || scope);
export function createResearchConsentClient(fetcher: typeof fetch = fetch) {
  async function request<T>(path: string, body?: unknown, caller?: AbortSignal): Promise<T> {
    const timeout = AbortSignal.timeout(30_000), signal = caller ? AbortSignal.any([timeout, caller]) : timeout;
    signal.throwIfAborted();
    const response = await fetcher(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", redirect: "error", signal,
      headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const value = await response.json();
    if (!response.ok) throw new ResearchConsentError(response.status, value.code || "CONSENT_FAILED", value.detail || value.message || "The connection request could not be checked. Please retry.");
    return value as T;
  }
  const base = "/api/backend/v1/research-oauth/consent";
  return {
    get: (lookup: ConsentLookup, signal?: AbortSignal) => request<ResearchConsent>(`${base}?${new URLSearchParams(lookup)}`, undefined, signal),
    decide: (requestId: string, approve: boolean, localWorkId?: string, signal?: AbortSignal) => request<ConsentDecision>(base, { request_id: requestId, approve, ...(localWorkId ? { local_work_id: localWorkId } : {}) }, signal),
    claim: (key: string, signal?: AbortSignal) => fetcher("/api/session/claim", { method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error", signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30_000)]) : AbortSignal.timeout(30_000), headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify({ consent: true }) }).then(async response => {
      const value = await response.json();
      if (!response.ok) throw new ResearchConsentError(response.status, value.code || "CLAIM_FAILED", value.detail || "Your anonymous research could not be moved. Your original records are retained.");
      return value;
    }),
  };
}
export const researchConsentApi = createResearchConsentClient();
