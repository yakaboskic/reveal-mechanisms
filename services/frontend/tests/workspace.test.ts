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

const draftFixture = (version = 2) => ({ id: "draft", version, composer: { source_gap: { id: "gap" } } }) as Schema<"Draft">;
const requestFixture = (version = 2, id = "request") => ({ id, source_draft_id: "draft", source_draft_version: version, composer: { source_gap: { id: "gap" } } }) as Schema<"ResearchRequest">;
const jobFixture = (status: Schema<"Job">["status"], id = "run", request = "request") => ({ id, kind: "analysis", research_request_id: request, status, created_at: "2026-09-29" }) as Schema<"Job">;

for (const status of ["succeeded", "insufficient_evidence", "failed", "cancelled"] as const) {
  test(`workspace hides the finished ${status} revision without deleting its draft or history`, () => {
    const draft = draftFixture(), request = requestFixture(), job = jobFixture(status);
    const drafts = [draft], jobs = [job], requests = [request];
    const activity = workspaceRuns(jobs, requests, drafts);
    assert.deepEqual(drafts.filter(activity.isDraftVisible), []);
    assert.deepEqual(drafts, [draft]);
    assert.deepEqual(jobs, [job]);
    assert.deepEqual(requests, [request]);
    assert.equal(activity.byDraft.get(draft.id)?.[0], job);
    assert.equal(activity.byGap.get("gap")?.[0], job);
    assert.equal(activity.href(job), "/?job=run&draft=draft");
  });
}

test("workspace keeps a newly edited revision visible after every terminal outcome", () => {
  const draft = draftFixture(3);
  for (const status of ["succeeded", "insufficient_evidence", "failed", "cancelled"] as const) {
    const activity = workspaceRuns([jobFixture(status)], [requestFixture(2)], [draft]);
    assert.equal(activity.isDraftVisible(draft), true, status);
  }
});

test("workspace keeps queued, running and cancelling retries visible despite an older finished run", () => {
  const draft = draftFixture();
  for (const status of ["queued", "running", "cancel_requested"] as const) {
    const retry = jobFixture(status, "retry", "retry-request");
    for (const retryVersion of [1, 2]) {
      const activity = workspaceRuns([jobFixture("succeeded"), retry], [requestFixture(), requestFixture(retryVersion, "retry-request")], [draft]);
      assert.equal(activity.isDraftVisible(draft), true, `${status} version ${retryVersion}`);
      assert.equal(activity.byDraft.get(draft.id)?.[0], retry);
    }
  }
});

test("workspace keeps unsubmitted drafts and does not infer completion from unrelated or missing requests", () => {
  const draft = draftFixture();
  assert.equal(workspaceRuns([], [], [draft]).isDraftVisible(draft), true);
  const paragraph = { ...jobFixture("succeeded"), kind: "paragraph" } as Schema<"Job">;
  assert.equal(workspaceRuns([paragraph], [requestFixture()], [draft]).isDraftVisible(draft), true);
  assert.equal(workspaceRuns([jobFixture("succeeded")], [], [draft]).isDraftVisible(draft), true);
  const other = { ...requestFixture(), source_draft_id: "other-draft" };
  assert.equal(workspaceRuns([jobFixture("succeeded")], [other], [draft]).isDraftVisible(draft), true);
});

test("a finished retry hides only its submitted revision, not another draft for the same gap", () => {
  const draft = draftFixture(), other = { ...draftFixture(), id: "another-draft" };
  const jobs = [jobFixture("failed"), jobFixture("succeeded", "retry", "retry-request")];
  const activity = workspaceRuns(jobs, [requestFixture(), requestFixture(2, "retry-request")], [draft, other]);
  assert.deepEqual([draft, other].filter(activity.isDraftVisible), [other]);
  assert.equal(activity.byDraft.get(draft.id)?.length, 2);
});
