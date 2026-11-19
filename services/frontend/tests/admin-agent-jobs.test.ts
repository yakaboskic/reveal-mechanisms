import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { accountSource, deliverableLabel, depositOutcome, fileSize, jobKindLabel, jobLabel, screenSummary, shortSha, type AgentJob, type AgentJobs, type AgentLimitUse, type AgentRun, type AgentRunContent } from "../src/lib/admin-job";
import { AdminAgentJobsView, AgentRunDetail } from "../src/components/AdminAgentJobs";
import { AgentRequestView } from "../src/components/AdminAgentRequest";

const noResult: AgentRun["result"] = { response_truncated: null, response_characters: null, files: null, account_id: null, objects: null, claims: null,
  quotes: null, papers: null, lint_warnings: null, dapper_version: null, deposit_status: null, deposit_id: null, deposit_reason: null, deposit_http_status: null };
const accountRun: AgentRun = {
  deliverable: "scientific_account", input_format: "reveal.agent-input/5", model: "claude-opus-5-5", effort: "medium",
  tools: ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch"], graphs: [], network: "open", allowed_domains: [], claims_repository: true,
  limits: { timeout_seconds: 3600, max_turns: 999, max_budget_usd: 5, tool_calls: 90, max_output_tokens: 64000 },
  attribute_requester: false, prompt_characters: 1344, system_prompt_characters: null, question_characters: 143, claims_requested: 3, claims_with_triple: 3,
  result: { ...noResult, response_truncated: false, response_characters: 3111, files: 5, account_id: "dapper:ScientificAccount.wF3k", objects: 37, claims: 3, quotes: 6, papers: 5,
    lint_warnings: 1, dapper_version: "0.2.1-a1", deposit_status: "deposited", deposit_id: "dep_01a1e52fa3c2b989bcafce5d37f4feb4" },
};
// An answer job whose record lacks some settings: the console prints no value for them.
const sparseRun: AgentRun = { ...accountRun, deliverable: "answer", tools: [], network: "restricted", claims_repository: null, attribute_requester: null,
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
const sparse = job("09f4db0c-0000", "succeeded", sparseRun, { turns: 8, turn_limit: 50, tool_calls: 6, tool_failures: 0, lint_checks: null, draft_writes: null });
const agents: AgentJobs = { window_limit: 100, jobs: [deposited, rejected, failed, sparse], types: [
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
  assert.equal(depositOutcome(sparse), null);
  assert.equal(depositOutcome(job("c", "succeeded", { ...accountRun, result: { ...accountRun.result, deposit_status: "not_configured" } }))?.tone, "bad");
  assert.equal(depositOutcome(job("d", "running", { ...accountRun, result: noResult }))?.text, "Pending");
  const inconsistent = job("e", "succeeded", { ...accountRun, result: { ...accountRun.result, deposit_status: "failed", deposit_reason: "inconsistent" } });
  assert.deepEqual(depositOutcome(inconsistent), { text: "Deposit inconsistent: the claims repository holds part of the account otherwise than sent", tone: "bad" });
});

test("the job detail says where the owner's account is read from", () => {
  assert.equal(accountSource(deposited.agent.result), "served from the claims repository");
  assert.equal(accountSource({ account_id: "dapper:ScientificAccount.x", deposit_status: "withheld_unsafe" }), "served from the copy kept with the job");
  assert.equal(accountSource({ account_id: null, deposit_status: null }), null);
});

test("the agent jobs view shows type, settings, usage and deposit for each job", () => {
  const html = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents, kinds: [{ kind: "analysis", count: 46 }, { kind: "agent", count: 4 }] }));
  for (const text of ["Scientific account", "Answer", "46 research runs · 4 agent jobs", "1 deposited", "1 succeeded without a deposit", "Deposit failed: rejected (HTTP 422)",
    "3 claims to assess, 3 with a triple", "49 of 999 turns", "48 tool calls, 7 failed", "90 REVEAL tool calls", "64,000 output tokens per response", "37 objects",
    "VALIDATION_FAILED", "No built-in tools", "12,161 characters", "Not applicable", "claims repository · open network",
    "No claims repository, graphs or extra hosts"]) assert.ok(html.includes(text), text);
  assert.ok(!/literature|Paper search|searches ×|before job types existed/.test(html));
  // The sparse job never recorded these, so the view does not print a value for them.
  const row = html.slice(html.indexOf("09f4db0c"), html.indexOf("</tr>", html.indexOf("09f4db0c")));
  assert.ok(!/searches|REVEAL tool calls|output tokens|claims? to assess|lint checks/.test(row), row);
});

test("an empty window says so", () => {
  const html = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents: { window_limit: 100, jobs: [], types: [] } }));
  assert.ok(html.includes("No general agent job has been submitted yet."));
});

test("the job detail lists how the job was run and never a value the input lacks", () => {
  const html = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: accountRun, job: deposited }));
  for (const text of ["How this agent job was run · Scientific account", "Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch", "1,344 characters · no system prompt", "3 claims to assess, 3 with a triple",
    "Deposited", "dep_01a1e52fa3c2b989bcafce5d37f4feb4", "dapper:ScientificAccount.wF3k", "37 objects · 3 claims · 6 quotes · 5 papers · 1 lint warning · DAPPER 0.2.1-a1",
    "$5.00", "1.0h", "<dt>REVEAL tool calls</dt><dd>90</dd>", "<dt>Output per response</dt><dd>64,000 tokens</dd>", "open"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("Paper search") && !html.includes("Papers read"));
  const old = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: sparseRun, job: sparse }));
  assert.ok(old.includes("How this agent job was run · Answer"));
  // An input frozen before these limits existed lacks tool_calls and max_output_tokens.
  assert.equal(old.match(/Not part of this input/g)?.length, 2);
  assert.ok(old.includes("<dt>Output per response</dt><dd>Not part of this input</dd>"));
  assert.ok(old.includes("<dt>Claims repository lookups</dt><dd>—</dd>"));
  assert.ok(!old.includes("Deposit"));
});

// The deposit screen withheld this account: the job succeeded, its account was returned, nothing was deposited.
const withheldRun: AgentRun = { ...accountRun, result: { ...accountRun.result, deposit_status: "withheld_unsafe", deposit_id: null, deposit_reason: "prompt_injection",
  screen_outcome: "withheld", screen_check: "prompt_injection", screen_score: 0.973, screen_threshold: 0.5, screen_location: "sources/PMC3491803.txt line=212-214",
  screen_classifier: "prompt-guard-2-22m@0123456789ab", screen_windows: 84, screen_seconds: 2.4 } };
const withheld = job("cccccccc-0000", "succeeded", withheldRun);

test("an account the deposit screen withheld shows the check, the score against its threshold and where", () => {
  assert.deepEqual(depositOutcome(withheld), { text: "Withheld as unsafe: prompt injection", tone: "bad" });
  assert.equal(screenSummary(withheldRun.result), "Withheld: prompt injection · score 0.97 (threshold 0.50) · at sources/PMC3491803.txt line=212-214 · 84 windows in 2.4 s");
  assert.equal(screenSummary({ ...accountRun.result, screen_outcome: "passed", screen_score: 0.012, screen_threshold: 0.5, screen_location: "ledger call 7 (WebFetch) line=1-3",
    screen_classifier: "prompt-guard-2-22m@0123456789ab", screen_windows: 31, screen_seconds: 0.9 }),
    "Passed · score 0.01 (threshold 0.50) · highest at ledger call 7 (WebFetch) line=1-3 · 31 windows in 0.9 s");
  assert.equal(screenSummary({ ...accountRun.result, screen_outcome: "withheld", screen_check: "unsafe_text", screen_score: null, screen_threshold: 0.5,
    screen_location: "account scientific_accounts[0].closing_remarks", screen_classifier: "not_loaded", screen_windows: 0 }),
    "Withheld: unsafe text · classifier not loaded · at account scientific_accounts[0].closing_remarks");
  assert.equal(screenSummary(accountRun.result), null);
  const detail = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: withheldRun, job: withheld }));
  for (const text of ["Withheld as unsafe: prompt injection", "<dt>Prompt-injection screen</dt>", "score 0.97 (threshold 0.50)", "sources/PMC3491803.txt line=212-214"])
    assert.ok(detail.includes(text), text);
  const view = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents: { window_limit: 100, jobs: [withheld, deposited], types: [
    { ...agents.types[0], jobs: 2, succeeded: 2, failed: 0, deposited: 1, deposit_not_made: 0, withheld_unsafe: 1, withheld_by_check: { prompt_injection: 1 } }] } }));
  for (const text of ["1 withheld as unsafe", "(prompt injection 1)", "Withheld as unsafe: prompt injection", "at sources/PMC3491803.txt line=212-214"])
    assert.ok(view.includes(text), text);
  assert.ok(!view.includes("succeeded without a deposit"));
});

const XML_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3491803/fullTextXML";
const sourcesRun: AgentRun = { ...accountRun, result: { ...accountRun.result, screen_outcome: "passed", screen_score: 0.012, screen_threshold: 0.5,
  screen_location: "sources/lepr.txt line=40-41", screen_classifier: "prompt-guard-2-22m@0123456789ab", screen_windows: 31, screen_texts: 6, screen_seconds: 0.9 },
  sources: { total: 2, items: [
    { path: "sources/PMC3491803.xml", url: XML_URL, command: `curl -sSL -o output/sources/PMC3491803.xml ${XML_URL}`,
      observed_command: `mkdir -p output/sources && curl -sSL -o output/sources/PMC3491803.xml ${XML_URL}`, derived_from: null,
      sha256: "3f2a9c41d07be5aa" + "0".repeat(48), media_type: "application/xml", size_bytes: 48213, evidence_items: 2 },
    { path: "sources/lepr.txt", url: "https://example.org/lepr.pdf", command: "pdftotext -layout output/sources/lepr.pdf output/sources/lepr.txt",
      observed_command: "pdftotext -layout output/sources/lepr.pdf output/sources/lepr.txt", derived_from: "sources/lepr.pdf",
      sha256: "b".repeat(64), media_type: "text/plain", size_bytes: 1_204_000, evidence_items: 0 }] } };

test("an account job's detail lists each saved source with its URL, commands, size, checksum, type and quotes", () => {
  assert.equal(shortSha("3f2a9c41d07be5aa" + "0".repeat(48)), "3f2a9c41d07b");
  assert.equal(shortSha(null), "—");
  assert.deepEqual([fileSize(1), fileSize(999), fileSize(48213), fileSize(1_204_000), fileSize(null)], ["1 byte", "999 bytes", "48.2 kB", "1.2 MB", "—"]);
  const html = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: sourcesRun, job: job("dddddddd-0000", "succeeded", sourcesRun) }));
  for (const text of ["Sources the agent saved", "<th>Observed command</th>", "<th>Quotes citing it</th>", "sources/PMC3491803.xml", XML_URL,
    `curl -sSL -o output/sources/PMC3491803.xml ${XML_URL}`, `mkdir -p output/sources &amp;&amp; curl -sSL -o output/sources/PMC3491803.xml`,
    "48.2 kB", "3f2a9c41d07b", "application/xml", "converted from sources/lepr.pdf", "Same as the command", "1.2 MB", "text/plain",
    "never the file&#x27;s text"]) assert.ok(html.includes(text), text);
  const rows = html.slice(html.indexOf("<tbody>", html.indexOf("Sources the agent saved")));
  assert.ok(/<td>2<\/td><\/tr>/.test(rows) && /<td>0<\/td><\/tr>/.test(rows), "the quote counts");
  // The checksum is shortened on screen; the whole one is its tooltip.
  assert.ok(html.includes(`title="3f2a9c41d07be5aa${"0".repeat(48)}">3f2a9c41d07b</td>`), "the checksum is shortened on screen");
  assert.ok(!html.includes("The first"));
  // The screen verdict says how many texts it scored, in the row it already had.
  assert.equal(screenSummary(sourcesRun.result), "Passed · score 0.01 (threshold 0.50) · highest at sources/lepr.txt line=40-41 · 31 windows from 6 texts in 0.9 s");
  assert.equal(html.match(/Prompt-injection screen/g)?.length, 1);
  // A capped list says so; an account citing nothing says so; an older backend or an answer job shows no section.
  const capped = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: { ...sourcesRun, sources: { ...sourcesRun.sources!, total: 130 } }, job: deposited }));
  assert.ok(capped.includes("The first 2 of 130 sources."));
  const none = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: { ...sourcesRun, sources: { items: [], total: 0 } }, job: deposited }));
  assert.ok(none.includes("The account cites no saved source file."));
  for (const run of [accountRun, { ...sourcesRun, sources: null }, { ...sparseRun, sources: sourcesRun.sources }])
    assert.ok(!renderToStaticMarkup(React.createElement(AgentRunDetail, { run, job: deposited })).includes("Sources the agent saved"));
  // The job list never shows them.
  const view = renderToStaticMarkup(React.createElement(AdminAgentJobsView, { agents: { ...agents, jobs: [job("dddddddd-0000", "succeeded", sourcesRun)] } }));
  assert.ok(!view.includes(XML_URL) && !view.includes("Sources the agent saved"));
});

// The classifier's lock is not pinned: only the fixed checks ran, and the screen row says the classifier is off.
test("a screen run with the classifier disabled says it is off until its lock is pinned", () => {
  const passed: AgentRun = { ...accountRun, result: { ...accountRun.result, screen_outcome: "passed", screen_check: null, screen_score: null,
    screen_threshold: 0.5, screen_location: null, screen_classifier: "disabled", screen_windows: 0, screen_texts: 0, screen_seconds: 0.1 } };
  assert.equal(screenSummary(passed.result), "Passed · classifier off — lock not pinned");
  assert.equal(screenSummary({ ...passed.result, screen_outcome: "withheld", screen_check: "unsafe_text", screen_location: "account scientific_accounts[0].closing_remarks" }),
    "Withheld: unsafe text · classifier off — lock not pinned · at account scientific_accounts[0].closing_remarks");
  const detail = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: passed, job: job("eeeeeeee-0000", "succeeded", passed) }));
  assert.ok(detail.includes("<dt>Prompt-injection screen</dt><dd>Passed · classifier off — lock not pinned</dd>"), detail);
});

// The admin content read (GET /api/admin/jobs/{id}/run) of an answer job stopped at the max_turns its request set.
const limitRow = (key: string, frozen: number, extra: Partial<AgentLimitUse> = {}): AgentLimitUse => ({ key, requested: false, requested_basis: "recorded",
  differs_from_current_default: null, frozen, ceiling: frozen * 2, ceiling_basis: "queue_time", applied: frozen, used: null, hit: false, ...extra });
const content: AgentRunContent = {
  job: { id: "95d0bc18-0000", owner: "owner-1", status: "failed", stage: "complete", created_at: "2026-11-16T16:20:14Z", updated_at: null, completed_at: null,
    failure: { code: "AGENT_EXECUTION_FAILED", message: "The agent reached its 12-turn limit before finishing." } },
  input: { format: "reveal.agent-input/5", prompt: "List every LEPR paper.\n  Keep <b>markup</b> as text.", system_prompt: null, tools: ["Read", "Bash"], graphs: [],
    network: "restricted", allowed_domains: ["eutils.ncbi.nlm.nih.gov"], effort: "high", model: "claude-opus-5-5", deliverable: "answer", claims_repository: false,
    output_schema: { type: "object" }, limits: { max_turns: 12 }, account: null },
  limits: [limitRow("timeout_seconds", 600, { used: 42 }), limitRow("max_turns", 12, { requested: true, used: 12, hit: true }),
    limitRow("max_budget_usd", 1, { used: .31, applied: .5 }), limitRow("tool_calls", 30, { all_tool_calls: 18 }), limitRow("max_output_tokens", 32000, { reached: false, ceiling_basis: "current" })],
  stop: { limit: "max_turns", by: "request", text: "Stopped at its own max_turns (12), which the request set." },
  run: { subtype: "error_max_turns", status: "failed", reason: null, tool_calls: 18, tool_failures: 0 },
  response: { source: "captured", attempt: 2, text: "RESPONSE-TEXT in full", truncated: false, characters: 21, status: "failed", subtype: "error_max_turns",
    structured_output: { genes: ["LEPR"] }, captured_unavailable: null, files: [{ path: "table.csv", size_bytes: 1200, sha256: "c".repeat(64) }] },
  read: { by: "admin@example.org", at: "2026-11-19T10:00:00Z" },
};

test("the request shows the prompt, settings, limits with the hit one highlighted, the stop reason and the response", () => {
  const html = renderToStaticMarkup(React.createElement(AgentRequestView, { data: content }));
  assert.ok(html.includes('<p class="admin-stop-reason hit" role="status"><strong>Stopped at its own max_turns (12), which the request set.</strong></p>'));
  // The prompt is preformatted text, escaped, with a copy button; a missing system prompt says so.
  assert.ok(html.includes("<pre>List every LEPR paper.\n  Keep &lt;b&gt;markup&lt;/b&gt; as text.</pre>"));
  assert.ok(html.includes('aria-label="Copy prompt"') && html.includes("No system prompt."));
  for (const text of ["<dt>Model · effort</dt><dd>claude-opus-5-5 · high</dd>", "<dt>Extra hosts</dt><dd>eutils.ncbi.nlm.nih.gov</dd>", "<dt>Built-in tools</dt><dd>Read, Bash</dd>",
    "&quot;type&quot;: &quot;object&quot;", "<th>Requested</th><th>Frozen</th><th>Ceiling</th><th>Used</th>", "18 tool calls of every kind", "Not reached",
    "Today&#x27;s ceiling: not recorded when queued", "The run was given $0.50", "RESPONSE-TEXT in full", "the agent&#x27;s full final message, from its response record (attempt 2)", "&quot;LEPR&quot;",
    "table.csv</span> · 1.2 kB", "Read by admin@example.org"]) assert.ok(html.includes(text), text);
  // Only the hit limit's row is highlighted, and it says so in words.
  assert.equal(html.match(/admin-limit-hit/g)?.length, 1);
  const hit = html.slice(html.indexOf('<tr class="admin-limit-hit">'), html.indexOf("</tr>", html.indexOf('<tr class="admin-limit-hit">')));
  assert.ok(hit.includes("Turns") && hit.includes("Hit") && hit.includes("<td>12</td>"), hit);
  assert.ok(html.includes("<td>Run time</td><td>Not set: default</td>"));
});

test("an older job's request says what is not recorded, and a cut response says it is cut", () => {
  const older: AgentRunContent = { ...content, stop: { limit: null, by: null, text: "Finished normally." },
    limits: content.limits.map(row => ({ ...row, hit: false, requested: null, requested_basis: "not_recorded", differs_from_current_default: row.key === "max_turns" })),
    response: { ...content.response, source: "job_result", truncated: true, characters: 50000, captured_unavailable: "not_captured", structured_output: null } };
  const html = renderToStaticMarkup(React.createElement(AgentRequestView, { data: older }));
  assert.ok(html.includes('<p class="admin-stop-reason" role="status"><strong>Finished normally.</strong></p>'));
  assert.ok(!html.includes("admin-limit-hit"));
  assert.equal(html.match(/Not recorded/g)?.length, 5);
  assert.ok(html.includes("Differs from today&#x27;s default") && html.includes("Same as today&#x27;s default"));
  assert.ok(html.includes("50,000 characters · truncated") && html.includes("cut at 50,000 characters. The full text is not available: no response record was captured."));
  assert.ok(!html.includes("Structured output"));
});

test("the job detail offers the request on demand instead of saying it is never shown", () => {
  const html = renderToStaticMarkup(React.createElement(AgentRunDetail, { run: accountRun, job: deposited }));
  assert.ok(html.includes(">Show request</button>") && html.includes("Each time you show it, your email and the time are recorded."));
  assert.ok(!/not shown here|never shown/.test(html));
  assert.ok(!html.includes("admin-stop-reason"));   // nothing is loaded until asked
});
