import type { Schema } from "./client";

/** Presentation only: the event store and its replay IDs remain unchanged. */
export function coalesceMessageDeltas(events: Schema<"JobEvent">[]): Schema<"JobEvent">[] {
  const rows: Schema<"JobEvent">[] = [];
  for (const event of events) {
    const previous = rows.at(-1);
    const delta = event.detail?.kind === "agent_message" && event.detail.message_delta === true;
    if (delta && previous?.detail?.kind === "agent_message" && previous.detail.message_delta === true &&
      previous.job_id === event.job_id && previous.stage === event.stage && previous.status === event.status) {
      rows[rows.length - 1] = { ...previous, message: previous.message + event.message };
    } else rows.push(event);
  }
  return rows;
}

type Event = Schema<"JobEvent">;
export type ActivityStage = "preparation" | "setup" | "research" | "validation" | "saving";
export const stageLabels: Record<ActivityStage, string> = {
  preparation: "Evidence preparation", setup: "Runtime setup", research: "Research agent",
  validation: "Scientific validation", saving: "Saving results",
};

export function activityStage(stage: Schema<"Job">["stage"]): ActivityStage {
  if (["queued", "freezing_inputs", "retrieving_cfde", "preparing_evidence"].includes(stage)) return "preparation";
  if (stage === "starting_agent") return "setup";
  if (stage === "validating") return "validation";
  if (stage === "persisting" || stage === "complete") return "saving";
  return "research";
}

/** Keep repeated authoring/validation attempts in their original order. */
export function activitySections(events: Event[]) {
  const sections: { id: string; stage: ActivityStage; events: Event[] }[] = [];
  for (const event of events) {
    const stage = activityStage(event.stage);
    const previous = sections.at(-1);
    if (previous?.stage === stage) previous.events.push(event);
    else sections.push({ id: event.id, stage, events: [event] });
  }
  return sections;
}

export type ActivityRow = { event: Event; result?: Event };
/** Pair explicit tool lifecycles only; narrative never implies completion. */
export function activityRows(events: Event[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  const calls = new Map<string, ActivityRow>();
  for (const event of coalesceMessageDeltas(events)) {
    const id = event.detail?.call_id;
    const call = id ? calls.get(id) : undefined;
    if (event.detail?.kind === "tool_result" && call && !call.result) {
      call.result = event;
    } else {
      const row = { event };
      rows.push(row);
      if (id && event.detail?.kind === "tool_call") calls.set(id, row);
    }
  }
  return rows;
}

/** SSE is authoritative as it arrives, including before the next job fetch. */
export function activityProgress(job: Schema<"Job">, events: Event[]) {
  const latest = events.at(-1);
  return latest && BigInt(latest.id) >= BigInt(job.last_event_id)
    ? { status: latest.status, stage: latest.stage } : { status: job.status, stage: job.stage };
}
