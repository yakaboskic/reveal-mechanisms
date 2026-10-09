/** Browser contracts for local research. Workspace archives contain no credentials. */
import { changesWorkspace, invalidateWorkspace } from "./workspace-events";

export type LocalWorkState = "preparing" | "ready" | "preparation_failed" | "closed";
export type LocalAgentClient = "codex" | "claude_code";
export type LocalSetupProgress = { phase: "preparing" | "downloading"; receivedBytes: number; totalBytes?: number };
export const localClientPreferenceKey = "reveal.local-agent-client";
export const localClientPreference = (value: string | null): LocalAgentClient => value === "claude_code" ? value : "codex";
export const localLaunchCommand = (client: LocalAgentClient, mode?: "--login" | "--logout" | "--check-only" | "--offline") => `python3 start.py ${client === "claude_code" ? "claude" : "codex"}${mode ? ` ${mode}` : ""}`;

/** Blob URLs are short-lived and never persisted; abort also releases a pending download URL. */
export function downloadLocalBlob(blob: Blob, filename: string, signal?: AbortSignal) {
  signal?.throwIfAborted();
  const url = URL.createObjectURL(blob), anchor = document.createElement("a");
  let released = false, timer: ReturnType<typeof setTimeout> | undefined;
  const release = () => { if (released) return; released = true; if (timer) clearTimeout(timer); URL.revokeObjectURL(url); signal?.removeEventListener("abort", release); };
  signal?.addEventListener("abort", release, { once: true });
  try {
    anchor.href = url; anchor.download = filename; document.body.appendChild(anchor);
    signal?.throwIfAborted(); anchor.click(); timer = setTimeout(release, 1_000);
  } catch (error) { release(); throw error; }
  finally { anchor.remove(); }
}
export type LocalGrant = { grant_id: string; expires_at: string; revoked_at?: string | null };
export type LocalSubmission = {
  id: string; state?: string; status?: string; created_at?: string;
  account_ids?: string[]; reused_account_ids?: string[]; validation_only?: boolean; result?: { account_ids?: string[] }; report?: unknown;
  error?: string | { message?: string; detail?: string } | null;
  validation_report?: unknown; last_error?: string | { message?: string; detail?: string } | null;
};
export type LocalWork = {
  id: string; state: LocalWorkState; created_at: string; last_activity?: string | null; last_action?: string;
  request: { id?: string; question_id?: string; lightning_audit_id?: string; composer?: { context?: string; research_direction?: string }; document?: { knowledge_gaps?: { id: string; text?: string; name?: string }[] } };
  package_id?: string | null; package_sha256?: string | null; package?: Record<string, unknown> | null;
  submissions: LocalSubmission[]; grants: LocalGrant[];
  last_error?: string | { message?: string; detail?: string } | null;
};
export type LocalGrantResponse = LocalGrant & {
  token?: string | null; mcp_url: string; local_work_id: string; prompt?: string;
  instructions?: { codex?: string; claude_code?: string };
};
export type LocalWorkList = { items: LocalWork[]; page?: { next_cursor?: string | null; has_more?: boolean } };
export class LocalWorkError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
const base = "/api/backend/v1/local-work";
export const localWorkHref = (id: string) => `/local-runs/${encodeURIComponent(id)}`;
export const localPackageHref = (id: string) => `${base}/${encodeURIComponent(id)}/package`;
export const localWorkTitle = (work: LocalWork) => work.request?.document?.knowledge_gaps?.find(gap => gap.id === work.request.question_id)?.text || work.request?.composer?.research_direction || work.request?.composer?.context || `Local research ${work.id.slice(0, 8)}`;
export const localStateLabel: Record<LocalWorkState, string> = { preparing: "Preparing research seed", ready: "Ready for your local agent", preparation_failed: "Preparation needs attention", closed: "Closed" };
export const activeLocalGrants = (work: LocalWork, now = Date.now()) => work.grants.filter(grant => !grant.revoked_at && new Date(grant.expires_at).getTime() > now);
export const submissionAccounts = (submission: LocalSubmission) => submission.validation_only || (submission.state || submission.status) !== "accepted"
  ? [] : [...new Set([...(submission.account_ids || submission.result?.account_ids || []), ...(submission.reused_account_ids || [])])];
export const analysisAccountResults = (result: { account_ids: string[]; reused_account_ids?: string[] }) => [
  ...result.account_ids.map(id => ({ id, reused: false })),
  ...(result.reused_account_ids || []).filter(id => !result.account_ids.includes(id)).map(id => ({ id, reused: true })),
];
export const localErrorMessage = (value: LocalWork["last_error"]) => typeof value === "string" ? value : value?.message || value?.detail || "";

export function localResearchPrompt(workId: string) {
  return `Work on local research ${workId} using the frozen research seed and authoring instructions in this folder. Verify the manifest checksums. Begin with anonymous Reveal MCP access to public scientific data. Do not request private records or submit anything until the user chooses to sign in and approves access through connect_reveal. Use get_reveal_connection to check access and disconnect_reveal to remove it. Search existing accepted accounts for this gap and relevant Propositions and Claims before authoring new objects; preserve original identities, evidence and authorship through reuse receipts. Discover the data operations available for the bound imports. Investigate the frozen gap using the selected factors, loaded CFDE/PIGEAN/EAGGL data and authorized evidence tools. Only the two explicit small-model gene–phenotype and gene-set–phenotype tools may query external reference data. Retain evidence receipt IDs and exact source locators. Distinguish missing or unqueried data from negative evidence. Seek a defensible CFDE connection; if none exists, explain the limitation and proceed with other supported evidence. Keep one newly authored ScientificAccount and its dependencies per document, or select an existing account answering this exact question. When findings are ready, explain that server validation, uploads and submission require a connected Reveal account. After the user approves access, validate, repair errors, and submit the documents and reuse selections. Return the submission ID and account links. Do not publish or request hosted statement generation automatically.`;
}

/** Neither setup configuration nor the research prompt includes the actual credential. */
export function localConnectionConfig(client: "codex" | "claude_code", url: string, workId: string) {
  const parsed = new URL(url);
  if (parsed.username || parsed.password || parsed.search || parsed.hash || (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname)))) throw new Error("Reveal returned an invalid MCP endpoint.");
  const name = `reveal_${workId.replace(/[^a-zA-Z0-9_]/g, "_")}`;
  return client === "codex"
    ? `[mcp_servers.${name}]\nurl = ${JSON.stringify(url)}`
    : JSON.stringify({ mcpServers: { [name]: { type: "http", url } } }, null, 2);
}

export function createLocalWorkClient(fetcher: typeof fetch = fetch) {
  async function request<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
    const headers = new Headers({ Accept: "application/json" });
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (options.key) headers.set("Idempotency-Key", options.key);
    const timeout = AbortSignal.timeout(30_000);
    const response = await fetcher(path, { method: options.method || "GET", credentials: "same-origin", cache: "no-store", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal ? AbortSignal.any([timeout, options.signal]) : timeout });
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new LocalWorkError(response.status, problem.code || "API_ERROR", problem.detail || problem.message || `Local research request failed (${response.status}).`);
    }
    if (changesWorkspace(options.method || "GET", path)) invalidateWorkspace(undefined, true);
    if (response.status === 204) return undefined as T;
    return response.json();
  }
  const workPath = (id: string) => `${base}/${encodeURIComponent(id)}`;
  return {
    setupKit: async (id: string, client: LocalAgentClient, caller?: AbortSignal, onProgress?: (progress: LocalSetupProgress) => void) => {
      const timeout = AbortSignal.timeout(120_000), signal = caller ? AbortSignal.any([timeout, caller]) : timeout;
      signal.throwIfAborted();
      onProgress?.({ phase: "preparing", receivedBytes: 0 });
      const response = await fetcher(`${workPath(id)}/setup-kit`, { method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Accept: "application/zip", "Content-Type": "application/json" }, body: JSON.stringify({ client }), signal });
      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new LocalWorkError(response.status, problem.code || "API_ERROR", problem.detail || problem.message || `Workspace download failed (${response.status}).`);
      }
      if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "application/zip") throw new Error("Reveal did not return a workspace ZIP. Please try again.");
      const contentLength = Number(response.headers.get("Content-Length"));
      const totalBytes = Number.isSafeInteger(contentLength) && contentLength > 0 ? contentLength : undefined;
      onProgress?.({ phase: "downloading", receivedBytes: 0, totalBytes });
      const reader = response.body?.getReader();
      let blob: Blob;
      if (reader) {
        const chunks: Uint8Array<ArrayBuffer>[] = []; let receivedBytes = 0;
        const cancelRead = () => { void reader.cancel().catch(() => {}); };
        signal.addEventListener("abort", cancelRead, { once: true });
        try {
          signal.throwIfAborted();
          while (true) {
            const { done, value } = await reader.read(); signal.throwIfAborted();
            if (done) break;
            chunks.push(new Uint8Array(value)); receivedBytes += value.byteLength;
            onProgress?.({ phase: "downloading", receivedBytes, totalBytes });
          }
          blob = new Blob(chunks, { type: "application/zip" });
        } catch (error) { await reader.cancel().catch(() => {}); throw error; }
        finally { signal.removeEventListener("abort", cancelRead); reader.releaseLock(); }
      } else { blob = await response.blob(); }
      signal.throwIfAborted();
      if (!blob.size) throw new Error("The workspace ZIP was empty. Please try again.");
      return blob;
    },
    create: (body: { draft_id: string; draft_version: number }, key: string) => request<LocalWork>(base, { method: "POST", body, key }),
    list: (cursor?: string, signal?: AbortSignal) => request<LocalWorkList>(base + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""), { signal }),
    get: (id: string, signal?: AbortSignal) => request<LocalWork>(workPath(id), { signal }),
    package: (id: string) => request<Record<string, unknown>>(localPackageHref(id)),
    grant: (id: string, key: string) => request<LocalGrantResponse>(`${workPath(id)}/grants`, { method: "POST", body: {}, key }),
    revoke: (id: string, grantId: string) => request<unknown>(`${workPath(id)}/grants/${encodeURIComponent(grantId)}`, { method: "DELETE" }),
    close: (id: string, key: string) => request<LocalWork>(`${workPath(id)}/close`, { method: "POST", body: {}, key }),
    generateStatement: (accountId: string, key: string) => request<{ id: string }>("/api/backend/v1/jobs", { method: "POST", body: { kind: "paragraph", account_id: accountId }, key }),
  };
}
export const localWorkApi = createLocalWorkClient();
