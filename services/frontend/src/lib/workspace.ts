import { terminal, type Schema } from "./client";

export async function allWorkspacePages<T>(fetchPage: (cursor?: string) => Promise<{ items: T[]; page: Schema<"Page"> }>) {
  const items: T[] = [], seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const result = await fetchPage(cursor); items.push(...result.items);
    cursor = result.page.next_cursor || undefined;
    if (cursor && seen.has(cursor)) throw new Error("Workspace history changed. Please reload it.");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

export const isSavedDraft = (draft: Schema<"Draft">) => draft.lifecycle !== "temporary";
export const draftHref = (id: string) => `/drafts/${encodeURIComponent(id)}`;
export const runHref = (id: string) => `/runs/${encodeURIComponent(id)}`;

export function workspaceRuns(jobs: Schema<"Job">[], requests: Schema<"ResearchRequest">[], _drafts?: Schema<"Draft">[]) {
  const frozen = new Map(requests.map(request => [request.id, request]));
  const activeDrafts = new Set<string>(), byGap = new Map<string, Schema<"Job">[]>(), byDraft = new Map<string, Schema<"Job">[]>();
  for (const job of jobs) {
    if (job.kind !== "analysis") continue;
    const request = frozen.get(job.research_request_id!);
    if (!request) continue;
    byDraft.set(request.source_draft_id, [...(byDraft.get(request.source_draft_id) || []), job]);
    if (!terminal(job.status)) activeDrafts.add(request.source_draft_id);
    const gap = request.composer.source_gap?.id;
    if (gap) byGap.set(gap, [...(byGap.get(gap) || []), job]);
  }
  for (const runs of [...byGap.values(), ...byDraft.values()]) runs.sort((a, b) => Number(terminal(a.status)) - Number(terminal(b.status)) || b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
  // A saved draft is explicit retained work. Job state cannot hide it or
  // change its editor destination; a run reads its own frozen request.
  const href = (job: Schema<"Job">) => runHref(job.id);
  return { activeDrafts, byGap, byDraft, isDraftVisible: isSavedDraft, href };
}
