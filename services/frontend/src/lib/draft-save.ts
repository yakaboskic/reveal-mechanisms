import type { Schema } from "./client";

export type DraftSaveAttempt = {
  navigation: string; owner: string; key: string; draft: Schema<"Draft"> | null;
  composer: Schema<"Composer">; name: string;
};
const storageKey = "reveal:explicit-save";
/** Only an explicit Save intent may survive reload; ordinary edits are never written. */
export function rememberDraftSave(attempt: DraftSaveAttempt | null, completedKey?: string) {
  try {
    if (attempt) sessionStorage.setItem(storageKey, JSON.stringify(attempt));
    else if (!completedKey || JSON.parse(sessionStorage.getItem(storageKey) || "null")?.key === completedKey) sessionStorage.removeItem(storageKey);
  } catch { /* The in-memory Save still works in restricted browsers. */ }
}
export function restoreDraftSave(navigation: string, owner: string): DraftSaveAttempt | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) || "null") as DraftSaveAttempt | null;
    if (value?.navigation !== navigation) return null;
    if (value.owner === owner && typeof value.key === "string" && value.name?.trim()
      && value.composer?.source_gap && Array.isArray(value.composer.eaggl_anchors)) return value;
    rememberDraftSave(null);
  } catch { rememberDraftSave(null); }
  return null;
}
