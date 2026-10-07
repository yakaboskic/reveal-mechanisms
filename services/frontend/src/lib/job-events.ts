/** One job's activity stream for one view: replays from its cursor, renews bounded streams and stops at a terminal job. */
import { ApiError, readEvents, terminal, type Schema } from "./client";
import { documentVisibility, streamAttempt, type PageVisibility } from "./page-visibility";

type Job = Schema<"Job">;
export type JobActivityView = {
  event: (event: Schema<"JobEvent">) => void;
  connection: (label: string) => void;
  error: (message: string) => void;
  /** GET /v1/jobs/{id}, applied to the view. */
  refresh: () => Promise<Job>;
  /** The job snapshot the view already holds. */
  known: () => Job;
};

function pause(ms: number, signal: AbortSignal) {
  return new Promise<void>(resolve => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

/**
 * A finished job's stream ends right after its history (the server no longer subscribes for it). A terminal event
 * already in the view's snapshot needs no job read, and the read a terminal event starts also answers whether to
 * reconnect, so a stream end never repeats it. A hidden tab parks its stream without counting a failure or reading
 * the job, and resumes from `cursor` once shown.
 */
export async function followJobEvents(signal: AbortSignal, jobId: string, cursor: { current: string }, view: JobActivityView,
  fetcher: typeof fetch = fetch, page: PageVisibility = documentVisibility()) {
  let failures = 0;
  while (!signal.aborted) {
    if (page.hidden()) { await page.untilVisible(signal); continue; }
    const attempt = streamAttempt(signal, page);
    try {
      view.connection(failures ? "Reconnecting to activity…" : "Retrieving job status and activity…");
      const response = await fetcher(`/api/backend/v1/jobs/${encodeURIComponent(jobId)}/events?after=${cursor.current}`, { headers: { Accept: "text/event-stream", "Last-Event-ID": cursor.current }, signal: attempt.signal });
      const ending: { settled: Promise<Job> | null } = { settled: null };
      await readEvents(response, event => {
        if (event.job_id !== jobId || BigInt(event.id) <= BigInt(cursor.current)) return;
        cursor.current = event.id; view.event(event); view.connection("Live activity"); failures = 0;
        if (!terminal(event.status)) { ending.settled = null; return; }
        const known = view.known();
        const settled = ending.settled = terminal(known.status) && BigInt(known.last_event_id) >= BigInt(event.id) ? Promise.resolve(known) : view.refresh();
        settled.catch(() => view.connection("Refreshing job status…"));
      });
      const current = await (ending.settled ? ending.settled.catch(() => view.refresh()) : view.refresh());
      if (terminal(current.status)) { view.connection(""); return; }
    } catch (failure) {
      if (signal.aborted) return;
      if (attempt.parked()) continue;
      if (failure instanceof ApiError && failure.code === "EVENT_CURSOR_EXPIRED") {
        try {
          const current = await view.refresh(); cursor.current = current.last_event_id;
          view.error("Earlier activity has expired. The saved job and scientific results remain available.");
          if (terminal(current.status)) return;
        } catch {
          failures++; view.connection("Connection interrupted. Reconnecting…");
          if (failures >= 6) { view.error("Activity could not reconnect. The job continues on the server."); return; }
        }
      } else {
        failures++; view.connection("Connection interrupted. Reconnecting…");
        try { const current = await view.refresh(); if (terminal(current.status)) return; } catch { /* preserve events while offline */ }
        if (failures >= 6) { view.error("Activity could not reconnect. The job continues on the server."); return; }
      }
    } finally { attempt.end(); }
    await pause(Math.min(1000 * 2 ** failures, 15000), signal);
  }
}
