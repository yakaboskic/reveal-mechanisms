"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import Link from "next/link";
import { jobKindLabel, jobLabel, refusalNote } from "@/lib/admin-job";
import { distinct, filterJobs, isActive, ownerLine, TERMINAL, type Snapshot } from "@/lib/admin-telemetry";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { jobHref, pageNumber, pageOf, urlWith, type UrlParams } from "@/lib/admin-url";
import { JobCost } from "../AdminCosts";
import type { UrlUpdate } from "./use-url-state";
import { Age, CopyButton, Empty, FilterRow, PageHeading, Pagination, Panel, SearchFilter, SelectFilter, StateStatus, Table } from "./ui";

export const JOBS_PAGE_SIZE = 25;
const number = (value: number) => value.toLocaleString();
export const JOB_HEADINGS = ["Job / owner", "State", "Started", "Finished", "Attempt / Box", "Wall time", "Agent time", "Agent cost", "Worker lease"];

/** Today's Execution table, filtered by kind, state, owner and free text, a page at a time; each job opens its own page. */
export function JobsView({ data, params, update }: { data: Snapshot; params: UrlParams; update: UrlUpdate }) {
  const filter = { q: params.get("q"), kind: params.get("kind"), status: params.get("status"), owner: params.get("owner") };
  const owners = data.owners ?? {};
  const jobs = filterJobs(data.jobs, filter, owners);
  const shown = pageOf(jobs, pageNumber(params.get("page")), JOBS_PAGE_SIZE);
  const filtered = !!(filter.q || filter.kind || filter.status || filter.owner);
  const set = (key: string, push = true) => (value: string) => update({ [key]: value, page: null }, { push });
  return <>
    <PageHeading title="Jobs" subtitle="Jobs and Box agents: the latest 100 jobs across all workspaces. Elapsed times include recovery pauses." />
    <ul className="admin-status-counts" aria-label="Jobs by state (all time)">{data.statuses.map(s => <li key={s.status}>
      <Link href={urlWith("/admin/jobs", params, { status: filter.status === s.status ? null : s.status, page: null })} aria-current={filter.status === s.status ? "true" : undefined}><StateStatus value={s.status} /> {number(s.count)}</Link>
    </li>)}</ul>
    <FilterRow label="Filter jobs">
      <SearchFilter label="Filter jobs" placeholder="Filter by job, owner, type, worker or status" value={filter.q || ""} onChange={set("q", false)} />
      <SelectFilter label="Kind" value={filter.kind || ""} all="All kinds" options={distinct(data.jobs, j => j.kind).map(k => [k, jobKindLabel(k)])} onChange={set("kind")} />
      <SelectFilter label="State" value={filter.status || ""} all="All states" options={[["active", "Not finished"], ...distinct(data.jobs, j => j.status).map(s => [s, s.replaceAll("_", " ")] as [string, string])]} onChange={set("status")} />
      <SelectFilter label="Owner" value={filter.owner || ""} all="All owners" options={distinct(data.jobs, j => j.owner).map(o => [o, ownerLine(o, owners)])} onChange={set("owner")} />
      {filtered && <Link className="text-button" href={urlWith("/admin/jobs", params, { q: null, kind: null, status: null, owner: null, page: null })}>Clear filters</Link>}
    </FilterRow>
    <Panel title="Jobs and Box agents" description="State counts above are all time; this list holds the latest 100 jobs. Open a job for its logs, diagnostics, attempts and timings."
      footer={shown.total > JOBS_PAGE_SIZE ? <Pagination page={shown.page} pages={shown.pages} total={shown.total} noun="job" href={page => urlWith("/admin/jobs", params, { page })} /> : undefined}>
      {shown.items.length ? <Table label="Jobs" headings={JOB_HEADINGS}>{shown.items.map(j => <tr key={j.id}>
        <td><Link className="admin-job-link" href={jobHref(j.id)} aria-label={`View logs for ${jobLabel(j)} job ${j.id}`} title={j.id}>{j.id.slice(0, 8)} · {jobLabel(j)}</Link> <CopyButton text={j.id} /><small title={j.owner}>{ownerLine(j.owner, owners)}</small></td>
        <td><StateStatus value={j.status} /><small>{j.stage}</small>{j.failure_code && <small>{j.failure_code}</small>}{j.error_type && <small title="Recorded exception type and phase">{j.error_type}{j.error_phase ? ` · ${j.error_phase}` : ""}</small>}{refusalNote(j) && <small className="admin-warning">{refusalNote(j)}</small>}</td>
        <td className="admin-date"><Age value={j.started_at} /></td>
        <td className="admin-date"><Age value={j.completed_at} /></td>
        <td>{j.attempt}<small>{j.box_phase || "No Box runtime"}</small></td>
        <td>{duration(j.wall_seconds)}{isActive(j.status) && <small>in progress</small>}</td>
        <td>{duration(j.agent_seconds)}</td>
        <td><JobCost job={j} /></td>
        <td>{j.lease_expired ? <span className="admin-warning">Expired; awaiting recovery</span> : TERMINAL.has(j.status) ? "Finished" : j.lease_until ? date(j.lease_until) : "Awaiting worker"}<small>{j.recoveries} recoveries</small></td>
      </tr>)}</Table> : <Empty>{filtered ? "No jobs match this view. Clear the filters to see every job." : "No jobs match this view. Research jobs will appear here as they are created."}</Empty>}
    </Panel>
  </>;
}
