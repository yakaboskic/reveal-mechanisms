"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { useState, type ReactNode } from "react";
import { accountSource, deliverableLabel, depositOutcome, fileSize, screenSummary, shortSha, type AgentLimitUse, type AgentRun, type AgentRunContent, type RunConfiguration } from "@/lib/admin-job";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { usd } from "../AdminCosts";
import { asTone, Callout, Json, KeyValues, Status } from "./ui";

// Sections 2 (Prompt and settings) and 3 (Response) of the job detail. Settings, sizes, the deposit, the sources and the
// screen come from the job record (read under the telemetry assertion, refreshed with the page). The prompt, response,
// limits table and configuration come from GET /api/admin/jobs/{id}/run, a recorded read loaded only when asked.

export const LIMIT_LABELS: [string, string][] = [["timeout_seconds", "Run time"], ["max_turns", "Turns"], ["max_budget_usd", "Budget"], ["tool_calls", "REVEAL tool calls"], ["max_output_tokens", "Output per response"]];
export const limitValue = (key: string, value: number) => key === "max_budget_usd" ? usd(value) : key === "timeout_seconds" ? duration(value) : key === "max_output_tokens" ? `${value.toLocaleString()} tokens` : value.toLocaleString();
const shown = (key: string, value: number | null | undefined) => value == null ? "—" : limitValue(key, value);
const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const plural = (value: number, noun: string) => `${value.toLocaleString()} ${noun}${value === 1 ? "" : "s"}`;
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
const NO_CONFIGURATION: Record<string, string> = {
  not_captured: "No runtime record was captured for this attempt.",
  not_recorded: "This run is from before the configuration was recorded.",
  too_large: "The runtime record is too large to read here.",
  unreadable: "The runtime record is unreadable or belongs to another run.",
  unavailable: "The saved files are unavailable. Retry shortly.",
};

/** The recorded read of one agent job, held once by the page and shared by the sections that show it. */
export type RunState = { data: AgentRunContent | null; loading: boolean; error: string };
export const NO_RUN: RunState = { data: null, loading: false, error: "" };

/** Copies a long text; unlike the id copy button, its label names the text instead of repeating it. */
export function CopyText({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState("");
  return <button type="button" className="text-button" aria-label={`Copy ${label.toLowerCase()}`} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState("Copied"); } catch { setState("Copy is unavailable in this browser"); }
  }}>{state || "Copy"}</button>;
}

/** A labelled block of text with a copy button; `empty` says what a missing value means. */
export function Text({ label, text, empty, prose = false, copyLabel }: { label: string; text: string | null; empty: string; prose?: boolean; copyLabel?: string }) {
  return <div className="admin-request-text">
    <div className="admin-request-text-heading"><h4>{label}</h4>{text != null && <CopyText text={text} label={copyLabel ?? label} />}</div>
    {text != null ? prose ? <div className="admin-answer">{text}</div> : <pre>{text}</pre> : <p>{empty}</p>}
  </div>;
}

/** Shows or reads again the recorded request; each press is one recorded read. */
export function RunButton({ run, onLoad }: { run: RunState; onLoad: () => void }) {
  return <button type="button" className="submit" disabled={run.loading} onClick={onLoad}>{run.loading ? "Loading…" : run.data ? "Read again" : "Show prompt and response"}</button>;
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

/** Requested | Frozen | Ceiling | Used for every limit, with the one the run hit highlighted. `given` is the frozen input's
 * limits: a limit it lacks was not part of the input. */
export function LimitsTable({ rows, given }: { rows: AgentLimitUse[]; given: Record<string, unknown> | null | undefined }) {
  return <div className="admin-table-scroll" tabIndex={0}><table aria-label="Limits: requested, frozen, ceiling and used"><thead><tr>{["Limit", "Requested", "Frozen", "Ceiling", "Used"].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
    {LIMIT_LABELS.map(([key, label]) => {
      const row = rows.find(r => r.key === key);
      if (!row) return null;
      return <tr key={key} className={row.hit ? "admin-limit-hit" : undefined}>
        <td>{label}{row.hit && <strong className="admin-warning"> Hit</strong>}</td>
        <td>{requested(row)}</td>
        <td>{row.frozen == null && given && !(key in given) ? "Not part of this input" : shown(key, row.frozen)}{row.applied != null && row.frozen != null && row.applied !== row.frozen && <small>The run was given {limitValue(key, row.applied)}</small>}</td>
        <td>{shown(key, row.ceiling)}{row.ceiling_basis === "current" && <small>Today&apos;s ceiling: not recorded when queued</small>}</td>
        <td>{used(row)}</td>
      </tr>;
    })}
  </tbody></table></div>;
}

/** The limits as the job record froze them, for when the recorded read is not loaded. */
function GivenLimits({ limits }: { limits: Record<string, number> | null }) {
  if (!limits) return <p>No limits were recorded for this job.</p>;
  return <KeyValues rows={LIMIT_LABELS.map(([key, label]) => [label, limits[key] == null ? "Not part of this input" : limitValue(key, limits[key])])} />;
}

function claimsAsked(run: AgentRun) {
  if (run.claims_requested == null) return null;
  return run.claims_requested ? `${plural(run.claims_requested, "claim")} to assess, ${run.claims_with_triple ?? 0} with a triple` : "No claims named";
}

/** Every setting of the frozen input, with the sizes of the prompt, question and claims. From the job record. */
export function settingsRows(run: AgentRun): [string, ReactNode][] {
  const account = run.deliverable === "scientific_account";
  return [
    ["Asked to return", deliverableLabel(run.deliverable)],
    ["Model · effort", [run.model, run.effort].filter(Boolean).join(" · ") || "—"],
    ["Built-in tools", list(run.tools)],
    ["Claims repository lookups", yesNo(run.claims_repository)],
    ["Graphs", list(run.graphs)],
    ["Network", run.network || "—"],
    ["Extra hosts", list(run.allowed_domains)],
    ["Prompt", run.prompt_characters == null ? "—" : `${count(run.prompt_characters)} characters${run.system_prompt_characters ? ` · system prompt ${count(run.system_prompt_characters)} characters` : " · no system prompt"}`],
    ...(account ? [
      ["Question", run.question_characters == null ? "—" : `${count(run.question_characters)} characters`],
      ["Claims named by the requester", claimsAsked(run) ?? "—"],
      ["Requester named in the account", yesNo(run.attribute_requester)],
    ] as [string, ReactNode][] : []),
    ...(run.output_schema_characters != null ? [
      ["Structured output", `Asked for, schema ${count(run.output_schema_characters)} characters · ${run.result.structured_output_characters == null ? "none returned" : `returned ${count(run.result.structured_output_characters)} characters`}`],
    ] as [string, ReactNode][] : []),
    ["Input format", run.input_format || "—"],
  ];
}

const code = (value: unknown) => value == null || value === "" ? "—" : <code className="admin-mono">{typeof value === "string" ? value : JSON.stringify(value)}</code>;
const words = (value: string[] | null | undefined) => value == null ? "—" : value.length ? <code className="admin-mono">{value.join(" ")}</code> : "None";
const entries = (value: Record<string, unknown> | null | undefined) => value == null ? "—" : Object.keys(value).length
  ? Object.entries(value).map(([k, v]) => `${k} ${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · ") : "None";
const limitsLine = (value: Record<string, number | null> | null | undefined) => value == null ? "—"
  : Object.entries(value).map(([key, v]) => `${LIMIT_LABELS.find(([k]) => k === key)?.[1] ?? key} ${v == null ? "—" : limitValue(key, v)}`).join(" · ") || "None";

/** How the attempt's Claude Code was started: its command line and environment, the tools and MCP servers it was given
 * and what it reported having, the network policy, the toolchain and the transcript files. */
export function ConfigurationView({ configuration }: { configuration: RunConfiguration | null | undefined }) {
  if (!configuration) return <p>This backend does not report how Claude Code was started.</p>;
  const c = configuration;
  const why = c.missing ? NO_CONFIGURATION[c.missing] ?? c.missing.replaceAll("_", " ") : null;
  const observed = c.observed;
  const rows: [string, ReactNode][] = [
    ["Attempt", c.attempt == null ? "No attempt has started" : String(c.attempt)],
    ["Claude Code", c.claude_code ? `pinned ${c.claude_code.pinned_version || "—"} · observed ${c.claude_code.observed_version || "—"}` : "—"],
    ["Arguments", words(c.cli?.argv)],
    ["Started by", Array.isArray(c.cli?.wrapper) ? words(c.cli.wrapper) : code(c.cli?.wrapper)],
    ["Prompt sent on", c.cli?.prompt === "stdin" ? "Standard input (the prompt above)" : code(c.cli?.prompt)],
    ["Environment", c.cli?.environment ? <KeyValues rows={Object.entries(c.cli.environment).map(([k, v]) => [k, <code key={k} className="admin-mono">{v}</code>])} /> : "—"],
    ["Built-in tools given", list(c.tools?.builtin)],
    ["REVEAL MCP tools given", list(c.tools?.mcp)],
    ["MCP servers", c.tools?.mcp_servers ? code(c.tools.mcp_servers) : "—"],
    ["Network (Box egress)", c.network ? c.network.mode === "allow-all" ? "Open: every host" : `Restricted to ${list(c.network.allowed_domains)}` : "—"],
    ["Toolchain stamp", c.toolchain?.stamp ? <code className="admin-mono" title={c.toolchain.stamp}>{shortSha(c.toolchain.stamp)}</code> : "—"],
    ["Account kit", c.account_kit ? <>{c.account_kit.skill || "—"}<small> skill sha256 {shortSha(c.account_kit.skill_sha256)} · schema excerpt sha256 {shortSha(c.account_kit.schema_excerpt_sha256)}</small></> : "Not an account job"],
    ["Limits enforced", limitsLine(c.limits?.execution)],
    ["Limits frozen with the job", limitsLine(c.limits?.agent)],
    ["Fetch and convert tools", c.source_tools ? <>{entries(c.source_tools.packages)}<small>{entries(c.source_tools.programs)}</small></> : "—"],
    ["Reported at start", observed ? `Claude Code ${observed.claude_code_version || "—"} · ${observed.model || "—"} · permission mode ${observed.permission_mode || "—"}` : "Not reported"],
    ["Tools it reported", observed ? list(observed.tools) : "—"],
    ["MCP servers it reported", observed?.mcp_servers ? observed.mcp_servers.length ? observed.mcp_servers.map(s => `${s.name} (${s.status})`).join(", ") : "None" : "—"],
    ["Transcript files", c.transcript ? <ul>{Object.entries(c.transcript).map(([name, file]) => <li key={name}><span className="admin-mono">{name}</span> · {fileSize(file.stored_bytes)} stored, {fileSize(file.original_bytes)} written{file.cut ? <> · <b className="admin-warning">middle left out ({fileSize(file.omitted_bytes)}, {count(file.omitted_lines)} lines)</b></> : ""}</li>)}</ul> : "—"],
  ];
  return <>{why && <Callout tone="warn">{why}</Callout>}<KeyValues rows={rows} /></>;
}

/** Section 2: what the job was asked, every setting and limit, and how Claude Code was started. */
export function PromptSettingsSection({ agent, run }: { agent: AgentRun | null | undefined; run: RunState }) {
  const data = run.data;
  const input = data?.input;
  const account = input?.account;
  return <div className="admin-request">
    {data && <p className={`admin-stop-reason${data.stop.limit ? " hit" : ""}`} role="status"><strong>{data.stop.text}</strong></p>}
    {data?.job.failure && <p>{[data.job.failure.code, data.job.failure.message].filter(Boolean).join(": ")}</p>}
    {run.error && <Callout tone="bad" role="alert">{run.error}{data && " Showing the previous read."}</Callout>}
    {input ? <>
      <Text label="Prompt" text={input.prompt} empty="No prompt was recorded." />
      <Text label="System prompt" text={input.system_prompt} empty="No system prompt." />
      {account && <>
        <Text label="Question" text={account.question} empty="No question was recorded." />
        <h4>Claims named by the requester</h4>
        {account.claims.length ? <ol className="admin-request-claims">{account.claims.map((claim, i) => <li key={i}>{claim.statement}
          {claim.subject_entity && <small className="admin-mono">{claim.subject_entity} · {claim.relation} · {claim.object_entity}</small>}</li>)}</ol> : <p>No claims named.</p>}
      </>}
      {input.output_schema != null ? <Text label="Output schema" text={JSON.stringify(input.output_schema, null, 2)} empty="" /> : <p>No output schema: the job was not asked for structured output.</p>}
    </> : <p className="admin-muted">The prompt, system prompt, question, claims and output schema are not loaded. Show them with the button above; each read records your email and the time.</p>}
    <h4>Settings</h4>
    {agent ? <KeyValues rows={settingsRows(agent)} /> : <p>No settings were recorded for this job.</p>}
    <h4>Limits</h4>
    {data ? <LimitsTable rows={data.limits} given={input?.limits} /> : <><p className="admin-muted">The limits the job was given. Show the prompt and response for what it requested, the ceiling and what it used.</p><GivenLimits limits={agent?.limits ?? null} /></>}
    <h4>How Claude Code was started</h4>
    {data ? <ConfigurationView configuration={data.configuration} /> : <p className="admin-muted">Its arguments, environment, tools, MCP servers and network policy load with the prompt.</p>}
    {data && <><p><small>Read by {data.read.by} at {date(data.read.at)}. Every read of the prompt and response is recorded with the admin and the time; this page does not refresh it.</small></p>
      <Json value={{ input: data.input, limits: data.limits, stop: data.stop, run: data.run, configuration: data.configuration }} label="Raw request JSON" /></>}
  </div>;
}

/** The files an account job saved and cited, one row each: what the agent recorded and what REVEAL measured. */
export function SavedSources({ sources }: { sources: NonNullable<AgentRun["sources"]> }) {
  const cell = (value: string | null) => value ? <code className="admin-mono">{value}</code> : "—";
  return <>
    {sources.items.length ? <div className="admin-table-scroll" tabIndex={0}><table aria-label="Sources the agent saved"><thead><tr>{["Path", "URL", "Command", "Observed command", "Size", "sha256", "Media type", "Quotes citing it"].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
      {sources.items.map((source, index) => <tr key={`${source.path}-${index}`}>
        <td className="admin-mono">{source.path || "—"}{source.derived_from && <small>converted from {source.derived_from}</small>}</td>
        <td className="admin-mono">{source.url || "—"}</td>
        <td>{cell(source.command)}</td>
        <td>{source.observed_command && source.observed_command === source.command ? "Same as the command" : cell(source.observed_command)}</td>
        <td>{fileSize(source.size_bytes)}</td>
        <td className="admin-mono" title={source.sha256 || undefined}>{shortSha(source.sha256)}</td>
        <td>{source.media_type || "—"}</td>
        <td>{count(source.evidence_items)}</td>
      </tr>)}
    </tbody></table></div> : <p>The account cites no saved source file.</p>}
    {sources.total > sources.items.length && <p>The first {count(sources.items.length)} of {count(sources.total)} sources.</p>}
  </>;
}

const score = (value: number | null | undefined) => value == null ? "—" : value.toFixed(2);
/** The deposit screen's verdict, split into its parts. Never the text it scored. */
function screenRows(result: AgentRun["result"]): [string, ReactNode][] {
  const classifier = result.screen_classifier === "disabled" ? "Disabled: its lock is not pinned, so only the fixed checks ran"
    : result.screen_classifier === "not_loaded" ? "Enabled but not loaded" : result.screen_classifier ? <>Enabled · <code className="admin-mono">{result.screen_classifier}</code></> : "—";
  return [
    ["Prompt-injection screen", screenSummary(result) ?? "—"],
    ["Verdict", <Status key="verdict" tone={result.screen_outcome === "withheld" ? "bad" : "good"}>{result.screen_outcome === "withheld" ? "Withheld" : "Passed"}</Status>],
    ["Classifier", classifier],
    ["Failed check", result.screen_check ? result.screen_check.replaceAll("_", " ") : result.screen_outcome === "withheld" ? "Not recorded" : "None"],
    ["Score", result.screen_score == null ? "—" : `${score(result.screen_score)} against a threshold of ${score(result.screen_threshold)}`],
    [result.screen_outcome === "withheld" ? "Where" : "Highest score at", result.screen_location ? <code className="admin-mono">{result.screen_location}</code> : "—"],
    ["Texts scored", result.screen_windows ? `${plural(result.screen_windows, "window")}${result.screen_texts ? ` from ${plural(result.screen_texts, "text")}` : ""}${result.screen_seconds != null ? ` in ${result.screen_seconds.toFixed(1)} s` : ""}` : "None"],
  ];
}

/** Section 3: what the job returned, then (for an account) where it went, the sources it cites and the deposit screen. */
export function ResponseSection({ agent, status, run }: { agent: AgentRun | null | undefined; status: string; run: RunState }) {
  const response = run.data?.response;
  const result = agent?.result;
  const account = agent?.deliverable === "scientific_account";
  const outcome = agent ? depositOutcome({ status, agent }) : null;
  return <div className="admin-request">
    <h4>Answer</h4>
    {result && <p className="admin-muted">{result.response_characters == null ? "No response size was recorded." : `${count(result.response_characters)} characters${result.response_truncated ? " (truncated)" : ""}${result.files ? ` · ${plural(result.files, "file")}` : ""}.`}</p>}
    {response ? <>
      {response.source ? <p>From {SOURCES[response.source]}{response.attempt ? ` (attempt ${response.attempt})` : ""}{response.truncated ? ", cut at 50,000 characters" : ""}.{response.truncated && response.captured_unavailable ? ` The full text is not available: ${UNAVAILABLE[response.captured_unavailable] ?? response.captured_unavailable}.` : ""}</p>
        : <p>No response was returned{response.captured_unavailable ? `: ${UNAVAILABLE[response.captured_unavailable] ?? response.captured_unavailable}` : ""}.</p>}
      {response.text != null && <Text label={`Response${response.characters != null ? ` · ${response.characters.toLocaleString()} characters` : ""}${response.truncated ? " · truncated" : ""}`} text={response.text} empty="" prose copyLabel="Response text" />}
      {response.structured_output != null && <Text label="Structured output" text={JSON.stringify(response.structured_output, null, 2)} empty="" />}
      <h4>Output files</h4>
      {response.files.length ? <ul>{response.files.map((file, i) => <li key={i}><span className="admin-mono">{file.path || "—"}</span> · {fileSize(file.size_bytes)}{file.sha256 && <> · <span className="admin-mono" title={file.sha256}>sha256 {shortSha(file.sha256)}</span></>}</li>)}</ul> : <p>No output files.</p>}
    </> : <p className="admin-muted">The answer, structured output and output files load with the prompt (a recorded read).</p>}
    {account && result ? <>
      <h4>Deposit and account</h4>
      <KeyValues rows={[
        ["Deposit", outcome ? <><Status tone={asTone(outcome.tone)}>{outcome.text}</Status>{result.deposit_id && <small className="admin-mono"> {result.deposit_id}</small>}</> : "—"],
        // No claims-repository address is configured in the frontend, so the account is named by its DAPPER id, not linked.
        ["Account", result.account_id ? <><span className="admin-mono">{result.account_id}</span> <small>{accountSource(result)}</small></> : "—"],
        ["Account contents", result.account_id ? [`${count(result.objects)} objects`, result.claims != null && plural(result.claims, "claim"), result.quotes != null && plural(result.quotes, "quote"), result.papers != null && plural(result.papers, "paper"), result.lint_warnings != null && plural(result.lint_warnings, "lint warning"), result.dapper_version && `DAPPER ${result.dapper_version}`].filter(Boolean).join(" · ") : "—"],
      ]} />
      <h4>Sources the agent saved</h4>
      {agent.sources ? <SavedSources sources={agent.sources} /> : <p>No sources are recorded: the job returned no account.</p>}
      <p className="admin-muted">Each saved source shows the path, URL and commands the agent recorded and what REVEAL measured, never the file&apos;s text.</p>
      <h4>Deposit screen</h4>
      {result.screen_outcome ? <KeyValues rows={screenRows(result)} /> : <p>The deposit screen has not run for this job.</p>}
    </> : agent ? <p className="admin-muted">An answer job deposits nothing: it has no account, saved sources or deposit screen.</p> : null}
    {(result || response) && <Json value={{ result, response: response ?? "not loaded" }} label="Raw response JSON" />}
  </div>;
}
