import type { Schema } from "./client";
import { activityProgress, activitySections, activityStage, type ActivityRow } from "./activity";

export type StepState = "working" | "completed" | "failed" | "stopped" | "unavailable";
const isTerminal = (status: string) => ["succeeded", "failed", "cancelled", "insufficient_evidence"].includes(status);
const timestamp = (value?: string | null) => { const time = value ? Date.parse(value) : NaN; return Number.isFinite(time) ? time : null; };
const duration = (start: number | null, end: number | null) => start == null || end == null ? null : Math.max(0, end - start);

export function elapsedLabel(milliseconds: number | null) {
  if (milliseconds == null) return "Timing unavailable";
  const seconds = Math.floor(milliseconds / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m ${seconds % 60}s`;
}

/** Durations use recorded boundaries; an incomplete replay never invents a start. */
export function timedActivitySections(events: Schema<"JobEvent">[], job: Schema<"Job">, now: number) {
  const progress = activityProgress(job, events), active = !isTerminal(progress.status);
  const sections = activitySections(events), currentStage = activityStage(progress.stage);
  if (sections.at(-1)?.stage !== currentStage) sections.push({ id: "pending", stage: currentStage, events: [] });
  const last = events.at(-1);
  const finishedAt = last && isTerminal(last.status) && BigInt(last.id) >= BigInt(job.last_event_id)
    ? timestamp(last.occurred_at) : timestamp(job.completed_at);
  return sections.map((section, index) => {
    const current = index === sections.length - 1;
    const state: StepState = current ? active ? "working" : progress.status === "failed" ? "failed" : progress.status === "succeeded" ? "completed" : "stopped" : "completed";
    const start = timestamp(section.events[0]?.occurred_at) ?? (current && progress.stage === "queued" ? timestamp(job.created_at) : null);
    const end = current ? active ? now : finishedAt : timestamp(sections[index + 1].events[0]?.occurred_at);
    return { ...section, state, current, start, end, durationMs: duration(start, end) };
  });
}

/** Operational messages mark sequential worker steps; tools retain explicit call/result lifecycles. */
export function operationalStep(row: ActivityRow, followingRows: ActivityRow[], section: { state: StepState; end: number | null }, now: number) {
  const event = row.event;
  const next = followingRows.find(item => !["agent_message", "tool_call", "tool_result"].includes(item.event.detail?.kind || ""));
  const explicit = event.detail?.state;
  const state: StepState = explicit === "failed" ? "failed" : explicit === "unavailable" ? "unavailable" : explicit === "completed" || next ? "completed" : section.state;
  const end = next ? timestamp(next.event.occurred_at) : state === "working" ? now : section.end;
  // A point-in-time completion notice without a recorded start has no duration.
  const durationMs = event.detail?.duration_ms ?? (["completed", "failed", "unavailable"].includes(explicit || "") ? null : duration(timestamp(event.occurred_at), end));
  return { state, durationMs };
}

export function toolElapsed(row: ActivityRow, active: boolean, now: number) {
  const detail = row.result?.detail || (row.event.detail?.kind === "tool_result" ? row.event.detail : null);
  if (detail?.duration_ms != null) return detail.duration_ms;
  if (row.result) return duration(timestamp(row.event.occurred_at), timestamp(row.result.occurred_at));
  // Completed legacy results and interrupted calls lack a reliable end boundary.
  return active && row.event.detail?.kind === "tool_call" ? duration(timestamp(row.event.occurred_at), now) : null;
}
