import { jobLabel, type AdminJob, type AgentJobs } from "./admin-job";
import type { AdminCosts } from "@/components/AdminCosts";
import type { RuntimeMetrics } from "@/components/AdminPerformance";

/** GET /api/admin/telemetry: the 15-second snapshot every admin page reads. Sizes and counts, never content. */
export type Snapshot = {
  generated_at: string; query_ms: number; database: string;
  counts: { kind: string; count: number; updated_at: string }[];
  recent: { kind: string; id: string; owner: string; version: number; updated_at: string }[];
  statuses: { status: string; count: number }[]; jobs: AdminJob[]; costs?: AdminCosts;
  // Absent from backends older than agent-job types.
  job_kinds?: { kind: string; count: number }[]; agent_jobs?: AgentJobs;
  events: { id: string; job_id: string; occurred_at: string; event_type: string; status: string; stage: string }[];
  tables: { name: string; present: boolean; estimated_rows: number | null; bytes: number | null }[];
  imports: Record<string, string | number | null>[];
  runtime: RuntimeMetrics;
};

export const TERMINAL = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);
export const isActive = (status: string) => !TERMINAL.has(status);

/** The five totals at the top of the overview. Statuses count every job ever queued; records are exact. */
export function totals(data: Pick<Snapshot, "counts" | "statuses" | "costs">) {
  return {
    records: data.counts.reduce((n, c) => n + c.count, 0),
    active: data.statuses.filter(s => isActive(s.status)).reduce((n, s) => n + s.count, 0),
    failed: data.statuses.find(s => s.status === "failed")?.count || 0,
    identities: data.counts.find(c => c.kind === "principal")?.count || 0,
    weekSpend: data.costs?.totals.week.spend_usd,
  };
}

/** The Jobs list's filters. status "active" means any state that has not finished. */
export type JobFilter = { q?: string | null; kind?: string | null; status?: string | null; owner?: string | null };
export function filterJobs(jobs: AdminJob[], { q, kind, status, owner }: JobFilter): AdminJob[] {
  const text = (q || "").toLowerCase();
  return jobs.filter(j => (!kind || j.kind === kind)
    && (!status || (status === "active" ? isActive(j.status) : j.status === status))
    && (!owner || j.owner === owner)
    && (!text || `${j.id} ${j.owner} ${jobLabel(j)} ${j.status} ${j.worker || ""}`.toLowerCase().includes(text)));
}

/** The distinct values of one field, in first-seen order, for a filter's options. */
export const distinct = <T,>(items: T[], value: (item: T) => string) => [...new Set(items.map(value))];
