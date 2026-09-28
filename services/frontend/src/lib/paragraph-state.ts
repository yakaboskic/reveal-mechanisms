import type { Schema } from "./client";

/** A job result is authoritative only for its own account and paragraph. */
export function paragraphStateFromJob(job: Schema<"Job">, accountId: string): Schema<"ParagraphState"> | null {
  if (job.kind !== "paragraph" || job.input_account_id !== accountId || job.status === "insufficient_evidence") return null;
  if (job.status === "succeeded" && (job.result?.kind !== "paragraph" || job.result.account_id !== accountId)) return null;
  return {
    status: job.status === "cancel_requested" ? "running" : job.status,
    job_id: job.id,
    paragraph_id: job.result?.kind === "paragraph" ? job.result.paragraph_id : null,
  };
}
