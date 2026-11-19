import type { TranscriptPage, TranscriptTurn } from "./admin-job";

/** One row of a transcript as the job detail shows it. A tool call carries its result, paired by call_id; a result whose
 * call is not in the loaded turns (a null call_id, or a call left out by a cut) stands alone; everything else is a turn. */
export type TranscriptItem =
  | { type: "turn"; turn: TranscriptTurn }
  | { type: "call"; call: TranscriptTurn; result: TranscriptTurn | null }
  | { type: "result"; result: TranscriptTurn };

/** Pairs every tool call with its result over all the turns loaded so far, so a result on a later page joins its call. */
export function transcriptItems(turns: TranscriptTurn[]): TranscriptItem[] {
  const calls = new Set<string>();
  const results = new Map<string, TranscriptTurn>();
  for (const turn of turns) {
    if (turn.kind === "tool_use" && turn.call_id) calls.add(turn.call_id);
    if (turn.kind === "tool_result" && turn.call_id && !results.has(turn.call_id)) results.set(turn.call_id, turn);
  }
  const items: TranscriptItem[] = [];
  for (const turn of turns) {
    if (turn.kind === "tool_use") items.push({ type: "call", call: turn, result: turn.call_id ? results.get(turn.call_id) ?? null : null });
    else if (turn.kind === "tool_result") {
      // The first result of a loaded call is shown with the call; any other (none, or a repeat) stands alone.
      if (!(turn.call_id && calls.has(turn.call_id) && results.get(turn.call_id) === turn)) items.push({ type: "result", result: turn });
    } else items.push({ type: "turn", turn });
  }
  return items;
}

/** Whether a tool call failed: a call whose result failed, or a failed result on its own. */
export function callFailed(item: TranscriptItem): boolean {
  return item.type === "call" ? !!item.result?.is_error : item.type === "result" && item.result.is_error;
}

/** Whether a row shows a failure: a call whose result failed, a failed result on its own, or a run that ended in error. */
export function itemFailed(item: TranscriptItem): boolean {
  if (item.type === "call") return !!item.result?.is_error;
  if (item.type === "result") return item.result.is_error;
  return item.turn.is_error;
}

/** A tool's input for reading: pretty JSON when it parses; otherwise the text as received (a partial call's input is the
 * JSON so far, and a cut one ends early). */
export function toolInput(text: string | null): { text: string; parsed: boolean } | null {
  if (text == null) return null;
  try { return { text: JSON.stringify(JSON.parse(text), null, 2), parsed: true }; } catch { return { text, parsed: false }; }
}

/** The transcript after loading one more page: the new turns appended once each (by index), the rest from the newer page. */
export function appendPage(current: TranscriptPage | null, next: TranscriptPage): TranscriptPage {
  if (!current || current.attempt !== next.attempt) return next;
  const last = current.turns.length ? current.turns[current.turns.length - 1].index : -1;
  return { ...next, turns: [...current.turns, ...next.turns.filter(turn => turn.index > last)] };
}

export const MISSING_TRANSCRIPT: Record<string, string> = {
  not_captured: "No transcript was captured for this attempt: it ran before transcripts were kept, never started Claude Code, or has not been captured yet.",
  too_large: "The transcript is too large to read here.",
  unreadable: "The stored transcript is not a readable gzip file.",
  unavailable: "Storage could not be read. Retry shortly.",
};
