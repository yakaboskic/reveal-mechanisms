import { ApiError, backend, responseError } from "./api";
import { terminal, type JobEvent, type WorkspaceEvent } from "./types";

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
type StreamOptions = { signal: AbortSignal; onState: (state: string) => void };
export async function followJob(jobId: string, options: StreamOptions & { onEvent: (event: JobEvent) => void;
  onResync: () => Promise<{ cursor: string; terminal: boolean }> }) {
  let cursor = "0", failures = 0;
  while (!options.signal.aborted) {
    try {
      const response = await fetch(backend("jobs/" + encodeURIComponent(jobId) + "/events"), {
        headers: { Accept: "text/event-stream", "Last-Event-ID": cursor }, credentials: "same-origin", cache: "no-store", signal: options.signal,
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
      if ((error instanceof ApiError && ![502, 503, 504, 429].includes(error.status)) || ++failures > 5) throw error;
      options.onState("Connection interrupted; reconnecting…");
      await wait(Math.min(1000 * 2 ** (failures - 1), 15000), options.signal);
    }
  }
}
export async function followWorkspace(options: StreamOptions & { onChange: (event: WorkspaceEvent | null) => void }) {
  let cursor = "", failures = 0;
  while (!options.signal.aborted) {
    try {
      const response = await fetch(backend("me/workspace/events"), { credentials: "same-origin", cache: "no-store", signal: options.signal,
        headers: { Accept: "text/event-stream", ...(cursor ? { "Last-Event-ID": cursor } : {}) } });
      if (!response.ok) throw await responseError(response);
      await read(response, frame => {
        if (frame.event === "access_revoked") throw new ApiError(401, "SESSION_EXPIRED", "Reconnect your workspace to continue.");
        if (frame.id) cursor = frame.id;
        if (frame.event === "workspace_change") { options.onChange(JSON.parse(frame.data)); failures = 0; }
        if (frame.event === "resync_required") options.onChange(null);
        if (frame.event === "ready") { options.onState("Workspace live"); failures = 0; }
        if (frame.event === "connection_degraded") options.onState("Workspace connection reconnecting…");
      });
      await wait(300, options.signal);
    } catch (error) {
      if (options.signal.aborted) return;
      if ((error instanceof ApiError && ![502, 503, 504, 429].includes(error.status)) || ++failures > 5) throw error;
      options.onState("Workspace connection reconnecting…");
      await wait(Math.min(1000 * 2 ** (failures - 1), 15000), options.signal);
    }
  }
}
