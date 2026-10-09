"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatAdminDate as date, formatAdminDuration as duration, formatAdminJson } from "@/lib/admin-time";
import { coalesceMessageDeltas } from "@/lib/activity";
import type { Schema } from "@/lib/client";
import type { AdminJob } from "@/lib/admin-job";

type Diagnostic = { path: string; available: boolean; data?: unknown; reason?: string };
type Tokens = { input: number | null; output: number | null; cache_write: number | null; cache_read: number | null };
type Metrics = {
  parent_job_id?: string | null; recorded_at?: string;
  agent?: { model: string | null; status: string | null; subtype: string | null; reason: string | null; cost_usd: number | null; estimated_cost_usd?: number | null; cost_source: string; budget_usd: number | null; budget_used: number | null; turns: number | null; turn_limit: number | null; elapsed_seconds: number | null; time_limit_seconds: number | null; api_seconds: number | null; tokens: Tokens | null; tool_calls: number | null; tool_failures: number | null; lint_checks: number | null; draft_writes: number | null };
  error?: { phase: string | null; error_type: string; message: string | null; recorded_at: string };
};
type Detail = {
  job: AdminJob & { failure: { code: string; message: string; retryable: boolean; budget?: { limit_usd: number; spent_usd: number | null } } | null; warnings: string[]; research_request_id: string | null; input_account_id: string | null };
  events: Schema<"JobEvent">[]; next_before: string | null;
  attempts: { attempt: number; started_at: string | null; worker_id: string | null; "remote_handle.box_id": string | null }[];
  attempts_limited: boolean; diagnostics: Diagnostic[]; diagnostics_limited: boolean; metrics?: Metrics | null;
};
const terminal = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);
const json = (value: unknown) => formatAdminJson(JSON.stringify(value, null, 2));
const usd = (value: number | null | undefined) => value == null ? "—" : `$${value.toFixed(4)}`;
const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const of = (value: string, limit: string | null) => limit ? `${value} of ${limit}` : value;

function AgentUsage({ metrics }: { metrics: Metrics }) {
  const agent = metrics.agent;
  if (!agent) return null;
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
  return <details className="admin-agent-usage" open><summary>Agent cost and usage · {agent.cost_usd != null ? usd(agent.cost_usd) : agent.estimated_cost_usd != null ? `≈${usd(agent.estimated_cost_usd)} est.` : "not reported"}</summary>
    {agent.budget_used != null && <div className="admin-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, 100 * agent.budget_used)}%` }} /></div>}
    <dl className="admin-detail-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {agent.reason && <p>Runner reason: {agent.reason}</p>}
    {metrics.parent_job_id && <p>Research statement for <a href={`/admin?job=${metrics.parent_job_id}`}>{metrics.parent_job_id}</a>.</p>}
  </details>;
}

export function AdminJobDetail({ jobId, auto, onClose }: { jobId: string; auto: boolean; onClose: () => void }) {
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [olderLoaded, setOlderLoaded] = useState(false);
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState("");
  const [copied, setCopied] = useState(false);
  const region = useRef<HTMLElement>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => { region.current?.scrollIntoView({ block: "start", behavior: "smooth" }); }, [jobId]);
  const read = useCallback(async (signal: AbortSignal, before?: string): Promise<Detail> => {
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
    void read(controller.signal).then(value => { if (!controller.signal.aborted) { setData(value); setOlderLoaded(false); setCopied(false); } })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) { pending.current = null; setLoading(false); } });
    return () => { pending.current?.abort(); pending.current = null; };
  }, [read, revision]);
  const active = data ? !terminal.has(data.job.status) : false;
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
        setOlderLoaded(true); setCopied(false);
      }
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Earlier logs are unavailable."); }
    finally { if (!controller.signal.aborted) { pending.current = null; setLoading(false); } }
  }
  const job = data?.job;
  const events = coalesceMessageDeltas(data?.events || []).filter(e => `${e.message} ${e.stage} ${e.event_type} ${JSON.stringify(e.detail)}`.toLowerCase().includes(search.toLowerCase()));
  const failures = data?.diagnostics.filter(d => d.available && d.path.endsWith("/failure.json")) || [];
  return <section className="admin-job-detail" ref={region} aria-label="Job logs and diagnostics">
    <div className="admin-section-heading"><div><h2>Execution detail{job ? ` · ${job.kind}` : ""}</h2><p className="admin-mono">{jobId}</p></div><div><button className="text-button" disabled={loading} onClick={() => setRevision(v => v + 1)}>Refresh logs</button><button className="text-button" onClick={onClose}>Close detail</button></div></div>
    {!data && loading && <p role="status">Loading job logs and diagnostics…</p>}
    {error && <p role="alert" className="error">{error}{data && " Showing previously loaded logs."}</p>}
    {data && job && <>
      <p><span className={`admin-status ${job.status === "failed" ? "bad" : job.status === "succeeded" ? "good" : ""}`}>{job.status.replaceAll("_", " ")}</span> <span className="admin-log-stage">{job.stage.replaceAll("_", " ")}</span> · <a href={`/admin?job=${job.id}`}>Link to this job</a></p>
      {job.failure && <div className="admin-job-failure"><h3>{job.failure.code}</h3><p>{job.failure.message}</p>{data.metrics?.error && <div><strong>{data.metrics.error.error_type}{data.metrics.error.phase ? ` · ${data.metrics.error.phase.replaceAll("_", " ")}` : ""}</strong><p>{data.metrics.error.message || "No message was recorded."}</p><small>Recorded cause · {date(data.metrics.error.recorded_at)}</small></div>}{failures.map(item => {
        const failure = item.data as { phase?: string; error_type?: string; message?: string };
        return <div key={item.path}><strong>{failure.error_type || "Saved failure"}{failure.phase ? ` · ${failure.phase.replaceAll("_", " ")}` : ""}</strong><p>{failure.message}</p><small>{item.path}</small></div>;
      })}</div>}
      {data.metrics && <AgentUsage metrics={data.metrics} />}
      <dl className="admin-detail-grid">{([['Created', job.created_at], ['Started', job.started_at], ['Updated', job.updated_at], ['Finished', job.completed_at], ['Current attempt started', job.attempt_started_at]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{date(value)}</dd></div>)}<div><dt>Worker</dt><dd>{job.worker || "—"}</dd></div><div><dt>Box</dt><dd>{job.box_id || "No Box runtime recorded"}</dd></div><div><dt>Attempt / recoveries</dt><dd>{job.attempt} / {job.recoveries}</dd></div></dl>
      <div className="admin-section-heading"><div><h3>Event log</h3><p>{data.events.length} stored events loaded{data.next_before ? " · earlier events available" : " · beginning of retained history"}. {active ? auto && !olderLoaded ? "Live logs refresh every 15s." : "Refresh logs to see current activity." : "Job finished."}</p></div><input className="field" aria-label="Search job logs" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search loaded messages, tools or stages" /></div>
      <div className="admin-log-actions"><button className="submit" disabled={loading || !data.next_before} onClick={() => void older()}>{loading && data ? "Loading…" : "Load earlier events"}</button><button className="text-button" onClick={async () => { try { await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); setCopied(true); } catch { setError("Copy is unavailable in this browser."); } }}>{copied ? "Copied" : "Copy loaded logs"}</button><span>Oldest to newest · adjacent agent text fragments are combined</span></div>
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
      {!!job.warnings?.length && <details><summary>Warnings ({job.warnings.length})</summary><ul>{job.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>}
      <h3>Saved diagnostics</h3><p>Worker errors, token measurements and validation reports available from the artifact store.</p>
      {data.diagnostics.map(item => <details className="admin-diagnostic" key={item.path} open={item.path.endsWith("failure.json")}><summary>{item.path}</summary>{item.available ? <pre>{json(item.data)}</pre> : <p>{item.reason}</p>}</details>)}
      {!data.diagnostics.length && <p>No saved diagnostics are available on this server. Older jobs may only have the persisted events above.</p>}
      {data.diagnostics_limited && <p>Showing the first 20 saved diagnostic files.</p>}
      <details className="admin-attempt-history"><summary>Attempt history ({data.attempts.length}{data.attempts_limited ? "+" : ""})</summary><div className="admin-table-scroll"><table><thead><tr><th>Attempt</th><th>Started</th><th>Worker at claim</th><th>Box at claim</th></tr></thead><tbody>{data.attempts.map(attempt => <tr key={attempt.attempt}><td>{attempt.attempt}</td><td>{date(attempt.started_at)}</td><td>{attempt.worker_id || "—"}</td><td>{attempt["remote_handle.box_id"] || "—"}</td></tr>)}</tbody></table></div>{data.attempts_limited && <p>Latest 50 attempt snapshots.</p>}</details>
      <details className="admin-duration-detail"><summary>Wall time and phase timings · {duration(job.wall_seconds)} total</summary><div className="admin-timings">{([['Queue / pre-attempt', job.queue_seconds], ['Attempt wall time', job.attempt_seconds], ['Box lifetime', job.box_seconds], ['Box setup', job.setup_seconds], ['Agent execution', job.agent_seconds], ['Artifact capture', job.capture_seconds], ['Box cleanup', job.cleanup_seconds]] as [string, number | null][]).map(([label, value]) => <div key={label}><span>{label}</span><div className="admin-bar"><i style={{ width: value == null ? 0 : `${Math.min(100, 100 * value / Math.max(job.wall_seconds || 1, 1))}%` }} /></div><strong>{duration(value)}</strong></div>)}</div><p>Phase timings depend on instrumentation available when the job ran. Agent time includes polling and event delivery. Queue time ends at the first worker claim.</p></details>
    </>}
  </section>;
}
