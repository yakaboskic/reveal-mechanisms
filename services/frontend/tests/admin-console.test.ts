import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { filterJobs, totals, type Snapshot } from "../src/lib/admin-telemetry";
import { depositFilter, type AdminJob, type AgentJob, type AgentRun } from "../src/lib/admin-job";
import { ADMIN_NAV, activeSection, AdminSidebar } from "../src/components/admin/AdminNav";
import { ConsoleNotices } from "../src/components/admin/Telemetry";
import { OverviewView } from "../src/components/admin/OverviewView";
import { JOB_HEADINGS, JobsView } from "../src/components/admin/JobsView";
import { ActivityView } from "../src/components/admin/ActivityView";
import { DatabaseView } from "../src/components/admin/DatabaseView";
import { AdminAgentJobsView, AGENT_HEADINGS } from "../src/components/AdminAgentJobs";

const render = <P extends object>(component: React.ComponentType<P>, props: P) => renderToStaticMarkup(React.createElement(component, props));
const params = (query = "") => new URLSearchParams(query);
const noUpdate = () => {};

const job = (id: string, status: string, extra: Partial<AdminJob> = {}): AdminJob => ({
  id, owner: "owner-1", kind: "analysis", status, stage: status === "running" ? "agent" : "complete", created_at: "2026-11-16T16:20:14Z",
  started_at: "2026-11-16T16:20:20Z", attempt_started_at: null, completed_at: status === "running" ? null : "2026-11-16T16:27:47Z",
  updated_at: "2026-11-16T16:27:47Z", wall_seconds: 453, queue_seconds: 1, attempt_seconds: 450, attempt: 1, worker: "worker-a", lease_until: null,
  recoveries: 0, lease_expired: false, box_id: null, box_phase: null, box_seconds: null, setup_seconds: null, agent_seconds: 364, capture_seconds: null,
  cleanup_seconds: null, failure_code: status === "failed" ? "AGENT_EXECUTION_FAILED" : null, ...extra });
const snapshot = (jobs: AdminJob[], extra: Partial<Snapshot> = {}): Snapshot => ({
  generated_at: "2026-11-19T10:00:00Z", query_ms: 12, database: "MySQL reveal", counts: [{ kind: "principal", count: 7, updated_at: "2026-11-19T09:00:00Z" }, { kind: "job", count: 40, updated_at: "2026-11-19T09:00:00Z" }],
  recent: [], statuses: [{ status: "succeeded", count: 30 }, { status: "failed", count: 6 }, { status: "running", count: 2 }, { status: "queued", count: 1 }], jobs,
  events: [], tables: [], imports: [], runtime: { pid: 1, uptime_seconds: 60, scope: "", rows: [] }, ...extra });

test("empty lists are said in words", () => {
  const overview = render(OverviewView, { data: snapshot([], { statuses: [] }) });
  for (const text of ["No job has been queued yet.", "No job among the latest 100 has failed.", "Nothing is running or queued."]) assert.ok(overview.includes(text), text);
  assert.ok(render(JobsView, { data: snapshot([]), params: params(), update: noUpdate, auto: true }).includes("No jobs match this view. Research jobs will appear here as they are created."));
  assert.ok(render(JobsView, { data: snapshot([job("a", "failed")]), params: params("status=running"), update: noUpdate, auto: true }).includes("Clear the filters to see every job."));
  const activity = render(ActivityView, { data: snapshot([]), params: params(), update: noUpdate });
  assert.ok(activity.includes("No records are stored yet.") && activity.includes("No job events are retained yet."));
  const database = render(DatabaseView, { data: snapshot([]), params: params(), update: noUpdate });
  assert.ok(database.includes("No import run metadata is available.") && database.includes("The database reported no tables."));
});

test("the sidebar names each section, marks the current one and says who is signed in", () => {
  assert.deepEqual(ADMIN_NAV.map(item => item.label), ["Overview", "Jobs", "Agent jobs", "Costs", "Database", "Activity", "Performance", "Limits"]);
  assert.deepEqual(["/admin", "/admin/", "/admin/jobs", "/admin/jobs/abc", "/admin/agent-jobs", "/admin/limits"].map(path => activeSection(path).label),
    ["Overview", "Overview", "Jobs", "Jobs", "Agent jobs", "Limits"]);
  const signedIn = render(AdminSidebar, { pathname: "/admin/costs", email: "admin@example.org", bypass: false });
  assert.ok(signedIn.includes('<a aria-current="page" href="/admin/costs">Costs</a>') && signedIn.includes("admin@example.org") && signedIn.includes(">Sign out</button>"));
  assert.equal(signedIn.match(/aria-current/g)?.length, 1);
  const bypass = render(AdminSidebar, { pathname: "/admin", email: null, bypass: true });
  assert.ok(bypass.includes("Sign-in is bypassed on this server.") && !bypass.includes("Sign out"));
});

test("every page keeps the bypass notice, the stale-data banner and the time-zone note", () => {
  const data = snapshot([]);
  const stale = render(ConsoleNotices, { bypass: true, telemetry: { data, error: "Telemetry is unavailable.", timeZone: "Europe/Rome" } });
  assert.ok(stale.includes("Development access: admin sign-in is bypassed on this server."));
  assert.ok(stale.includes('role="alert">Telemetry is unavailable. Showing the last successful snapshot; data is stale.</div>'));
  assert.ok(stale.includes("Times shown in Europe/Rome. Each timestamp includes its UTC offset"));
  const fresh = render(ConsoleNotices, { bypass: false, telemetry: { data: null, error: "", timeZone: "" } });
  assert.ok(!fresh.includes("Development access") && !fresh.includes('role="alert"') && fresh.includes("your browser’s local timezone"));
  // Without a snapshot the error stands alone: nothing is called stale.
  assert.ok(!render(ConsoleNotices, { bypass: false, telemetry: { data: null, error: "Telemetry is unavailable.", timeZone: "" } }).includes("stale"));
});

test("the overview's five totals link to the lists they count", () => {
  const data = snapshot([job("f1000000", "failed"), job("r1000000", "running"), job("s1000000", "succeeded")]);
  assert.deepEqual(totals(data), { records: 47, active: 3, failed: 6, identities: 7, weekSpend: undefined });
  const html = render(OverviewView, { data });
  for (const [title, href] of [["Application records", "/admin/database"], ["Active jobs", "/admin/jobs?status=active"], ["Failed jobs", "/admin/jobs?status=failed"],
    ["Workspace identities", "/admin/database"], ["Agent spend, last 7 days", "/admin/costs"]])
    assert.ok(html.includes(`href="${href}"><span class="admin-metric-title">${title}</span>`), title);
  assert.ok(html.includes('href="/admin/jobs?status=queued"><span class="admin-status info">queued</span> 1</a>'));
  assert.ok(html.includes('href="/admin/jobs?job=f1000000"') && html.includes("AGENT_EXECUTION_FAILED") && html.includes('href="/admin/jobs?job=r1000000"'));
});

test("the jobs list filters by kind, state, owner and text, pages through the latest 100, and opens a job in the URL", () => {
  const jobs = [job("f1", "failed", { kind: "agent", deliverable: "answer" }), job("r1", "running", { owner: "owner-2" }), job("s1", "succeeded")];
  assert.deepEqual(filterJobs(jobs, { status: "active" }).map(j => j.id), ["r1"]);
  assert.deepEqual(filterJobs(jobs, { owner: "owner-2" }).map(j => j.id), ["r1"]);
  assert.deepEqual(filterJobs(jobs, { kind: "agent" }).map(j => j.id), ["f1"]);
  assert.deepEqual(filterJobs(jobs, { q: "AGENT · answer" }).map(j => j.id), ["f1"]);
  assert.deepEqual(filterJobs(jobs, { q: "worker-a", status: "succeeded" }).map(j => j.id), ["s1"]);
  const html = render(JobsView, { data: snapshot(jobs), params: params("status=failed"), update: noUpdate, auto: true });
  assert.deepEqual([...html.matchAll(/<th scope="col">([^<]*)<\/th>/g)].map(m => m[1]), JOB_HEADINGS);
  assert.equal(JOB_HEADINGS.length, 9);
  assert.ok(html.includes('href="/admin/jobs?status=failed&amp;job=f1"') && !html.includes("job=r1"));
  assert.ok(html.includes('aria-current="true"') && html.includes("Clear filters"));
  assert.ok(html.includes('<option value="active">Not finished</option>') && html.includes('<option value="owner-2">owner-2</option>') && html.includes('<option value="agent">agent job</option>'));
  const many = Array.from({ length: 30 }, (_, i) => job(`j${String(i).padStart(2, "0")}`, "succeeded"));
  const second = render(JobsView, { data: snapshot(many), params: params("page=2"), update: noUpdate, auto: true });
  assert.ok(second.includes("30 jobs · Page 2 of 2") && second.includes("job=j25") && !second.includes("job=j24"));
  // The open job's detail is today's, shown under the list.
  const open = render(JobsView, { data: snapshot(jobs), params: params("job=f1"), update: noUpdate, auto: true });
  assert.ok(open.includes('aria-label="Job logs and diagnostics"') && open.includes('<tr aria-selected="true">'));
});

const noResult: AgentRun["result"] = { response_truncated: null, response_characters: null, files: null, account_id: null, objects: null, claims: null,
  quotes: null, papers: null, lint_warnings: null, dapper_version: null, deposit_status: null, deposit_id: null, deposit_reason: null, deposit_http_status: null };
const run = (deliverable: string, result: Partial<AgentRun["result"]> = {}): AgentRun => ({ deliverable, input_format: null, model: "claude-opus-5-5", effort: null,
  tools: [], graphs: [], network: "open", allowed_domains: [], claims_repository: true, limits: null, attribute_requester: false, prompt_characters: 10,
  system_prompt_characters: null, question_characters: null, claims_requested: null, claims_with_triple: null, result: { ...noResult, ...result } });
const agentJob = (id: string, status: string, agent: AgentRun): AgentJob => ({ ...job(id, status, { kind: "agent", deliverable: agent.deliverable }), agent });

test("agent jobs: a sources column, a withheld filter with its reason, and the screen's verdict", () => {
  const withheld = agentJob("w1000000", "succeeded", run("scientific_account", { account_id: "dapper:A", papers: 5, deposit_status: "withheld_unsafe", deposit_reason: "prompt_injection",
    screen_outcome: "withheld", screen_check: "prompt_injection", screen_score: 0.97, screen_threshold: 0.5, screen_location: "sources/x.txt line=3-4", screen_classifier: "pg2" }));
  const deposited = agentJob("d1000000", "succeeded", run("scientific_account", { account_id: "dapper:B", papers: 1, deposit_status: "deposited", screen_outcome: "passed", screen_score: 0.01, screen_threshold: 0.5, screen_classifier: "pg2" }));
  const missing = agentJob("m1000000", "succeeded", run("scientific_account", { account_id: "dapper:C", deposit_status: null }));
  const answer = agentJob("a1000000", "succeeded", run("answer", { response_characters: 40 }));
  assert.deepEqual([withheld, deposited, missing, answer].map(depositFilter), ["withheld", "deposited", "failed", "none"]);
  const agents = { window_limit: 100, jobs: [withheld, deposited, missing, answer], types: [
    { deliverable: "scientific_account", jobs: 3, succeeded: 3, failed: 0, cancelled: 0, active: 0, deposited: 1, deposit_not_made: 1, spend_usd: 1, owners: 1, latest_at: null,
      withheld_unsafe: 1, withheld_by_check: { prompt_injection: 1 }, typical_cost_usd: null, typical_turns: null, typical_seconds: null }] };
  const all = render(AdminAgentJobsView, { agents });
  assert.deepEqual([...all.matchAll(/<th scope="col">([^<]*)<\/th>/g)].map(m => m[1]), AGENT_HEADINGS);
  assert.ok(all.includes("5 files") && all.includes(">1 file<"));
  assert.ok(all.includes('href="/admin/agent-jobs?type=scientific_account"'));
  assert.ok(all.includes('<option value="withheld">Withheld as unsafe</option>'));
  const view = render(AdminAgentJobsView, { agents, params: params("deposit=withheld"), update: noUpdate });
  assert.ok(view.includes("w1000000") && !view.includes("d1000000") && !view.includes("a1000000"));
  assert.ok(view.includes('<span class="admin-status bad">Withheld as unsafe: prompt injection</span>'));
  assert.ok(view.includes("<td>Withheld: prompt injection · score 0.97 (threshold 0.50) · at sources/x.txt line=3-4</td>"));
  assert.ok(view.includes('href="/admin/jobs?job=w1000000"'));
  assert.ok(render(AdminAgentJobsView, { agents, params: params("deposit=deposited&q=nothing-matches") }).includes("No agent jobs match this view."));
});
