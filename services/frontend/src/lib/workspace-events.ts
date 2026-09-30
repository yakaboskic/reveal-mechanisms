/** Session-memory notifications. Redis credentials and private data stay on the server. */
import type { components } from "./api.generated";
export type WorkspaceEvent = components["schemas"]["WorkspaceEvent"];
export type WorkspaceConnection = "connecting" | "live" | "reconnecting" | "expired";
const listeners = new Set<(reset: boolean, event?: WorkspaceEvent) => void>();
export function onWorkspaceChange(listener: (reset: boolean, event?: WorkspaceEvent) => void) {
  listeners.add(listener); return () => { listeners.delete(listener); };
}
export function invalidateWorkspace(event?: WorkspaceEvent) { for (const listener of listeners) listener(false, event); }
export function resetWorkspaceCache() { for (const listener of listeners) listener(true); }

export function changesWorkspace(method: string, path: string) {
  return ["POST", "PATCH", "DELETE"].includes(method) && /\/v1\/(?:drafts(?:\/[^/]+)?|jobs(?:\/[^/]+\/(?:cancel|retry-review))?|me\/explorations|accounts\/[^/]+\/publication|analysis-outcomes\/[^/]+\/publication)$/.test(path);
}

export function affectedWorkspaceTabs(event?: WorkspaceEvent): ("gaps" | "accounts" | "explorations")[] {
  if (!event || event.collections.includes("identity") || event.collections.includes("catalog")) return ["gaps", "accounts", "explorations"];
  return (["gaps", "accounts", "explorations"] as const).filter(tab => event.collections.includes(tab)
    || (tab === "gaps" && event.collections.some(value => ["drafts", "jobs", "requests"].includes(value))));
}

type Frame = { event: string; id?: string; data: string };
export async function readWorkspaceFrames(response: Response, receive: (frame: Frame) => void) {
  if (!response.body) throw new Error("Workspace event stream has no body.");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "", event = "message", id: string | undefined, data: string[] = [];
  const line = (value: string) => {
    if (!value) {
      if (data.length) receive({ event, id, data: data.join("\n") });
      event = "message"; id = undefined; data = []; return;
    }
    if (value.startsWith(":")) return;
    const separator = value.indexOf(":"), field = separator < 0 ? value : value.slice(0, separator);
    let content = separator < 0 ? "" : value.slice(separator + 1); if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") event = content;
    if (field === "id" && !content.includes("\0")) id = content;
    if (field === "data") data.push(content);
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) { line(buffer.slice(0, end).replace(/\r$/, "")); buffer = buffer.slice(end + 1); }
      if (buffer.length > 1_000_000) throw new Error("Workspace event frame exceeded its limit.");
      if (done) return;
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>(resolve => {
    if (signal.aborted) return resolve();
    const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, ms); signal.addEventListener("abort", finish, { once: true });
  });
}

type Handlers = { change: (event?: WorkspaceEvent) => void; status: (status: WorkspaceConnection) => void; revoked: () => void };
export async function connectWorkspaceEvents(signal: AbortSignal, handlers: Handlers, fetcher: typeof fetch = fetch) {
  let cursor = "", failures = 0, opened = false;
  const applied = { workspace: 0n, public: 0n };
  while (!signal.aborted) {
    handlers.status(opened ? "reconnecting" : "connecting");
    let revoked = false;
    try {
      const response = await fetcher("/api/backend/v1/me/workspace/events", {
        headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) }, signal, cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) {
        handlers.status("expired"); handlers.revoked(); return;
      }
      if (cursor && [400, 409].includes(response.status)) {
        const problem = await response.json().catch(() => ({})) as { code?: string };
        if (["INVALID_CURSOR", "EVENT_CURSOR_EXPIRED"].includes(problem.code || "")) {
          cursor = ""; applied.workspace = 0n; applied.public = 0n; handlers.change(); continue;
        }
      }
      if (!response.ok) throw new Error("Workspace event stream is unavailable.");
      await readWorkspaceFrames(response, frame => {
        if (signal.aborted || revoked) return;
        if (frame.event === "access_revoked") {
          revoked = true; handlers.status("expired"); handlers.revoked(); return;
        }
        if (frame.event === "ready") { if (frame.id) cursor = frame.id; opened = true; failures = 0; handlers.status("live"); return; }
        if (frame.event === "connection_degraded") { handlers.status("reconnecting"); return; }
        if (frame.event === "resync_required") {
          applied.workspace = 0n; applied.public = 0n; handlers.change(); if (frame.id) cursor = frame.id; return;
        }
        if (frame.event !== "workspace_change") return;
        const event = JSON.parse(frame.data) as WorkspaceEvent;
        if (event.schema_version !== 1 || !["workspace", "public"].includes(event.scope) || !/^\d+$/.test(event.cursor)
          || !Array.isArray(event.collections)) throw new Error("Unsupported workspace event.");
        const position = BigInt(event.cursor);
        if (position <= applied[event.scope]) return;
        handlers.change(event); applied[event.scope] = position; if (frame.id) cursor = frame.id;
      });
      if (revoked) return;
      // A normal bounded renewal replays only missed events. No list refresh.
      failures = 0;
    } catch {
      if (signal.aborted) return;
      failures++; handlers.status("reconnecting");
    }
    await delay(Math.min(30_000, 500 * 2 ** Math.min(failures, 6)), signal);
  }
}
