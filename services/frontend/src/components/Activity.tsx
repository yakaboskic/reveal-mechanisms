"use client";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { activityProgress, activityRows, activitySections, activityStage, groupedWarnings, stageLabels, type ActivityRow } from "@/lib/activity";
import { prettyRecordedValue, toolInvocation } from "@/lib/tool-display";
import { api, ApiError, messageOf, readEvents, terminal, type Schema } from "@/lib/client";
import "./workspace-activity.css";
export function Pulse() { return <span className="pulse" aria-hidden="true"><i /><i /><i /></span>; }

function ToolActivity({ row, active, onInspect }: { row: ActivityRow; active: boolean; onInspect: () => void }) {
  const detail = row.event.detail!;
  const result = row.result?.detail || (detail.kind === "tool_result" ? detail : undefined);
  const state = result?.state || (active ? "started" : "unavailable");
  const output = result?.output_excerpt;
  const name = detail.tool_name || result?.tool_name || "Tool";
  const args = detail.display_arguments || result?.display_arguments;
  return <details className="activity-tool activity-tool-details" data-state={state}>
    <summary className="tool-heading" onClick={onInspect}>
      <code className="tool-invocation">{toolInvocation(name, args)}</code>
      <span className="tool-status"><span className="tool-state">{state === "failed" ? "Failed" : state === "completed" ? "Returned" : state === "started" ? "Calling" : "No result recorded"}</span>{result?.duration_ms != null && <span className="tool-duration">{(result.duration_ms / 1000).toFixed(1)}s</span>}</span>
      <span className="tool-caret" aria-hidden="true">›</span><span className="sr-only">Show tool details</span>
    </summary>
    <div className="tool-details-body">
      <dl className="tool-metadata"><div><dt>Tool</dt><dd>{name}</dd></div>{detail.call_id && <div><dt>Call ID</dt><dd>{detail.call_id}</dd></div>}</dl>
      <div className="tool-arguments"><span className="tool-detail-label">Recorded arguments</span><pre>{args ? toolInvocation(name, args, false) : "Arguments were not recorded."}</pre></div>
      {result && <div className="tool-output"><span className="tool-detail-label">Result excerpt</span>{output ? <pre>{prettyRecordedValue(output)}</pre> : <p>{state === "failed" ? row.result?.message || row.event.message : "Tool completed. No result preview was recorded."}</p>}
        {result.artifact_sha256 && <small className="tool-artifact">Result SHA-256: {result.artifact_sha256}</small>}
      </div>}
    </div>
  </details>;
}

function ActivityEntry({ row, active, onInspect }: { row: ActivityRow; active: boolean; onInspect: () => void }) {
  const event = row.event;
  if (event.detail?.kind === "tool_call" || event.detail?.kind === "tool_result") return <ToolActivity row={row} active={active} onInspect={onInspect} />;
  const narrative = event.detail?.kind === "agent_message";
  return <div className={`activity-entry ${narrative ? "agent-update" : "system-update"} ${event.detail?.state || ""}`}>
    {narrative && <span className="entry-label">Agent update</span>}
    <p>{event.message}</p>
    {event.detail?.counts && <small>{event.detail.counts.nodes} nodes, {event.detail.counts.edges} edges ({event.detail.counts.scope})</small>}
  </div>;
}
export function Activity({ initial, onJob }: { initial: Schema<"Job">; onJob: (job: Schema<"Job">) => void }) {
  const [job, setJob] = useState(initial);
  const [events, setEvents] = useState<Schema<"JobEvent">[]>([]);
  const [connection, setConnection] = useState("Connecting to activity…");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const [following, setFollowing] = useState(true);
  const historyId = useId();
  const cursor = useRef("0"); const jobRef = useRef(initial); const callback = useRef(onJob); callback.current = onJob;
  const feed = useRef<HTMLDivElement>(null); const content = useRef<HTMLDivElement>(null); const follow = useRef(true);
  const update = (value: Schema<"Job">) => { jobRef.current = value; setJob(value); callback.current(value); };
  useEffect(() => {
    // A saved browser snapshot can mount before the explicit job fetch returns.
    // Accept that fresher snapshot without restarting or discarding the stream.
    if (initial !== jobRef.current && BigInt(initial.last_event_id) >= BigInt(jobRef.current.last_event_id)) {
      jobRef.current = initial; setJob(initial);
    }
  }, [initial]);
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
  const progress = activityProgress(job, events);
  const complete = progress.status === "succeeded";
  const active = !terminal(progress.status);
  const sections = activitySections(events);
  const currentStage = activityStage(progress.stage);
  // During replay, the fetched job can be ahead of the history received so far.
  if (sections.at(-1)?.stage !== currentStage) sections.push({ id: "pending", stage: currentStage, events: [] });
  const jumpToLatest = () => {
    follow.current = true; setFollowing(true);
    if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  };
  const pauseFollowing = () => { follow.current = false; setFollowing(false); };
  useLayoutEffect(() => {
    if (follow.current && feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [events, expanded]);
  useEffect(() => {
    const element = feed.current;
    if (!element || !content.current) return;
    const resize = () => {
      // Document position is stable while the page scrolls. Reserve room below
      // the panel; a long question on mobile still leaves a useful log viewport.
      const top = element.getBoundingClientRect().top + window.scrollY;
      const height = Math.max(180, Math.min(620, window.innerHeight * .65, window.innerHeight - top - 76));
      element.style.setProperty("--activity-height", `${height}px`);
      if (follow.current) element.scrollTop = element.scrollHeight;
    };
    const observer = new ResizeObserver(resize);
    observer.observe(content.current);
    if (element.parentElement?.parentElement) observer.observe(element.parentElement.parentElement);
    window.addEventListener("resize", resize); resize();
    return () => { observer.disconnect(); window.removeEventListener("resize", resize); };
  }, [expanded, complete]);
  const cancel = async () => { try { update(await api.cancel(job.id)); } catch (failure) { setError(messageOf(failure)); } };
  return <section className={`activity reveal-activity ${active ? "is-running" : "is-terminal"} ${complete ? "is-complete" : ""}`} aria-label="Research activity">
    {complete && <button className="complete-disclosure" aria-expanded={expanded} aria-controls={historyId} onClick={() => setExpanded(!expanded)}><span className="completion-check" aria-hidden="true">✓</span>Gap analysis complete <span className="completion-caret" aria-hidden="true">›</span></button>}
    {(!complete || expanded) && <>
      {!complete && <div className="activity-header"><div><h2>{active ? "Agent activity" : progress.status === "cancelled" ? "Research stopped" : progress.status === "insufficient_evidence" ? "Insufficient evidence" : "Research could not complete"}</h2>{active && <p className="connection" role="status">{connection}</p>}</div>
        {active && <button className="stop" onClick={cancel} disabled={progress.status === "cancel_requested"}><span className="stop-square" aria-hidden="true" />{progress.status === "cancel_requested" ? "Stopping…" : "Stop"}</button>}
      </div>}
      <div id={historyId} className="activity-history" ref={feed} onScroll={() => {
        const element = feed.current!;
        const atTail = element.scrollHeight - element.scrollTop - element.clientHeight < 48;
        follow.current = atTail; setFollowing(atTail);
      }} tabIndex={0} role="region" aria-label="Activity history">
        <div className="activity-content" ref={content}>
          {sections.map((section, index) => {
            const current = index === sections.length - 1;
            const working = active && current;
            const state = working ? "working" : current && progress.status === "failed" ? "failed" : current && ["cancelled", "insufficient_evidence"].includes(progress.status) ? "stopped" : "completed";
            const label = working ? progress.status === "cancel_requested" ? "Stopping" : section.stage === "preparation" && progress.stage === "queued" ? "Queued" : "Working" : state === "failed" ? "Could not complete" : state === "stopped" ? "Stopped" : "Complete";
            return <details className="activity-stage" data-stage={section.stage} data-state={state} key={section.id} open={current || section.stage === "research" || section.stage === "validation"}>
              <summary>{working ? <Pulse /> : <span className="stage-mark" aria-hidden="true">{state === "completed" ? "✓" : state === "failed" ? "!" : "·"}</span>}<span className="stage-name">{stageLabels[section.stage]}</span><span className="stage-state">{label}</span></summary>
              <div className="stage-log" role="log" aria-live="off" aria-label={`${stageLabels[section.stage]} log`}>
                {!section.events.length && <p className="activity-waiting">{progress.stage === "queued" ? "Waiting for a research worker…" : active ? "Waiting for activity…" : "No further activity recorded."}</p>}
                {activityRows(section.events.filter(event => event.event_type !== "warning")).map(row => <ActivityEntry key={row.event.id} row={row} active={working} onInspect={pauseFollowing} />)}
              </div>
            </details>;
          })}
          {groupedWarnings(job.warnings, events).map(({ message, count }) => <p key={message} className="notice">{message}{count > 1 && <small>{count} occurrences</small>}</p>)}
        </div>
      </div>
      <div className="activity-follow">{following ? <span>{active ? "Following live activity" : "End of activity"}</span> : <><span>Auto-follow paused</span><button onClick={jumpToLatest}>Jump to latest <span aria-hidden="true">↓</span></button></>}</div>
    </>}
    {job.failure && <p className="error" role="alert">{job.failure.message}</p>}
    {progress.status === "insufficient_evidence" && <p className="notice">The available evidence did not support a scientific account. Your selected gap, anchors, and activity are retained.</p>}
    {error && <div className="error" role="alert">{error} <button onClick={() => { setError(""); setRetry(n => n + 1); }}>Reconnect</button></div>}
  </section>;
}
