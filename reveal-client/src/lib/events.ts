import { ApiError, backend, errorMessage, responseError } from "./api";
import { terminal, type Job, type JobEvent, type WorkspaceEvent } from "./types";

export type Frame = { event: string; data: string; id?: string };
/** Streaming SSE framing, including CR/LF split across arbitrary UTF-8 chunks. */
export function createEventParser(emit: (frame: Frame) => void) {
  const decoder = new TextDecoder();
  let line = "", data: string[] = [], event = "message", id: string | undefined, afterCR = false, size = 0;
  function endLine() {
    if (!line) {
      if (data.length) emit({ event, data: data.join("\n"), ...(id === undefined ? {} : { id }) });
      data = []; event = "message"; id = undefined; size = 0;
    } else if (!line.startsWith(":")) {
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") data.push(value);
      if (field === "event") event = value;
      if (field === "id" && !value.includes("\0")) id = value;
    }
    line = "";
  }
  function text(value: string) {
    for (const char of value) {
      if (afterCR) { afterCR = false; if (char === "\n") continue; }
      if (char === "\r" || char === "\n") { endLine(); afterCR = char === "\r"; }
      else { line += char; if (++size > 2_000_000) throw new Error("Event exceeds the supported size."); }
    }
  }
  return { push: (chunk: Uint8Array) => text(decoder.decode(chunk, { stream: true })), finish: () => text(decoder.decode()) };
}

export function jobEvent(frame: Frame, jobId: string, cursor: string): JobEvent | null {
  if (["ready", "connection_degraded", "resync_required"].includes(frame.event)) return null;
  const value = JSON.parse(frame.data) as JobEvent;
  if (!value || value.job_id !== jobId || typeof value.id !== "string" || !/^\d+$/.test(value.id)
      || typeof value.message !== "string" || !["queued", "running", "cancel_requested", "succeeded", "failed", "cancelled", "insufficient_evidence"].includes(value.status)
      || (frame.id !== undefined && frame.id !== value.id)) throw new Error("The API returned an invalid job event.");
  return BigInt(value.id) <= BigInt(cursor) ? null : value;
}

export function wait(delay: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, delay);
    signal.addEventListener("abort", abort, { once: true });
  });
}
async function read(response: Response, onFrame: (frame: Frame) => boolean | void) {
  if (!response.body) throw new Error("The event stream has no body.");
  const reader = response.body.getReader(); let stopped = false;
  const parser = createEventParser(frame => { if (!stopped) stopped = onFrame(frame) === false; });
  try {
    while (!stopped) {
      const { done, value } = await reader.read();
      if (done) { parser.finish(); break; }
      parser.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  return stopped;
}
/**
 * Over HTTP/1.1 (local development) a browser profile has six connections per origin across all tabs, and each open
 * stream holds one. A tab hidden for a minute parks its streams and resumes from its cursor once shown.
 */
export const parkHiddenAfterMs = 60_000;
export type Visibility = { hidden(): boolean; untilVisible(signal: AbortSignal): Promise<void>; onHiddenFor(ms: number, callback: () => void): () => void };
export const alwaysVisible: Visibility = { hidden: () => false, untilVisible: async () => {}, onHiddenFor: () => () => {} };
export function pageVisibility(doc: (EventTarget & { visibilityState: DocumentVisibilityState }) | undefined = typeof document === "undefined" ? undefined : document): Visibility {
  if (!doc) return alwaysVisible;
  const hidden = () => doc.visibilityState === "hidden";
  return {
    hidden,
    untilVisible: signal => new Promise<void>(resolve => {
      if (!hidden() || signal.aborted) return resolve();
      const check = () => {
        if (hidden() && !signal.aborted) return;
        doc.removeEventListener("visibilitychange", check); signal.removeEventListener("abort", check); resolve();
      };
      doc.addEventListener("visibilitychange", check); signal.addEventListener("abort", check);
    }),
    onHiddenFor: (ms, callback) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const change = () => { clearTimeout(timer); timer = hidden() ? setTimeout(callback, ms) : undefined; };
      change(); doc.addEventListener("visibilitychange", change);
      return () => { clearTimeout(timer); doc.removeEventListener("visibilitychange", change); };
    },
  };
}
/** One request: aborts with the caller's signal or when the hidden page parks it; `parked` tells the two apart. */
function attempt(signal: AbortSignal, page: Visibility) {
  const controller = new AbortController(), abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  const unpark = page.onHiddenFor(parkHiddenAfterMs, abort);
  return { signal: controller.signal, abort, parked: () => controller.signal.aborted && !signal.aborted,
    end: () => { unpark(); signal.removeEventListener("abort", abort); } };
}
const untilAborted = (signal: AbortSignal) => new Promise<void>(resolve => {
  if (signal.aborted) resolve(); else signal.addEventListener("abort", () => resolve(), { once: true });
});

/** The job a view shows once `event` is applied. */
export const withJobEvent = (job: Job, event: JobEvent): Job => ({ ...job, status: event.status, stage: event.stage,
  result: event.result || job.result, updated_at: event.occurred_at, last_event_id: event.id });
/**
 * A job opened after it finished already holds the outcome its replay ends with: only the terminal event that is its
 * saved last event needs no job read. followJob stops at the first terminal event, and a review retry leaves earlier
 * ones (failed, then queued again) in the history, so a replay that stops before the saved outcome must re-read it.
 */
export const savedOutcome = (known: Pick<Job, "id" | "status" | "last_event_id"> | null, event: JobEvent) =>
  known?.id === event.job_id && terminal(known.status) && known.last_event_id === event.id;
type StreamOptions = { signal: AbortSignal; onState: (state: string) => void; visibility?: Visibility };
export async function followJob(jobId: string, options: StreamOptions & { onEvent: (event: JobEvent) => void;
  onResync: () => Promise<{ cursor: string; terminal: boolean }> }) {
  let cursor = "0", failures = 0;
  const page = options.visibility || pageVisibility();
  while (!options.signal.aborted) {
    if (page.hidden()) { await page.untilVisible(options.signal); continue; }
    const current = attempt(options.signal, page);
    try {
      const response = await fetch(backend("jobs/" + encodeURIComponent(jobId) + "/events"), {
        headers: { Accept: "text/event-stream", "Last-Event-ID": cursor }, credentials: "same-origin", cache: "no-store", signal: current.signal,
      });
      if (!response.ok) {
        const error = await responseError(response);
        if (error.code === "EVENT_CURSOR_EXPIRED") {
          const latest = await options.onResync(); cursor = latest.cursor; if (latest.terminal) return; continue;
        }
        throw error;
      }
      options.onState("Live updates connected");
      const ended = await read(response, frame => {
        if (frame.event === "access_revoked") throw new ApiError(401, "SESSION_EXPIRED", "Reconnect your workspace to continue.");
        const item = jobEvent(frame, jobId, cursor);
        if (!item) return;
        failures = 0; cursor = item.id; options.onEvent(item);
        if (terminal(item.status)) return false;
      });
      if (ended) return;
      options.onState("Renewing live connection…");
      await wait(300, options.signal);
    } catch (error) {
      if (options.signal.aborted) return;
      if (current.parked()) continue;
      if ((error instanceof ApiError && ![502, 503, 504, 429].includes(error.status)) || ++failures > 5) throw error;
      options.onState("Connection interrupted; reconnecting…");
      await wait(Math.min(1000 * 2 ** (failures - 1), 15000), options.signal);
    } finally { current.end(); }
  }
}

type WorkspaceHandlers = { onState: (state: string) => void; onChange: (event: WorkspaceEvent | null) => void; onCursor?: (cursor: string) => void };
async function streamWorkspace(signal: AbortSignal, handlers: WorkspaceHandlers, page: Visibility, resume = "") {
  let cursor = resume, failures = 0;
  while (!signal.aborted) {
    if (page.hidden()) { await page.untilVisible(signal); continue; }
    const current = attempt(signal, page);
    try {
      const response = await fetch(backend("me/workspace/events"), { credentials: "same-origin", cache: "no-store", signal: current.signal,
        headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) } });
      if (!response.ok) throw await responseError(response);
      await read(response, frame => {
        if (frame.event === "access_revoked") throw new ApiError(401, "SESSION_EXPIRED", "Reconnect your workspace to continue.");
        if (frame.id) { cursor = frame.id; handlers.onCursor?.(cursor); }
        if (frame.event === "workspace_change") { handlers.onChange(JSON.parse(frame.data)); failures = 0; }
        if (frame.event === "resync_required") handlers.onChange(null);
        if (frame.event === "ready") { handlers.onState("Workspace live"); failures = 0; }
        if (frame.event === "connection_degraded") handlers.onState("Workspace connection reconnecting…");
      });
      await wait(300, signal);
    } catch (error) {
      if (signal.aborted) return;
      if (current.parked()) continue;
      if ((error instanceof ApiError && ![502, 503, 504, 429].includes(error.status)) || ++failures > 5) throw error;
      handlers.onState("Workspace connection reconnecting…");
      await wait(Math.min(1000 * 2 ** (failures - 1), 15000), signal);
    } finally { current.end(); }
  }
}

type Channel = Pick<BroadcastChannel, "postMessage" | "close"> & { onmessage: ((event: MessageEvent) => void) | null };
export type StreamSharing = { locks?: Pick<LockManager, "request"> | null; channel?: ((name: string) => Channel) | null };
type Message = { owner: string } & ({ type: "hello"; at: number } | { type: "state"; state: string; cursor: string }
  | { type: "changes"; changes: (WorkspaceEvent | null)[]; cursor: string } | { type: "failed"; status: number; code: string; message: string });
type Outgoing = Message extends infer M ? M extends Message ? Omit<M, "owner"> : never : never;
const browserSharing = (): StreamSharing => ({
  locks: typeof navigator === "undefined" ? null : navigator.locks,
  channel: typeof BroadcastChannel === "undefined" ? null : name => new BroadcastChannel(name),
});

/**
 * Workspace invalidations. With `owner`, every tab of that user in this browser shares one stream: the tab holding
 * the user's Web Lock streams and rebroadcasts on a BroadcastChannel, the others apply that. Waiting for the lock
 * and holding it both end after a minute hidden; a closed tab releases it and the next visible tab resumes from the
 * last broadcast cursor. A stream failure (revocation, repeated errors) rejects in every tab, and none of those tabs
 * reconnects until it starts again; they keep applying what a later tab streams. Without an owner, Web Locks or
 * BroadcastChannel, each tab streams for itself.
 */
export async function followWorkspace(options: StreamOptions & { onChange: (event: WorkspaceEvent | null) => void; owner?: string; sharing?: StreamSharing }) {
  const { signal, owner } = options, sharing = options.sharing || browserSharing(), page = options.visibility || pageVisibility();
  if (!owner || !sharing.locks || !sharing.channel) return streamWorkspace(signal, options, page);
  const name = "reveal:workspace-events:" + owner, channel = sharing.channel(name), locks = sharing.locks;
  let cursor = "", state = "", leading = false, unusable = false, failure: unknown = null, failedAt = Infinity, yieldLock = () => {};
  let batch: (WorkspaceEvent | null)[] = [], term: ReturnType<typeof attempt> | null = null, failed = () => {};
  const failing = new Promise<void>(resolve => { failed = resolve; });
  const post = (message: Outgoing) => { try { channel.postMessage({ ...message, owner }); } catch { /* closed */ } };
  const flush = () => { if (batch.length) post({ type: "changes", changes: batch, cursor }); batch = []; };
  const lead: WorkspaceHandlers = {
    onState: value => { state = value; options.onState(value); flush(); post({ type: "state", state: value, cursor }); },
    onCursor: value => { cursor = value; },
    onChange: change => { options.onChange(change); if (!batch.length) queueMicrotask(flush); batch.push(change); },
  };
  channel.onmessage = ({ data }: MessageEvent<Message>) => {
    if (!data || data.owner !== owner || signal.aborted) return;
    if (data.type === "failed") {
      if (!failure) { failure = data.status ? new ApiError(data.status, data.code, data.message) : new Error(data.message); failedAt = Date.now(); term?.abort(); failed(); }
      return;
    }
    if (data.type === "hello") { if (leading) post({ type: "state", state, cursor }); else if (data.at > failedAt) yieldLock(); return; }
    if (leading) return;
    if (data.type === "state") { cursor = data.cursor; state = data.state; if (data.state) options.onState(data.state); }
    if (data.type === "changes") { for (const change of data.changes) options.onChange(change); cursor = data.cursor; }
  };
  const close = () => { channel.onmessage = null; channel.close(); };
  post({ type: "hello", at: Date.now() });
  while (!signal.aborted && !failure) {
    await page.untilVisible(signal);
    if (signal.aborted || failure) break;
    const current = term = attempt(signal, page);
    const held = locks.request(name, { signal: current.signal }, async () => {
      if (current.signal.aborted || failure) return;
      leading = true;
      try { await streamWorkspace(current.signal, lead, alwaysVisible, cursor); }
      catch (error) {
        // A park or an unmount can interrupt a reconnect wait; only a real stream failure ends every tab.
        if (!current.signal.aborted) {
          failure = error; failedAt = Date.now(); flush();
          post({ type: "failed", status: error instanceof ApiError ? error.status : 0, code: error instanceof ApiError ? error.code : "STREAM_FAILED", message: errorMessage(error) });
          failed();
        }
      } finally { leading = false; flush(); }
      // The failed tab keeps the lock, so no tab that saw the failure reconnects; a tab started since takes over.
      if (failure) await new Promise<void>(resolve => { yieldLock = resolve; void untilAborted(signal).then(resolve); });
    });
    held.catch(() => {});
    try {
      await Promise.race([held, failing]);
    } catch {
      // An aborted wait is a park or an unmount; any other refusal means locks are unusable in this tab.
      if (!current.signal.aborted) { unusable = true; break; }
    } finally { current.end(); term = null; }
  }
  if (failure) { void untilAborted(signal).then(close); throw failure; }
  close();
  if (unusable && !signal.aborted) return streamWorkspace(signal, options, page, cursor);
}
