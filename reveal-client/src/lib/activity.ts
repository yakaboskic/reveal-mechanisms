import type { JobEvent } from "./types";

/** Presentation only; immutable historical events retain their original text. */
export function activityMessage(event: JobEvent) {
  if (event.stage === "collecting_output" && event.detail?.source === "worker") {
    if (event.message === "Capturing completed output and evidence.") return "Preserving available output and evidence.";
    if (["Restoring the saved execution result and evidence.", "Restoring saved execution result and evidence."].includes(event.message)) return "Restoring saved output and evidence.";
  }
  return event.message;
}

export function activityStageLabel(stage: string) {
  return stage === "collecting_output" ? "Preserving output and evidence" : stage.replaceAll("_", " ");
}
