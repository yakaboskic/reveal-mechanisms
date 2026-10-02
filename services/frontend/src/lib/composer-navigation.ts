/** Editor and run identities are separate; legacy query links remain readable. */
export type ComposerSelection = { job: string | null; draft: string | null; gap: string | null };
type SavedSelection = { job?: { id: string } | null; draft?: { id: string } | null; gap?: { object: { id: string } } | null };

export function composerSelection(params: Pick<URLSearchParams, "get">, pathname = "/"): ComposerSelection {
  const route = /^\/(drafts|runs)\/([^/]+)\/?$/.exec(pathname);
  const job = route?.[1] === "runs" ? decodeURIComponent(route[2]) : params.get("job") || null;
  if (job) return { job, draft: null, gap: null };
  return { job: null, draft: route?.[1] === "drafts" ? decodeURIComponent(route[2]) : params.get("draft") || null, gap: params.get("gap") || null };
}
export function selectionKey(selection: ComposerSelection) {
  return JSON.stringify([selection.job, selection.draft, selection.gap]);
}
export function hasSelection(selection: ComposerSelection) {
  return !!(selection.job || selection.draft || selection.gap);
}
export function matchesSavedSelection(selection: ComposerSelection, saved: SavedSelection | null) {
  return !!saved && hasSelection(selection)
    && (!selection.job || selection.job === saved.job?.id)
    && (!selection.draft || selection.draft === saved.draft?.id)
    && (!selection.gap || selection.gap === saved.gap?.object.id);
}
export function questionSelection(draftId?: string | null, gapId?: string | null): ComposerSelection {
  return { job: null, draft: draftId || null, gap: draftId ? null : gapId || null };
}
export function selectionUrl(url: URL, selection: ComposerSelection) {
  const next = new URL(url);
  next.pathname = selection.job ? `/runs/${encodeURIComponent(selection.job)}` : selection.draft ? `/drafts/${encodeURIComponent(selection.draft)}` : "/";
  for (const key of ["job", "draft", "gap"]) next.searchParams.delete(key);
  if (!selection.job && !selection.draft && selection.gap) next.searchParams.set("gap", selection.gap);
  next.searchParams.delete("error");
  return next;
}
/** A draft replacing one dropped at a reference cutover is adopted in place: the same question
 * under a new draft id. Work begun under the dropped draft's selection carries on under it. */
export type AdoptedSelection = { from: string; to: string };
export function followsSelection(begun: string, shown: string, adopted: AdoptedSelection | null) {
  return begun === shown || (!!adopted && adopted.from === begun && adopted.to === shown);
}
