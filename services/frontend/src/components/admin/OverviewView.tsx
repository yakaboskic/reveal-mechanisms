"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import Link from "next/link";
import { jobLabel } from "@/lib/admin-job";
import { isActive, totals, type Snapshot } from "@/lib/admin-telemetry";
import { jobHref, urlWith } from "@/lib/admin-url";
import { usd } from "../AdminCosts";
import { Age, Empty, Metric, MetricGrid, PageHeading, Panel, ShortId, StateStatus, Table } from "./ui";

const number = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const LATEST = 8;

export function OverviewView({ data }: { data: Snapshot }) {
  const total = totals(data);
  const failures = data.jobs.filter(j => j.status === "failed").slice(0, LATEST);
  const running = data.jobs.filter(j => isActive(j.status)).slice(0, LATEST);
  return <>
    <PageHeading title="Overview" subtitle={<>Database activity, scientific data, and agent execution · {data.database}</>} />
    <MetricGrid label="Application totals">
      <Metric title="Application records" value={number(total.records)} hint="Exact count of stored records, by type on Database" href="/admin/database" />
      <Metric title="Active jobs" value={number(total.active)} hint="Every job not yet finished; the list shows those among the latest 100" href={urlWith("/admin/jobs", "", { status: "active" })} />
      <Metric title="Failed jobs" value={number(total.failed)} hint="All time; the list shows those among the latest 100" href={urlWith("/admin/jobs", "", { status: "failed" })} />
      <Metric title="Workspace identities" value={number(total.identities)} hint="Principal records" href="/admin/database" />
      <Metric title="Agent spend, last 7 days" value={usd(total.weekSpend)} hint="Provider-reported, with estimates for stopped runs" href="/admin/costs" />
    </MetricGrid>
    <Panel title="Jobs by state" description="Every job ever queued, by its current state. Each opens the Jobs list filtered to that state, which holds the latest 100 jobs." padded>
      {data.statuses.length ? <ul className="admin-status-counts">{data.statuses.map(s => <li key={s.status}><Link href={urlWith("/admin/jobs", "", { status: s.status })}><StateStatus value={s.status} /> {number(s.count)}</Link></li>)}</ul>
        : <Empty>No job has been queued yet.</Empty>}
    </Panel>
    <div className="admin-columns">
      <Panel title="Latest failures" description={`The most recent failed jobs among the latest 100, newest first (up to ${LATEST}).`}
        action={<Link className="submit" href={urlWith("/admin/jobs", "", { status: "failed" })}>All failures</Link>}>
        {failures.length ? <Table label="Latest failures" headings={["Job", "Failure", "Finished"]}>{failures.map(j => <tr key={j.id}>
          <td><ShortId value={j.id} href={jobHref(j.id)} /><small>{jobLabel(j)}</small></td>
          <td><span className="admin-mono">{j.failure_code || "—"}</span>{j.error_type && <small>{j.error_type}{j.error_phase ? ` · ${j.error_phase}` : ""}</small>}</td>
          <td><Age value={j.completed_at || j.updated_at} /></td>
        </tr>)}</Table> : <Empty>No job among the latest 100 has failed.</Empty>}
      </Panel>
      <Panel title="Running and queued" description={`Jobs among the latest 100 that have not finished (up to ${LATEST}).`}
        action={<Link className="submit" href={urlWith("/admin/jobs", "", { status: "active" })}>All active jobs</Link>}>
        {running.length ? <Table label="Running and queued jobs" headings={["Job", "State", "Started"]}>{running.map(j => <tr key={j.id}>
          <td><ShortId value={j.id} href={jobHref(j.id)} /><small>{jobLabel(j)}</small></td>
          <td><StateStatus value={j.status} /><small>{j.stage.replaceAll("_", " ")}</small>{j.lease_expired && <small><span className="admin-warning">Lease expired; awaiting recovery</span></small>}</td>
          <td><Age value={j.started_at} empty="Not started" /></td>
        </tr>)}</Table> : <Empty>Nothing is running or queued.</Empty>}
      </Panel>
    </div>
  </>;
}
