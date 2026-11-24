"use client";
// React is imported by name because the unit tests render this file with the classic JSX runtime.
import React from "react";
import Link from "next/link";
import { deliverableLabel, depositFilter, depositOutcome, jobKindLabel, refusalNote, screenSummary, type AgentJob, type AgentJobs, type AgentRun, type DepositFilter } from "@/lib/admin-job";
import { ownerLine, ownerName, type Owners } from "@/lib/admin-telemetry";
import { jobHref, pageNumber, pageOf, urlWith, type UrlParams } from "@/lib/admin-url";
import type { UrlUpdate } from "./admin/use-url-state";
import { Age, asTone, CopyButton, Empty, FilterRow, Metric, MetricGrid, PageHeading, Pagination, Panel, SearchFilter, SelectFilter, StateStatus, Status, Table } from "./admin/ui";
import { formatAdminDate as date, formatAdminDuration as duration } from "@/lib/admin-time";
import { JobCost, usd } from "./AdminCosts";

const count = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const plural = (value: number, noun: string) => `${value.toLocaleString()} ${noun}${value === 1 ? "" : "s"}`;
const of = (used: number | null | undefined, limit: number | null | undefined) => limit == null ? count(used) : `${count(used)} of ${count(limit)}`;
const terminal = new Set(["succeeded", "failed", "cancelled", "insufficient_evidence"]);

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

export const AGENT_PAGE_SIZE = 25;
export const AGENT_HEADINGS = ["Job / owner", "Type", "State", "Asked for", "Limits given", "Used", "Returned", "Sources", "Deposit", "Screen", "Created", "Cost"];
const DEPOSIT_FILTERS: [DepositFilter, string][] = [["deposited", "Deposited"], ["withheld", "Withheld as unsafe"], ["failed", "Deposit failed or missing"], ["none", "No deposit (answers, pending, no account)"]];
const noUpdate: UrlUpdate = () => {};

/** The agent jobs window: per-type cards, then the list filtered by type, deposit and free text, a page at a time. */
export function AdminAgentJobsView({ agents, kinds, owners = {}, params = new URLSearchParams(), update = noUpdate }: { agents: AgentJobs; kinds?: { kind: string; count: number }[]; owners?: Owners; params?: UrlParams; update?: UrlUpdate }) {
  const type = params.get("type") || "", deposit = params.get("deposit") || "", filter = params.get("q") || "";
  const jobs = agents.jobs.filter(j => (!type || (j.agent.deliverable || "unknown") === type)
    && (!deposit || depositFilter(j) === deposit)
    && `${j.id} ${j.owner} ${ownerName(owners[j.owner]) || ""} ${owners[j.owner]?.email || ""} ${j.status} ${deliverableLabel(j.agent.deliverable)} ${j.agent.model || ""} ${depositOutcome(j)?.text || ""}`.toLowerCase().includes(filter.toLowerCase()));
  const shown = pageOf(jobs, pageNumber(params.get("page")), AGENT_PAGE_SIZE);
  const set = (key: string, push = true) => (value: string) => update({ [key]: value, page: null }, { push });
  const filtered = !!(type || deposit || filter);
  return <section aria-label="Agent jobs by type" className="admin-agent-jobs-view">
    <PageHeading title="Agent jobs" subtitle="General agent jobs by what each was asked to return: a Scientific Account deposited in the claims repository, or an answer." />
    {!!kinds?.length && <p className="admin-note">All jobs ever queued, by kind: {kinds.map(k => `${count(k.count)} ${jobKindLabel(k.kind)}${k.count === 1 ? "" : "s"}`).join(" · ")}.</p>}
    <MetricGrid label="Agent jobs by type">
      {agents.types.map(t => <Metric key={t.deliverable} href={urlWith("/admin/agent-jobs", params, { type: t.deliverable, page: null })} title={`${deliverableLabel(t.deliverable === "unknown" ? null : t.deliverable)} jobs`} value={count(t.jobs)}
        hint={<>{count(t.succeeded)} succeeded · {count(t.failed)} failed{t.cancelled ? ` · ${count(t.cancelled)} cancelled` : ""}{t.active ? ` · ${count(t.active)} running or queued` : ""}
          {t.deliverable === "scientific_account" && <> · {count(t.deposited)} deposited{t.deposit_not_made ? <> · <b className="admin-warning">{count(t.deposit_not_made)} succeeded without a deposit</b></> : ""}{t.withheld_unsafe ? <> · <b className="admin-warning">{count(t.withheld_unsafe)} withheld as unsafe</b>{t.withheld_by_check && ` (${Object.entries(t.withheld_by_check).map(([check, n]) => `${check.replaceAll("_", " ")} ${count(n)}`).join(", ")})`}</> : ""}</>}
          <br />Typical successful run: {usd(t.typical_cost_usd)} · {count(t.typical_turns)} turns · {duration(t.typical_seconds)}<br />{usd(t.spend_usd)} spent · {plural(t.owners, "requester")} · latest {date(t.latest_at)}</>} />)}
    </MetricGrid>
    <FilterRow label="Filter agent jobs">
      <SearchFilter label="Filter agent jobs" placeholder="Filter by job, owner, state, model or deposit" value={filter} onChange={set("q", false)} />
      <SelectFilter label="Job type" value={type} all={`All types (${count(agents.jobs.length)})`} options={agents.types.map(t => [t.deliverable, `${deliverableLabel(t.deliverable === "unknown" ? null : t.deliverable)} (${count(t.jobs)})`])} onChange={set("type")} />
      <SelectFilter label="Deposit" value={deposit} all="Any deposit" options={DEPOSIT_FILTERS} onChange={set("deposit")} />
      {filtered && <Link className="text-button" href={urlWith("/admin/agent-jobs", params, { q: null, type: null, deposit: null, page: null })}>Clear filters</Link>}
    </FilterRow>
    <Panel title="Agent jobs" description={<>The latest {count(agents.jobs.length)} general agent jobs across all workspaces (limit {count(agents.window_limit)}). Counts and typical figures above cover these jobs, not all time. This list shows sizes only; open a job to read the prompt, question, claims and response (a recorded read). Sources counts the files the account lists; each one&apos;s URL, commands and checksum are on the job&apos;s page.</>}
      footer={shown.total > AGENT_PAGE_SIZE ? <Pagination page={shown.page} pages={shown.pages} total={shown.total} noun="agent job" href={page => urlWith("/admin/agent-jobs", params, { page })} /> : undefined}>
      {shown.items.length ? <Table label="Agent jobs" className="admin-agent-jobs" headings={AGENT_HEADINGS}>
      {shown.items.map(j => { const run = j.agent; const outcome = depositOutcome(j); const limits = run.limits; const account = run.deliverable === "scientific_account"; return <tr key={j.id}>
        <td><Link className="admin-job-link" href={jobHref(j.id)} aria-label={`View how job ${j.id} was run`} title={j.id}>{j.id.slice(0, 8)}</Link> <CopyButton text={j.id} /><small title={j.owner}>{ownerLine(j.owner, owners)}</small></td>
        <td>{deliverableLabel(run.deliverable)}</td>
        <td><StateStatus value={j.status} />{j.failure_code && <small>{j.failure_code}</small>}{!terminal.has(j.status) && j.stage !== j.status && <small>{j.stage.replaceAll("_", " ")}</small>}{refusalNote(j) && <small className="admin-warning">{refusalNote(j)}</small>}</td>
        <td>{[run.model, run.effort && `${run.effort} effort`].filter(Boolean).join(" · ") || "—"}<small>{sources(run)}</small><small>{run.tools == null ? "Tools not recorded" : run.tools.length ? `Tools: ${run.tools.join(", ")}` : "No built-in tools"}</small>{claimsAsked(run) && <small>{claimsAsked(run)}</small>}<small>Prompt {count(run.prompt_characters)} characters</small></td>
        <td>{limits ? <>{limits.max_turns != null ? `${count(limits.max_turns)} turns` : "—"} · {limits.max_budget_usd != null ? usd(limits.max_budget_usd) : "—"} · {limits.timeout_seconds != null ? duration(limits.timeout_seconds) : "—"}{limits.tool_calls != null && <small>{count(limits.tool_calls)} REVEAL tool calls</small>}{limits.max_output_tokens != null && <small>{count(limits.max_output_tokens)} output tokens per response</small>}</> : "—"}</td>
        <td>{j.turns == null ? "—" : <>{of(j.turns, j.turn_limit)} turns{j.tool_calls != null && <small>{count(j.tool_calls)} tool calls{j.tool_failures ? `, ${count(j.tool_failures)} failed` : ""}</small>}{j.agent_seconds != null && <small>{duration(j.agent_seconds)}{j.time_limit_seconds != null ? ` of ${duration(j.time_limit_seconds)}` : ""}</small>}{(j.lint_checks != null || j.draft_writes != null) && <small>{count(j.lint_checks)} lint checks · {count(j.draft_writes)} draft writes</small>}</>}</td>
        <td><Returned job={j} /></td>
        <td>{account && run.result.papers != null ? <span title="Saved source files the account lists">{plural(run.result.papers, "file")}</span> : "—"}</td>
        <td>{outcome ? <><Status tone={asTone(outcome.tone)}>{outcome.text}</Status>{run.result.deposit_id && <small className="admin-mono" title={run.result.deposit_id}>{run.result.deposit_id.slice(0, 16)}…</small>}</> : <span title="An answer job deposits nothing.">Not applicable</span>}</td>
        <td>{screenSummary(run.result) ?? "—"}</td>
        <td className="admin-date"><Age value={j.created_at} /><small>{duration(j.wall_seconds)} wall time</small></td>
        <td><JobCost job={j} /></td>
      </tr>; })}
      </Table> : <Empty>{agents.jobs.length ? "No agent jobs match this view." : "No general agent job has been submitted yet."}</Empty>}
    </Panel>
  </section>;
}
