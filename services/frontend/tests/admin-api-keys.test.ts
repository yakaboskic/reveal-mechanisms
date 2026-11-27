import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  budgetFields, isKeysView, isMinted, mintForm, readBudgetRequest, readMintRequest, usageText,
  type ApiKeySummary, type ApiKeysView, type MintFields,
} from "../src/lib/admin-api-keys";
import { ConfiguredKeys, KeysTable, MintedKey } from "../src/components/AdminApiKeys";
import { ADMIN_NAV } from "../src/components/admin/AdminNav";

const RAW = "rvl_" + "A".repeat(43);
const key: ApiKeySummary = {
  key_id: "6f1c2c1e-6a43-4f53-9b0f-1f2a3b4c5d6e", label: "reveal-claims demo", prefix: RAW.slice(0, 8),
  user_id: "13b86465-2324-43c7-9f13-de73e1d27256", agent_jobs: true, budget: { limit_usd: 100, period: "month" },
  usage: { spent_usd: 12.5, in_progress_usd: 2, jobs: 4, period_start: "2026-11-01T00:00:00Z", limit_usd: 100, period: "month", remaining_usd: 87.5 },
  workspace: { principal_kind: "registered", display_name: "reveal-claims demo", workspace_expires_at: null, retired: false },
  created_at: "2026-11-27T10:00:00Z", created_by: "admin@example.org", revoked_at: null, revoked_by: null,
};
const view: ApiKeysView = { format: "reveal.api-keys/1", keys: [key], environment: { keys: [{ fingerprint: "abcdef012345", user_id: key.user_id, agent_jobs: false }], problem: null } };
const form = (fields: Partial<MintFields>): MintFields => ({ label: "demo", workspace: "new", userId: "", agentJobs: false, period: "none", limit: "", ...fields });
const noop = async () => true;

test("the console lists API keys in its navigation", () => {
  assert.ok(ADMIN_NAV.some(item => item.href === "/admin/api-keys"));
});

test("the mint form checks the label, an existing workspace id and the budget", () => {
  assert.deepEqual(mintForm(form({ agentJobs: true })), { request: { label: "demo", agent_jobs: true, budget: null } });
  assert.deepEqual(mintForm(form({ workspace: "existing", userId: ` ${key.user_id.toUpperCase()} ` })),
    { request: { label: "demo", agent_jobs: false, budget: null, user_id: key.user_id } });
  assert.deepEqual(mintForm(form({ period: "month", limit: "$1,000.50" })),
    { request: { label: "demo", agent_jobs: false, budget: { limit_usd: 1000.5, period: "month" } } });
  for (const label of ["", " demo", "-demo", "x".repeat(65)]) assert.ok("errors" in mintForm(form({ label })));
  const bad = mintForm(form({ workspace: "existing", userId: "nope" }));
  assert.ok("errors" in bad && bad.errors.userId);
  for (const limit of ["", "0", "-5", "12.345", "abc", "100001"]) {
    const result = mintForm(form({ period: "total", limit }));
    assert.ok("errors" in result && result.errors.limit, limit);
  }
  assert.deepEqual(budgetFields({ period: "none", limit: "garbage" }), { budget: null });
});

test("the proxy forwards only a small plain object with the known fields", async () => {
  const post = (body: string) => new Request("http://localhost/api/admin/api-keys", { method: "POST", body });
  assert.deepEqual(await readMintRequest(post('{"label":"demo","agent_jobs":true,"budget":null}')), { ok: true, request: { label: "demo", agent_jobs: true, budget: null } });
  for (const body of ["[]", "null", "not json", '{"label":"demo","owner":"x"}']) assert.equal((await readMintRequest(post(body))).ok, false);
  const large = await readMintRequest(post(JSON.stringify({ label: "x".repeat(5000) })));
  assert.equal(large.ok, false); assert.equal(!large.ok && large.status, 413);
  assert.equal((await readBudgetRequest(post('{"budget":{"limit_usd":5,"period":"month"}}'))).ok, true);
  assert.equal((await readBudgetRequest(post('{"budget":null}'))).ok, true);
  for (const body of ["{}", '{"budget":null,"label":"x"}']) assert.equal((await readBudgetRequest(post(body))).ok, false);
});

test("views and minted responses are recognized only in their own shape", () => {
  assert.ok(isKeysView(view));
  assert.ok(!isKeysView({ ...view, format: "other" }));
  assert.ok(isMinted({ key, api_key: RAW }));
  assert.ok(!isMinted({ key, api_key: "rvl_short" }));
});

test("spend reads against the budget, and a key with no budget says so", () => {
  assert.equal(usageText(key), "$12.50 of $100.00 this month · $87.50 left");
  assert.equal(usageText({ ...key, budget: { limit_usd: 20, period: "total" }, usage: { ...key.usage!, remaining_usd: 7.5 } }), "$12.50 of $20.00 in total · $7.50 left");
  assert.equal(usageText({ ...key, budget: null }), "$12.50 spent, no budget");
});

test("the table shows a key's prefix, never more, its spend, and revoked keys have no actions", () => {
  const html = renderToStaticMarkup(React.createElement(KeysTable, { keys: [key, { ...key, key_id: "x", revoked_at: "2026-11-27T11:00:00Z", revoked_by: "admin@example.org" }], busy: false, onRevoke: () => {}, onBudget: noop }));
  assert.match(html, /rvl_AAAA…/);
  assert.ok(!html.includes(RAW));
  assert.equal((html.match(/Revoke…/g) || []).length, 1);
  assert.equal((html.match(/Change budget/g) || []).length, 1);
  assert.match(html, /\$12\.50 of \$100\.00 this month/);
  assert.match(html, /held for jobs in progress/);
  assert.match(html, /revoked/);
});

test("the minted key is shown once in full, and configured keys are read-only", () => {
  const minted = renderToStaticMarkup(React.createElement(MintedKey, { minted: { key, api_key: RAW }, onDone: () => {} }));
  assert.ok(minted.includes(RAW)); assert.match(minted, /shown only this once/);
  const configured = renderToStaticMarkup(React.createElement(ConfiguredKeys, { view }));
  assert.match(configured, /abcdef012345…/); assert.ok(!configured.includes("Revoke"));
});
