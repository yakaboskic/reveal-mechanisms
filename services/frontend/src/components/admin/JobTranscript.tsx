"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import type { TranscriptPage, TranscriptTurn } from "@/lib/admin-job";
import { callFailed, itemFailed, MISSING_TRANSCRIPT, toolInput, transcriptItems, type TranscriptItem } from "@/lib/admin-transcript";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { usd } from "../AdminCosts";
import { Callout, Json, Status } from "./ui";

/** The transcript as loaded so far, held by the page: each page is a recorded read, loaded only when asked. */
export type TranscriptState = { data: TranscriptPage | null; loading: boolean; error: string };
export const NO_TRANSCRIPT: TranscriptState = { data: null, loading: false, error: "" };

const ROLES: Record<string, string> = { assistant: "Assistant", user: "User", tool: "Tool result", system: "Claude Code", result: "Run ended" };
const number = (value: unknown) => typeof value === "number" ? value : null;

function Marks({ turn }: { turn: TranscriptTurn }) {
  return <>
    {turn.partial && <Status tone="warn" title="Claude Code never finished this message: the run stopped mid-message, or its end fell in a cut. It is rebuilt from the streamed pieces.">Unfinished</Status>}
    {turn.truncated && <Status tone="warn" title="The text is cut to its first 20,000 characters.">Cut at 20,000 characters</Status>}
  </>;
}

function Meta({ turn, label }: { turn: TranscriptTurn; label: React.ReactNode }) {
  return <div className="admin-log-meta"><span>#{turn.index}</span><span>{label}</span>{turn.at && <time>{date(turn.at)}</time>}<Marks turn={turn} /></div>;
}

function ToolResult({ result }: { result: TranscriptTurn }) {
  return <details className="admin-turn-result" open={result.is_error}>
    <summary>{result.is_error ? "Failed result" : "Result"} · #{result.index}{result.text != null ? ` · ${result.text.length.toLocaleString()} characters` : ""}{result.truncated ? " · cut at 20,000 characters" : ""}</summary>
    {result.text ? <pre>{result.text}</pre> : <p>The result is empty.</p>}
  </details>;
}

function Call({ call, result, more }: { call: TranscriptTurn; result: TranscriptTurn | null; more: boolean }) {
  const input = toolInput(call.tool_input);
  return <>
    <Meta turn={call} label={<>Tool call{call.call_id ? <> · <code title={call.call_id}>{call.call_id.slice(-8)}</code></> : ""}</>} />
    <strong className="admin-tool-name">{call.tool_name || "Unnamed tool"}{result?.is_error && <> <Status tone="bad">Failed</Status></>}</strong>
    {input ? <details open={!!result?.is_error}><summary>Arguments{input.parsed ? "" : " · as received, not valid JSON"}{call.tool_input_truncated ? " · cut at 20,000 characters" : ""}</summary><pre>{input.text}</pre></details> : <p className="admin-muted">No arguments.</p>}
    {result ? <ToolResult result={result} /> : <p className="admin-muted">{more ? "No result yet among the loaded turns; it may be on a later page." : "No result was recorded for this call."}</p>}
  </>;
}

function ResultDetail({ detail }: { detail: Record<string, unknown> }) {
  const parts = [
    typeof detail.subtype === "string" && detail.subtype, typeof detail.stop_reason === "string" && `stop reason ${detail.stop_reason}`,
    number(detail.num_turns) != null && `${number(detail.num_turns)!.toLocaleString()} turns`,
    number(detail.duration_ms) != null && `${duration(number(detail.duration_ms)! / 1000)} (API ${duration(number(detail.duration_api_ms) == null ? null : number(detail.duration_api_ms)! / 1000)})`,
    number(detail.total_cost_usd) != null && usd(number(detail.total_cost_usd)),
  ].filter(Boolean);
  return parts.length ? <p><small>{parts.join(" · ")}</small></p> : null;
}

function Turn({ turn }: { turn: TranscriptTurn }) {
  if (turn.kind === "thinking") return <>
    <Meta turn={turn} label="Thinking" />
    <details><summary>Thinking · {(turn.text || "").length.toLocaleString()} characters</summary><pre>{turn.text}</pre></details>
  </>;
  if (turn.kind === "result") return <>
    <Meta turn={turn} label={<>{ROLES.result} {turn.is_error ? <Status tone="bad">Error</Status> : <Status tone="good">Finished</Status>}</>} />
    {turn.text && <p>{turn.text}</p>}
    {turn.detail && <ResultDetail detail={turn.detail} />}
  </>;
  // A cut stands where the middle of the stream was left out.
  const cut = turn.role === "system" && turn.detail && "omitted_bytes" in turn.detail;
  return <>
    <Meta turn={turn} label={ROLES[turn.role] ?? turn.role} />
    {cut ? <Callout tone="warn">{turn.text}</Callout> : turn.text ? <p>{turn.text}</p> : <p className="admin-muted">No text.</p>}
    {turn.detail && !cut && <details><summary>Details</summary><pre>{JSON.stringify(turn.detail, null, 2)}</pre></details>}
  </>;
}

function Item({ item, more }: { item: TranscriptItem; more: boolean }) {
  const failed = itemFailed(item);
  const key = item.type === "call" ? item.call : item.type === "result" ? item.result : item.turn;
  return <article className={`admin-log-event admin-turn ${key.kind}${failed ? " failed" : ""}`}>
    {item.type === "call" ? <Call call={item.call} result={item.result} more={more} />
      : item.type === "result" ? <>
        <Meta turn={item.result} label={<>Tool result{item.result.tool_name ? ` · ${item.result.tool_name}` : ""}</>} />
        <p className="admin-muted">Its call is not among the loaded turns{item.result.call_id ? "" : " (no call id was recorded)"}.</p>
        <ToolResult result={item.result} />
      </> : <Turn turn={item.turn} />}
  </article>;
}

/** Section 4: Claude Code's stream for one attempt, turn by turn, paged. Tool calls carry their results, paired by call
 * id over every loaded page; failed calls are highlighted; thinking is folded; stderr comes last. */
export function TranscriptSection({ transcript, onLoad, onMore }: { transcript: TranscriptState; onLoad: (attempt?: number) => void; onMore: () => void }) {
  const { data, loading, error } = transcript;
  const attempts = data?.latest_attempt ? Array.from({ length: data.latest_attempt }, (_, i) => data.latest_attempt! - i) : [];
  if (!data) return <div className="admin-transcript">
    {error && <Callout tone="bad" role="alert">{error}</Callout>}
    <p className="admin-muted">Not loaded. Each page of the transcript is a recorded read with your email and the time; the page never refreshes it.</p>
    <button type="button" className="submit" disabled={loading} onClick={() => onLoad()}>{loading ? "Loading…" : "Show transcript"}</button>
  </div>;
  const items = transcriptItems(data.turns);
  const failures = items.filter(callFailed).length;
  return <div className="admin-transcript">
    {error && <Callout tone="bad" role="alert">{error} Showing the turns already loaded.</Callout>}
    <div className="admin-log-actions">
      {attempts.length > 1 ? <label className="admin-select"><span>Attempt</span><select aria-label="Attempt" value={data.attempt ?? ""} disabled={loading} onChange={e => onLoad(Number(e.target.value))}>
        {attempts.map(a => <option key={a} value={a}>Attempt {a}{a === data.latest_attempt ? " (newest)" : ""}</option>)}</select></label>
        : <span>{data.attempt == null ? "No attempt has started." : `Attempt ${data.attempt}`}</span>}
      <span>{data.turns.length.toLocaleString()} of {data.total.toLocaleString()} turns loaded{failures ? ` · ${failures.toLocaleString()} failed tool call${failures === 1 ? "" : "s"}` : ""}{data.skipped ? ` · ${data.skipped.toLocaleString()} notices skipped` : ""}</span>
      <button type="button" className="text-button" disabled={loading} onClick={() => onLoad(data.attempt ?? undefined)}>Read from the start again</button>
    </div>
    {data.missing && <Callout tone="warn">{MISSING_TRANSCRIPT[data.missing] ?? `No transcript: ${data.missing.replaceAll("_", " ")}.`}</Callout>}
    {data.cut && <Callout tone="warn">The stored stream had its middle left out to fit the 20 MB capture limit; a marked turn stands where it was.</Callout>}
    {data.complete === false && <Callout tone="warn">The stored file did not end properly: the runner stopped while writing it. These are the turns that could be read.</Callout>}
    {!!items.length && <div className="admin-event-log admin-transcript-turns" role="region" aria-label="Transcript turns" tabIndex={0}>
      {items.map(item => <Item key={(item.type === "call" ? item.call : item.type === "result" ? item.result : item.turn).index} item={item} more={data.has_more} />)}
    </div>}
    {!items.length && !data.missing && <p className="admin-empty">The transcript has no turns.</p>}
    {data.has_more && <div className="admin-log-actions"><button type="button" className="submit" disabled={loading} onClick={onMore}>{loading ? "Loading…" : "Load more turns"}</button><span>Each page is a recorded read.</span></div>}
    <h4>Claude Code&apos;s stderr</h4>
    {data.stderr == null ? <p>No stderr was captured.</p> : data.stderr.unreadable ? <p>The stored stderr is not readable.</p> : <>
      {(data.stderr.truncated || data.stderr.cut) && <p className="admin-muted">{[data.stderr.truncated && "Its last 20,000 characters.", data.stderr.cut && "The stored file had its middle left out."].filter(Boolean).join(" ")}</p>}
      {data.stderr.text ? <pre aria-label="Claude Code's stderr">{data.stderr.text}</pre> : <p>Empty.</p>}
    </>}
    <p><small>Last page read by {data.read.by} at {date(data.read.at)}.</small></p>
    <Json value={{ ...data, turns: `${data.turns.length} turns, shown above` }} label="Raw transcript JSON (the last page, without its turns)" />
  </div>;
}
