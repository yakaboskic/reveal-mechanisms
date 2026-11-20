"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import type { Snapshot } from "@/lib/admin-telemetry";
import { jobHref, pageNumber, pageOf, urlWith, type UrlParams } from "@/lib/admin-url";
import type { UrlUpdate } from "./use-url-state";
import { Age, Empty, FilterRow, PageHeading, Pagination, Panel, SelectFilter, ShortId, StateStatus, Table } from "./ui";

export const ACTIVITY_PAGE_SIZE = 25;

/** Recent record changes (filtered by type) and the job event stream, each a page at a time. */
export function ActivityView({ data, params, update }: { data: Snapshot; params: UrlParams; update: UrlUpdate }) {
  const kind = params.get("kind") || "";
  const records = pageOf(data.recent.filter(r => !kind || r.kind === kind), pageNumber(params.get("records")), ACTIVITY_PAGE_SIZE);
  const events = pageOf(data.events, pageNumber(params.get("events")), ACTIVITY_PAGE_SIZE);
  const kinds = [...new Set(data.recent.map(r => r.kind))];
  return <>
    <PageHeading title="Activity" subtitle="What changed most recently: stored records and job events." />
    <FilterRow label="Filter record changes">
      <SelectFilter label="Record type" value={kind} all="All record types" options={kinds} onChange={value => update({ kind: value, records: null })} />
    </FilterRow>
    <Panel title="Recent record changes" description="Latest 100 currently stored records, ordered by last update. This is a snapshot, not a historical audit log; deleted records are not included."
      footer={records.total > ACTIVITY_PAGE_SIZE ? <Pagination page={records.page} pages={records.pages} total={records.total} noun="record" href={page => urlWith("/admin/activity", params, { records: page })} /> : undefined}>
      {records.items.length ? <Table label="Recent record changes" headings={["Type", "Record", "Owner", "Version", "Updated"]}>{records.items.map(r => <tr key={`${r.kind}-${r.id}`}>
        <td>{r.kind}</td><td><ShortId value={r.id} length={12} /></td><td><ShortId value={r.owner} length={12} /></td><td>{r.version}</td><td><Age value={r.updated_at} /></td>
      </tr>)}</Table> : <Empty>{kind ? "No recent change to this record type." : "No records are stored yet."}</Empty>}
    </Panel>
    <Panel title="Job event stream" description="Latest 100 event envelopes. Scientific content, tool arguments and credentials are excluded. A job id opens that job."
      footer={events.total > ACTIVITY_PAGE_SIZE ? <Pagination page={events.page} pages={events.pages} total={events.total} noun="event" href={page => urlWith("/admin/activity", params, { events: page })} /> : undefined}>
      {events.items.length ? <Table label="Job event stream" headings={["Time", "Job", "Event", "State", "Stage"]}>{events.items.map(e => <tr key={`${e.job_id}-${e.id}`}>
        <td><Age value={e.occurred_at} /></td><td><ShortId value={e.job_id} href={jobHref(e.job_id)} /></td><td>{e.event_type}</td><td><StateStatus value={e.status} /></td><td>{e.stage}</td>
      </tr>)}</Table> : <Empty>No job events are retained yet.</Empty>}
    </Panel>
  </>;
}
