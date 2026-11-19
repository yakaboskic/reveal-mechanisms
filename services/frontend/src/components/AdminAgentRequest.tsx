"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { useState } from "react";
import { deliverableLabel, fileSize, type AgentLimitUse, type AgentRunContent } from "@/lib/admin-job";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { usd } from "./AdminCosts";

export const LIMIT_LABELS: [string, string][] = [["timeout_seconds", "Run time"], ["max_turns", "Turns"], ["max_budget_usd", "Budget"], ["tool_calls", "REVEAL tool calls"], ["max_output_tokens", "Output per response"]];
export const limitValue = (key: string, value: number) => key === "max_budget_usd" ? usd(value) : key === "timeout_seconds" ? duration(value) : key === "max_output_tokens" ? `${value.toLocaleString()} tokens` : value.toLocaleString();
const shown = (key: string, value: number | null | undefined) => value == null ? "—" : limitValue(key, value);
const yesNo = (value: boolean | null | undefined) => value == null ? "—" : value ? "Yes" : "No";
const list = (values: string[] | null | undefined) => values == null ? "—" : values.length ? values.join(", ") : "None";
const SOURCES: Record<string, string> = {
  captured: "the agent's full final message, from its response record",
  job_result: "the job result's copy",
};
const UNAVAILABLE: Record<string, string> = {
  not_captured: "no response record was captured", too_large: "the response record is too large to read here",
  unreadable: "the response record is unreadable or belongs to another run", unavailable: "the saved files are unavailable",
};

function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState("");
  return <button type="button" className="text-button" aria-label={`Copy ${label.toLowerCase()}`} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState("Copied"); } catch { setState("Copy is unavailable in this browser"); }
  }}>{state || "Copy"}</button>;
}

function Text({ label, text, empty }: { label: string; text: string | null; empty: string }) {
  return <div className="admin-request-text">
    <div className="admin-request-text-heading"><h4>{label}</h4>{text != null && <CopyButton text={text} label={label} />}</div>
    {text != null ? <pre>{text}</pre> : <p>{empty}</p>}
  </div>;
}

function requested(row: AgentLimitUse) {
  if (row.requested_basis === "recorded") return row.requested ? shown(row.key, row.frozen) : "Not set: default";
  return <>Not recorded<small>{row.differs_from_current_default ? "Differs from today's default" : row.differs_from_current_default === false ? "Same as today's default" : ""}</small></>;
}

function used(row: AgentLimitUse) {
  if (row.key === "tool_calls") return <>Not counted on their own{row.all_tool_calls != null && <small>{row.all_tool_calls.toLocaleString()} tool calls of every kind</small>}</>;
  if (row.key === "max_output_tokens") return row.reached == null ? "—" : row.reached ? "A response reached it" : "Not reached";
  if (row.used == null) return "—";
  return <>{limitValue(row.key, row.used)}{row.used_estimated && <small>Estimated from streamed tokens</small>}</>;
}

/** Requested | Frozen | Ceiling | Used for every limit, with the one the run hit highlighted. */
function LimitsTable({ rows }: { rows: AgentLimitUse[] }) {
  return <div className="admin-table-scroll" tabIndex={0}><table aria-label="Limits: requested, frozen, ceiling and used"><thead><tr>{["Limit", "Requested", "Frozen", "Ceiling", "Used"].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
    {LIMIT_LABELS.map(([key, label]) => {
      const row = rows.find(r => r.key === key);
      if (!row) return null;
      return <tr key={key} className={row.hit ? "admin-limit-hit" : undefined}>
        <td>{label}{row.hit && <strong className="admin-warning"> Hit</strong>}</td>
        <td>{requested(row)}</td>
        <td>{shown(key, row.frozen)}{row.applied != null && row.frozen != null && row.applied !== row.frozen && <small>The run was given {limitValue(key, row.applied)}</small>}</td>
        <td>{shown(key, row.ceiling)}{row.ceiling_basis === "current" && <small>Today&apos;s ceiling: not recorded when queued</small>}</td>
        <td>{used(row)}</td>
      </tr>;
    })}
  </tbody></table></div>;
}

/** The content of one agent job: what was asked, the limits and why it stopped, and what came back. */
export function AgentRequestView({ data }: { data: AgentRunContent }) {
  const { input, response, stop } = data;
  const account = input.account;
  const settings: [string, React.ReactNode][] = [
    ["Asked to return", deliverableLabel(input.deliverable)],
    ["Model · effort", [input.model, input.effort].filter(Boolean).join(" · ") || "—"],
    ["Built-in tools", list(input.tools)],
    ["Graphs", list(input.graphs)],
    ["Network", input.network || "—"],
    ["Extra hosts", list(input.allowed_domains)],
    ["Claims repository lookups", yesNo(input.claims_repository)],
    ...(account ? [["Requester named in the account", yesNo(account.attribute_requester)]] as [string, React.ReactNode][] : []),
    ["Input format", input.format || "—"],
    ["Owner workspace", <span key="owner" className="admin-mono">{data.job.owner}</span>],
  ];
  return <div className="admin-request">
    <p className={`admin-stop-reason${stop.limit ? " hit" : ""}`} role="status"><strong>{stop.text}</strong></p>
    {data.job.failure && <p>{[data.job.failure.code, data.job.failure.message].filter(Boolean).join(": ")}</p>}
    <Text label="Prompt" text={input.prompt} empty="No prompt was recorded." />
    <Text label="System prompt" text={input.system_prompt} empty="No system prompt." />
    {account && <>
      <Text label="Question" text={account.question} empty="No question was recorded." />
      <h4>Claims named by the requester</h4>
      {account.claims.length ? <ol className="admin-request-claims">{account.claims.map((claim, i) => <li key={i}>{claim.statement}
        {claim.subject_entity && <small className="admin-mono">{claim.subject_entity} · {claim.relation} · {claim.object_entity}</small>}</li>)}</ol> : <p>No claims named.</p>}
    </>}
    {input.output_schema != null && <Text label="Output schema" text={JSON.stringify(input.output_schema, null, 2)} empty="" />}
    <h4>Settings</h4>
    <dl className="admin-detail-grid">{settings.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <h4>Limits</h4>
    <LimitsTable rows={data.limits} />
    <details className="admin-request-response" open={response.text != null && response.text.length <= 4000}>
      <summary>Response{response.characters != null ? ` · ${response.characters.toLocaleString()} characters` : ""}{response.truncated ? " · truncated" : ""}</summary>
      {response.source ? <p>From {SOURCES[response.source]}{response.attempt ? ` (attempt ${response.attempt})` : ""}{response.truncated ? ", cut at 50,000 characters" : ""}.{response.truncated && response.captured_unavailable ? ` The full text is not available: ${UNAVAILABLE[response.captured_unavailable] ?? response.captured_unavailable}.` : ""}</p>
        : <p>No response was returned{response.captured_unavailable ? `: ${UNAVAILABLE[response.captured_unavailable] ?? response.captured_unavailable}` : ""}.</p>}
      {response.text != null && <Text label="Response text" text={response.text} empty="" />}
    </details>
    {response.structured_output != null && <details className="admin-request-response"><summary>Structured output</summary><Text label="Structured output" text={JSON.stringify(response.structured_output, null, 2)} empty="" /></details>}
    <h4>Output files</h4>
    {response.files.length ? <ul>{response.files.map((file, i) => <li key={i}><span className="admin-mono">{file.path || "—"}</span> · {fileSize(file.size_bytes)}</li>)}</ul> : <p>No output files.</p>}
    <p><small>Read by {data.read.by} at {date(data.read.at)}. Every read of this section is recorded with the admin and the time.</small></p>
  </div>;
}

/** Loads the request only when asked: each load is a recorded read, so the console's refresh never repeats it. */
export function AgentRequest({ jobId }: { jobId: string }) {
  const [data, setData] = useState<AgentRunContent | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  async function load() {
    setLoading(true); setError("");
    try {
      const response = await fetch(`/api/admin/jobs/${encodeURIComponent(jobId)}/run`, { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The request is unavailable.");
      setData(result);
    } catch (e) { setError(e instanceof Error ? e.message : "The request is unavailable."); }
    finally { setLoading(false); }
  }
  return <section className="admin-request-section" aria-label="Request">
    <div className="admin-section-heading"><div><h3>Request</h3><p>The prompt, settings and limits exactly as the job was asked, and its full response. Each time you show it, your email and the time are recorded.</p></div>
      {data ? <button type="button" className="text-button" onClick={() => setData(null)}>Hide request</button>
        : <button type="button" className="submit" disabled={loading} onClick={() => void load()}>{loading ? "Loading…" : "Show request"}</button>}</div>
    {error && <p role="alert" className="error">{error}</p>}
    {data && <AgentRequestView data={data} />}
  </section>;
}
