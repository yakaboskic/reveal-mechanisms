"use client";
import React from "react";
import Link from "next/link";
import type { AdminJob } from "@/lib/admin-job";
import { formatAdminDuration as duration } from "@/lib/admin-time";
import { jobHref } from "@/lib/admin-url";
import { Age, Callout, Empty, Metric, MetricGrid, PageHeading, Panel, StateStatus, Table } from "./admin/ui";

type Bucket = { spend_usd: number; jobs: number; estimated: number; unreported: number; failed: number };
type Distribution = { succeeded: number; p50_usd: number | null; p90_usd: number | null; max_usd: number | null; mean_usd: number | null };
export type AdminCosts = {
  window_jobs: number; window_limit: number;
  totals: Record<"day" | "week" | "month" | "window", Bucket>;
  // agent_usd and agents are absent from backends older than general agent jobs.
  daily: { date: string; analysis_usd: number; paragraph_usd: number; agent_usd?: number; jobs: number; failed: number }[];
  research: Distribution;
  agents?: Distribution;
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

export function AdminCostsView({ costs, jobs }: { costs: AdminCosts; jobs: AdminJob[] }) {
  const dayTotal = (d: AdminCosts["daily"][number]) => d.analysis_usd + d.paragraph_usd + (d.agent_usd ?? 0);
  const peak = Math.max(0.01, ...costs.daily.map(dayTotal));
  const expensive = jobs.filter(j => j.cost_usd != null).sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0)).slice(0, 10);
  const tiles: [string, Bucket][] = [["Last 24 hours", costs.totals.day], ["Last 7 days", costs.totals.week], ["Last 30 days", costs.totals.month]];
  return <section aria-label="Agent costs and failures" className="admin-costs">
    <PageHeading title="Costs" subtitle="Agent spend: what each research, statement and general agent run cost, and where runs failed." />
    <p className="admin-note">Provider-reported Claude cost of each research, statement and general agent run, from the latest {count(costs.window_jobs)} jobs with recorded metrics (limit {count(costs.window_limit)}). Runs stopped before the provider reported a total, such as timeouts and cancellations, use an estimate from their streamed token usage when one was recorded, and otherwise count as not reported rather than $0. Box, storage and delivery charges are not included.</p>
    <MetricGrid label="Spend totals">
      {tiles.map(([label, bucket]) => <Metric key={label} title={label} value={usd(bucket.spend_usd)} hint={<>{count(bucket.jobs)} jobs{bucket.failed ? ` · ${count(bucket.failed)} failed` : ""}{bucket.estimated ? ` · ${count(bucket.estimated)} estimated` : ""}{bucket.unreported ? ` · ${count(bucket.unreported)} not reported` : ""}</>} />)}
      <Metric title="Typical successful research run" value={usd(costs.research.p50_usd)} hint={`p90 ${usd(costs.research.p90_usd)} · max ${usd(costs.research.max_usd)} (${count(costs.research.succeeded)} runs)`} />
      {costs.agents && costs.agents.succeeded > 0 && <Metric title="Typical successful agent job" value={usd(costs.agents.p50_usd)} hint={`p90 ${usd(costs.agents.p90_usd)} · max ${usd(costs.agents.max_usd)} (${count(costs.agents.succeeded)} runs)`} href="/admin/agent-jobs" />}
    </MetricGrid>
    <Callout tone={costs.budget.exceeded ? "warn" : "neutral"}>Budget stops: <strong>{count(costs.budget.exceeded)}</strong> runs hit their dollar cap; <strong>{count(costs.budget.near_limit)}</strong> more used at least 80% of it.</Callout>
    <Panel title="Daily spend (last 30 days)" description="Each day's spend, split by kind of run. Hover a bar for the split in dollars." padded
      footer={<span className="admin-cost-legend"><i /> Research runs <b /> Research statements (paragraphs) <u /> General agent jobs</span>}>
      {costs.daily.length ? <div className="admin-cost-days" role="table" aria-label="Daily spend">{costs.daily.map(d => <div role="row" key={d.date}>
        <span role="cell">{d.date}</span>
        <div role="cell" className="admin-cost-bar" title={`Research ${usd(d.analysis_usd)} · statements ${usd(d.paragraph_usd)} · agent jobs ${usd(d.agent_usd ?? 0)}`}><i style={{ width: `${100 * d.analysis_usd / peak}%` }} /><b style={{ width: `${100 * d.paragraph_usd / peak}%` }} /><u style={{ width: `${100 * (d.agent_usd ?? 0) / peak}%` }} /></div>
        <strong role="cell">{usd(dayTotal(d))}</strong>
        <small role="cell">{count(d.jobs)} jobs{d.failed ? ` · ${count(d.failed)} failed` : ""}</small>
      </div>)}</div> : <Empty>No job metrics have been recorded in the last 30 days.</Empty>}
    </Panel>
    <Panel title="Failures by code" description="Failed jobs in this window by failure code, with what they spent and the causes their runs recorded.">
      {costs.failures.length ? <Table label="Failures by code" headings={["Failure code", "Jobs", "Spend", "Recorded causes"]}>
        {costs.failures.map(f => <tr key={f.code}><td className="admin-mono">{f.code}</td><td>{count(f.jobs)}</td><td>{usd(f.spend_usd)}</td><td>{Object.entries(f.causes).sort((a, b) => b[1] - a[1]).map(([cause, n]) => <small key={cause}>{cause} × {n}</small>)}{!Object.keys(f.causes).length && "—"}</td></tr>)}
      </Table> : <Empty>No failed jobs in this window.</Empty>}
    </Panel>
    <Panel title="Spend by workspace owner" description="Owners are opaque principal ids. Client principals, such as a partner frontend's shared demo identity, are named because every visitor of that client shares them.">
      {costs.owners.length ? <Table label="Spend by workspace owner" headings={["Owner", "Jobs", "Spend", "Failed", "Estimated / not reported"]}>
        {costs.owners.map(o => <tr key={o.owner}><td>{o.label ? <strong>{o.label}</strong> : null}<small className="admin-mono">{o.owner}</small></td><td>{count(o.jobs)}</td><td>{usd(o.spend_usd)}</td><td>{count(o.failed)}</td><td>{count(o.estimated)} / {count(o.unreported)}</td></tr>)}
      </Table> : <Empty>No owner has a recorded cost in this window.</Empty>}
    </Panel>
    <Panel title="Most expensive recent jobs" description="The ten costliest of the latest 100 jobs. A job opens its detail.">
      {expensive.length ? <Table label="Most expensive recent jobs" headings={["Job", "Created", "Outcome", "Cost", "Tokens (output · cache write · cache read)", "Agent time"]}>
        {expensive.map(j => <tr key={j.id}><td><Link className="admin-job-link" href={jobHref(j.id)} title={j.id}>{j.id.slice(0, 8)} · {j.kind}</Link>{j.parent_job_id && <small>statement for <Link href={jobHref(j.parent_job_id)} title={j.parent_job_id}>{j.parent_job_id.slice(0, 8)}</Link></small>}</td><td className="admin-date"><Age value={j.created_at} /></td><td><StateStatus value={j.status} />{j.failure_code && <small>{j.failure_code}</small>}{j.error_type && <small>{j.error_type}{j.error_phase ? ` · ${j.error_phase}` : ""}</small>}</td><td><JobCost job={j} /></td><td>{j.tokens ? `${count(j.tokens.output ?? 0)} · ${count(j.tokens.cache_write ?? 0)} · ${count(j.tokens.cache_read ?? 0)}` : "—"}</td><td>{duration(j.agent_seconds)}</td></tr>)}
      </Table> : <Empty>No recent job has a recorded cost yet.</Empty>}
    </Panel>
  </section>;
}
