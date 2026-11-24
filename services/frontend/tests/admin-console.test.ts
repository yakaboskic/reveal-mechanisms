import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { filterJobs, ownerLine, ownerName, ownerNote, totals, type Owners, type Snapshot } from "../src/lib/admin-telemetry";
import { depositFilter, refusalNote, type AdminJob, type AgentJob, type AgentRun } from "../src/lib/admin-job";
import { ADMIN_NAV, activeSection, AdminSidebar } from "../src/components/admin/AdminNav";
import { ConsoleNotices } from "../src/components/admin/Telemetry";
import { OverviewView } from "../src/components/admin/OverviewView";
import { JOB_HEADINGS, JobsView } from "../src/components/admin/JobsView";
import { ActivityView } from "../src/components/admin/ActivityView";
import { DatabaseView } from "../src/components/admin/DatabaseView";
import { AdminAgentJobsView, AGENT_HEADINGS } from "../src/components/AdminAgentJobs";
import { AdminCostsView, type AdminCosts } from "../src/components/AdminCosts";

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
  assert.ok(render(JobsView, { data: snapshot([]), params: params(), update: noUpdate }).includes("No jobs match this view. Research jobs will appear here as they are created."));
  assert.ok(render(JobsView, { data: snapshot([job("a", "failed")]), params: params("status=running"), update: noUpdate }).includes("Clear the filters to see every job."));
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
  assert.ok(html.includes('href="/admin/jobs/f1000000"') && html.includes("AGENT_EXECUTION_FAILED") && html.includes('href="/admin/jobs/r1000000"'));
});

test("an owner is shown by name wherever an owner id is, and an anonymous workspace is said to have none", () => {
  const ada = "13b86465-2324-43c7-9f13-de73e1d27256", key = "7c363256-ce39-464a-a323-c0d11a279b58", guest = "9d9f0eec-6a2f-4aec-a3da-98ad1237113b", system = "gateway";
  const owners: Owners = {
    [ada]: { kind: "registered", name: "Ada Lovelace", email: "ada@example.org", sign_in: "Google" },
    [key]: { kind: "api_key", name: "Lab colleague (API key)", email: null, sign_in: null },
    [guest]: { kind: "anonymous", name: null, email: null, sign_in: null } };
  assert.deepEqual([ada, key, guest, system].map(id => ownerName(owners[id])), ["Ada Lovelace", "Lab colleague (API key)", "Anonymous workspace", null]);
  assert.deepEqual([ada, key, guest].map(id => ownerNote(owners[id])), ["ada@example.org · Google", "API key", null]);
  assert.equal(ownerName({ kind: "registered", name: null, email: "only@example.org", sign_in: "ORCID" }), "only@example.org");
  assert.equal(ownerNote({ kind: "registered", name: "Ada Lovelace", email: "ada@example.org", sign_in: "Google", api_key: true }), "ada@example.org · Google · has an API key");
  assert.equal(ownerName({ kind: "api_key", name: null, email: null, sign_in: null, api_key: true }), "API key workspace");
  assert.equal(ownerName({ kind: "registered", name: null, email: null, sign_in: null }), "Signed in, no name on record");
  assert.equal(ownerLine(ada, owners), "Ada Lovelace · 13b86465");
  assert.equal(ownerLine(system, owners), system);
  const jobs = [job("a1", "succeeded", { owner: ada }), job("k1", "failed", { owner: key }), job("g1", "succeeded", { owner: guest }), job("s1", "succeeded", { owner: system })];
  assert.deepEqual(filterJobs(jobs, { q: "lovelace" }, owners).map(j => j.id), ["a1"]);
  assert.deepEqual(filterJobs(jobs, { q: "ada@example.org" }, owners).map(j => j.id), ["a1"]);
  assert.deepEqual(filterJobs(jobs, { q: "lovelace" }).map(j => j.id), []);
  const list = render(JobsView, { data: snapshot(jobs, { owners }), params: params(), update: noUpdate });
  for (const text of [`<small title="${ada}">Ada Lovelace · 13b86465</small>`, `<small title="${key}">Lab colleague (API key) · 7c363256</small>`,
    `<small title="${guest}">Anonymous workspace · 9d9f0eec</small>`, `<small title="gateway">gateway</small>`,
    `<option value="${key}">Lab colleague (API key) · 7c363256</option>`]) assert.ok(list.includes(text), text);
  const recent = [ada, guest, system].map((owner, i) => ({ kind: "draft", id: `record-${i}`, owner, version: 1, updated_at: "2026-11-19T09:00:00Z" }));
  const activity = render(ActivityView, { data: snapshot([], { recent, owners }), params: params(), update: noUpdate });
  for (const text of ['<span class="admin-owner-name">Ada Lovelace</span><small>ada@example.org · Google</small>',
    '<span class="admin-owner-name admin-muted">Anonymous workspace</span>', `<code title="${ada}">13b86465-232</code>`, '<code title="gateway">gateway</code>']) assert.ok(activity.includes(text), text);
  // A backend that reports no owners still shows every id.
  assert.ok(render(ActivityView, { data: snapshot([], { recent }), params: params(), update: noUpdate }).includes(`<code title="${ada}">13b86465-232</code>`));
  const bucket = { spend_usd: 1, jobs: 1, estimated: 0, unreported: 0, failed: 0 };
  const costs: AdminCosts = { window_jobs: 2, window_limit: 1000, totals: { day: bucket, week: bucket, month: bucket, window: bucket }, daily: [],
    research: { succeeded: 0, p50_usd: null, p90_usd: null, max_usd: null, mean_usd: null },
    owners: [{ owner: key, ...bucket }, { owner: system, ...bucket }], failures: [], budget: { exceeded: 0, near_limit: 0 } };
  const spend = render(AdminCostsView, { costs, jobs: [], owners });
  assert.ok(spend.includes('<span class="admin-owner-name">Lab colleague (API key)</span><small>API key</small>') && spend.includes('<code title="gateway">gateway</code>'));
  assert.ok(!spend.includes("opaque"));
});

test("a run whose responses the provider's safety classifier stopped says so, although it succeeded", () => {
  assert.equal(refusalNote({}), null);
  assert.equal(refusalNote({ refusals: 0, refusal_categories: {}, ended_on_refusal: false, models: ["claude-opus-5-5"] }), null);
  assert.equal(refusalNote({ refusals: 1, refusal_categories: { bio: 1 }, ended_on_refusal: false, models: ["claude-opus-5-5"] }),
    "1 response stopped by the model provider's safety classifier (bio)");
  const stopped = { refusals: 2, refusal_categories: { bio: 1, not_reported: 1 }, ended_on_refusal: false, models: ["claude-opus-5-5", "claude-opus-5"] };
  assert.equal(refusalNote(stopped), "2 responses stopped by the model provider's safety classifier (bio) · continued on claude-opus-5");
  assert.ok(refusalNote({ ...stopped, ended_on_refusal: true })?.endsWith("the last response was stopped"));
  const list = render(JobsView, { data: snapshot([job("r1", "succeeded", stopped), job("c1", "succeeded", { refusals: 0 }), job("o1", "succeeded")]), params: params(), update: noUpdate });
  assert.equal(list.split("stopped by the model provider").length - 1, 1);
  assert.ok(list.includes('<small class="admin-warning">2 responses stopped by the model provider&#x27;s safety classifier (bio) · continued on claude-opus-5</small>'));
});

test("the jobs list filters by kind, state, owner and text, pages through the latest 100, and links each job to its own page", () => {
  const jobs = [job("f1", "failed", { kind: "agent", deliverable: "answer" }), job("r1", "running", { owner: "owner-2" }), job("s1", "succeeded")];
  assert.deepEqual(filterJobs(jobs, { status: "active" }).map(j => j.id), ["r1"]);
  assert.deepEqual(filterJobs(jobs, { owner: "owner-2" }).map(j => j.id), ["r1"]);
  assert.deepEqual(filterJobs(jobs, { kind: "agent" }).map(j => j.id), ["f1"]);
  assert.deepEqual(filterJobs(jobs, { q: "AGENT · answer" }).map(j => j.id), ["f1"]);
  assert.deepEqual(filterJobs(jobs, { q: "worker-a", status: "succeeded" }).map(j => j.id), ["s1"]);
  const html = render(JobsView, { data: snapshot(jobs), params: params("status=failed"), update: noUpdate });
  assert.deepEqual([...html.matchAll(/<th scope="col">([^<]*)<\/th>/g)].map(m => m[1]), JOB_HEADINGS);
  assert.equal(JOB_HEADINGS.length, 9);
  assert.ok(html.includes('href="/admin/jobs/f1"') && !html.includes("/admin/jobs/r1"));
  assert.ok(html.includes('aria-current="true"') && html.includes("Clear filters"));
  assert.ok(html.includes('<option value="active">Not finished</option>') && html.includes('<option value="owner-2">owner-2</option>') && html.includes('<option value="agent">agent job</option>'));
  const many = Array.from({ length: 30 }, (_, i) => job(`j${String(i).padStart(2, "0")}`, "succeeded"));
  const second = render(JobsView, { data: snapshot(many), params: params("page=2"), update: noUpdate });
  assert.ok(second.includes("30 jobs · Page 2 of 2") && second.includes('href="/admin/jobs/j25"') && !second.includes('href="/admin/jobs/j24"'));
  // A job opens on its own page, never below the list: an old ?job= in the URL opens nothing here.
  const old = render(JobsView, { data: snapshot(jobs), params: params("job=f1"), update: noUpdate });
  assert.ok(!old.includes("Refresh logs") && !old.includes("aria-selected") && !old.includes("Event log"));
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
  assert.ok(view.includes('href="/admin/jobs/w1000000"'));
  assert.ok(render(AdminAgentJobsView, { agents, params: params("deposit=deposited&q=nothing-matches") }).includes("No agent jobs match this view."));
});

test("filters from the URL show as chosen even when no listed job has that value, and job ids copy whole", () => {
  const html = render(JobsView, { data: snapshot([job("f1", "failed")]), params: params("status=cancel_requested&q=worker"), update: noUpdate });
  assert.ok(html.includes('<option value="cancel_requested" selected="">cancel requested</option>'), "the outside value is an option, chosen");
  assert.ok(html.includes('value="worker"'), "the search box starts from the URL");
  const all = render(JobsView, { data: snapshot([job("f1", "failed")]), params: params(), update: noUpdate });
  assert.ok(all.includes('aria-label="Copy: f1"'));
});

test("only a table the snapshot lists as present opens in the inspector", () => {
  const tables = [{ name: "reveal_records", present: true, estimated_rows: 3, bytes: 10 }, { name: "old_table", present: false, estimated_rows: null, bytes: null }];
  const open = render(DatabaseView, { data: snapshot([], { tables }), params: params("table=reveal_records"), update: noUpdate });
  assert.ok(open.includes('aria-label="Table contents"') && open.includes("Back to database tables"));
  for (const name of ["old_table", "no_such_table", "../jobs"]) {
    const html = render(DatabaseView, { data: snapshot([], { tables }), params: params(`table=${encodeURIComponent(name)}`), update: noUpdate });
    assert.ok(!html.includes('aria-label="Table contents"') && html.includes("to inspect. Choose one below.") && html.includes('aria-label="Database tables"'), name);
  }
});
