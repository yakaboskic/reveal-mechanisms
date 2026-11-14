import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  fallbackLimit, isLimitsView, limitFields, limitsForm, parseLimit, readLimitsChange, supportedRange,
  type AgentLimit, type AgentLimitsView, type LimitFields,
} from "../src/lib/admin-limits";
import { adminActor, adminEmail } from "../src/lib/admin-policy";
import { AdminLimits, LimitsHistory, LimitsTable } from "../src/components/AdminLimits";

const papers: AgentLimit = {
  key: "literature_papers", label: "Papers read", unit: "papers", description: "Distinct papers.", type: "integer",
  minimum: 1, exclusive_minimum: false, maximum: 1000, code: { default: 4, ceiling: 50 }, console: { default: 40, ceiling: null },
  default: { value: 40, source: "console", environment: "REVEAL_AGENT_JOB_DEFAULT_LITERATURE_PAPERS" },
  ceiling: { value: 50, source: "code", environment: "REVEAL_AGENT_JOB_MAX_LITERATURE_PAPERS" },
};
const budget: AgentLimit = {
  key: "max_budget_usd", label: "Budget", unit: "USD", description: "Provider cost of one run.", type: "number",
  minimum: 0, exclusive_minimum: true, maximum: 1000, code: { default: 2, ceiling: 5 }, console: { default: null, ceiling: 20 },
  default: { value: 2, source: "code", environment: "REVEAL_AGENT_JOB_DEFAULT_BUDGET_USD" },
  ceiling: { value: 20, source: "console", environment: "REVEAL_AGENT_JOB_MAX_BUDGET_USD" },
};
const turns: AgentLimit = {
  key: "max_turns", label: "Turns", unit: "turns", description: "Claude Code turns in one run.", type: "integer",
  minimum: 1, exclusive_minimum: false, maximum: 1000, code: { default: 50, ceiling: 100 }, console: { default: null, ceiling: null },
  default: { value: 50, source: "code", environment: "REVEAL_AGENT_JOB_DEFAULT_TURNS" },
  ceiling: { value: 80, source: "environment", environment: "REVEAL_AGENT_JOB_MAX_TURNS" },
};
const view: AgentLimitsView = {
  format: "reveal.agent-limits/1", version: 3, updated_at: "2026-11-12T20:29:03.303472Z", updated_by: "a@b.org", limits: [turns, budget, papers],
  history: [{ at: "2026-11-12T20:29:03.303472Z", by: "a@b.org", changes: { literature_papers: { default: [null, 40] }, max_budget_usd: { ceiling: [5.5, 20] } } }],
};
const edited = (changes: Record<string, Partial<Record<"default" | "ceiling", string>>>): LimitFields => {
  const fields = limitFields(view);
  for (const [key, change] of Object.entries(changes)) fields[key] = { ...fields[key], ...change };
  return fields;
};

test("the form starts from what the console set, never from effective values, and is unchanged", () => {
  const fields = limitFields(view);
  assert.deepEqual(fields, { max_turns: { default: "", ceiling: "" }, max_budget_usd: { default: "", ceiling: "20" }, literature_papers: { default: "40", ceiling: "" } });
  const form = limitsForm(view, fields);
  assert.deepEqual(form.changed, []);
  assert.deepEqual(form.errors, {});
  // The PUT replaces everything, so every console-set field is sent and nothing else.
  assert.deepEqual(form.values, { max_budget_usd: { ceiling: 20 }, literature_papers: { default: 40 } });
});

test("an edit sends all console-set values; another spelling of the saved number is not a change", () => {
  const form = limitsForm(view, edited({ max_turns: { ceiling: " 60 " } }));
  assert.deepEqual(form.changed, ["max_turns.ceiling"]);
  assert.deepEqual(form.values, { max_turns: { ceiling: 60 }, max_budget_usd: { ceiling: 20 }, literature_papers: { default: 40 } });
  assert.deepEqual(limitsForm(view, edited({ max_budget_usd: { ceiling: "20.0" }, literature_papers: { default: " 40" } })).changed, []);
  assert.deepEqual(limitsForm(view, edited({ max_budget_usd: { default: "0.5" } })).values.max_budget_usd, { default: 0.5, ceiling: 20 });
});

test("clearing a field removes it from the values, and a limit with nothing set is left out", () => {
  const form = limitsForm(view, edited({ literature_papers: { default: "" } }));
  assert.deepEqual(form.changed, ["literature_papers.default"]);
  assert.deepEqual(form.errors, {});
  assert.deepEqual(form.values, { max_budget_usd: { ceiling: 20 } });
  assert.deepEqual(limitsForm(view, edited({ literature_papers: { default: "   " }, max_budget_usd: { ceiling: "" } })).values, {});
  // A key the form has no entry for is read as not set.
  assert.deepEqual(limitsForm(view, {}).values, {});
  assert.deepEqual(limitsForm(view, {}).changed, ["max_budget_usd.ceiling", "literature_papers.default"]);
});

test("integer limits take whole numbers only; number limits take decimals; neither takes other text", () => {
  assert.deepEqual(parseLimit(papers, ""), { value: null });
  assert.deepEqual(parseLimit(papers, " 12 "), { value: 12 });
  for (const text of ["4.5", "4.0", "1e2", "1,000", "0x10", "abc", "4 0", "+4", "Infinity", "٤"]) assert.deepEqual(parseLimit(papers, text), { error: "Enter a whole number, digits only." }, text);
  assert.deepEqual(parseLimit(budget, "2.5"), { value: 2.5 });
  assert.deepEqual(parseLimit(budget, ".5"), { value: 0.5 });
  assert.deepEqual(parseLimit(budget, "20"), { value: 20 });
  for (const text of ["1e2", "1,5", "NaN", "Infinity", "2.5.1", ".", "-", "$5"]) assert.deepEqual(parseLimit(budget, text), { error: "Enter a number, such as 2 or 2.5." }, text);
  const form = limitsForm(view, edited({ literature_papers: { default: "4.5" } }));
  assert.deepEqual(form.errors, { "literature_papers.default": "Enter a whole number, digits only." });
  assert.deepEqual(form.changed, ["literature_papers.default"]);
  assert.equal("literature_papers" in form.values, false);
});

test("values must lie in the supported range, and an exclusive minimum excludes the minimum itself", () => {
  const whole = { error: "Enter a whole number from 1 to 1,000." };
  assert.deepEqual(parseLimit(papers, "1"), { value: 1 });
  assert.deepEqual(parseLimit(papers, "1000"), { value: 1000 });
  for (const text of ["0", "-3", "1001", "999999999999999999999999"]) assert.deepEqual(parseLimit(papers, text), whole, text);
  const decimal = { error: "Enter a number above 0, up to 1,000." };
  assert.deepEqual(parseLimit(budget, "0.01"), { value: 0.01 });
  assert.deepEqual(parseLimit(budget, "1000"), { value: 1000 });
  for (const text of ["0", "0.0", "-0", "-1", "1000.01"]) assert.deepEqual(parseLimit(budget, text), decimal, text);
  // The two flags are independent of the type.
  assert.deepEqual(parseLimit({ ...papers, exclusive_minimum: true }, "1"), { error: "Enter a whole number above 1, up to 1,000." });
  assert.deepEqual(parseLimit({ ...budget, exclusive_minimum: false }, "0"), { value: 0 });
  assert.equal(supportedRange(papers), "1 to 1,000");
  assert.equal(supportedRange(budget), "above 0, up to 1,000");
});

test("a default set here may not exceed the ceiling set here, or the fallback ceiling when that field is empty", () => {
  // Both set in the form.
  assert.deepEqual(limitsForm(view, edited({ max_turns: { default: "70", ceiling: "60" } })).errors, { "max_turns.default": "The default 70 is above the ceiling 60." });
  assert.deepEqual(limitsForm(view, edited({ max_turns: { default: "60", ceiling: "60" } })).errors, {});
  // Ceiling field empty: the environment's 80 and the built-in 50 are what the server will use.
  assert.deepEqual(fallbackLimit(turns, "ceiling"), { value: 80, known: true });
  assert.deepEqual(limitsForm(view, edited({ max_turns: { default: "81" } })).errors, { "max_turns.default": "The default 81 is above the ceiling 80." });
  assert.deepEqual(limitsForm(view, edited({ max_turns: { default: "80" } })).errors, {});
  assert.deepEqual(limitsForm(view, edited({ literature_papers: { default: "51" } })).errors, { "literature_papers.default": "The default 51 is above the ceiling 50." });
  // An empty default under a lower ceiling is legal: the server lowers the default to the ceiling.
  assert.deepEqual(limitsForm(view, edited({ max_turns: { ceiling: "10" } })).errors, {});
  assert.deepEqual(limitsForm(view, edited({ literature_papers: { default: "", ceiling: "2" } })).errors, {});
  // An unusable ceiling is reported on the ceiling alone.
  assert.deepEqual(Object.keys(limitsForm(view, edited({ max_turns: { default: "70", ceiling: "x" } })).errors), ["max_turns.ceiling"]);
});

test("clearing a console ceiling leaves the comparison to the server, because the view hides the environment value", () => {
  assert.deepEqual(fallbackLimit(budget, "ceiling"), { value: 5, known: false });
  assert.deepEqual(fallbackLimit(budget, "default"), { value: 2, known: true });
  const form = limitsForm(view, edited({ max_budget_usd: { default: "10", ceiling: "" } }));
  assert.deepEqual(form.errors, {});
  assert.deepEqual(form.values.max_budget_usd, { default: 10 });
  assert.deepEqual(limitsForm(view, edited({ max_budget_usd: { default: "21" } })).errors, { "max_budget_usd.default": "The default 21 is above the ceiling 20." });
});

test("only the current limits format is treated as a view", () => {
  assert.equal(isLimitsView(view), true);
  for (const value of [null, "x", [], {}, { error: "Not Found" }, { ...view, format: "reveal.agent-limits/2" }, { ...view, limits: null }, { ...view, history: undefined }]) assert.equal(isLimitsView(value), false);
});

const put = (body: BodyInit, headers: Record<string, string> = {}) => new Request("http://localhost/api/admin/agent-limits", { method: "PUT", body, headers, duplex: "half" } as RequestInit);
test("a limits change is parsed JSON reduced to expected_version and values", async () => {
  const sent = { expected_version: 3, values: { literature_papers: { default: 40 } }, admin_email: "attacker@example.org" };
  assert.deepEqual(await readLimitsChange(put(JSON.stringify(sent))), { ok: true, change: { expected_version: 3, values: { literature_papers: { default: 40 } } } });
  assert.deepEqual(await readLimitsChange(put('{"expected_version":null,"values":{}}')), { ok: true, change: { expected_version: null, values: {} } });
  for (const body of ["", "not json", "[]", "null", '"text"', '{"values":{}}', '{"expected_version":"3","values":{}}', '{"expected_version":1.5,"values":{}}', '{"expected_version":3}', '{"expected_version":3,"values":[]}', '{"expected_version":3,"values":null}']) {
    const result = await readLimitsChange(put(body));
    assert.deepEqual([result.ok, !result.ok && result.status], [false, 400], body);
  }
  const notUtf8 = await readLimitsChange(put(new Uint8Array([0x7b, 0xff, 0x7d])));
  assert.equal(!notUtf8.ok && notUtf8.status, 400);
});

test("a limits change larger than the cap is refused whether or not its length is declared", async () => {
  const body = JSON.stringify({ expected_version: 3, values: { padding: "x".repeat(9000) } });
  const declared = await readLimitsChange(put(body));
  assert.deepEqual(declared, { ok: false, status: 413, error: "The limits change is too large." });
  // A streamed body declares no length; the bytes read are what count.
  let pulled = 0;
  const stream = new ReadableStream<Uint8Array>({ pull(controller) { pulled += 1; controller.enqueue(new TextEncoder().encode("x".repeat(1024))); } });
  const request = put(stream);
  assert.equal(request.headers.get("content-length"), null);
  assert.deepEqual(await readLimitsChange(request), declared);
  assert.ok(pulled < 20, "reading stops at the cap");
  assert.equal((await readLimitsChange(put('{"expected_version":3,"values":{}}'), 16)).ok, false);
  const exact = '{"expected_version":3,"values":{}}';
  assert.equal((await readLimitsChange(put(exact), exact.length)).ok, true);
});

test("a change is attributed to the verified email, lower-cased, or to the development bypass", () => {
  const emails = "chase@broadinstitute.org";
  assert.equal(adminEmail(" Chase@BroadInstitute.org ", true, emails), "chase@broadinstitute.org");
  for (const [email, verified] of [["chase@broadinstitute.org", false], ["other@broadinstitute.org", true], [null, true]] as const) assert.equal(adminEmail(email, verified, emails), null);
  assert.equal(adminActor({ allowed: true, bypass: false, email: "chase@broadinstitute.org" }), "chase@broadinstitute.org");
  assert.equal(adminActor({ allowed: true, bypass: true, email: null }), "local-development-bypass");
  assert.equal(adminActor({ allowed: false, bypass: false, email: null }), null);
});

test("each limit input is labelled, shows its fallback and is tied to its error and note", () => {
  const fields = edited({ max_turns: { default: "81" }, max_budget_usd: { ceiling: "" } });
  const form = limitsForm(view, fields);
  const html = renderToStaticMarkup(React.createElement(LimitsTable, { view, fields, errors: form.errors, changed: form.changed, disabled: false, onChange() {} }));
  const inputs = [...html.matchAll(/<input[^>]*>/g)].map(match => match[0]);
  assert.equal(inputs.length, 6);
  const attribute = (tag: string, name: string) => new RegExp(` ${name}="([^"]*)"`).exec(tag)?.[1];
  assert.deepEqual(inputs.map(tag => attribute(tag, "aria-label")), ["Turns default, in turns", "Turns ceiling, in turns", "Budget default, in USD", "Budget ceiling, in USD", "Papers read default, in papers", "Papers read ceiling, in papers"]);
  // Empty fields show what applies instead. Under a console value (saved or just cleared) the view does not reveal it.
  assert.deepEqual(inputs.map(tag => attribute(tag, "placeholder")), ["50", "80", "2", "Not set", "Not set", "50"]);
  assert.deepEqual(inputs.map(tag => attribute(tag, "value")), ["81", "", "", "", "40", ""]);
  assert.deepEqual(inputs.map(tag => attribute(tag, "inputMode") ?? attribute(tag, "inputmode")), ["numeric", "numeric", "decimal", "decimal", "numeric", "numeric"]);
  assert.deepEqual(inputs.map(tag => attribute(tag, "aria-invalid")), ["true", undefined, undefined, undefined, undefined, undefined]);
  for (const tag of inputs) for (const id of (attribute(tag, "aria-describedby") || "").split(" ").filter(Boolean)) assert.ok(html.includes(` id="${id}"`), id);
  const described = attribute(inputs[0], "aria-describedby")!.split(" ");
  assert.equal(described.length, 2);
  assert.ok(html.includes(`<small id="${described[0]}" class="admin-limit-error">The default 81 is above the ceiling 80.</small>`));
  assert.ok(html.includes(`<small id="${described[1]}">Unsaved.</small>`));
  assert.match(html, /Unsaved; the saved value is 20\. Not set here: REVEAL_AGENT_JOB_MAX_BUDGET_USD applies if it is set, otherwise the built-in 5\./);
  assert.match(html, /Not set here: the environment variable REVEAL_AGENT_JOB_MAX_TURNS applies\./);
  // Effective values name their source.
  assert.match(html, /<strong class="admin-limit-value">80<\/strong> turns<small>Environment <span class="admin-mono">REVEAL_AGENT_JOB_MAX_TURNS<\/span><\/small>/);
  assert.match(html, /<strong class="admin-limit-value">40<\/strong> papers<small>Set in this console<\/small>/);
  assert.match(html, /<strong class="admin-limit-value">50<\/strong> papers<small>Built-in<\/small>/);
  assert.match(html, /Number<small>above 0, up to 1,000<\/small>/);
  assert.equal((html.match(/>Clear<\/button>/g) || []).length, 6);
});

test("flags from the server and the change history are spelled out", () => {
  const lowered: AgentLimit = { ...turns, default: { ...turns.default, value: 10, lowered_to_ceiling: true }, ceiling: { value: 10, source: "console", environment: turns.ceiling.environment, ignored_environment_value: true }, console: { default: null, ceiling: 10 } };
  const flagged = { ...view, limits: [lowered] };
  const table = renderToStaticMarkup(React.createElement(LimitsTable, { view: flagged, fields: limitFields(flagged), errors: {}, changed: [], disabled: true, onChange() {} }));
  assert.match(table, /Lowered to the ceiling; the built-in default is higher\./);
  assert.match(table, /REVEAL_AGENT_JOB_MAX_TURNS<\/span> is set to an unusable value and is ignored\./);
  assert.equal((table.match(/<input[^>]* disabled=""/g) || []).length, 2);
  const history = renderToStaticMarkup(React.createElement(LimitsHistory, { view }));
  assert.match(history, /<td>a@b\.org<\/td>/);
  assert.match(history, /<div>Papers read default: not set → 40<\/div><div>Budget ceiling: 5\.5 → 20<\/div>/);
  assert.match(renderToStaticMarkup(React.createElement(LimitsHistory, { view: { ...view, history: [] } })), /No change has been saved from this console yet\./);
});

test("the tab announces loading before its own request returns and is hidden while another tab is open", () => {
  const open = renderToStaticMarkup(React.createElement(AdminLimits, { active: true }));
  assert.match(open, /^<section class="admin-limits" aria-label="General agent job limits">/);
  assert.match(open, /<p role="status">Loading limits…<\/p>/);
  assert.match(open, /apply to general agent jobs only\. A change applies to jobs queued after it is saved\. A request can lower any limit, or raise it up to the ceiling\./);
  assert.match(renderToStaticMarkup(React.createElement(AdminLimits, { active: false })), /^<section class="admin-limits" hidden=""/);
});
