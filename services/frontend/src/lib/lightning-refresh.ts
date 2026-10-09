import { lightningAccessLost, lightningPending, type LightningAudit } from "./lightning-audit";

/** One serial status read, parked immediately while hidden. Only GET is retried. */
export function createLightningRefresher({ load, hidden, onAudit, onError, now = Date.now, timers = {
  set: (run: () => void, ms: number): unknown => setTimeout(run, ms), clear: (id: unknown) => clearTimeout(id as ReturnType<typeof setTimeout>),
} }: {
  load: (signal: AbortSignal, wait: number, afterRevision?: number) => Promise<LightningAudit>; hidden: () => boolean;
  onAudit: (audit: LightningAudit) => void; onError: (error: unknown) => void; now?: () => number;
  timers?: { set: (run: () => void, ms: number) => unknown; clear: (id: unknown) => void };
}) {
  let audit: LightningAudit | null = null, controller: AbortController | null = null;
  let stopped = false, again = false, queued = false, timer: unknown, failures = 0, unchanged = 0;
  const cancelTimer = () => { if (timer !== undefined) timers.clear(timer); timer = undefined; };
  const schedule = (ms: number) => { if (!stopped && !hidden()) timer = timers.set(() => { timer = undefined; void read(true); }, ms); };
  async function read(longPoll = false) {
    if (stopped || hidden()) return;
    cancelTimer();
    if (controller) { again = true; return; }
    const request = new AbortController(); controller = request;
    const started = now(), previous = audit ? `${audit.status}:${audit.updated_at}:${audit.progress?.revision ?? 0}` : null;
    try {
      const pending = !!audit && lightningPending(audit);
      const value = await load(request.signal, longPoll && pending ? 15 : 0, pending ? audit?.progress?.revision ?? 0 : undefined);
      if (stopped || request.signal.aborted) return;
      audit = value; failures = 0; unchanged = previous === `${value.status}:${value.updated_at}:${value.progress?.revision ?? 0}` ? unchanged + 1 : 0; onAudit(value);
    } catch (error) {
      if (stopped || request.signal.aborted) return;
      onError(error); failures++;
      if (lightningAccessLost(error)) stopped = true;
    } finally {
      controller = null;
      if (!stopped && !hidden()) {
        if (again) { again = false; void read(); }
        else if (failures) schedule(Math.min(60_000, 4000 * 2 ** Math.min(failures - 1, 4)));
        else if (audit && lightningPending(audit)) {
          // Stream revisions need a responsive detail read, independent of workspace list refreshes.
          const interval = audit.status === "assessing" ? (audit.progress && !unchanged ? 250 : 1000) : Math.min(4000, 1000 * Math.max(unchanged, 1));
          schedule(Math.max(0, interval - (now() - started)));
        }
      }
    }
  }
  function refresh() {
    if (queued || stopped) return;
    queued = true;
    queueMicrotask(() => { queued = false; void read(); });
  }
  return {
    refresh,
    visibility: () => {
      cancelTimer();
      if (hidden()) { again = false; controller?.abort(); }
      else refresh();
    },
    stop: () => { stopped = true; again = false; cancelTimer(); controller?.abort(); },
  };
}
