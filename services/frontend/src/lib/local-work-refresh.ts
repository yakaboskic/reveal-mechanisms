import type { LocalWork } from "./local-work";

/**
 * Local research is refreshed by workspace events ('jobs' carries local work and submission changes),
 * by returning to the page, and by a slow fallback for changes that are not pushed (grants, activity)
 * or a disconnected stream. Hidden pages make no requests.
 */
export const localWorkFallbackMs = 60_000;
/** Preparation is pushed too; a short backoff covers the time before the stream connects. */
export const localWorkPreparingMs = [2_000, 4_000, 8_000, 16_000] as const;
export const localWorkRetryMs = [4_000, 8_000, 16_000, 32_000, 60_000] as const;

/** Delay before the next fallback read, or null when only events and page returns refresh it. */
export function nextLocalWorkRefresh(state: LocalWork["state"] | null, preparingReads: number, failures: number): number | null {
  if (failures > 0) return localWorkRetryMs[Math.min(failures, localWorkRetryMs.length) - 1];
  if (state === "closed" || state === "preparation_failed") return null;
  if (state === "preparing") return localWorkPreparingMs[Math.min(Math.max(preparingReads, 1), localWorkPreparingMs.length) - 1];
  return localWorkFallbackMs;
}

type Timers = { set: (run: () => void, ms: number) => unknown; clear: (timer: unknown) => void };
const browserTimers: Timers = { set: (run, ms) => setTimeout(run, ms), clear: timer => clearTimeout(timer as ReturnType<typeof setTimeout>) };

/** Serial reads: requests in one task coalesce, and a request during a read schedules exactly one more. */
export function createLocalWorkRefresher({ load, hidden, terminal, timers = browserTimers }: {
  load: () => Promise<Pick<LocalWork, "state">>; hidden: () => boolean; terminal: (error: unknown) => boolean; timers?: Timers;
}) {
  let timer: unknown, queued = false, reading = false, again = false, stopped = false;
  let state: LocalWork["state"] | null = null, preparingReads = 0, failures = 0;
  const cancel = () => { if (timer !== undefined) timers.clear(timer); timer = undefined; };
  async function read() {
    cancel();
    if (stopped) return;
    if (hidden()) return;  // Returning to the page refreshes immediately.
    if (reading) { again = true; return; }
    reading = true;
    try {
      const value = await load();
      if (stopped) return;
      state = value.state; preparingReads = state === "preparing" ? preparingReads + 1 : 0; failures = 0;
    } catch (error) {
      if (stopped) return;
      if (terminal(error)) { stopped = true; return; }
      failures++;
    } finally { reading = false; }
    if (again) { again = false; return read(); }
    const delay = nextLocalWorkRefresh(state, preparingReads, failures);
    if (delay !== null) timer = timers.set(() => { timer = undefined; refresh(); }, delay);
  }
  function refresh() {
    if (queued || stopped) return;
    queued = true;
    queueMicrotask(() => { queued = false; void read(); });
  }
  return {
    refresh,
    visible: () => { if (!hidden()) refresh(); },
    stop: () => { stopped = true; cancel(); },
  };
}
