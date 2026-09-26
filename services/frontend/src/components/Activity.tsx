"use client";
import { useEffect, useId, useRef, useState } from "react";
import { coalesceMessageDeltas } from "@/lib/activity";
import { api, ApiError, messageOf, readEvents, terminal, type Schema } from "@/lib/client";
import "./workspace-activity.css";
export function Pulse() { return <span className="pulse" aria-hidden="true"><i /><i /><i /></span>; }
export function Activity({ initial, onJob }: { initial: Schema<"Job">; onJob: (job: Schema<"Job">) => void }) {
  const [job, setJob] = useState(initial);
  const [events, setEvents] = useState<Schema<"JobEvent">[]>([]);
  const [connection, setConnection] = useState("Connecting to activity…");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const historyId = useId();
  const cursor = useRef("0"); const jobRef = useRef(initial); const callback = useRef(onJob); callback.current = onJob;
  const feed = useRef<HTMLDivElement>(null); const follow = useRef(true);
  const update = (value: Schema<"Job">) => { jobRef.current = value; setJob(value); callback.current(value); };
  useEffect(() => {
    const controller = new AbortController(); let failures = 0;
    const refresh = async () => { const value = await api.job(initial.id); if (!controller.signal.aborted) update(value); return value; };
    const run = async () => {
      while (!controller.signal.aborted) {
        try {
          setConnection(failures ? "Reconnecting to activity…" : "Live activity");
          const response = await fetch(`/api/backend/v1/jobs/${encodeURIComponent(initial.id)}/events?after=${cursor.current}`, { headers: { Accept: "text/event-stream", "Last-Event-ID": cursor.current }, signal: controller.signal });
          await readEvents(response, event => {
            if (event.job_id !== initial.id || BigInt(event.id) <= BigInt(cursor.current)) return;
            cursor.current = event.id; setEvents(existing => [...existing, event]); setConnection("Live activity"); failures = 0;
            if (terminal(event.status)) void refresh();
          });
          const current = await refresh(); if (terminal(current.status)) { setConnection(""); break; }
        } catch (failure) {
          if (controller.signal.aborted) break;
          if (failure instanceof ApiError && failure.code === "EVENT_CURSOR_EXPIRED") {
            const current = await refresh(); cursor.current = current.last_event_id;
            setError("Earlier activity has expired. The saved job and scientific results remain available.");
            if (terminal(current.status)) break;
          } else {
            failures++; setConnection("Connection interrupted. Reconnecting…");
            try { const current = await refresh(); if (terminal(current.status)) break; } catch { /* preserve events while offline */ }
            if (failures >= 6) { setError("Activity could not reconnect. The job continues on the server."); break; }
          }
        }
        await new Promise<void>(resolve => { const timer = setTimeout(resolve, Math.min(1000 * 2 ** failures, 15000)); controller.signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true }); });
      }
    };
    void run(); return () => controller.abort();
  }, [initial.id, retry]);
  useEffect(() => { if (follow.current && feed.current) feed.current.scrollTop = feed.current.scrollHeight; }, [events]);
  const cancel = async () => { try { update(await api.cancel(job.id)); } catch (failure) { setError(messageOf(failure)); } };
  const complete = job.status === "succeeded"; const active = !terminal(job.status);
  const preparation = events.filter(e => ["queued", "freezing_inputs", "retrieving_cfde", "preparing_evidence"].includes(e.stage));
  const agent = events.filter(e => !preparation.includes(e));
  return <section className={`activity reveal-activity ${active ? "is-running" : "is-terminal"} ${complete ? "is-complete" : ""}`} aria-label="Research activity">
    {complete && <button className="complete-disclosure" aria-expanded={expanded} aria-controls={historyId} onClick={() => setExpanded(!expanded)}><span className="completion-check" aria-hidden="true">✓</span>Gap analysis complete <span className="completion-caret" aria-hidden="true">›</span></button>}
    {(!complete || expanded) && <>
      {!complete && <div className="activity-header"><div><h2>{active ? "Agent activity" : job.status === "cancelled" ? "Research stopped" : job.status === "insufficient_evidence" ? "Insufficient evidence" : "Research could not complete"}</h2>{active && <p className="connection" role="status">{connection}</p>}</div>
        {active && <button className="stop" onClick={cancel} disabled={job.status === "cancel_requested"}><span className="stop-square" aria-hidden="true" />{job.status === "cancel_requested" ? "Stopping…" : "Stop"}</button>}
      </div>}
      <div id={historyId} className="activity-history" ref={feed} onScroll={() => { const el = feed.current!; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }} tabIndex={0} role="region" aria-label="Activity history">
        <details className="preparation" open={!agent.length}><summary>{active && !agent.length && <Pulse />}<span>Evidence preparation</span>{!!agent.length && <span className="preparation-status">Ready</span>}</summary><ol className="preparation-log">{preparation.map(event => <li key={event.id}><span className="preparation-mark" aria-hidden="true">{event.detail?.state === "completed" ? "✓" : "·"}</span><div>{event.message}{event.detail?.counts && <small>{event.detail.counts.nodes} nodes, {event.detail.counts.edges} edges ({event.detail.counts.scope})</small>}</div></li>)}</ol></details>
        {!!agent.length && <details className="agent-workspace" open><summary>{active && <Pulse />}<span>Research agent</span><span className="agent-state-label">{active ? job.status === "cancel_requested" ? "Stopping" : "Working" : complete ? "Complete" : job.status === "failed" ? "Failed" : job.status === "insufficient_evidence" ? "Finished" : "Stopped"}</span></summary><div className="transcript" role="log" aria-live="off" aria-label="Observable agent messages and tools">
          {coalesceMessageDeltas(agent).map(event => <div className={`transcript-event ${event.detail?.state || ""}`} key={event.id}><span className="event-dot" aria-hidden="true" /><div><p>{event.message}</p>{event.detail?.tool_name && <code>{event.detail.tool_name} {event.detail.display_arguments}</code>}{event.detail?.output_excerpt && <pre>{event.detail.output_excerpt}</pre>}{event.detail?.duration_ms != null && <small>{(event.detail.duration_ms / 1000).toFixed(1)}s</small>}</div></div>)}
        </div></details>}
        {job.warnings.map(warning => <p key={warning} className="notice">{warning}</p>)}
      </div>
    </>}
    {job.failure && <p className="error" role="alert">{job.failure.message}</p>}
    {job.status === "insufficient_evidence" && <p className="notice">The available evidence did not support a scientific account. Your selected gap, anchors, and activity are retained.</p>}
    {error && <div className="error" role="alert">{error} <button onClick={() => { setError(""); setRetry(n => n + 1); }}>Reconnect</button></div>}
  </section>;
}
