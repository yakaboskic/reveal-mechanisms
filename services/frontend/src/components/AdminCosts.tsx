"use client";
import type { AdminJob } from "@/lib/admin-job";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";

type Bucket = { spend_usd: number; jobs: number; estimated: number; unreported: number; failed: number };
export type AdminCosts = {
  window_jobs: number; window_limit: number;
  totals: Record<"day" | "week" | "month" | "window", Bucket>;
  daily: { date: string; analysis_usd: number; paragraph_usd: number; jobs: number; failed: number }[];
  research: { succeeded: number; p50_usd: number | null; p90_usd: number | null; max_usd: number | null; mean_usd: number | null };
  owners: (Bucket & { owner: string; label: string | null })[];
  failures: { code: string; jobs: number; spend_usd: number; causes: Record<string, number> }[];
  budget: { exceeded: number; near_limit: number };
};

export const usd = (value: number | null | undefined) => value == null ? "—" : `$${value < 10 ? value.toFixed(2) : value.toFixed(0)}`;
const count = (value: number) => value.toLocaleString();
const percent = (value: number | null | undefined) => value == null ? "—" : `${Math.round(value * 100)}%`;

/** One job's agent spend: provider-reported dollars, an estimate for a stopped run, or why there is no figure. */
export function JobCost({ job }: { job: AdminJob }) {
  if (job.cost_usd == null && job.estimated_cost_usd != null) return <span title="Estimated from streamed token usage at list prices; the run stopped before the provider reported its total.">≈{usd(job.estimated_cost_usd)} est.<small>{job.budget_used != null ? `${percent(job.budget_used)} of ${usd(job.budget_usd)}` : ""}</small></span>;
  if (job.cost_usd == null) return <>{job.cost_source === "unreported" ? <span title="The run stopped before the provider reported its total (timeout or cancellation).">Not reported</span> : "—"}</>;
  return <>{usd(job.cost_usd)}<small>{[job.turns != null && `${job.turns} turns`, job.budget_used != null && `${percent(job.budget_used)} of ${usd(job.budget_usd)}`].filter(Boolean).join(" · ")}</small></>;
}

export function AdminCostsView({ costs, jobs, onSelect }: { costs: AdminCosts; jobs: AdminJob[]; onSelect: (id: string) => void }) {
  const peak = Math.max(0.01, ...costs.daily.map(d => d.analysis_usd + d.paragraph_usd));
  const expensive = jobs.filter(j => j.cost_usd != null).sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0)).slice(0, 10);
  const tiles: [string, Bucket][] = [["Last 24 hours", costs.totals.day], ["Last 7 days", costs.totals.week], ["Last 30 days", costs.totals.month]];
  return <section aria-label="Agent costs and failures">
    <h2>Agent spend</h2>
    <p>Provider-reported Claude cost of each research and statement run, from the latest {count(costs.window_jobs)} jobs with recorded metrics (limit {count(costs.window_limit)}). Runs stopped before the provider reported a total, such as timeouts and cancellations, use an estimate from their streamed token usage when one was recorded, and otherwise count as not reported rather than $0. Box, storage and delivery charges are not included.</p>
    <section className="admin-overview admin-cost-overview" aria-label="Spend totals">
      {tiles.map(([label, bucket]) => <div key={label}><strong>{usd(bucket.spend_usd)}</strong><span>{label} · {count(bucket.jobs)} jobs{bucket.failed ? ` · ${count(bucket.failed)} failed` : ""}{bucket.estimated ? ` · ${count(bucket.estimated)} estimated` : ""}{bucket.unreported ? ` · ${count(bucket.unreported)} not reported` : ""}</span></div>)}
      <div><strong>{usd(costs.research.p50_usd)}</strong><span>Typical successful research run · p90 {usd(costs.research.p90_usd)} · max {usd(costs.research.max_usd)} ({count(costs.research.succeeded)} runs)</span></div>
    </section>
    <p>Budget stops: <strong>{count(costs.budget.exceeded)}</strong> runs hit their dollar cap; <strong>{count(costs.budget.near_limit)}</strong> more used at least 80% of it.</p>
    <h3>Daily spend (last 30 days)</h3>
    {costs.daily.length ? <div className="admin-cost-days" role="table" aria-label="Daily spend">{costs.daily.map(d => <div role="row" key={d.date}>
      <span role="cell">{d.date}</span>
      <div role="cell" className="admin-cost-bar" title={`Research ${usd(d.analysis_usd)} · statements ${usd(d.paragraph_usd)}`}><i style={{ width: `${100 * d.analysis_usd / peak}%` }} /><b style={{ width: `${100 * d.paragraph_usd / peak}%` }} /></div>
      <strong role="cell">{usd(d.analysis_usd + d.paragraph_usd)}</strong>
      <small role="cell">{count(d.jobs)} jobs{d.failed ? ` · ${count(d.failed)} failed` : ""}</small>
    </div>)}</div> : <p className="admin-empty">No job metrics have been recorded in the last 30 days.</p>}
    <p className="admin-cost-legend"><i /> Research runs <b /> Research statements (paragraphs)</p>
    <h3>Failures by code</h3>
    <div className="admin-table-scroll" tabIndex={0}><table><thead><tr><th>Failure code</th><th>Jobs</th><th>Spend</th><th>Recorded causes</th></tr></thead><tbody>
      {costs.failures.map(f => <tr key={f.code}><td className="admin-mono">{f.code}</td><td>{count(f.jobs)}</td><td>{usd(f.spend_usd)}</td><td>{Object.entries(f.causes).sort((a, b) => b[1] - a[1]).map(([cause, n]) => <small key={cause}>{cause} × {n}</small>)}{!Object.keys(f.causes).length && "—"}</td></tr>)}
    </tbody></table></div>
    {!costs.failures.length && <p className="admin-empty">No failed jobs in this window.</p>}
    <h3>Spend by workspace owner</h3>
    <p>Owners are opaque principal ids. Client principals, such as a partner frontend's shared demo identity, are named because every visitor of that client shares them.</p>
    <div className="admin-table-scroll" tabIndex={0}><table><thead><tr><th>Owner</th><th>Jobs</th><th>Spend</th><th>Failed</th><th>Estimated / not reported</th></tr></thead><tbody>
      {costs.owners.map(o => <tr key={o.owner}><td>{o.label ? <strong>{o.label}</strong> : null}<small className="admin-mono">{o.owner}</small></td><td>{count(o.jobs)}</td><td>{usd(o.spend_usd)}</td><td>{count(o.failed)}</td><td>{count(o.estimated)} / {count(o.unreported)}</td></tr>)}
    </tbody></table></div>
    <h3>Most expensive recent jobs</h3>
    <p>From the latest 100 jobs.</p>
    <div className="admin-table-scroll" tabIndex={0}><table><thead><tr><th>Job</th><th>Created</th><th>Outcome</th><th>Cost</th><th>Tokens (output · cache write · cache read)</th><th>Agent time</th></tr></thead><tbody>
      {expensive.map(j => <tr key={j.id}><td><button className="admin-job-link" onClick={() => onSelect(j.id)}>{j.id.slice(0, 8)} · {j.kind}</button>{j.parent_job_id && <small>statement for {j.parent_job_id.slice(0, 8)}</small>}</td><td className="admin-date">{date(j.created_at)}</td><td>{j.status.replaceAll("_", " ")}{j.failure_code && <small>{j.failure_code}</small>}{j.error_type && <small>{j.error_type}{j.error_phase ? ` · ${j.error_phase}` : ""}</small>}</td><td><JobCost job={j} /></td><td>{j.tokens ? `${count(j.tokens.output ?? 0)} · ${count(j.tokens.cache_write ?? 0)} · ${count(j.tokens.cache_read ?? 0)}` : "—"}</td><td>{duration(j.agent_seconds)}</td></tr>)}
    </tbody></table></div>
    {!expensive.length && <p className="admin-empty">No recent job has a recorded cost yet.</p>}
  </section>;
}
