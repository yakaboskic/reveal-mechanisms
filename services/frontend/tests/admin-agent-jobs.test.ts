import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { deliverableLabel, depositOutcome, jobKindLabel, jobLabel, type AgentJob, type AgentJobs, type AgentRun } from "../src/lib/admin-job";
import { AdminAgentJobsView, AgentRunDetail } from "../src/components/AdminAgentJobs";

const noResult: AgentRun["result"] = { response_truncated: null, response_characters: null, files: null, account_id: null, objects: null, claims: null,
  quotes: null, papers: null, lint_warnings: null, dapper_version: null, deposit_status: null, deposit_id: null, deposit_reason: null, deposit_http_status: null };
const accountRun: AgentRun = {
  deliverable: "scientific_account", input_format: "reveal.agent-input/2", model: "claude-opus-5-5", effort: "medium",
  tools: ["Read", "Write", "Edit", "Glob", "Grep"], graphs: [], literature: true, network: "restricted", allowed_domains: [], claims_repository: true,
  limits: { timeout_seconds: 3600, max_turns: 999, max_budget_usd: 5, tool_calls: 90, literature_searches: 12, literature_search_results: 25, literature_papers: 9, literature_characters: 180000 },
  attribute_requester: false, prompt_characters: 1344, system_prompt_characters: null, question_characters: 143, claims_requested: 3, claims_with_triple: 3,
  result: { ...noResult, response_truncated: false, response_characters: 3111, files: 5, account_id: "dapper:ScientificAccount.wF3k", objects: 37, claims: 3, quotes: 6, papers: 5,
    lint_warnings: 1, dapper_version: "0.2.1-a1", deposit_status: "deposited", deposit_id: "dep_01a1e52fa3c2b989bcafce5d37f4feb4" },
};
const legacyRun: AgentRun = { ...accountRun, deliverable: "answer", input_format: "reveal.agent-input/1", tools: [], claims_repository: null, attribute_requester: null,
  limits: { max_turns: 50, max_budget_usd: 2, timeout_seconds: 1800 }, question_characters: null, claims_requested: null, claims_with_triple: null,
  result: { ...noResult, response_truncated: false, response_characters: 12161, files: 0 } };
const base = { owner: "owner-1", kind: "agent", stage: "complete", created_at: "2026-11-16T16:20:14Z", started_at: null, attempt_started_at: null,
  completed_at: "2026-11-16T16:27:47Z", updated_at: "2026-11-16T16:27:47Z", wall_seconds: 453, queue_seconds: 1, attempt_seconds: 450, attempt: 1, worker: null,
  lease_until: null, recoveries: 0, lease_expired: false, box_id: null, box_phase: null, box_seconds: null, setup_seconds: null, agent_seconds: 364,
  capture_seconds: null, cleanup_seconds: null, failure_code: null };
const job = (id: string, status: string, agent: AgentRun, extra: Partial<AgentJob> = {}): AgentJob =>
  ({ ...base, id, status, deliverable: agent.deliverable, agent, cost_usd: 1.31, budget_usd: 5, budget_used: .26, turns: 49, turn_limit: 999, tool_calls: 48, tool_failures: 7,
    lint_checks: 3, draft_writes: 2, time_limit_seconds: 3600, ...extra });
const deposited = job("95d0bc18-0000", "succeeded", accountRun);
const rejected = job("aaaaaaaa-0000", "succeeded", { ...accountRun, result: { ...accountRun.result, deposit_status: "failed", deposit_id: null, deposit_reason: "rejected", deposit_http_status: 422 } });
const failed = job("bbbbbbbb-0000", "failed", { ...accountRun, result: noResult }, { failure_code: "VALIDATION_FAILED", turns: null, tool_calls: null, cost_usd: null, lint_checks: null, draft_writes: null });
const legacy = job("09f4db0c-0000", "succeeded", legacyRun, { turns: 8, turn_limit: 50, tool_calls: 6, tool_failures: 0, lint_checks: null, draft_writes: null });
const agents: AgentJobs = { window_limit: 100, jobs: [deposited, rejected, failed, legacy], types: [
  { deliverable: "scientific_account", jobs: 3, succeeded: 2, failed: 1, cancelled: 0, active: 0, deposited: 1, deposit_not_made: 1, spend_usd: 2.62, owners: 1,
    latest_at: "2026-11-16T16:20:14Z", typical_cost_usd: 1.31, typical_turns: 49, typical_seconds: 453 },
  { deliverable: "answer", jobs: 1, succeeded: 1, failed: 0, cancelled: 0, active: 0, deposited: 0, deposit_not_made: 0, spend_usd: .3, owners: 1,
    latest_at: "2026-11-10T00:00:00Z", typical_cost_usd: .3, typical_turns: 8, typical_seconds: 181 }] };

test("an agent job is named by what it was asked to return", () => {
  assert.equal(deliverableLabel("scientific_account"), "Scientific account");
  assert.equal(deliverableLabel("answer"), "Answer");
  assert.equal(deliverableLabel(null), "Unknown type");
  assert.equal(jobLabel({ kind: "agent", deliverable: "scientific_account" }), "agent · scientific account");
  assert.equal(jobLabel({ kind: "agent", deliverable: null }), "agent");
  assert.equal(jobLabel({ kind: "agent" }), "agent");
  assert.equal(jobLabel({ kind: "analysis", deliverable: null }), "analysis");
  assert.equal(jobKindLabel("paragraph"), "research statement");
});

test("a succeeded job whose deposit did not happen is called out", () => {
  assert.deepEqual(depositOutcome(deposited), { text: "Deposited", tone: "good" });
  assert.deepEqual(depositOutcome(rejected), { text: "Deposit failed: rejected (HTTP 422)", tone: "bad" });
  assert.deepEqual(depositOutcome(failed), { text: "No account", tone: "" });
  assert.equal(depositOutcome(legacy), null);
  assert.equal(depositOutcome(job("c", "succeeded", { ...accountRun, result: { ...accountRun.result, deposit_status: "not_configured" } }))?.tone, "bad");
  assert.equal(depositOutcome(job("d", "running", { ...accountRun, result: noResult }))?.text, "Pending");
});

test("the agent jobs view shows type, settings, usage and deposit for each job", () => {
  const html = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents, kinds: [{ kind: "analysis", count: 46 }, { kind: "agent", count: 4 }], onSelect() {} }));
  for (const text of ["Scientific account", "Answer", "46 research runs · 4 agent jobs", "1 deposited", "1 succeeded without a deposit", "Deposit failed: rejected (HTTP 422)",
    "3 claims to assess, 3 with a triple", "49 of 999 turns", "48 tool calls, 7 failed", "12 searches × 25 results · 9 papers · 180,000 characters", "37 objects",
    "VALIDATION_FAILED", "before job types existed", "No built-in tools", "12,161 characters", "Not applicable", "papers · claims repository"]) assert.ok(html.includes(text), text);
  // The legacy job never recorded these, so the view does not print a value for them.
  const row = html.slice(html.indexOf("09f4db0c"), html.indexOf("</tr>", html.indexOf("09f4db0c")));
  assert.ok(!/searches|REVEAL tool calls|claims? to assess|lint checks/.test(row), row);
});

test("an empty window says so", () => {
  const html = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents: { window_limit: 100, jobs: [], types: [] }, onSelect() {} }));
  assert.ok(html.includes("No general agent job has been submitted yet."));
});

test("the job detail lists how the job was run and never a value the input lacks", () => {
  const html = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: accountRun, job: deposited }));
  for (const text of ["How this agent job was run · Scientific account", "Read, Write, Edit, Glob, Grep", "1,344 characters · no system prompt", "3 claims to assess, 3 with a triple",
    "Deposited", "dep_01a1e52fa3c2b989bcafce5d37f4feb4", "dapper:ScientificAccount.wF3k", "37 objects · 3 claims · 6 quotes · 5 papers · 1 lint warning · DAPPER 0.2.1-a1",
    "$5.00", "1.0h", "180,000"]) assert.ok(html.includes(text), text);
  const old = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: legacyRun, job: legacy }));
  assert.ok(old.includes("How this agent job was run · Answer"));
  assert.equal(old.match(/Not part of this input/g)?.length, 5);
  assert.ok(old.includes("<dt>Claims repository lookups</dt><dd>—</dd>"));
  assert.ok(!old.includes("Deposit"));
});
