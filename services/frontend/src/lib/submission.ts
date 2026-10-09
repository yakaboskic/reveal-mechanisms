import type { Schema } from "./client";
import { currentComposer } from "./reference";
import type { ResearchMode } from "./lightning-audit";

export const submissionStorageKey = "reveal:submission";
export type SubmissionMethod = "anonymous" | "google" | "orcid" | "session";
export type SubmissionStage = "signing-in" | "saving" | "submitting";
export type SubmissionAttempt = {
  method: SubmissionMethod;
  mode?: ResearchMode;
  question: string;
  gap: Schema<"GapRecord"> | null;
  composer: Schema<"Composer">;
  draft: Schema<"Draft"> | null;
  owner: string | null;
  anonymousKey: string;
  requestKeys: [string, string][];
  submitKey: { binding: string; key: string } | null;
};

/** Owner-derived idempotency namespaces cannot safely replay a possibly committed paid audit. */
export const lightningSubmissionOwnerChanged = (attempt: SubmissionAttempt | null, owner: string | undefined) =>
  !!attempt && attempt.mode === "lightning" && !!attempt.submitKey && !!attempt.owner && !!owner && attempt.owner !== owner;

/** Receipt reconciliation reads the original dispatch; current editor validation applies only to a new run. */
export function submissionActionDisabled({ ready, busy, uncertain, newInputsValid }: {
  ready: boolean; busy: boolean; uncertain: boolean; newInputsValid: boolean;
}) {
  return !ready || busy || (!uncertain && !newInputsValid);
}

export function rememberSubmission(attempt: SubmissionAttempt | null) {
  try {
    if (attempt) sessionStorage.setItem(submissionStorageKey, JSON.stringify(attempt));
    else sessionStorage.removeItem(submissionStorageKey);
  } catch { /* A restricted browser can still complete the current attempt in memory. */ }
}

export function restoreSubmission(): SubmissionAttempt | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(submissionStorageKey) || "null") as SubmissionAttempt | null;
    if (value && (value.mode === undefined || ["online", "local", "lightning"].includes(value.mode)) && ["anonymous", "google", "orcid", "session"].includes(value.method) && value.composer?.source_gap && value.composer.eaggl_anchors.length && Array.isArray(value.requestKeys) && value.requestKeys.every(entry => Array.isArray(entry) && entry.length === 2 && entry.every(part => typeof part === "string")) && typeof value.anonymousKey === "string"
      // An uncertain dispatch may already have committed. Preserve its exact receipt so a retry
      // reconciles that job even after a reference cutover; unsubmitted stale inputs are discarded.
      && (value.submitKey || currentComposer(value.composer))) return value;
  } catch { /* Ignore an incomplete browser snapshot. */ }
  rememberSubmission(null);
  return null;
}
