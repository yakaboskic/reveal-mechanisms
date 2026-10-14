import type { Schema } from "./client";
import { publicFreshMs } from "./page-return";

export type LeaderboardView = Schema<"LeaderboardView">;
export type LeaderboardSort = Schema<"LeaderboardSort">;
export type LeaderboardMetric = Schema<"LeaderboardMetric">;
export type LeaderboardEvidence = Schema<"LeaderboardEvidence">;
export type LeaderboardScope = Schema<"LeaderboardScope">;
export type LeaderboardEntry = Schema<"LeaderboardEntry">;
export type LeaderboardQuery = { view: LeaderboardView; sort: LeaderboardSort; evidence: LeaderboardEvidence; scope: LeaderboardScope; selected: string | null; metric: LeaderboardMetric };

export const leaderboardViews: { id: LeaderboardView; label: string }[] = [
  { id: "researchers", label: "Researchers" }, { id: "accounts", label: "Scientific accounts" }, { id: "datasets", label: "Datasets" },
];
export const leaderboardSorts: Record<LeaderboardView, { id: LeaderboardSort; label: string }[]> = {
  researchers: [{ id: "overall", label: "Overall contribution" }, { id: "accounts", label: "Most public accounts" }, { id: "votes", label: "Community recognition" }, { id: "gaps", label: "Broadest gap coverage" }, { id: "explored", label: "Most gaps explored" }],
  accounts: [{ id: "votes", label: "Highest rated" }],
  datasets: [{ id: "accounts", label: "Most accounts informed" }, { id: "claims", label: "Most claims informed" }, { id: "traced_claims", label: "Most claims traced" }, { id: "gaps", label: "Broadest gap coverage" }, { id: "researchers", label: "Widest researcher adoption" }],
};
const metrics: Record<LeaderboardView, LeaderboardMetric[]> = {
  researchers: ["accounts", "votes", "gaps", "explored"],
  accounts: ["accounts", "votes", "claims", "gaps", "researchers"],
  datasets: ["accounts", "claims", "traced_claims", "traced_accounts", "gaps", "researchers", "files"],
};
export const leaderboardScopes: { id: LeaderboardScope; label: string }[] = [{ id: "published", label: "Published accounts" }, { id: "all", label: "All accounts" }];
/** All accounts rank datasets only by their stored traces; private accounts and claims are never listed. */
export const sortsFor = (view: LeaderboardView, scope: LeaderboardScope) => scope === "all" ? leaderboardSorts.datasets.filter(item => item.id === "traced_claims") : leaderboardSorts[view];
const metricsFor = (view: LeaderboardView, scope: LeaderboardScope): LeaderboardMetric[] => scope === "all" ? ["traced_claims", "traced_accounts"] : metrics[view];
export const sortMetric = (sort: LeaderboardSort): LeaderboardMetric => sort === "overall" ? "accounts" : sort;

/** Ignore unsupported URL state rather than issuing a mismatched ranking request. */
export function readLeaderboardQuery(params: Pick<URLSearchParams, "get">): LeaderboardQuery {
  const requestedView = params.get("view") || params.get("tab");
  const view = leaderboardViews.find(item => item.id === requestedView)?.id || "researchers";
  const scope: LeaderboardScope = view === "datasets" && params.get("scope") === "all" ? "all" : "published";
  const sort = sortsFor(view, scope).find(item => item.id === params.get("sort"))?.id || sortsFor(view, scope)[0].id;
  const metric = metricsFor(view, scope).find(item => item === params.get("metric")) || sortMetric(sort);
  return { view, sort, evidence: view === "datasets" && scope === "published" && params.get("evidence") === "supporting" ? "supporting" : "all", scope, selected: params.get("selected") || null, metric };
}

export function leaderboardHref(query: LeaderboardQuery, change: Partial<LeaderboardQuery> = {}) {
  const next = { ...query, ...change };
  const params = new URLSearchParams({ view: next.view, sort: next.sort });
  if (next.view === "datasets" && next.scope === "all") params.set("scope", next.scope);
  if (next.view === "datasets" && next.evidence === "supporting") params.set("evidence", next.evidence);
  if (next.selected) { params.set("selected", next.selected); params.set("metric", next.metric); }
  return `/leaderboard?${params}`;
}

export class LeaderboardPageChangedError extends Error {}

export function mergeLeaderboardPages(previous: Schema<"LeaderboardList"> | null, incoming: Schema<"LeaderboardList">, query: Pick<LeaderboardQuery, "view" | "sort" | "evidence" | "scope">) {
  if (incoming.view !== query.view || incoming.sort !== query.sort || incoming.evidence !== query.evidence || incoming.scope !== query.scope) throw new Error("These rankings do not match the selected view. Please retry.");
  if (previous && previous.page.snapshot_id !== incoming.page.snapshot_id) throw new LeaderboardPageChangedError("The public rankings changed. Reload the first page to see current results.");
  if (incoming.page.has_more && (!incoming.page.next_cursor || incoming.page.next_cursor === previous?.page.next_cursor)) throw new LeaderboardPageChangedError("The next ranking page could not be verified. Reload the rankings.");
  const rows = new Map<string, LeaderboardEntry>();
  const expectedKind = ({ researchers: "researcher", accounts: "account", datasets: "dataset" } as const)[query.view];
  for (const row of [...(previous?.items || []), ...incoming.items]) {
    if (row.kind !== expectedKind) throw new Error("A ranking record belongs to a different view. Please retry.");
    rows.set(row.id, row);
  }
  return { ...incoming, items: [...rows.values()] };
}

/** What returning to the page (from a dataset's source, an organization's record, another tab) does to rankings or
 * drilldown records: nothing while a load is in flight or the rows are fresh; a full load when nothing current is shown;
 * otherwise a quiet revalidation, which keeps the rows and the reader's place unless the rankings changed. */
export function leaderboardReturn(state: { busy: boolean; shown: boolean; failed: boolean; loadedAt: number; now: number }): "none" | "load" | "revalidate" {
  if (state.busy) return "none";
  if (!state.shown || state.failed) return "load";
  return state.now - state.loadedAt >= publicFreshMs ? "revalidate" : "none";
}

/** Public drilldowns must stay on known scientific pages, never arbitrary URLs. */
export function publicLeaderboardHref(value: string) {
  if (!value.startsWith("/") || value.startsWith("//")) return null;
  try {
    const url = new URL(value, "https://reveal.invalid");
    if (url.origin !== "https://reveal.invalid") return null;
    return /^\/(accounts|claims|knowledge-gaps|analyses|id)\/[^/]+$/.test(url.pathname) || url.pathname === "/leaderboard" ? `${url.pathname}${url.search}${url.hash}` : null;
  } catch { return null; }
}

export const verifiedOrcidHref = (value: string | null) => value && /^https:\/\/orcid\.org\/\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/.test(value) ? value : null;

export function formatLeaderboardNumber(value: number, score = false) {
  return new Intl.NumberFormat("en", { maximumFractionDigits: score ? 1 : 0, minimumFractionDigits: score ? 1 : 0 }).format(value);
}
