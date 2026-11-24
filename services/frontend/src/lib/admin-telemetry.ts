import { jobLabel, type AdminJob, type AgentJobs } from "./admin-job";
import type { AdminCosts } from "@/components/AdminCosts";
import type { RuntimeMetrics } from "@/components/AdminPerformance";

/** Who an owner id is. kind: "registered" (a person who signed in), "api_key" (a workspace that only holds an
 *  operator-issued key; named when an operator registered it under a name), "client" or "application" (a partner
 *  frontend's shared identity) or "anonymous" (a browser workspace nobody signed in to, so there is no name). name
 *  and email are what the sign-in supplied. api_key: this deployment has a key configured for the workspace. */
export type OwnerLabel = { kind: string; name: string | null; email: string | null; sign_in: string | null; api_key?: boolean };
export type Owners = Record<string, OwnerLabel>;

/** What to call an owner: the name or email its sign-in supplied, else the kind of workspace. Null when the id is no
 *  known principal (a system owner, or a backend that does not report owners). */
export function ownerName(label: OwnerLabel | null | undefined): string | null {
  if (!label) return null;
  return label.name || label.email
    || (label.kind === "anonymous" ? "Anonymous workspace" : label.kind === "api_key" ? "API key workspace" : "Signed in, no name on record");
}

/** The line under the name: the email and sign-in provider of a person, or what kind of shared identity this is. */
export function ownerNote(label: OwnerLabel | null | undefined): string | null {
  if (!label) return null;
  if (label.kind === "api_key") return "API key";
  if (label.kind === "client") return "Shared client identity";
  if (label.kind === "application") return "Application identity";
  if (label.kind === "anonymous") return null;
  return [label.name ? label.email : null, label.sign_in, label.api_key ? "has an API key" : null].filter(Boolean).join(" · ") || null;
}

/** One line for a list or a filter option: "Ada Lovelace · 13b86465", or the whole id when the owner has no label. */
export const ownerLine = (id: string, owners: Owners = {}) => {
  const name = ownerName(owners[id]);
  return name ? `${name} · ${id.slice(0, 8)}` : id;
};

/** GET /api/admin/telemetry: the 15-second snapshot every admin page reads. Sizes and counts, never content; the
 *  owners directory is the one personal projection. */
export type Snapshot = {
  generated_at: string; query_ms: number; database: string;
  counts: { kind: string; count: number; updated_at: string }[];
  recent: { kind: string; id: string; owner: string; version: number; updated_at: string }[];
  statuses: { status: string; count: number }[]; jobs: AdminJob[]; costs?: AdminCosts;
  // Every owner id in this snapshot that is a principal. Absent from backends older than owner names.
  owners?: Owners;
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
export function filterJobs(jobs: AdminJob[], { q, kind, status, owner }: JobFilter, owners: Owners = {}): AdminJob[] {
  const text = (q || "").toLowerCase();
  return jobs.filter(j => (!kind || j.kind === kind)
    && (!status || (status === "active" ? isActive(j.status) : j.status === status))
    && (!owner || j.owner === owner)
    && (!text || `${j.id} ${j.owner} ${ownerName(owners[j.owner]) || ""} ${owners[j.owner]?.email || ""} ${jobLabel(j)} ${j.status} ${j.worker || ""}`.toLowerCase().includes(text)));
}

/** The distinct values of one field, in first-seen order, for a filter's options. */
export const distinct = <T,>(items: T[], value: (item: T) => string) => [...new Set(items.map(value))];
