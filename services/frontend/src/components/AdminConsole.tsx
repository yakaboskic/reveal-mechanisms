"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { signIn, signOut } from "next-auth/react";
import { useIdentity } from "./Session";
import { AdminTableInspector } from "./AdminTableInspector";
import { AdminJobDetail } from "./AdminJobDetail";
import type { AdminJob as Job } from "@/lib/admin-job";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { AdminPerformance, type RuntimeMetrics } from "./AdminPerformance";
import { AdminCostsView, JobCost, usd, type AdminCosts } from "./AdminCosts";

type Snapshot = {
  generated_at: string; query_ms: number; database: string;
  counts: { kind: string; count: number; updated_at: string }[];
  recent: { kind: string; id: string; owner: string; version: number; updated_at: string }[];
  statuses: { status: string; count: number }[]; jobs: Job[]; costs?: AdminCosts;
  events: { id: string; job_id: string; occurred_at: string; event_type: string; status: string; stage: string }[];
  tables: { name: string; present: boolean; estimated_rows: number | null; bytes: number | null }[];
  imports: Record<string, string | number | null>[];
  runtime: RuntimeMetrics;
};
const number = (value: number | null) => value == null ? "—" : value.toLocaleString();
const terminal = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);
function Status({ value }: { value: string }) { return <span className={`admin-status ${value === "failed" ? "bad" : value === "succeeded" || value === "ready" ? "good" : ""}`}>{value.replaceAll("_", " ")}</span>; }
function Table({ headings, children }: { headings: string[]; children: React.ReactNode }) { return <div className="admin-table-scroll" tabIndex={0}><table><thead><tr>{headings.map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>{children}</tbody></table></div>; }
export function AdminLogin({ signedIn }: { signedIn: boolean }) {
  const { status, ready } = useIdentity();
  return <section className="admin-gate"><h2>{signedIn ? "This account does not have admin access" : "Sign in to view telemetry"}</h2><p>Access requires a verified Google or ORCID email listed in ADMIN_EMAILS. ORCID sign-in may not supply a verified email; use Google in that case.</p>
    {(["google", "orcid"] as const).map(provider => <button className="submit" key={provider} disabled={!ready || !status.providers[provider]} onClick={() => void signIn(provider, { callbackUrl: "/admin" })}>Continue with {provider === "google" ? "Google" : "ORCID"}</button>)}
    {ready && !status.providers.google && !status.providers.orcid && <p>No sign-in providers are configured.</p>}
    {signedIn && <button className="text-button" onClick={() => void signOut({ callbackUrl: "/admin" })}>Sign out</button>}
  </section>;
}
export function AdminConsole({ bypass }: { bypass: boolean }) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [timeZone, setTimeZone] = useState("");
  useEffect(() => setTimeZone(Intl.DateTimeFormat().resolvedOptions().timeZone), []);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [tab, setTab] = useState("Execution");
  const [filter, setFilter] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [inspectedTable, setInspectedTable] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (controller.current) return;
    const request = new AbortController(); controller.current = request; setLoading(true);
    try {
      const response = await fetch("/api/admin/telemetry", { cache: "no-store", signal: request.signal });
      const value = await response.json();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) setData(null);
        throw new Error(value.error || "Telemetry is unavailable.");
      }
      setData(value); setError("");
    } catch (e) { if (!request.signal.aborted) setError(e instanceof Error ? e.message : "Telemetry is unavailable."); }
    finally { if (controller.current === request) { controller.current = null; setLoading(false); } }
  }, []);
  useEffect(() => { void refresh(); return () => { controller.current?.abort(); controller.current = null; }; }, [refresh]);
  useEffect(() => { if (!auto) return; const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 15000); return () => clearInterval(timer); }, [auto, refresh]);
  const jobs = data?.jobs.filter(j => `${j.id} ${j.owner} ${j.kind} ${j.status} ${j.worker || ""}`.toLowerCase().includes(filter.toLowerCase())) || [];
  useEffect(() => {
    const fromUrl = () => { const value = new URLSearchParams(window.location.search).get("job"); setJobId(value); if (value) setTab("Execution"); };
    fromUrl(); window.addEventListener("popstate", fromUrl);
    return () => window.removeEventListener("popstate", fromUrl);
  }, []);
  function selectJob(id: string | null) {
    setJobId(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("job", id); else url.searchParams.delete("job");
    window.history.pushState({}, "", url);
  }
  return <>
    {bypass && <p className="admin-dev">Development access: admin sign-in is bypassed on this server.</p>}
    <div className="admin-toolbar"><p role="status">{data ? <>Updated {date(data.generated_at)} <span>· {data.query_ms} ms database read</span></> : "Connecting to telemetry…"}</p><label><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> Refresh every 15s</label><button className="submit" disabled={loading} onClick={() => void refresh()}>{loading ? "Refreshing…" : "Refresh now"}</button></div>
    <p className="admin-timezone">Times shown in {timeZone || "your browser’s local timezone"}. Each timestamp includes its UTC offset; “—” means not recorded.</p>
    {error && <p role="alert" className="error">{error}{data && " Showing the last successful snapshot; data is stale."}</p>}
    {data && <>
      <section className="admin-overview" aria-label="Application totals">
        <div><strong>{number(data.counts.reduce((n, c) => n + c.count, 0))}</strong><span>Application records</span></div>
        <div><strong>{number(data.statuses.filter(s => !terminal.has(s.status)).reduce((n, s) => n + s.count, 0))}</strong><span>Active jobs</span></div>
        <div><strong>{number(data.statuses.find(s => s.status === "failed")?.count || 0)}</strong><span>Failed jobs</span></div>
        <div><strong>{number(data.counts.find(c => c.kind === "principal")?.count || 0)}</strong><span>Workspace identities</span></div>
        <div><strong>{usd(data.costs?.totals.week.spend_usd)}</strong><span>Agent spend, last 7 days</span></div>
      </section>
      <nav className="admin-tabs" aria-label="Telemetry views">{["Execution", "Costs", "Database", "Activity", "Performance"].map(t => <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => { setTab(t); setFilter(""); }}>{t}</button>)}</nav>
      {tab === "Execution" && <section><div className="admin-section-heading"><div><h2>Jobs and Box agents</h2><p>Latest 100 jobs across all workspaces. Elapsed times include recovery pauses.</p></div><input className="field" aria-label="Filter jobs" placeholder="Filter by job, owner, worker or status" value={filter} onChange={e => setFilter(e.target.value)} /></div>
        <div className="admin-statuses">{data.statuses.map(s => <span key={s.status}><Status value={s.status} /> {number(s.count)}</span>)}</div>
        <Table headings={["Job / owner", "State", "Started", "Finished", "Attempt / Box", "Wall time", "Agent time", "Agent cost", "Worker lease"]}>{jobs.map(j => <tr key={j.id}><td><button className="admin-job-link" onClick={() => selectJob(j.id)} aria-label={`View logs for ${j.kind} job ${j.id}`}>{j.id.slice(0, 8)} · {j.kind}</button><small title={j.owner}>{j.owner}</small></td><td><Status value={j.status} /><small>{j.stage}</small>{j.failure_code && <small>{j.failure_code}</small>}{j.error_type && <small title="Recorded exception type and phase">{j.error_type}{j.error_phase ? ` · ${j.error_phase}` : ""}</small>}</td><td className="admin-date">{date(j.started_at)}</td><td className="admin-date">{date(j.completed_at)}</td><td>{j.attempt}<small>{j.box_phase || "No Box runtime"}</small></td><td>{duration(j.wall_seconds)}{!terminal.has(j.status) && <small>in progress</small>}</td><td>{duration(j.agent_seconds)}</td><td><JobCost job={j} /></td><td>{j.lease_expired ? <span className="admin-warning">Expired; awaiting recovery</span> : terminal.has(j.status) ? "Finished" : j.lease_until ? date(j.lease_until) : "Awaiting worker"}<small>{j.recoveries} recoveries</small></td></tr>)}</Table>
        {!jobs.length && <p className="admin-empty">No jobs match this view. Research jobs will appear here as they are created.</p>}
        {jobId && <AdminJobDetail key={jobId} jobId={jobId} auto={auto} onClose={() => selectJob(null)} />}
      </section>}
      {tab === "Database" && <section>{inspectedTable ? <AdminTableInspector key={inspectedTable} table={inspectedTable} onClose={() => setInspectedTable(null)} /> : <><h2>Scientific database</h2><p>{data.database}. Select a table to inspect its rows. Table row counts are database estimates; application record counts below are exact. Missing tables are shown explicitly.</p>
        <Table headings={["Prisma table", "Availability", "Estimated rows", "Data + indexes"]}>{data.tables.map(t => <tr key={t.name}><td className="admin-mono">{t.present ? <button className="admin-job-link" onClick={() => setInspectedTable(t.name)}>{t.name}</button> : t.name}</td><td>{t.present ? "Present" : <span className="admin-warning">Missing</span>}</td><td>{number(t.estimated_rows)}</td><td>{t.bytes == null ? "—" : `${(t.bytes / 1048576).toFixed(2)} MB`}</td></tr>)}</Table>
        <h2>Imports and embedding runs</h2><p>Latest 10 runs per table. Counts reflect persisted import progress.</p><Table headings={["Source", "Run / import", "State", "Loaded / expected", "Created", "Updated"]}>{data.imports.map((r, i) => <tr key={`${r.table}-${i}`}><td>{r.table}</td><td className="admin-mono">{r.run_id || r.import_id}</td><td><Status value={String(r.status || "unknown")} /></td><td>{r.loaded_rows != null ? `${r.loaded_rows} / ${r.expected_rows}` : r.loaded_vectors != null ? `${r.loaded_vectors} / ${r.expected_vectors} vectors; ${r.loaded_bindings} / ${r.expected_bindings} bindings` : "—"}</td><td>{date(r.created_at)}</td><td>{date(r.updated_at)}</td></tr>)}</Table>{!data.imports.length && <p>No import run metadata is available.</p>}
        <h2>Application record types</h2><Table headings={["Record type", "Exact count", "Last update"]}>{data.counts.map(c => <tr key={c.kind}><td>{c.kind}</td><td>{number(c.count)}</td><td>{date(c.updated_at)}</td></tr>)}</Table></>}
      </section>}
      {tab === "Activity" && <section><h2>Recent record changes</h2><p>Latest 100 currently stored records, ordered by last update. This is a snapshot, not a historical audit log; deleted records are not included.</p><Table headings={["Type", "Record", "Owner", "Version", "Updated"]}>{data.recent.map(r => <tr key={`${r.kind}-${r.id}`}><td>{r.kind}</td><td className="admin-mono">{r.id}</td><td className="admin-mono">{r.owner}</td><td>{r.version}</td><td>{date(r.updated_at)}</td></tr>)}</Table>
        <h2>Job event stream</h2><p>Latest 100 event envelopes. Scientific content, tool arguments and credentials are excluded.</p><Table headings={["Time", "Job", "Event", "State", "Stage"]}>{data.events.map(e => <tr key={`${e.job_id}-${e.id}`}><td>{date(e.occurred_at)}</td><td className="admin-mono">{e.job_id}</td><td>{e.event_type}</td><td><Status value={e.status} /></td><td>{e.stage}</td></tr>)}</Table>
      </section>}
      {tab === "Costs" && (data.costs ? <AdminCostsView costs={data.costs} jobs={data.jobs} onSelect={id => { setTab("Execution"); selectJob(id); }} /> : <p className="admin-empty">This backend does not report agent costs yet.</p>)}
      {tab === "Performance" && <AdminPerformance runtime={data.runtime} />}
    </>}
  </>;
}
