import { api, type Schema } from "./client";
import { allWorkspacePages, isSavedDraft } from "./workspace";
import type { LoadMode } from "./revalidation-cache";
import { parseReferenceState, type ReferenceState } from "./reference";

export const workspaceTabs = ["gaps", "accounts", "drafts", "explorations", "runs"] as const;
export type WorkspaceTab = typeof workspaceTabs[number];
/**
 * One cache entry per listing. Accounts and explorations add an EAGGL reference filter (`:current` or
 * `:archived`; `all` adds nothing) and a normalized, encoded search (`?query`); gaps keep the plain tab key.
 */
export const searchableWorkspaceTab = (tab: WorkspaceTab): tab is "accounts" | "explorations" => tab === "accounts" || tab === "explorations";
export type WorkspaceKey = WorkspaceTab | `${"accounts" | "explorations"}${"" | ":current" | ":archived"}${"" | `?${string}`}`;
export function workspaceKey(tab: WorkspaceTab, query = "", reference: ReferenceState = "all"): WorkspaceKey {
  if (!searchableWorkspaceTab(tab)) return tab;
  const normalized = query.trim().replace(/\s+/g, " ");
  const listing = reference === "all" ? tab : `${tab}:${reference}` as const;
  return normalized ? `${listing}?${encodeURIComponent(normalized)}` : listing;
}
export function workspaceKeyParts(key: WorkspaceKey): { tab: WorkspaceTab; query: string; reference: ReferenceState } {
  const [listing, query = ""] = key.split("?");
  const [tab, reference] = listing.split(":");
  return { tab: tab as WorkspaceTab, query: decodeURIComponent(query), reference: parseReferenceState(reference) };
}
export type WorkspaceData = {
  gaps: Schema<"Exploration">[]; accounts: Schema<"AccountSummary">[]; outcomes: Schema<"AnalysisOutcomeSummary">[];
  drafts: Schema<"Draft">[]; jobs: Schema<"Job">[]; requests: Schema<"ResearchRequest">[];
  cursor: string | null; pages: number;
};
const blank: WorkspaceData = { gaps: [], accounts: [], outcomes: [], drafts: [], jobs: [], requests: [], cursor: null, pages: 0 };

async function listing<T>(fetch: (cursor?: string) => Promise<{ items: T[]; page: Schema<"Page"> }>, id: (item: T) => string,
  previous: T[], depth: number, append: boolean, cursor: string | null) {
  let next = append ? cursor : null;
  const items = new Map((append ? previous : []).map(item => [id(item), item]));
  const seen = new Set<string>(); let pages = 0;
  do {
    const result = await fetch(next || undefined); pages++;
    for (const item of result.items) items.set(id(item), item);
    next = result.page.next_cursor;
    if (next && seen.has(next)) throw new Error("Workspace history changed. Please refresh it.");
    if (next) seen.add(next);
  } while (next && !append && pages < depth);
  return { items: [...items.values()], cursor: next, pages: append ? depth + 1 : pages };
}

export async function loadWorkspaceData(key: WorkspaceKey, previous: WorkspaceData | undefined, mode: LoadMode, signal: AbortSignal): Promise<WorkspaceData> {
  const { tab, query, reference } = workspaceKeyParts(key);
  const saved = previous || blank;
  if (mode === "activity" && previous && (tab === "gaps" || tab === "runs")) {
    const jobs = await allWorkspacePages(cursor => api.jobs(cursor, signal));
    const known = new Set(previous.requests.map(request => request.id));
    const requests = jobs.some(job => job.research_request_id && !known.has(job.research_request_id))
      ? await allWorkspacePages(cursor => api.requests(cursor, signal)) : previous.requests;
    return { ...previous, jobs: tab === "runs" ? jobs.filter(job => job.kind === "analysis") : jobs, requests };
  }
  const append = mode === "append" && !!previous;
  if (append && !saved.cursor) return saved;
  const depth = Math.max(1, saved.pages);
  if (tab === "drafts") {
    const list = await listing(cursor => api.drafts(cursor, signal), item => item.id, saved.drafts, depth, append, saved.cursor);
    return { ...saved, drafts: list.items.filter(isSavedDraft), cursor: list.cursor, pages: list.pages };
  }
  if (tab === "runs") {
    const [jobs, requests] = await Promise.all([
      allWorkspacePages(cursor => api.jobs(cursor, signal)),
      allWorkspacePages(cursor => api.requests(cursor, signal)),
    ]);
    return { ...saved, jobs: jobs.filter(job => job.kind === "analysis"), requests, cursor: null, pages: 1 };
  }
  if (tab === "accounts") {
    const list = await listing(cursor => api.accounts(cursor, signal, query || undefined, reference), item => item.account.id, saved.accounts, depth, append, saved.cursor);
    return { ...saved, accounts: list.items, cursor: list.cursor, pages: list.pages };
  }
  if (tab === "explorations") {
    const list = await listing(cursor => api.outcomes(cursor, signal, query || undefined, reference), item => item.id, saved.outcomes, depth, append, saved.cursor);
    return { ...saved, outcomes: list.items, cursor: list.cursor, pages: list.pages };
  }
  const [list, drafts, jobs, requests] = await Promise.all([
    listing(cursor => api.explorations(cursor, signal), item => item.source_gap.id, saved.gaps, depth, append, saved.cursor),
    append ? saved.drafts : allWorkspacePages(cursor => api.drafts(cursor, signal)),
    append ? saved.jobs : allWorkspacePages(cursor => api.jobs(cursor, signal)),
    append ? saved.requests : allWorkspacePages(cursor => api.requests(cursor, signal)),
  ]);
  return { ...saved, gaps: list.items, drafts: drafts.filter(isSavedDraft), jobs, requests, cursor: list.cursor, pages: list.pages };
}
