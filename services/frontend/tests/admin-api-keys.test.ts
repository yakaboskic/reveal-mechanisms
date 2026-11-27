import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { isKeysView, isMinted, mintForm, readMintRequest, type ApiKeySummary, type ApiKeysView } from "../src/lib/admin-api-keys";
import { ConfiguredKeys, KeysTable, MintedKey } from "../src/components/AdminApiKeys";
import { ADMIN_NAV } from "../src/components/admin/AdminNav";

const RAW = "rvl_" + "A".repeat(43);
const key: ApiKeySummary = {
  key_id: "6f1c2c1e-6a43-4f53-9b0f-1f2a3b4c5d6e", label: "reveal-claims demo", prefix: RAW.slice(0, 8),
  user_id: "13b86465-2324-43c7-9f13-de73e1d27256", agent_jobs: true,
  workspace: { principal_kind: "registered", display_name: "reveal-claims demo", workspace_expires_at: null, retired: false },
  created_at: "2026-11-27T10:00:00Z", created_by: "admin@example.org", revoked_at: null, revoked_by: null,
};
const view: ApiKeysView = { format: "reveal.api-keys/1", keys: [key], environment: { keys: [{ fingerprint: "abcdef012345", user_id: key.user_id, agent_jobs: false }], problem: null } };

test("the console lists API keys in its navigation", () => {
  assert.ok(ADMIN_NAV.some(item => item.href === "/admin/api-keys"));
});

test("the mint form checks the label and an existing workspace id", () => {
  assert.deepEqual(mintForm({ label: "demo", workspace: "new", userId: "", agentJobs: true }), { request: { label: "demo", agent_jobs: true } });
  assert.deepEqual(mintForm({ label: "demo", workspace: "existing", userId: ` ${key.user_id.toUpperCase()} `, agentJobs: false }),
    { request: { label: "demo", agent_jobs: false, user_id: key.user_id } });
  for (const label of ["", " demo", "-demo", "x".repeat(65)]) assert.ok("errors" in mintForm({ label, workspace: "new", userId: "", agentJobs: false }));
  const bad = mintForm({ label: "demo", workspace: "existing", userId: "nope", agentJobs: false });
  assert.ok("errors" in bad && bad.errors.userId);
});

test("the proxy forwards only a small plain object with the known fields", async () => {
  const post = (body: string) => new Request("http://localhost/api/admin/api-keys", { method: "POST", body });
  assert.deepEqual(await readMintRequest(post('{"label":"demo","agent_jobs":true}')), { ok: true, request: { label: "demo", agent_jobs: true } });
  for (const body of ["[]", "null", "not json", '{"label":"demo","owner":"x"}']) assert.equal((await readMintRequest(post(body))).ok, false);
  const large = await readMintRequest(post(JSON.stringify({ label: "x".repeat(5000) })));
  assert.equal(large.ok, false); assert.equal(!large.ok && large.status, 413);
});

test("views and minted responses are recognized only in their own shape", () => {
  assert.ok(isKeysView(view));
  assert.ok(!isKeysView({ ...view, format: "other" }));
  assert.ok(isMinted({ key, api_key: RAW }));
  assert.ok(!isMinted({ key, api_key: "rvl_short" }));
});

test("the table shows a key's prefix, never more, and revoked keys have no revoke button", () => {
  const html = renderToStaticMarkup(React.createElement(KeysTable, { keys: [key, { ...key, key_id: "x", revoked_at: "2026-11-27T11:00:00Z", revoked_by: "admin@example.org" }], busy: false, onRevoke: () => {} }));
  assert.match(html, /rvl_AAAA…/);
  assert.ok(!html.includes(RAW));
  assert.equal((html.match(/Revoke…/g) || []).length, 1);
  assert.match(html, /revoked/);
});

test("the minted key is shown once in full, and configured keys are read-only", () => {
  const minted = renderToStaticMarkup(React.createElement(MintedKey, { minted: { key, api_key: RAW }, onDone: () => {} }));
  assert.ok(minted.includes(RAW)); assert.match(minted, /shown only this once/);
  const configured = renderToStaticMarkup(React.createElement(ConfiguredKeys, { view }));
  assert.match(configured, /abcdef012345…/); assert.ok(!configured.includes("Revoke"));
});
