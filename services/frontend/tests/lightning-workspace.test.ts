import assert from "node:assert/strict";
import test from "node:test";

process.env.NEXT_PUBLIC_REVEAL_LIGHTNING_ENABLED = "true";
const dependencies = Promise.all([import("../src/lib/workspace-data"), import("../src/lib/lightning-audit"), import("../src/lib/client"), import("../src/lib/local-work")]);

const page = { next_cursor: null, has_more: false, snapshot_id: "test" };
test("mixed run history includes paginated audits, and activity refresh preserves frozen requests", async t => {
  const [{ loadWorkspaceData, workspaceSize }, { lightningApi }, { api }, { localWorkApi }] = await dependencies;
  t.mock.method(api, "jobs", async () => ({ items: [{ id: "online", kind: "analysis", research_request_id: "request" }], page }));
  let requests = 0;
  t.mock.method(api, "requests", async () => { requests++; return { items: [{ id: "request" }], page }; });
  t.mock.method(api, "drafts", () => assert.fail("Audit history must not depend on mutable drafts"));
  t.mock.method(localWorkApi, "list", async () => ({ items: [{ id: "local" }], page }));
  let status = "preparing";
  const cursors: (string | undefined)[] = [], signal = new AbortController().signal;
  t.mock.method(lightningApi, "list", async (cursor?: string, caller?: AbortSignal) => {
    assert.equal(caller, signal); cursors.push(cursor);
    return { items: (cursor ? ["a", "b"] : ["a"]).map(id => ({ id, status })), page: { next_cursor: cursor ? null : "next", has_more: !cursor } };
  });
  let data = await loadWorkspaceData("runs", undefined, "refresh", signal);
  assert.deepEqual(data.audits?.map(audit => audit.id), ["a", "b"]); assert.equal(workspaceSize("runs", data), 4);
  assert.deepEqual(cursors, [undefined, "next"]); assert.equal(requests, 1);
  status = "succeeded"; data = await loadWorkspaceData("runs", data, "activity", signal);
  assert.equal(data.audits?.[0].status, "succeeded"); assert.equal(requests, 1);
});

test("audit pagination loops fail explicitly and do not present incomplete history", async t => {
  const [{ loadWorkspaceData }, { lightningApi }, { api }, { localWorkApi }] = await dependencies;
  t.mock.method(api, "jobs", async () => ({ items: [], page }));
  t.mock.method(api, "requests", async () => ({ items: [], page }));
  t.mock.method(localWorkApi, "list", async () => ({ items: [], page }));
  t.mock.method(lightningApi, "list", async () => ({ items: [], page: { next_cursor: "repeated", has_more: true } }));
  await assert.rejects(loadWorkspaceData("runs", undefined, "refresh", new AbortController().signal), /Audit history changed/);
});
