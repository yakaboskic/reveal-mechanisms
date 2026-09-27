import type { Schema } from "./client";

export const submissionStorageKey = "reveal:submission";
export type SubmissionMethod = "anonymous" | "google" | "orcid" | "session";
export type SubmissionStage = "signing-in" | "saving" | "submitting";
export type SubmissionAttempt = {
  method: SubmissionMethod;
  question: string;
  gap: Schema<"GapRecord"> | null;
  composer: Schema<"Composer">;
  draft: Schema<"Draft"> | null;
  owner: string | null;
  anonymousKey: string;
  requestKeys: [string, string][];
  submitKey: { binding: string; key: string } | null;
};

export function rememberSubmission(attempt: SubmissionAttempt | null) {
  try {
    if (attempt) sessionStorage.setItem(submissionStorageKey, JSON.stringify(attempt));
    else sessionStorage.removeItem(submissionStorageKey);
  } catch { /* A restricted browser can still complete the current attempt in memory. */ }
}

export function restoreSubmission(): SubmissionAttempt | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(submissionStorageKey) || "null") as SubmissionAttempt | null;
    if (value && ["anonymous", "google", "orcid", "session"].includes(value.method) && value.composer?.source_gap && value.composer.eaggl_anchors.length && Array.isArray(value.requestKeys) && value.requestKeys.every(entry => Array.isArray(entry) && entry.length === 2 && entry.every(part => typeof part === "string")) && typeof value.anonymousKey === "string") return value;
  } catch { /* Ignore an incomplete browser snapshot. */ }
  rememberSubmission(null);
  return null;
}
