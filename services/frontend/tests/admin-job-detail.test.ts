import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { AgentRun, JobDetail, TranscriptPage, TranscriptTurn } from "../src/lib/admin-job";
import { appendPage, callFailed, itemFailed, toolInput, transcriptItems } from "../src/lib/admin-transcript";
import { boxConsoleUrl, JOB_SECTIONS, JobDetailView } from "../src/components/admin/JobDetail";
import { TranscriptSection, type TranscriptState } from "../src/components/admin/JobTranscript";

const render = <P extends object>(component: React.ComponentType<P>, props: P) => renderToStaticMarkup(React.createElement(component, props));
const ID = "95d0bc18-6f2a-4b6c-9c1e-3a1d2e4f5a6b";

const noResult: AgentRun["result"] = { response_truncated: null, response_characters: null, files: null, account_id: null, objects: null, claims: null,
  quotes: null, papers: null, lint_warnings: null, dapper_version: null, deposit_status: null, deposit_id: null, deposit_reason: null, deposit_http_status: null };
const agentRun: AgentRun = { deliverable: "answer", input_format: "reveal.agent-input/5", model: "claude-opus-5-5", effort: "high", tools: ["Read", "Bash"], graphs: [],
  network: "restricted", allowed_domains: [], claims_repository: true, limits: { max_turns: 12 }, attribute_requester: null, prompt_characters: 40,
  system_prompt_characters: null, question_characters: null, claims_requested: null, claims_with_triple: null, result: { ...noResult, response_characters: 21, files: 0 } };

const detail = (kind: string, extra: Partial<JobDetail["job"]> = {}, rest: Partial<JobDetail> = {}): JobDetail => ({
  job: { id: ID, owner: "owner-1", kind, deliverable: kind === "agent" ? "answer" : null, status: "failed", stage: "complete", created_at: "2026-11-16T16:20:14Z",
    started_at: "2026-11-16T16:20:20Z", attempt_started_at: "2026-11-16T16:20:20Z", completed_at: "2026-11-16T16:27:47Z", updated_at: "2026-11-16T16:27:47Z",
    wall_seconds: 453, queue_seconds: 1, attempt_seconds: 450, attempt: 2, worker: "worker-a", lease_until: null, recoveries: 1, lease_expired: false,
    box_id: "box_7Hq2", box_phase: "deleted", box_seconds: 400, setup_seconds: 20, agent_seconds: 364, capture_seconds: 8, cleanup_seconds: 2,
    failure_code: "AGENT_EXECUTION_FAILED", failure: { code: "AGENT_EXECUTION_FAILED", message: "The agent reached its 12-turn limit.", retryable: false },
    warnings: ["A source was larger than expected."], research_request_id: null, input_account_id: null, ...extra },
  events: [{ id: "41", job_id: ID, occurred_at: "2026-11-16T16:21:00Z", event_type: "activity", status: "running", stage: "running_agent", message: "Called Bash", result: null,
    detail: { kind: "tool", state: "failed", tool_name: "Bash", display_arguments: "ls /reveal", output_excerpt: "No such file" } as never }],
  next_before: "40", attempts: [{ attempt: 1, started_at: "2026-11-16T16:20:20Z", worker_id: "worker-a", "remote_handle.box_id": "box_1aaa" },
    { attempt: 2, started_at: "2026-11-16T16:22:00Z", worker_id: "worker-a", "remote_handle.box_id": "box_7Hq2" }],
  attempts_limited: false,
  diagnostics: [{ path: "attempt-2/output/failure.json", available: true, data: { phase: "run_agent", error_type: "TurnLimit", message: "12 turns used" } },
    { path: "attempt-2/runtime.json", available: false, reason: "The file is larger than 128 KiB." }],
  diagnostics_limited: false,
  metrics: { parent_job_id: null, agent: { model: "claude-opus-5-5", status: "failed", subtype: "error_max_turns", reason: "Reached its 12-turn limit", cost_usd: 0.3125,
    cost_source: "provider", budget_usd: 1, budget_used: 0.3125, turns: 12, turn_limit: 12, elapsed_seconds: 364, time_limit_seconds: 600, api_seconds: 120,
    tokens: { input: 100, output: 2000, cache_write: 300, cache_read: 4000 }, tool_calls: 18, tool_failures: 2, lint_checks: null, draft_writes: null },
    error: { phase: "run_agent", error_type: "AgentFailure", message: "Claude Code stopped at its turn limit.", recorded_at: "2026-11-16T16:27:40Z" } },
  agent: kind === "agent" ? agentRun : null,
  ...rest,
});

/** The panels' titles, in page order. */
const sections = (html: string) => [...html.matchAll(/<section class="admin-panel" aria-label="([^"]*)">/g)].map(m => m[1].replace("&amp;", "&"));

test("an agent job's page has the nine sections in order, the summary's status callout first", () => {
  const html = render(JobDetailView, { jobId: ID, data: detail("agent") });
  assert.deepEqual(sections(html), [...JOB_SECTIONS]);
  const summary = html.slice(html.indexOf('aria-label="Summary"'), html.indexOf('aria-label="Prompt and settings"'));
  // The callout comes first: the failure, its recorded cause and the saved failure.json; then the times; raw JSON last.
  assert.ok(summary.indexOf("admin-callout bad") < summary.indexOf("admin-key-values") && summary.indexOf("admin-key-values") < summary.indexOf("Raw job JSON"));
  for (const text of ["AGENT_EXECUTION_FAILED", "The agent reached its 12-turn limit.", "AgentFailure · run agent", "Recorded cause", "TurnLimit · run agent",
    "attempt-2/output/failure.json", "<dt>Kind</dt><dd>agent · answer</dd>", "<dt>Current attempt started</dt>", "<dt>Worker</dt><dd>worker-a</dd>",
    "<dt>Box</dt><dd>box_7Hq2</dd>", "<dt>Attempt / recoveries</dt><dd>2 / 1</dd>"]) assert.ok(summary.includes(text), text);
  // The recorded reads are offered, never made: nothing is loaded until asked.
  assert.equal(html.match(/>Show prompt and response<\/button>/g)?.length, 2);
  assert.ok(html.includes(">Show transcript</button>") && !html.includes("admin-stop-reason") && !html.includes("Read by"));
  // Event log, usage, timings, sandbox and diagnostics as today.
  for (const text of ["1 stored events loaded · earlier events available", "Job finished.", "Load earlier events", "Copy loaded logs", 'aria-label="Search job logs"',
    "<summary>Tool arguments</summary><pre>ls /reveal</pre>", "<details open=\"\"><summary>Tool output</summary>", "admin-log-event failed",
    "Agent cost and usage · $0.3125", "<dt>Budget used</dt><dd>31% of $1.0000</dd>", "<dt>Tokens: output · input</dt><dd>2,000 · 100</dd>", "<dt>Tool calls (failed)</dt><dd>18 (2)</dd>",
    "Runner reason: Reached its 12-turn limit", "Wall time · 7.5m total", "Queue / pre-attempt", "Box cleanup", "Agent time includes polling",
    "Warnings (1)", "A source was larger than expected.", "The file is larger than 128 KiB.", "Attempt history (2)", "<td>box_1aaa</td>"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes('href="/admin/jobs">Back to jobs</a>') && html.includes(">Refresh logs</button>"));
});

test("a research run's page keeps all nine sections and says the agent-only ones do not apply, asking for nothing", () => {
  const html = render(JobDetailView, { jobId: ID, data: detail("analysis", { status: "succeeded", failure: null, failure_code: null }, { agent: null }) });
  assert.deepEqual(sections(html), [...JOB_SECTIONS]);
  assert.equal(html.match(/Only general agent jobs record this\. This job is a research run:/g)?.length, 3);
  assert.ok(!html.includes("Show prompt and response") && !html.includes("Show transcript"));
  assert.ok(html.includes("admin-callout good") && !html.includes("admin-job-failure"));
});

test("the page says when it is loading, keeps stale logs on an error, and refreshes only a running job", () => {
  assert.ok(render(JobDetailView, { jobId: ID, data: null, loading: true }).includes("Loading job logs and diagnostics…"));
  const stale = render(JobDetailView, { jobId: ID, data: detail("agent"), error: "The database is unavailable." });
  assert.ok(stale.includes('role="alert">The database is unavailable. Showing previously loaded logs.</div>') && stale.includes("Event log"));
  const running = detail("agent", { status: "running", stage: "running_agent", failure: null, completed_at: null });
  assert.ok(render(JobDetailView, { jobId: ID, data: running, auto: true }).includes("Live logs refresh every 15s."));
  assert.ok(render(JobDetailView, { jobId: ID, data: running, auto: true, olderLoaded: true }).includes("Refresh logs to see current activity."));
  assert.ok(render(JobDetailView, { jobId: ID, data: running, auto: false }).includes("Refresh logs to see current activity."));
});

test("the sandbox shows the Box id to copy, links the console only when a template is configured, and has no snapshot yet", () => {
  const html = render(JobDetailView, { jobId: ID, data: detail("agent") });
  const sandbox = html.slice(html.indexOf('aria-label="Sandbox"'), html.indexOf('aria-label="Diagnostics'));
  assert.ok(sandbox.includes('aria-label="Copy: box_7Hq2"') && sandbox.includes("the link to the Box console comes with Phase 4"));
  assert.ok(sandbox.includes("attempt 1: box_1aaa · attempt 2: box_7Hq2") && sandbox.includes("Not yet: Box snapshots come with Phase 4."));
  const linked = render(JobDetailView, { jobId: ID, data: detail("agent"), boxConsole: "https://console.upstash.com/box/{box_id}" });
  assert.ok(linked.includes('href="https://console.upstash.com/box/box_7Hq2"'));
  assert.equal(boxConsoleUrl("https://c.example/{box_id}/x", "a b"), "https://c.example/a%20b/x");
  // Only an https template with the placeholder makes a link.
  for (const template of [null, "", "javascript:alert(1)//{box_id}", "http://c.example/{box_id}", "https://c.example/box"]) assert.equal(boxConsoleUrl(template, "b"), null, String(template));
  assert.equal(boxConsoleUrl("https://c.example/{box_id}", null), null);
});

// A transcript: start, thinking, a call that failed, a call that worked, a cut, an unfinished message, the end.
const turn = (index: number, extra: Partial<TranscriptTurn>): TranscriptTurn => ({ index, role: "assistant", kind: "text", text: null, truncated: false, tool_name: null,
  tool_input: null, tool_input_truncated: false, call_id: null, is_error: false, partial: false, detail: null, at: "2026-11-16T16:21:00Z", ...extra });
const turns: TranscriptTurn[] = [
  turn(0, { role: "system", text: "Claude Code 2.1.3 started with model claude-opus-5-5 and 9 tools.", detail: { model: "claude-opus-5-5", tools: ["Read"] } }),
  turn(1, { kind: "thinking", text: "PRIVATE-REASONING about the question" }),
  turn(2, { kind: "tool_use", tool_name: "Bash", tool_input: '{"command":"ls /missing"}', call_id: "toolu_01failed00" }),
  turn(3, { role: "tool", kind: "tool_result", tool_name: "Bash", text: "ls: /missing: No such file", call_id: "toolu_01failed00", is_error: true }),
  turn(4, { kind: "tool_use", tool_name: "mcp__reveal__query_claims", tool_input: '{"query":"LEPR"', call_id: "toolu_02partial0", tool_input_truncated: true }),
  turn(5, { role: "system", text: "1,024 bytes (3 lines) of the transcript were left out here to fit the capture limit.", detail: { omitted_bytes: 1024, omitted_lines: 3, original_bytes: 9000 } }),
  turn(6, { role: "tool", kind: "tool_result", tool_name: null, text: "orphan result text", call_id: "toolu_gone0000" }),
  turn(7, { role: "tool", kind: "tool_result", tool_name: "mcp__reveal__query_claims", text: "3 claims", call_id: "toolu_02partial0" }),
  turn(8, { text: "LEPR is linked to obesity", partial: true, truncated: true }),
  turn(9, { role: "result", kind: "result", text: "Reached max turns", is_error: true, detail: { subtype: "error_max_turns", stop_reason: "tool_use", num_turns: 12, duration_ms: 364000, duration_api_ms: 120000, total_cost_usd: 0.31 } }),
];
const page = (from: number, to: number, extra: Partial<TranscriptPage> = {}): TranscriptPage => ({
  job: { id: ID, status: "failed" }, attempt: 2, latest_attempt: 2, missing: null, turns: turns.slice(from, to), total: turns.length,
  next_after: to - 1, has_more: to < turns.length, cut: true, complete: true, skipped: 4,
  stderr: { text: "STDERR-TAIL: warning: rate limited", truncated: true, cut: false, unreadable: false }, read: { by: "admin@example.org", at: "2026-11-19T10:00:00Z" }, ...extra });
const shown = (data: TranscriptPage | null, extra: Partial<TranscriptState> = {}) =>
  render(TranscriptSection, { transcript: { data, loading: false, error: "", ...extra }, onLoad: () => {}, onMore: () => {} });

test("calls pair with their results by call id across pages; a result without its call stands alone", () => {
  const first = page(0, 5), second = page(5, 10);
  // On the first page the second call has no result yet.
  const early = transcriptItems(first.turns);
  assert.deepEqual(early.map(i => i.type), ["turn", "turn", "call", "call"]);
  assert.equal(early[3].type === "call" && early[3].result, null);
  const all = appendPage(first, second);
  assert.deepEqual(all.turns.map(t => t.index), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  assert.equal(all.has_more, false);
  // A page loaded twice adds nothing.
  assert.equal(appendPage(all, second).turns.length, 10);
  // Another attempt replaces the turns.
  assert.equal(appendPage(all, page(0, 2, { attempt: 1 })).turns.length, 2);
  const items = transcriptItems(all.turns);
  assert.deepEqual(items.map(i => i.type), ["turn", "turn", "call", "call", "turn", "result", "turn", "turn"]);
  const [, , failed, later] = items;
  assert.ok(failed.type === "call" && failed.result?.index === 3 && itemFailed(failed));
  assert.ok(later.type === "call" && later.result?.index === 7 && !itemFailed(later));
  assert.ok(items[5].type === "result" && items[5].result.call_id === "toolu_gone0000");
  assert.ok(itemFailed(items[7]) && !callFailed(items[7]));   // the run ended in error; that is not a failed call
  assert.deepEqual(items.map(callFailed), [false, false, true, false, false, false, false, false]);
  // A result with no call id never pairs.
  assert.deepEqual(transcriptItems([turn(0, { role: "tool", kind: "tool_result", text: "x" })]).map(i => i.type), ["result"]);
  // A partial call's input is shown as received; a whole one is pretty-printed.
  assert.deepEqual(toolInput('{"query":"LEPR"'), { text: '{"query":"LEPR"', parsed: false });
  assert.deepEqual(toolInput('{"a":1}'), { text: '{\n  "a": 1\n}', parsed: true });
  assert.equal(toolInput(null), null);
});

test("the transcript renders turn by turn: failed calls marked, thinking folded, unfinished and cut turns said, stderr last", () => {
  const html = shown(appendPage(page(0, 5), page(5, 10)));
  // Each call shows its arguments and result together; the failed one is highlighted and opened.
  const failed = html.slice(html.indexOf("toolu_01failed00") - 400, html.indexOf("No such file") + 40);
  assert.ok(failed.includes("admin-log-event admin-turn tool_use failed") && failed.includes('<span class="admin-status bad">Failed</span>'), failed);
  assert.ok(html.includes('<details class="admin-turn-result" open=""><summary>Failed result · #3'));
  assert.ok(html.includes("&quot;command&quot;: &quot;ls /missing&quot;"));
  assert.ok(html.includes("Arguments · as received, not valid JSON · cut at 20,000 characters") && html.includes("{&quot;query&quot;:&quot;LEPR&quot;</pre>"));
  assert.equal(html.match(/admin-turn tool_use failed/g)?.length, 1);
  // Thinking is folded (a closed <details>), with its size.
  assert.ok(/<details><summary>Thinking · 36 characters<\/summary><pre>PRIVATE-REASONING/.test(html));
  // The cut, the orphan result, the unfinished message and the run's end.
  assert.ok(html.includes('<div class="admin-callout warn">1,024 bytes (3 lines) of the transcript were left out here'));
  assert.ok(html.includes("Its call is not among the loaded turns.") && html.includes("orphan result text"));
  assert.ok(html.includes('<span class="admin-status warn" title="Claude Code never finished this message') && html.includes(">Cut at 20,000 characters</span>"));
  assert.ok(html.includes('<span class="admin-status bad">Error</span>') && html.includes("error_max_turns · stop reason tool_use · 12 turns · 6.1m (API 2.0m) · $0.31"));
  // One failed call: the run's error end is marked on its own turn, not counted as a failed call.
  assert.ok(html.includes("10 of 10 turns loaded · 1 failed tool call · 4 notices skipped"));
  assert.ok(html.includes("The stored stream had its middle left out to fit the 20 MB capture limit"));
  // Stderr comes after the turns; nothing more to load.
  assert.ok(html.indexOf("STDERR-TAIL") > html.indexOf("Reached max turns") && html.includes("Its last 20,000 characters."));
  assert.ok(!html.includes("Load more turns") && html.includes("Last page read by admin@example.org"));
});

test("the transcript pages on request, offers the attempts, and says why one is missing", () => {
  const first = shown(page(0, 5));
  assert.ok(first.includes(">Load more turns</button>") && first.includes("5 of 10 turns loaded"));
  assert.ok(first.includes("No result yet among the loaded turns; it may be on a later page."));
  assert.ok(first.includes('<option value="1">Attempt 1</option>') && first.includes('<option value="2" selected="">Attempt 2 (newest)</option>'));
  const one = shown(page(0, 10, { latest_attempt: 1, attempt: 1, has_more: false }));
  assert.ok(!one.includes("<select") && one.includes("Attempt 1") && !one.includes("Load more turns"));
  // A call whose result never came, with nothing more to load.
  assert.ok(!shown(page(0, 4, { has_more: false })).includes("No result was recorded for this call."));
  assert.ok(shown(page(4, 5, { has_more: false })).includes("No result was recorded for this call."));
  for (const [missing, text] of [["not_captured", "No transcript was captured for this attempt"], ["too_large", "The transcript is too large to read here."],
    ["unreadable", "The stored transcript is not a readable gzip file."], ["unavailable", "Storage could not be read."]]) {
    const html = shown(page(0, 0, { missing, total: 0, has_more: false, cut: false, complete: null, stderr: null }));
    assert.ok(html.includes(text) && html.includes("No stderr was captured.") && !html.includes("The transcript has no turns."), missing);
  }
  assert.ok(shown(page(0, 10, { complete: false })).includes("The stored file did not end properly"));
  assert.ok(shown(page(0, 10, { stderr: { text: null, truncated: false, cut: false, unreadable: true } })).includes("The stored stderr is not readable."));
  // Before it is asked for, nothing is shown but the button; a failed first read says why.
  const unloaded = shown(null);
  assert.ok(unloaded.includes(">Show transcript</button>") && unloaded.includes("Each page of the transcript is a recorded read"));
  assert.ok(shown(null, { error: "Access logging is unavailable." }).includes('role="alert">Access logging is unavailable.</div>'));
  // A failed later page keeps the turns already loaded.
  assert.ok(shown(page(0, 5), { error: "Retry shortly." }).includes("Retry shortly. Showing the turns already loaded."));
});
