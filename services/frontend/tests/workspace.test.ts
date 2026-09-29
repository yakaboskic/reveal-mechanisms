import assert from "node:assert/strict";
import test from "node:test";
import { allWorkspacePages, workspaceRuns } from "../src/lib/workspace";
import type { Schema } from "../src/lib/client";

test("workspace resolves active drafts and prefers running work over a newer terminal run", () => {
  const requests = ["active", "finished", "unsubmitted"].map(id => ({ id, source_draft_id: `draft-${id}`, composer: { source_gap: { id: "gap" } } })) as Schema<"ResearchRequest">[];
  for (const status of ["queued", "running", "cancel_requested"] as const) {
    const jobs = [
      { id: "done", kind: "analysis", research_request_id: "finished", status: "insufficient_evidence", created_at: "2026-09-29" },
      { id: "working", kind: "analysis", research_request_id: "active", status, created_at: "2026-09-28" },
      { id: "paragraph", kind: "paragraph", research_request_id: "unsubmitted", status: "running", created_at: "2026-09-30" },
    ] as Schema<"Job">[];
    const activity = workspaceRuns(jobs, requests);
    assert.deepEqual([...activity.activeDrafts], ["draft-active"]);
    assert.equal(activity.byGap.get("gap")?.[0].id, "working");
    assert.equal(activity.href(jobs[1]), "/?job=working&draft=draft-active");
    jobs[1].status = "failed";
    assert.equal(workspaceRuns(jobs, requests).activeDrafts.size, 0);
  }
});

test("workspace follows activity pages and rejects looping cursors", async () => {
  const cursors: (string | undefined)[] = [];
  const items = await allWorkspacePages(async cursor => {
    cursors.push(cursor);
    return { items: [cursor ? "older-running-job" : "recent-finished-job"], page: { has_more: !cursor, next_cursor: cursor ? null : "page-2", snapshot_id: "snapshot" } };
  });
  assert.deepEqual(items, ["recent-finished-job", "older-running-job"]);
  assert.deepEqual(cursors, [undefined, "page-2"]);
  await assert.rejects(allWorkspacePages(async () => ({ items: [], page: { has_more: true, next_cursor: "loop", snapshot_id: "snapshot" } })), /Workspace history changed/);
});

test("draft selection keeps each run separate and deleted drafts link to preserved research", () => {
  const requests = ["a", "b"].map(id => ({ id: `request-${id}`, source_draft_id: id, composer: { source_gap: { id: "gap" } } })) as Schema<"ResearchRequest">[];
  const jobs = [
    { id: "run-a", kind: "analysis", research_request_id: "request-a", status: "running", created_at: "2026-09-29" },
    { id: "run-b", kind: "analysis", research_request_id: "request-b", status: "succeeded", created_at: "2026-09-29" },
  ] as Schema<"Job">[];
  const activity = workspaceRuns(jobs, requests, [{ id: "a" }] as Schema<"Draft">[]);
  assert.equal(activity.byDraft.get("b")?.[0].id, "run-b");
  assert.equal(activity.activeDrafts.has("b"), false);
  assert.equal(activity.href(jobs[0]), "/?job=run-a&draft=a");
  assert.equal(activity.href(jobs[1]), "/?job=run-b&gap=gap");
});
