import React from "react";
import { formatAdminDuration as duration } from "../lib/admin-time";
import { Empty, PageHeading, Panel, Table } from "./admin/ui";

/** Per-request database cost the API adds to its http rows (runtime_metrics.REQUEST_FIELDS); other rows omit it. */
export type RuntimeRow = {
  category: string; name: string; count: number; errors: number; mean_ms: number; p95_ms: number;
  mean_statements?: number; mean_db_ms?: number; mean_pool_wait_ms?: number; mean_lock_wait_ms?: number; mean_connects?: number; mean_resets?: number;
};
export type RuntimeMetrics = { pid: number; uptime_seconds: number; scope: string; rows: RuntimeRow[] };
export const LOCK_NOTE = "FENCE is the global write-lock wait plus one round trip; LOCK_HOLD runs from the lock grant to COMMIT or ROLLBACK.";
const HEADINGS = ["Operation", "Requests", "Errors", "Mean", "Recent p95", "Statements / req", "DB ms / req", "Pool wait", "Lock wait", "Connects / req", "Resets / req"];
const count = (value?: number) => value == null ? "—" : String(value);
const ms = (value?: number) => value == null ? "—" : `${value} ms`;

export function AdminPerformance({ runtime }: { runtime: RuntimeMetrics }) {
  return <section aria-label="Runtime metrics">
    <PageHeading title="Performance" subtitle={`Process ${runtime.pid} · uptime ${duration(runtime.uptime_seconds)}`} />
    <Panel title="API and database latency" description={<>{runtime.scope && <span className="admin-block">{runtime.scope}</span>}{!runtime.scope?.includes("FENCE") && <span className="admin-block">{LOCK_NOTE}</span>}</>}>
      {runtime.rows.length ? <Table label="API and database latency" headings={HEADINGS}>{runtime.rows.map(r => <tr key={`${r.category}-${r.name}`}><td><small>{r.category}</small>{r.name}</td><td>{r.count.toLocaleString()}</td><td>{r.errors.toLocaleString()}</td><td>{r.mean_ms} ms</td><td>{r.p95_ms} ms</td>
        <td>{count(r.mean_statements)}</td><td>{ms(r.mean_db_ms)}</td><td>{ms(r.mean_pool_wait_ms)}</td><td>{ms(r.mean_lock_wait_ms)}</td><td>{count(r.mean_connects)}</td><td>{count(r.mean_resets)}</td></tr>)}</Table>
        : <Empty>This process has served no measured request yet.</Empty>}
    </Panel>
    <Panel title="Paper fetches by host" description="Failures and latency of the agents' recorded fetch commands, by host." padded>
      <Empty>Not recorded yet: the runtime metrics carry no per-host fetch figures, and the agents&apos; fetch commands are not aggregated.</Empty>
    </Panel>
  </section>;
}
