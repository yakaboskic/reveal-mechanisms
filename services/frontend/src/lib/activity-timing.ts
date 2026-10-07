import type { Schema } from "./client";
import { activityProgress, activitySections, activityStage, type ActivityRow, type ActivityStage } from "./activity";

export type StepState = "working" | "completed" | "ended" | "failed" | "stopped" | "unavailable";
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

export function stageStateLabel(stage: ActivityStage, state: StepState) {
  if (state === "failed") return "Could not complete";
  if (state === "stopped") return "Stopped";
  if (state === "ended") return "Ended";
  if (state === "unavailable") return "Unavailable";
  if (state === "working") return stage === "collection" ? "Preserving" : "Working";
  return stage === "collection" ? "Preserved" : stage === "research" ? "Finished" : "Complete";
}

/** Durations use recorded boundaries; an incomplete replay never invents a start. */
export function timedActivitySections(events: Schema<"JobEvent">[], job: Schema<"Job">, now: number) {
  const progress = activityProgress(job, events), active = !isTerminal(progress.status);
  const sections = activitySections(events);
  let currentStage = activityStage(progress.stage);
  if (currentStage === "research" && (sections.at(-1)?.stage === "outcome" ||
    progress.status === "failed" && sections.at(-1)?.stage === "collection")) currentStage = "outcome";
  if (sections.at(-1)?.stage !== currentStage) sections.push({ id: "pending", stage: currentStage, events: [] });
  const last = events.at(-1);
  const finishedAt = last && isTerminal(last.status) && BigInt(last.id) >= BigInt(job.last_event_id)
    ? timestamp(last.occurred_at) : timestamp(job.completed_at);
  const failedResearch = new Set<number>();
  sections.forEach((section, index) => {
    if (section.stage !== "outcome") return;
    // Attribute a late authoring failure to this attempt, never to an earlier
    // failed/retried attempt or to the successful act of preserving evidence.
    for (let previous = index - 1; previous >= 0; previous--) {
      if (sections[previous].stage === "research") { failedResearch.add(previous); break; }
    }
  });
  return sections.map((section, index) => {
    const current = index === sections.length - 1;
    const ended = section.events.at(-1)?.status;
    const failedNotice = section.events.findLast(event => event.detail?.kind === "preparation" && event.detail.state === "failed");
    const handoff = sections[index + 1]?.stage === "collection" ? sections[index + 1].events[0] : undefined;
    const agentNotice = handoff?.detail?.kind === "preparation" && handoff.detail.source === "harness" ? handoff : undefined;
    const state: StepState = failedNotice || failedResearch.has(index) || agentNotice?.detail?.state === "failed" ? "failed"
      : current ? active ? "working" : progress.status === "failed" ? "failed" : progress.status === "succeeded" ? "completed" : "stopped"
      : ended === "failed" ? "failed" : ended === "cancelled" || ended === "insufficient_evidence" ? "stopped"
      : section.stage === "research" && !agentNotice ? "ended" : "completed";
    const start = timestamp(section.events[0]?.occurred_at) ?? (current && progress.stage === "queued" ? timestamp(job.created_at) : null);
    const end = failedNotice ? timestamp(failedNotice.occurred_at) : current ? active ? now : finishedAt : ended && isTerminal(ended) ? timestamp(section.events.at(-1)?.occurred_at) : timestamp(sections[index + 1].events[0]?.occurred_at);
    return { ...section, state, current, start, end, durationMs: section.stage === "outcome" ? null : duration(start, end) };
  });
}

/** Operational messages mark sequential worker steps; tools retain explicit call/result lifecycles. */
export function operationalStep(row: ActivityRow, followingRows: ActivityRow[], section: { state: StepState; end: number | null; stage?: ActivityStage }, now: number) {
  const event = row.event;
  if (section.stage === "outcome") return { state: section.state, durationMs: null };
  const next = followingRows.find(item => !["agent_message", "tool_call", "tool_result"].includes(item.event.detail?.kind || ""));
  // Older saved agent-completion notices started the collection stage with a
  // "started" detail. They mark authoring's end, not capture's start. Actual
  // capture operations are worker events and retain their elapsed timers.
  const legacyCompletion = event.stage === "collecting_output" && event.detail?.kind === "preparation" &&
    event.detail.source === "harness" && event.detail.state === "started";
  const explicit = legacyCompletion ? "completed" : event.detail?.state;
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
