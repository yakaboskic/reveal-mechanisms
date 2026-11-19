"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React, { useState } from "react";
import { deliverableLabel, depositOutcome, fileSize, jobKindLabel, screenSummary, shortSha, type AgentJob, type AgentJobs, type AgentRun } from "@/lib/admin-job";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { JobCost, usd } from "./AdminCosts";

const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const plural = (value: number, noun: string) => `${value.toLocaleString()} ${noun}${value === 1 ? "" : "s"}`;
const of = (used: number | null | undefined, limit: number | null | undefined) => limit == null ? count(used) : `${count(used)} of ${count(limit)}`;
const yesNo = (value: boolean | null) => value == null ? "—" : value ? "Yes" : "No";
const terminal = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);
const LIMIT_LABELS: [string, string][] = [["timeout_seconds", "Run time"], ["max_turns", "Turns"], ["max_budget_usd", "Budget"], ["tool_calls", "REVEAL tool calls"]];
const limitValue = (key: string, value: number) => key === "max_budget_usd" ? usd(value) : key === "timeout_seconds" ? duration(value) : value.toLocaleString();

/** What the agent could read: the claims repository, graphs and the network. Unknown parts are left out. */
function sources(run: AgentRun) {
  return [run.claims_repository && "claims repository", run.graphs?.length ? plural(run.graphs.length, "graph") : null,
    run.network === "open" ? "open network" : run.allowed_domains?.length ? plural(run.allowed_domains.length, "extra host") : null].filter(Boolean).join(" · ") || "No claims repository, graphs or extra hosts";
}
function claimsAsked(run: AgentRun) {
  if (run.claims_requested == null) return null;
  return run.claims_requested ? `${plural(run.claims_requested, "claim")} to assess, ${run.claims_with_triple ?? 0} with a triple` : "No claims named";
}
function Returned({ job }: { job: AgentJob }) {
  const { deliverable, result } = job.agent;
  if (deliverable === "scientific_account") {
    if (!result.account_id) return <>—</>;
    return <>{count(result.objects)} objects<small>{[result.claims != null && plural(result.claims, "claim"), result.quotes != null && plural(result.quotes, "quote"), result.papers != null && plural(result.papers, "paper")].filter(Boolean).join(" · ")}</small>{!!result.lint_warnings && <small>{plural(result.lint_warnings, "lint warning")}</small>}</>;
  }
  if (result.response_characters == null) return <>—</>;
  return <>{count(result.response_characters)} characters{result.response_truncated && <small><span className="admin-warning">Truncated</span></small>}{!!result.files && <small>{plural(result.files, "file")}</small>}</>;
}

/** The files an account job saved and cited, one row each: what the agent recorded and what REVEAL measured. */
function SavedSources({ sources }: { sources: NonNullable<AgentRun["sources"]> }) {
  const cell = (value: string | null) => value ? <code className="admin-mono">{value}</code> : "—";
  return <>
    <h3>Sources the agent saved</h3>
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

/** The settings of one agent job, for the job detail. */
export function AgentRunDetail({ run, job }: { run: AgentRun; job: AgentJob | (Omit<AgentJob, "agent">) }) {
  const outcome = depositOutcome({ status: job.status, agent: run });
  const account = run.deliverable === "scientific_account";
  const rows: [string, React.ReactNode][] = [
    ["Asked to return", deliverableLabel(run.deliverable)],
    ["Model · effort", [run.model, run.effort].filter(Boolean).join(" · ") || "—"],
    ["Built-in tools", run.tools == null ? "—" : run.tools.length ? run.tools.join(", ") : "None"],
    ["Claims repository lookups", yesNo(run.claims_repository)],
    ["Graphs", run.graphs == null ? "—" : run.graphs.length ? run.graphs.join(", ") : "None"],
    ["Network", run.network ? `${run.network}${run.allowed_domains?.length ? ` · extra hosts: ${run.allowed_domains.join(", ")}` : ""}` : "—"],
    ["Prompt", run.prompt_characters == null ? "—" : `${count(run.prompt_characters)} characters${run.system_prompt_characters ? ` · system prompt ${count(run.system_prompt_characters)} characters` : " · no system prompt"}`],
    ...(account ? [
      ["Question", run.question_characters == null ? "—" : `${count(run.question_characters)} characters`],
      ["Claims named by the requester", claimsAsked(run) ?? "—"],
      ["Requester named in the account", yesNo(run.attribute_requester)],
      ["Deposit", outcome ? <><span className={`admin-status ${outcome.tone}`}>{outcome.text}</span>{run.result.deposit_id && <small className="admin-mono"> {run.result.deposit_id}</small>}</> : "—"],
      ["Prompt-injection screen", screenSummary(run.result) ?? "—"],
      ["Account", run.result.account_id ? <span className="admin-mono">{run.result.account_id}</span> : "—"],
      ["Account contents", run.result.account_id ? [`${count(run.result.objects)} objects`, run.result.claims != null && plural(run.result.claims, "claim"), run.result.quotes != null && plural(run.result.quotes, "quote"), run.result.papers != null && plural(run.result.papers, "paper"), run.result.lint_warnings != null && plural(run.result.lint_warnings, "lint warning"), run.result.dapper_version && `DAPPER ${run.result.dapper_version}`].filter(Boolean).join(" · ") : "—"],
    ] as [string, React.ReactNode][] : []),
    ["Response", run.result.response_characters == null ? "—" : `${count(run.result.response_characters)} characters${run.result.response_truncated ? " (truncated)" : ""}${run.result.files ? ` · ${plural(run.result.files, "file")}` : ""}`],
    ...(run.output_schema_characters != null ? [
      ["Structured output", `Asked for, schema ${count(run.output_schema_characters)} characters · ${run.result.structured_output_characters == null ? "none returned" : `returned ${count(run.result.structured_output_characters)} characters`}`],
    ] as [string, React.ReactNode][] : []),
    ["Input format", run.input_format || "—"],
  ];
  return <details className="admin-agent-usage" open><summary>How this agent job was run · {deliverableLabel(run.deliverable)}</summary>
    <dl className="admin-detail-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <h3>Limits the job was given</h3>
    {run.limits ? <dl className="admin-detail-grid">{LIMIT_LABELS.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{run.limits![key] == null ? "Not part of this input" : limitValue(key, run.limits![key])}</dd></div>)}</dl> : <p>No limits were recorded for this job.</p>}
    {account && run.sources && <SavedSources sources={run.sources} />}
    <p>The prompt, question, claims and response are not shown here: sizes and counts only.{account && run.sources ? " Each saved source shows the path, URL and commands the agent recorded and what REVEAL measured, never the file's text." : ""} The event log below holds what the agent did.</p>
  </details>;
}

export function AdminAgentJobsView({ agents, kinds, onSelect }: { agents: AgentJobs; kinds?: { kind: string; count: number }[]; onSelect: (id: string) => void }) {
  const [type, setType] = useState("");
  const [filter, setFilter] = useState("");
  const jobs = agents.jobs.filter(j => (!type || (j.agent.deliverable || "unknown") === type)
    && `${j.id} ${j.owner} ${j.status} ${deliverableLabel(j.agent.deliverable)} ${j.agent.model || ""} ${depositOutcome(j)?.text || ""}`.toLowerCase().includes(filter.toLowerCase()));
  return <section aria-label="Agent jobs by type">
    <div className="admin-section-heading"><div><h2>Agent jobs</h2><p>The latest {count(agents.jobs.length)} general agent jobs across all workspaces (limit {count(agents.window_limit)}), by what each was asked to return: a Scientific Account deposited in the claims repository, or an answer. Counts and typical figures below cover these jobs, not all time. The prompt, question and claims are never shown; only their sizes.</p></div><input className="field" aria-label="Filter agent jobs" placeholder="Filter by job, owner, state, model or deposit" value={filter} onChange={e => setFilter(e.target.value)} /></div>
    {!!kinds?.length && <p>All jobs ever queued, by kind: {kinds.map(k => `${count(k.count)} ${jobKindLabel(k.kind)}${k.count === 1 ? "" : "s"}`).join(" · ")}.</p>}
    <section className="admin-overview admin-cost-overview" aria-label="Agent jobs by type">
      {agents.types.map(t => <div key={t.deliverable}><strong>{count(t.jobs)}</strong><span>{deliverableLabel(t.deliverable === "unknown" ? null : t.deliverable)} jobs · {count(t.succeeded)} succeeded · {count(t.failed)} failed{t.cancelled ? ` · ${count(t.cancelled)} cancelled` : ""}{t.active ? ` · ${count(t.active)} running or queued` : ""}
        {t.deliverable === "scientific_account" && <> · {count(t.deposited)} deposited{t.deposit_not_made ? <> · <b className="admin-warning">{count(t.deposit_not_made)} succeeded without a deposit</b></> : ""}{t.withheld_unsafe ? <> · <b className="admin-warning">{count(t.withheld_unsafe)} withheld as unsafe</b>{t.withheld_by_check && ` (${Object.entries(t.withheld_by_check).map(([check, n]) => `${check.replaceAll("_", " ")} ${count(n)}`).join(", ")})`}</> : ""}</>}
        <br />Typical successful run: {usd(t.typical_cost_usd)} · {count(t.typical_turns)} turns · {duration(t.typical_seconds)}<br />{usd(t.spend_usd)} spent · {plural(t.owners, "requester")} · latest {date(t.latest_at)}</span></div>)}
    </section>
    <div className="admin-statuses" role="group" aria-label="Job type">
      {[["", "All types", agents.jobs.length] as const, ...agents.types.map(t => [t.deliverable, deliverableLabel(t.deliverable === "unknown" ? null : t.deliverable), t.jobs] as const)].map(([value, label, n]) =>
        <button key={value || "all"} type="button" className="text-button" aria-pressed={type === value} onClick={() => setType(value)}>{label} ({count(n)})</button>)}
    </div>
    <div className="admin-table-scroll admin-agent-jobs" tabIndex={0}><table><thead><tr>{["Job / owner", "Type", "State", "Asked for", "Limits given", "Used", "Returned", "Deposit", "Created", "Cost"].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>
      {jobs.map(j => { const run = j.agent; const outcome = depositOutcome(j); const limits = run.limits; return <tr key={j.id}>
        <td><button className="admin-job-link" onClick={() => onSelect(j.id)} aria-label={`View how job ${j.id} was run`}>{j.id.slice(0, 8)}</button><small title={j.owner}>{j.owner}</small></td>
        <td>{deliverableLabel(run.deliverable)}</td>
        <td><span className={`admin-status ${j.status === "failed" ? "bad" : j.status === "succeeded" ? "good" : ""}`}>{j.status.replaceAll("_", " ")}</span>{j.failure_code && <small>{j.failure_code}</small>}{!terminal.has(j.status) && j.stage !== j.status && <small>{j.stage.replaceAll("_", " ")}</small>}</td>
        <td>{[run.model, run.effort && `${run.effort} effort`].filter(Boolean).join(" · ") || "—"}<small>{sources(run)}</small><small>{run.tools == null ? "Tools not recorded" : run.tools.length ? `Tools: ${run.tools.join(", ")}` : "No built-in tools"}</small>{claimsAsked(run) && <small>{claimsAsked(run)}</small>}<small>Prompt {count(run.prompt_characters)} characters</small></td>
        <td>{limits ? <>{limits.max_turns != null ? `${count(limits.max_turns)} turns` : "—"} · {limits.max_budget_usd != null ? usd(limits.max_budget_usd) : "—"} · {limits.timeout_seconds != null ? duration(limits.timeout_seconds) : "—"}{limits.tool_calls != null && <small>{count(limits.tool_calls)} REVEAL tool calls</small>}</> : "—"}</td>
        <td>{j.turns == null ? "—" : <>{of(j.turns, j.turn_limit)} turns{j.tool_calls != null && <small>{count(j.tool_calls)} tool calls{j.tool_failures ? `, ${count(j.tool_failures)} failed` : ""}</small>}{j.agent_seconds != null && <small>{duration(j.agent_seconds)}{j.time_limit_seconds != null ? ` of ${duration(j.time_limit_seconds)}` : ""}</small>}{(j.lint_checks != null || j.draft_writes != null) && <small>{count(j.lint_checks)} lint checks · {count(j.draft_writes)} draft writes</small>}</>}</td>
        <td><Returned job={j} /></td>
        <td>{outcome ? <><span className={`admin-status ${outcome.tone}`}>{outcome.text}</span>{run.result.deposit_id && <small className="admin-mono" title={run.result.deposit_id}>{run.result.deposit_id.slice(0, 16)}…</small>}{run.result.screen_outcome === "withheld" && <small>{screenSummary(run.result)}</small>}</> : <span title="An answer job deposits nothing.">Not applicable</span>}</td>
        <td className="admin-date">{date(j.created_at)}<small>{duration(j.wall_seconds)} wall time</small></td>
        <td><JobCost job={j} /></td>
      </tr>; })}
    </tbody></table></div>
    {!jobs.length && <p className="admin-empty">{agents.jobs.length ? "No agent jobs match this view." : "No general agent job has been submitted yet."}</p>}
  </section>;
}
