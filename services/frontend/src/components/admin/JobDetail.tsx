"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { coalesceMessageDeltas } from "@/lib/activity";
import { jobKindLabel, jobLabel, refusalNote, type AdminJob, type JobDetail, type JobRunMetrics, type TranscriptPage } from "@/lib/admin-job";
import { appendPage } from "@/lib/admin-transcript";
import { formatAdminDate as date, formatAdminDuration as duration, formatAdminJson } from "@/lib/admin-time";
import { jobHref } from "@/lib/admin-url";
import { NO_RUN, PromptSettingsSection, ResponseSection, RunButton, type RunState } from "./JobContent";
import { NO_TRANSCRIPT, TranscriptSection, type TranscriptState } from "./JobTranscript";
import { useTelemetry } from "./Telemetry";
import { Callout, CopyButton, Empty, Json, KeyValues, Owner, PageHeading, Panel, StateStatus, statusTone } from "./ui";

// One job at /admin/jobs/[id], in nine sections. The job record (logs, diagnostics, settings, sizes) is read under the
// telemetry assertion and refreshes every 15 seconds while the job runs. The prompt and response (/run) and the
// transcript are recorded reads: each loads only when asked, once per press, and never with the refresh.

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);
const json = (value: unknown) => formatAdminJson(JSON.stringify(value, null, 2));
const usd = (value: number | null | undefined) => value == null ? "—" : `$${value.toFixed(4)}`;
const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const of = (value: string, limit: string | null) => limit ? `${value} of ${limit}` : value;

/** The nine sections, in order: their titles are the panels' names. */
export const JOB_SECTIONS = ["Summary", "Prompt and settings", "Response", "Transcript", "Event log", "Usage", "Timings", "Sandbox", "Diagnostics, attempts and warnings"] as const;

/** The Box console link from REVEAL_BOX_CONSOLE_URL_TEMPLATE (an https URL with `{box_id}`), or null when none is set. */
export function boxConsoleUrl(template: string | null | undefined, boxId: string | null | undefined): string | null {
  if (!template || !boxId || !/^https:\/\//.test(template) || !template.includes("{box_id}")) return null;
  return template.replaceAll("{box_id}", encodeURIComponent(boxId));
}

/** Section 1: a status callout (with the failure, its recorded cause and saved failure.json files), then the times. */
function SummarySection({ data }: { data: JobDetail }) {
  const { job, metrics } = data;
  const failures = data.diagnostics.filter(d => d.available && d.path.endsWith("/failure.json"));
  const tone = job.failure ? "bad" : statusTone(job.status);
  return <>
    <Callout tone={tone} role={job.failure ? "alert" : "status"}>
      <p><StateStatus value={job.status} /> <span className="admin-log-stage">{job.stage.replaceAll("_", " ")}</span></p>
      {job.failure && <div className="admin-job-failure"><h3>{job.failure.code}</h3><p>{job.failure.message}</p>{metrics?.error && <div><strong>{metrics.error.error_type}{metrics.error.phase ? ` · ${metrics.error.phase.replaceAll("_", " ")}` : ""}</strong><p>{metrics.error.message || "No message was recorded."}</p><small>Recorded cause · {date(metrics.error.recorded_at)}</small></div>}{failures.map(item => {
        const failure = item.data as { phase?: string; error_type?: string; message?: string };
        return <div key={item.path}><strong>{failure.error_type || "Saved failure"}{failure.phase ? ` · ${failure.phase.replaceAll("_", " ")}` : ""}</strong><p>{failure.message}</p><small>{item.path}</small></div>;
      })}</div>}
    </Callout>
    <KeyValues rows={[
      ["Kind", jobLabel(job)],
      ["Owner", <Owner key="owner" id={job.owner} owners={job.owner_label ? { [job.owner]: job.owner_label } : undefined} length={36} />],
      ...(job.api_key ? [["Started with API key", <span key="api-key">{job.api_key.label || "Unlabelled key"} <span className="admin-mono">{job.api_key.prefix ? `${job.api_key.prefix}…` : ""}</span></span>] as [string, React.ReactNode]] : []),
      ...([["Created", job.created_at], ["Started", job.started_at], ["Updated", job.updated_at], ["Finished", job.completed_at], ["Current attempt started", job.attempt_started_at]] as const).map(([label, value]) => [label, date(value)] as [string, React.ReactNode]),
      ["Safety classifier", job.refusals == null ? "Not recorded for this run" : refusalNote(job) ? <span key="refusals" className="admin-warning">{refusalNote(job)}. A stopped response is withheld from the run, so the answer may be incomplete.</span> : "No response was stopped"],
      ["Worker", job.worker || "—"],
      ["Box", job.box_id || "No Box runtime recorded"],
      ["Attempt / recoveries", `${job.attempt} / ${job.recoveries}${job.delivery_recoveries ? ` · ${job.delivery_recoveries} delivery recoveries` : ""}${job.handoffs ? ` · ${job.handoffs} handoffs` : ""}`],
    ]} />
    <Json value={job} label="Raw job JSON" />
  </>;
}

/** Section 5: today's event log, unchanged: search, load earlier, copy, tool arguments and output, event data. */
function EventLogSection({ data, loading, active, auto, olderLoaded, onOlder }: { data: JobDetail; loading: boolean; active: boolean; auto: boolean; olderLoaded: boolean; onOlder: () => void }) {
  const [search, setSearch] = useState("");
  const [copied, setCopied] = useState("");
  useEffect(() => setCopied(""), [data]);
  const events = coalesceMessageDeltas(data.events).filter(e => `${e.message} ${e.stage} ${e.event_type} ${JSON.stringify(e.detail)}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <div className="admin-section-heading"><p>{data.events.length} stored events loaded{data.next_before ? " · earlier events available" : " · beginning of retained history"}. {active ? auto && !olderLoaded ? "Live logs refresh every 15s." : "Refresh logs to see current activity." : "Job finished."}</p><input className="field" aria-label="Search job logs" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search loaded messages, tools or stages" /></div>
    <div className="admin-log-actions"><button className="submit" disabled={loading || !data.next_before} onClick={onOlder}>{loading ? "Loading…" : "Load earlier events"}</button><button className="text-button" onClick={async () => { try { await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); setCopied("Copied"); } catch { setCopied("Copy is unavailable in this browser."); } }}>{copied || "Copy loaded logs"}</button><span>Oldest to newest · adjacent agent text fragments are combined</span></div>
    <div className="admin-event-log" role="region" aria-label="Job event history" tabIndex={0}>
      {events.map(event => <article className={`admin-log-event ${event.event_type === "failure" || event.detail?.state === "failed" ? "failed" : ""}`} key={event.id}>
        <div className="admin-log-meta"><time>{date(event.occurred_at)}</time><span>#{event.id} · {event.detail?.kind || event.event_type}</span><span>{event.stage.replaceAll("_", " ")}{event.detail?.state ? ` · ${event.detail.state}` : ""}</span></div>
        <p>{event.message}</p>
        {event.detail?.tool_name && <strong className="admin-tool-name">{event.detail.tool_name}{event.detail.selected_kg ? ` · ${event.detail.selected_kg}` : ""}</strong>}
        {event.detail?.display_arguments && <details><summary>Tool arguments</summary><pre>{event.detail.display_arguments}</pre></details>}
        {event.detail?.output_excerpt && <details open={event.detail.state === "failed"}><summary>Tool output</summary><pre>{event.detail.output_excerpt}</pre></details>}
        <details><summary>Event data</summary><pre>{json(event)}</pre></details>
      </article>)}
      {!events.length && <p className="admin-empty">{search ? "No loaded events match this search." : "No retained events are available for this job."}</p>}
    </div>
  </>;
}

/** Section 6: everything today's AgentUsage showed. */
function UsageSection({ metrics }: { metrics: JobRunMetrics | null | undefined }) {
  const agent = metrics?.agent;
  if (!metrics || !agent) return <Empty>No agent usage was recorded for this job. A run records it when its agent finishes; jobs from before cost telemetry may have none.</Empty>;
  const rows: [string, string][] = [
    ["Agent cost", agent.cost_usd != null ? `${usd(agent.cost_usd)} (provider reported)` : agent.estimated_cost_usd != null ? `≈${usd(agent.estimated_cost_usd)} estimated from streamed tokens; the run stopped before the provider reported its total` : "Not reported: the run stopped before the provider reported its total"],
    ["Budget used", agent.budget_used == null ? of("—", agent.budget_usd == null ? null : usd(agent.budget_usd)) : `${Math.round(agent.budget_used * 100)}% of ${usd(agent.budget_usd)}`],
    ["Turns", of(count(agent.turns), agent.turn_limit == null ? null : count(agent.turn_limit))],
    ["Agent run time", of(duration(agent.elapsed_seconds), agent.time_limit_seconds == null ? null : duration(agent.time_limit_seconds))],
    ["Provider API time", duration(agent.api_seconds)],
    ["Tokens: output · input", agent.tokens ? `${count(agent.tokens.output)} · ${count(agent.tokens.input)}` : "—"],
    ["Tokens: cache write · cache read", agent.tokens ? `${count(agent.tokens.cache_write)} · ${count(agent.tokens.cache_read)}` : "—"],
    ["Tool calls (failed)", agent.tool_calls == null ? "—" : `${count(agent.tool_calls)} (${count(agent.tool_failures)})`],
    ["Lint checks · draft writes", `${count(agent.lint_checks)} · ${count(agent.draft_writes)}`],
    ["Model", agent.model || "—"],
    ["Run outcome", [agent.status, agent.subtype].filter(Boolean).join(" · ") || "—"],
  ];
  return <div className="admin-agent-usage">
    <p><strong>Agent cost and usage · {agent.cost_usd != null ? usd(agent.cost_usd) : agent.estimated_cost_usd != null ? `≈${usd(agent.estimated_cost_usd)} est.` : "not reported"}</strong></p>
    {agent.budget_used != null && <div className="admin-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, 100 * agent.budget_used)}%` }} /></div>}
    <dl className="admin-detail-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {agent.reason && <p>Runner reason: {agent.reason}</p>}
    {metrics.parent_job_id && <p>Research statement for <a href={jobHref(metrics.parent_job_id)}>{metrics.parent_job_id}</a>.</p>}
    <Json value={metrics} label="Raw metrics JSON" />
  </div>;
}

/** Section 7: today's phase-timing bars. */
function TimingsSection({ job }: { job: AdminJob }) {
  return <div className="admin-duration-detail">
    <p><strong>Wall time · {duration(job.wall_seconds)} total</strong></p>
    <div className="admin-timings">{([['Queue / pre-attempt', job.queue_seconds], ['Attempt wall time', job.attempt_seconds], ['Box lifetime', job.box_seconds], ['Box setup', job.setup_seconds], ['Agent execution', job.agent_seconds], ['Artifact capture', job.capture_seconds], ['Box cleanup', job.cleanup_seconds]] as [string, number | null][]).map(([label, value]) => <div key={label}><span>{label}</span><div className="admin-bar"><i style={{ width: value == null ? 0 : `${Math.min(100, 100 * value / Math.max(job.wall_seconds || 1, 1))}%` }} /></div><strong>{duration(value)}</strong></div>)}</div>
    <p>Phase timings depend on instrumentation available when the job ran. Agent time includes polling and event delivery. Queue time ends at the first worker claim.</p>
  </div>;
}

/** Section 8: the Box this job ran in. The console link needs REVEAL_BOX_CONSOLE_URL_TEMPLATE; snapshots are Phase 4. */
function SandboxSection({ data, boxConsole }: { data: JobDetail; boxConsole: string | null }) {
  const { job } = data;
  const link = boxConsoleUrl(boxConsole, job.box_id);
  const boxes = [...new Set(data.attempts.map(a => a["remote_handle.box_id"]).filter((id): id is string => !!id))];
  return <KeyValues rows={[
    ["Box", job.box_id ? <span key="box" className="admin-short-id"><code>{job.box_id}</code><CopyButton text={job.box_id} /></span> : "No Box runtime recorded"],
    ["Box phase", job.box_phase || "—"],
    ["Box lifetime", duration(job.box_seconds)],
    ["Console", !job.box_id ? "—" : link ? <a href={link} target="_blank" rel="noreferrer noopener">Open this Box in the Box console</a>
      : "No console link is configured. Copy the id above; the link to the Box console comes with Phase 4 (REVEAL_BOX_CONSOLE_URL_TEMPLATE)."],
    ["Boxes by attempt", boxes.length ? data.attempts.filter(a => a["remote_handle.box_id"]).map(a => `attempt ${a.attempt}: ${a["remote_handle.box_id"]}`).join(" · ") : "None recorded"],
    ["Snapshot", "Not yet: Box snapshots come with Phase 4."],
  ]} />;
}

/** Section 9: warnings, saved diagnostics and the attempt history, as today. */
function DiagnosticsSection({ data }: { data: JobDetail }) {
  const { job } = data;
  return <>
    {job.warnings?.length ? <details><summary>Warnings ({job.warnings.length})</summary><ul>{job.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details> : <p className="admin-muted">No warnings.</p>}
    <h3>Saved diagnostics</h3><p>Worker errors, token measurements and validation reports available from the artifact store.</p>
    {data.diagnostics.map(item => <details className="admin-diagnostic" key={item.path} open={item.path.endsWith("failure.json")}><summary>{item.path}</summary>{item.available ? <pre>{json(item.data)}</pre> : <p>{item.reason}</p>}</details>)}
    {!data.diagnostics.length && <p>No saved diagnostics are available on this server. Older jobs may only have the persisted events above.</p>}
    {data.diagnostics_limited && <p>Showing the first 20 saved diagnostic files.</p>}
    <details className="admin-attempt-history"><summary>Attempt history ({data.attempts.length}{data.attempts_limited ? "+" : ""})</summary><div className="admin-table-scroll"><table><thead><tr><th>Attempt</th><th>Started</th><th>Worker at claim</th><th>Box at claim</th></tr></thead><tbody>{data.attempts.map(attempt => <tr key={attempt.attempt}><td>{attempt.attempt}</td><td>{date(attempt.started_at)}</td><td>{attempt.worker_id || "—"}</td><td>{attempt["remote_handle.box_id"] || "—"}</td></tr>)}</tbody></table></div>{data.attempts_limited && <p>Latest 50 attempt snapshots.</p>}</details>
  </>;
}

const noop = () => {};
export type JobDetailViewProps = {
  jobId: string; data: JobDetail | null; error?: string; loading?: boolean; auto?: boolean; olderLoaded?: boolean;
  onRefresh?: () => void; onOlder?: () => void; boxConsole?: string | null;
  run?: RunState; onLoadRun?: () => void;
  transcript?: TranscriptState; onLoadTranscript?: (attempt?: number) => void; onMoreTranscript?: () => void;
};

/** The page itself, from state the container holds: nine panels, each with a plain-language description. */
export function JobDetailView({ jobId, data, error = "", loading = false, auto = true, olderLoaded = false, onRefresh = noop, onOlder = noop, boxConsole = null,
  run = NO_RUN, onLoadRun = noop, transcript = NO_TRANSCRIPT, onLoadTranscript = noop, onMoreTranscript = noop }: JobDetailViewProps) {
  const job = data?.job;
  const active = job ? !TERMINAL.has(job.status) : false;
  const agentJob = job?.kind === "agent";
  const notAgent = job ? <Empty>Only general agent jobs record this. This job is a {jobKindLabel(job.kind)}: its inputs and progress are in the event log and saved diagnostics.</Empty> : null;
  return <div className="admin-job-page">
    <PageHeading title={`Job ${jobId.slice(0, 8)}`} subtitle={<>{job ? `${jobLabel(job)} · ` : ""}<span className="admin-short-id"><code>{jobId}</code><CopyButton text={jobId} /></span></>}
      actions={<><button type="button" className="submit" disabled={loading} onClick={onRefresh}>Refresh logs</button><Link className="text-button" href="/admin/jobs">Back to jobs</Link></>} />
    {!data && loading && <p className="admin-loading" role="status">Loading job logs and diagnostics…</p>}
    {error && <Callout tone="bad" role="alert">{error}{data && " Showing previously loaded logs."}</Callout>}
    {data && job && <>
      <Panel title="Summary" padded description={`Where the job stands, why it failed if it did, and when each step happened. From the job record${active ? "; it refreshes every 15 seconds while the job runs" : ""}.`}>
        <SummarySection data={data} />
      </Panel>
      <Panel title="Prompt and settings" padded action={agentJob ? <RunButton run={run} onLoad={onLoadRun} /> : undefined}
        description="What the job was asked and every setting it ran with. Settings, sizes and the limits it was given come from the job record. The prompt, system prompt, question, claims, output schema, the limits table and how Claude Code was started are a recorded read: showing them records your email and the time, and this page never repeats it.">
        {agentJob ? <PromptSettingsSection agent={data.agent} run={run} /> : notAgent}
      </Panel>
      <Panel title="Response" padded action={agentJob ? <RunButton run={run} onLoad={onLoadRun} /> : undefined}
        description="What the job returned. The answer, structured output and output files load with the prompt (the same recorded read). An account's deposit, the sources it cites and the deposit screen come from the job record.">
        {agentJob ? <ResponseSection agent={data.agent} status={job.status} run={run} /> : notAgent}
      </Panel>
      <Panel title="Transcript" padded
        description="Claude Code's own stream for one attempt, turn by turn: its messages, folded thinking, each tool call with its arguments and result, failed calls highlighted, and the end of its stderr. Loaded a page at a time; each page is a recorded read.">
        {agentJob ? <TranscriptSection transcript={transcript} onLoad={onLoadTranscript} onMore={onMoreTranscript} /> : notAgent}
      </Panel>
      <Panel title="Event log" padded description="The events the job stored as it ran, oldest to newest: messages, stages, tool calls with their arguments and output, and each event's data.">
        <EventLogSection data={data} loading={loading} active={active} auto={auto} olderLoaded={olderLoaded} onOlder={onOlder} />
      </Panel>
      <Panel title="Usage" padded description="Cost, budget, turns, run time, tokens and tool calls, from the metrics the run recorded when its agent finished.">
        <UsageSection metrics={data.metrics} />
      </Panel>
      <Panel title="Timings" padded description="Where the wall time went, phase by phase, from the Box handle and capture marker.">
        <TimingsSection job={job} />
      </Panel>
      <Panel title="Sandbox" padded description="The Upstash Box the job ran in.">
        <SandboxSection data={data} boxConsole={boxConsole} />
      </Panel>
      <Panel title="Diagnostics, attempts and warnings" padded description="Warnings the job recorded, the files saved in its workspace checkpoint, and each attempt's worker and Box.">
        <DiagnosticsSection data={data} />
      </Panel>
    </>}
  </div>;
}

/** Holds the job record (refreshed), the recorded request and the transcript (each loaded on a press, never refreshed). */
export function JobDetailPage({ jobId, boxConsole }: { jobId: string; boxConsole: string | null }) {
  const { auto } = useTelemetry();
  const [data, setData] = useState<JobDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [olderLoaded, setOlderLoaded] = useState(false);
  const [revision, setRevision] = useState(0);
  const pending = useRef<AbortController | null>(null);
  const read = useCallback(async (signal: AbortSignal, before?: string): Promise<JobDetail> => {
    const response = await fetch(`/api/admin/jobs/${encodeURIComponent(jobId)}${before ? `?before=${before}` : ""}`, { cache: "no-store", signal });
    const result = await response.json();
    if (!response.ok) {
      if ([401, 403, 404].includes(response.status)) setData(null);
      throw new Error(result.error || "The job logs are unavailable.");
    }
    return result;
  }, [jobId]);
  useEffect(() => {
    const controller = new AbortController(); pending.current = controller; setLoading(true); setError("");
    void read(controller.signal).then(value => { if (!controller.signal.aborted) { setData(value); setOlderLoaded(false); } })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) { pending.current = null; setLoading(false); } });
    return () => { pending.current?.abort(); pending.current = null; };
  }, [read, revision]);
  const active = data ? !TERMINAL.has(data.job.status) : false;
  useEffect(() => {
    if (!auto || !active || olderLoaded) return;
    const timer = setInterval(() => { if (!document.hidden && !pending.current) setRevision(v => v + 1); }, 15000);
    return () => clearInterval(timer);
  }, [auto, active, olderLoaded]);
  async function older() {
    if (!data?.next_before || pending.current) return;
    const controller = new AbortController(); pending.current = controller; setLoading(true); setError("");
    try {
      const value = await read(controller.signal, data.next_before);
      if (!controller.signal.aborted) {
        setData(current => current ? { ...current, events: [...value.events, ...current.events], next_before: value.next_before } : value);
        setOlderLoaded(true);
      }
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Earlier logs are unavailable."); }
    finally { if (!controller.signal.aborted) { pending.current = null; setLoading(false); } }
  }

  // The recorded reads live here, keyed only by the job, so a refresh or a failed one never re-reads them.
  const [run, setRun] = useState<RunState>(NO_RUN);
  const runPending = useRef(false);
  async function loadRun() {
    if (runPending.current) return;
    runPending.current = true; setRun(current => ({ ...current, loading: true, error: "" }));
    try {
      const response = await fetch(`/api/admin/jobs/${encodeURIComponent(jobId)}/run`, { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The request is unavailable.");
      setRun({ data: result, loading: false, error: "" });
    } catch (e) { setRun(current => ({ ...current, loading: false, error: e instanceof Error ? e.message : "The request is unavailable." })); }
    finally { runPending.current = false; }
  }
  const [transcript, setTranscript] = useState<TranscriptState>(NO_TRANSCRIPT);
  const transcriptPending = useRef(false);
  async function loadTranscript(attempt?: number, after?: number) {
    if (transcriptPending.current) return;
    transcriptPending.current = true; setTranscript(current => ({ ...current, loading: true, error: "" }));
    const search = new URLSearchParams();
    if (attempt != null) search.set("attempt", String(attempt));
    if (after != null) search.set("after", String(after));
    try {
      const response = await fetch(`/api/admin/jobs/${encodeURIComponent(jobId)}/transcript${search.size ? `?${search}` : ""}`, { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The transcript is unavailable.");
      setTranscript(current => ({ data: after == null ? result as TranscriptPage : appendPage(current.data, result), loading: false, error: "" }));
    } catch (e) { setTranscript(current => ({ ...current, loading: false, error: e instanceof Error ? e.message : "The transcript is unavailable." })); }
    finally { transcriptPending.current = false; }
  }
  const more = () => { if (transcript.data?.has_more) void loadTranscript(transcript.data.attempt ?? undefined, transcript.data.next_after ?? undefined); };

  return <JobDetailView jobId={jobId} data={data} error={error} loading={loading} auto={auto} olderLoaded={olderLoaded} boxConsole={boxConsole}
    onRefresh={() => setRevision(v => v + 1)} onOlder={() => void older()}
    run={run} onLoadRun={() => void loadRun()}
    transcript={transcript} onLoadTranscript={attempt => void loadTranscript(attempt)} onMoreTranscript={more} />;
}
