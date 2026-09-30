import { api, type Schema } from "./client";
import { allWorkspacePages } from "./workspace";
import type { LoadMode } from "./revalidation-cache";

export const workspaceTabs = ["gaps", "accounts", "explorations"] as const;
export type WorkspaceTab = typeof workspaceTabs[number];
export type WorkspaceKey = WorkspaceTab | `${WorkspaceTab}?${string}`;
export function workspaceKey(tab: WorkspaceTab, query = ""): WorkspaceKey {
  const normalized = query.trim().replace(/\s+/g, " ");
  return normalized && tab !== "gaps" ? `${tab}?${encodeURIComponent(normalized)}` : tab;
}
export function workspaceKeyParts(key: WorkspaceKey): { tab: WorkspaceTab; query: string } {
  const [tab, query = ""] = key.split("?");
  return { tab: tab as WorkspaceTab, query: decodeURIComponent(query) };
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
  const { tab, query } = workspaceKeyParts(key);
  const saved = previous || blank;
  if (mode === "activity" && previous && tab === "gaps") {
    const jobs = await allWorkspacePages(cursor => api.jobs(cursor, signal));
    const known = new Set(previous.requests.map(request => request.id));
    const requests = jobs.some(job => job.research_request_id && !known.has(job.research_request_id))
      ? await allWorkspacePages(cursor => api.requests(cursor, signal)) : previous.requests;
    return { ...previous, jobs, requests };
  }
  const append = mode === "append" && !!previous;
  if (append && !saved.cursor) return saved;
  const depth = Math.max(1, saved.pages);
  if (tab === "accounts") {
    const list = await listing(cursor => api.accounts(cursor, signal, query || undefined), item => item.account.id, saved.accounts, depth, append, saved.cursor);
    return { ...saved, accounts: list.items, cursor: list.cursor, pages: list.pages };
  }
  if (tab === "explorations") {
    const list = await listing(cursor => api.outcomes(cursor, signal, query || undefined), item => item.id, saved.outcomes, depth, append, saved.cursor);
    return { ...saved, outcomes: list.items, cursor: list.cursor, pages: list.pages };
  }
  const [list, drafts, jobs, requests] = await Promise.all([
    listing(cursor => api.explorations(cursor, signal), item => item.source_gap.id, saved.gaps, depth, append, saved.cursor),
    append ? saved.drafts : allWorkspacePages(cursor => api.drafts(cursor, signal)),
    append ? saved.jobs : allWorkspacePages(cursor => api.jobs(cursor, signal)),
    append ? saved.requests : allWorkspacePages(cursor => api.requests(cursor, signal)),
  ]);
  return { ...saved, gaps: list.items, drafts, jobs, requests, cursor: list.cursor, pages: list.pages };
}
