"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import Link from "next/link";
import type { Snapshot } from "@/lib/admin-telemetry";
import { urlWith, type UrlParams } from "@/lib/admin-url";
import { AdminTableInspector } from "../AdminTableInspector";
import type { UrlUpdate } from "./use-url-state";
import { Age, Empty, PageHeading, Panel, StateStatus, Table } from "./ui";

const number = (value: number | null) => value == null ? "—" : value.toLocaleString();

/** How much of an import or embedding run is stored: rows, or vectors and bindings. */
export function loaded(r: Record<string, string | number | null>) {
  return r.loaded_rows != null ? `${r.loaded_rows} / ${r.expected_rows}`
    : r.loaded_vectors != null ? `${r.loaded_vectors} / ${r.expected_vectors} vectors; ${r.loaded_bindings} / ${r.expected_bindings} bindings` : "—";
}

/** Tables (with the inspector open on `?table=`), imports and embedding runs, and application record types. */
export function DatabaseView({ data, params, update }: { data: Snapshot; params: UrlParams; update: UrlUpdate }) {
  const table = params.get("table");
  if (table) return <>
    <PageHeading title="Database" subtitle={<>Inspecting <span className="admin-mono">{table}</span>, read only.</>} />
    <AdminTableInspector key={table} table={table} onClose={() => update({ table: null })} />
  </>;
  return <>
    <PageHeading title="Database" subtitle={<>Scientific database: {data.database}.</>} />
    <Panel title="Tables" description="Select a table to inspect its rows. Table row counts are database estimates; the record type counts below are exact. Missing tables are shown explicitly.">
      {data.tables.length ? <Table label="Database tables" headings={["Prisma table", "Availability", "Estimated rows", "Data + indexes"]}>{data.tables.map(t => <tr key={t.name}>
        <td className="admin-mono">{t.present ? <Link className="admin-job-link" href={urlWith("/admin/database", params, { table: t.name })}>{t.name}</Link> : t.name}</td>
        <td>{t.present ? "Present" : <span className="admin-warning">Missing</span>}</td>
        <td>{number(t.estimated_rows)}</td>
        <td>{t.bytes == null ? "—" : `${(t.bytes / 1048576).toFixed(2)} MB`}</td>
      </tr>)}</Table> : <Empty>The database reported no tables.</Empty>}
    </Panel>
    <Panel title="Imports and embedding runs" description="Latest 10 runs per table. Counts reflect persisted import progress.">
      {data.imports.length ? <Table label="Imports and embedding runs" headings={["Source", "Run / import", "State", "Loaded / expected", "Created", "Updated"]}>{data.imports.map((r, i) => <tr key={`${r.table}-${i}`}>
        <td>{r.table}</td><td className="admin-mono">{r.run_id || r.import_id}</td><td><StateStatus value={String(r.status || "unknown")} /></td>
        <td>{loaded(r)}</td><td><Age value={r.created_at as string | null} /></td><td><Age value={r.updated_at as string | null} /></td>
      </tr>)}</Table> : <Empty>No import run metadata is available.</Empty>}
    </Panel>
    <Panel title="Application record types" description="Exact counts of the records REVEAL stores, by type, with each type's latest update.">
      {data.counts.length ? <Table label="Application record types" headings={["Record type", "Exact count", "Last update"]}>{data.counts.map(c => <tr key={c.kind}>
        <td>{c.kind}</td><td>{number(c.count)}</td><td><Age value={c.updated_at} /></td>
      </tr>)}</Table> : <Empty>No application records are stored yet.</Empty>}
    </Panel>
  </>;
}
