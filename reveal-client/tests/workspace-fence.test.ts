import { test } from "node:test";
import assert from "node:assert/strict";
import { workspaceFence, workspaceHeaders } from "../src/lib/api";
import { assessmentApi } from "../src/lib/cfde-assessment";
import { createLocalWorkClient } from "../src/lib/local-work";

// The gateway refuses a guest's write that does not name the workspace the page read (WORKSPACE_CHANGED), so the
// clients that send their own requests (local research, CFDE support checks) carry the same fence as request().
const workspace = (init?: RequestInit) => new Headers(init?.headers).get("X-Reveal-Workspace-ID");

test("local research writes name the expected workspace; reads do not", async () => {
  const seen: { method: string; workspace: string | null }[] = [];
  const client = createLocalWorkClient(async (_input, init) => {
    seen.push({ method: init?.method || "GET", workspace: workspace(init) });
    if (String(_input).endsWith("/setup-kit")) return new Response(new Uint8Array([80, 75]), { headers: { "Content-Type": "application/zip" } });
    return init?.method === "DELETE" ? new Response(null, { status: 204 }) : Response.json({});
  }, () => ({ "X-Reveal-Workspace-ID": "guest-1" }));
  await client.create({ draft_id: "draft", draft_version: 1 }, "key"); await client.revoke("work", "grant");
  await client.setupKit("work", "codex"); await client.list(); await client.get("work");
  assert.deepEqual(seen, [{ method: "POST", workspace: "guest-1" }, { method: "DELETE", workspace: "guest-1" }, { method: "POST", workspace: "guest-1" },
    { method: "GET", workspace: null }, { method: "GET", workspace: null }]);
});

test("the fence names the workspace the page read, reading the session once when it has not", async t => {
  const requests: { path: string; workspace: string | null }[] = [];
  t.mock.method(globalThis, "fetch", async (input: string, init?: RequestInit) => {
    requests.push({ path: String(input), workspace: workspace(init) });
    if (String(input) === "/api/session") return Response.json({ principal: { user_id: "guest-2" } });
    return Response.json({ id: "a", draft_id: "d", draft_version: 1, composer_sha256: "x", status: "preparing", result: null, coverage: null, error: null, stale: false }, { status: 202 });
  });
  assert.deepEqual(workspaceHeaders(), {});
  assert.deepEqual(await workspaceFence(), { "X-Reveal-Workspace-ID": "guest-2" });
  assert.deepEqual(await workspaceFence(), { "X-Reveal-Workspace-ID": "guest-2" });
  const signal = new AbortController().signal;
  await assessmentApi.start({ id: "d", version: 1 }, { source_gap: null, eaggl_anchors: [] }, "key", signal);
  await assessmentApi.get("d", "a", signal);
  assert.deepEqual(requests, [{ path: "/api/session", workspace: null },
    { path: "/api/backend/v1/drafts/d/cfde-assessments", workspace: "guest-2" }, { path: "/api/backend/v1/drafts/d/cfde-assessments/a", workspace: null }]);
});
