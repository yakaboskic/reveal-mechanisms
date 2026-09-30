/** The URL chooses the question; browser storage only recovers that selection. */
export type ComposerSelection = { job: string | null; draft: string | null; gap: string | null };
type SavedSelection = { job?: { id: string } | null; draft?: { id: string } | null; gap?: { object: { id: string } } | null };

export function composerSelection(params: Pick<URLSearchParams, "get">): ComposerSelection {
  return { job: params.get("job") || null, draft: params.get("draft") || null, gap: params.get("gap") || null };
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
  for (const key of ["job", "draft", "gap"] as const) {
    if (selection[key]) next.searchParams.set(key, selection[key]);
    else next.searchParams.delete(key);
  }
  next.searchParams.delete("error");
  return next;
}
