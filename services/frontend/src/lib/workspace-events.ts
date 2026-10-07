/** Session-memory notifications. Redis credentials and private data stay on the server. */
import type { components } from "./api.generated";
import type { WorkspaceTab } from "./workspace-data";
import { alwaysVisible, documentVisibility, streamAttempt, untilAborted, type PageVisibility } from "./page-visibility";
export type WorkspaceEvent = components["schemas"]["WorkspaceEvent"];
export type WorkspaceConnection = "connecting" | "live" | "reconnecting" | "expired";
const listeners = new Set<(reset: boolean, event?: WorkspaceEvent) => void>();
export function onWorkspaceChange(listener: (reset: boolean, event?: WorkspaceEvent) => void) {
  listeners.add(listener); return () => { listeners.delete(listener); };
}
export function invalidateWorkspace(event?: WorkspaceEvent) { for (const listener of listeners) listener(false, event); }
export function resetWorkspaceCache() { for (const listener of listeners) listener(true); }

export function changesWorkspace(method: string, path: string) {
  return ["POST", "PATCH", "DELETE"].includes(method) && /\/v1\/(?:drafts(?:\/[^/]+(?:\/save)?)?|jobs(?:\/[^/]+\/(?:cancel|retry-review))?|local-work(?:\/[^/]+\/(?:close|grants(?:\/[^/]+)?))?|me\/explorations|accounts\/[^/]+\/(?:publication|vote)|knowledge-gaps\/[^/]+\/vote|analysis-outcomes\/[^/]+\/publication)$/.test(path);
}

export function affectedWorkspaceTabs(event?: WorkspaceEvent): WorkspaceTab[] {
  const tabs = ["drafts", "runs", "gaps", "accounts", "explorations"] as const;
  if (!event || event.collections.includes("identity") || event.collections.includes("catalog")) return [...tabs];
  return tabs.filter(tab => (tab !== "runs" && event.collections.includes(tab))
    || (tab === "runs" && event.collections.some(value => ["jobs", "requests"].includes(value)))
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

type Handlers = {
  change: (event?: WorkspaceEvent) => void; status: (status: WorkspaceConnection) => void; revoked: () => void;
  /** The signed resume position after each applied frame; a tab that takes over a shared stream resumes from it. */
  cursor?: (value: string) => void;
};
export async function connectWorkspaceEvents(signal: AbortSignal, handlers: Handlers, fetcher: typeof fetch = fetch, page: PageVisibility = documentVisibility(), resume = "") {
  let cursor = resume, failures = 0, opened = !!resume;
  const applied = { workspace: 0n, public: 0n };
  const advance = (value: string) => { cursor = value; handlers.cursor?.(value); };
  while (!signal.aborted) {
    if (page.hidden()) { await page.untilVisible(signal); continue; }
    handlers.status(opened ? "reconnecting" : "connecting");
    const attempt = streamAttempt(signal, page);
    let revoked = false;
    try {
      const response = await fetcher("/api/backend/v1/me/workspace/events", {
        headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) }, signal: attempt.signal, cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) {
        handlers.status("expired"); handlers.revoked(); return;
      }
      if (cursor && [400, 409].includes(response.status)) {
        const problem = await response.json().catch(() => ({})) as { code?: string };
        if (["INVALID_CURSOR", "EVENT_CURSOR_EXPIRED"].includes(problem.code || "")) {
          advance(""); applied.workspace = 0n; applied.public = 0n; handlers.change(); continue;
        }
      }
      if (!response.ok) throw new Error("Workspace event stream is unavailable.");
      await readWorkspaceFrames(response, frame => {
        if (attempt.signal.aborted || revoked) return;
        if (frame.event === "access_revoked") {
          revoked = true; handlers.status("expired"); handlers.revoked(); return;
        }
        if (frame.event === "ready") { if (frame.id) advance(frame.id); opened = true; failures = 0; handlers.status("live"); return; }
        if (frame.event === "connection_degraded") { handlers.status("reconnecting"); return; }
        if (frame.event === "resync_required") {
          applied.workspace = 0n; applied.public = 0n; handlers.change(); if (frame.id) advance(frame.id); return;
        }
        if (frame.event !== "workspace_change") return;
        const event = JSON.parse(frame.data) as WorkspaceEvent;
        if (event.schema_version !== 1 || !["workspace", "public"].includes(event.scope) || !/^\d+$/.test(event.cursor)
          || !Array.isArray(event.collections)) throw new Error("Unsupported workspace event.");
        const position = BigInt(event.cursor);
        if (position <= applied[event.scope]) return;
        handlers.change(event); applied[event.scope] = position; if (frame.id) advance(frame.id);
      });
      if (revoked) return;
      // A normal bounded renewal replays only missed events. No list refresh.
      failures = 0;
    } catch {
      if (signal.aborted) return;
      // A parked hidden tab is not a failure: it resumes from its cursor once shown.
      if (attempt.parked()) continue;
      failures++; handlers.status("reconnecting");
    } finally { attempt.end(); }
    await delay(Math.min(30_000, 500 * 2 ** Math.min(failures, 6)), signal);
  }
}

type Locks = Pick<LockManager, "request">;
type Channel = Pick<BroadcastChannel, "postMessage" | "close"> & { onmessage: ((event: MessageEvent) => void) | null };
export type StreamSharing = { locks?: Locks | null; channel?: ((name: string) => Channel) | null; fetcher?: typeof fetch; page?: PageVisibility };
type Message = { owner: string } & ({ type: "hello"; at: number } | { type: "revoked" }
  | { type: "state"; status: WorkspaceConnection; cursor: string } | { type: "changes"; events: (WorkspaceEvent | null)[]; cursor: string });
type Outgoing = Message extends infer M ? M extends Message ? Omit<M, "owner"> : never : never;
const sharedName = (owner: string) => `reveal:workspace-events:${owner}`;
const browserSharing = (): StreamSharing => ({
  locks: typeof navigator === "undefined" ? null : navigator.locks,
  channel: typeof BroadcastChannel === "undefined" ? null : name => new BroadcastChannel(name),
});

/**
 * One workspace stream per user and browser profile. The tab holding the user's Web Lock streams and rebroadcasts
 * what it applies on a BroadcastChannel; the other tabs apply that. A tab hears only what is broadcast after it
 * joins, and what it loaded before may predate that, so the first word it gets from a leader invalidates everything
 * once, as its own stream's fresh replay would have. Waiting for the lock and holding it both end after a minute
 * hidden, a closed tab releases it, and the next visible tab resumes from the last broadcast cursor.
 * Revocation reaches every tab, and none of those reconnects until its own identity changes. Without Web Locks or
 * BroadcastChannel (an insecure origin, an old browser) each tab streams for itself and still parks when hidden.
 */
export async function shareWorkspaceEvents(signal: AbortSignal, owner: string, handlers: Handlers, sharing: StreamSharing = browserSharing()) {
  const { locks, channel: open, fetcher = fetch, page = documentVisibility() } = sharing;
  if (!locks || !open) return connectWorkspaceEvents(signal, handlers, fetcher, page);
  const channel = open(sharedName(owner));
  let cursor = "", status: WorkspaceConnection = "connecting", leading = false, joined = false, stopped = false, unusable = false;
  let batch: (WorkspaceEvent | null)[] = [], term: ReturnType<typeof streamAttempt> | null = null, revokedAt = Infinity, yieldLock = () => {};
  const post = (message: Outgoing) => { try { channel.postMessage({ ...message, owner }); } catch { /* closed */ } };
  const flush = () => { if (batch.length) post({ type: "changes", events: batch, cursor }); batch = []; };
  const revoke = () => { stopped = true; revokedAt = Date.now(); term?.abort(); handlers.status("expired"); handlers.revoked(); };
  const lead: Handlers = {
    status: value => { status = value; handlers.status(value); flush(); post({ type: "state", status: value, cursor }); },
    cursor: value => { cursor = value; },
    // A replay burst reaches the other tabs as one message, so they invalidate once.
    change: event => { handlers.change(event); if (!batch.length) queueMicrotask(flush); batch.push(event ?? null); },
    revoked: () => { stopped = true; revokedAt = Date.now(); flush(); post({ type: "revoked" }); handlers.revoked(); },
  };
  channel.onmessage = ({ data }: MessageEvent<Message>) => {
    if (!data || data.owner !== owner || signal.aborted) return;
    if (data.type === "revoked") { if (!stopped) revoke(); return; }
    if (data.type === "hello") { if (leading) post({ type: "state", status, cursor }); else if (data.at > revokedAt) yieldLock(); return; }
    if (leading) return;
    if (!joined) { joined = true; handlers.change(); }
    if (data.type === "state") { cursor = data.cursor; status = data.status; handlers.status(data.status); }
    if (data.type === "changes") { for (const event of data.events) handlers.change(event ?? undefined); cursor = data.cursor; }
  };
  post({ type: "hello", at: Date.now() });
  try {
    while (!signal.aborted && !stopped) {
      await page.untilVisible(signal);
      if (signal.aborted || stopped) break;
      const attempt = term = streamAttempt(signal, page);
      try {
        await locks.request(sharedName(owner), { signal: attempt.signal }, async () => {
          if (attempt.signal.aborted || stopped) return;
          // A tab that never heard a leader streams from "" and so replays everything itself.
          leading = joined = true;
          try { await connectWorkspaceEvents(attempt.signal, lead, fetcher, alwaysVisible, cursor); } finally { leading = false; flush(); }
          // A revoked identity keeps the lock, so no tab that saw the revocation reconnects with it. A tab started
          // since (a new page) says hello after it and takes over.
          if (stopped) await new Promise<void>(resolve => { yieldLock = resolve; void untilAborted(signal).then(resolve); });
        });
      } catch {
        // An aborted wait is a park or an unmount; any other refusal means locks are unusable in this tab.
        if (!attempt.signal.aborted) { unusable = true; break; }
      } finally { attempt.end(); term = null; }
    }
    // After a revocation keep following: a later tab may still stream for this user.
    if (!unusable) await untilAborted(signal);
  } finally { channel.onmessage = null; channel.close(); }
  if (unusable && !signal.aborted && !stopped) await connectWorkspaceEvents(signal, handlers, fetcher, page, cursor);
}

/** Sign-out in one tab clears every other tab of that user at once rather than at its next identity check. */
export function announceSignOut(owner: string, sharing: StreamSharing = browserSharing()) {
  if (!sharing.channel) return;
  const channel = sharing.channel(sharedName(owner));
  try { channel.postMessage({ type: "revoked", owner }); } catch { /* best effort */ } finally { channel.close(); }
}
