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
