import assert from "node:assert/strict";
import test from "node:test";
import { composerSelection, hasSelection, matchesSavedSelection, questionSelection, selectionKey, selectionUrl } from "../src/lib/composer-navigation";

const selected = (search: string) => composerSelection(new URLSearchParams(search));
const saved = { draft: { id: "draft-a" }, gap: { object: { id: "dapper:KnowledgeGap.gap-a" } }, job: null };

test("bare home never restores an unsubmitted draft, a selected gap, or a submitted run", () => {
  for (const snapshot of [saved, { ...saved, draft: null }, { ...saved, job: { id: "job-a" } }]) {
    assert.equal(matchesSavedSelection(selected(""), snapshot), false);
    assert.equal(matchesSavedSelection(selected("draft=&gap=&job="), snapshot), false);
  }
});

test("explicit draft/gap/job links restore only their matching browser snapshot", () => {
  assert.equal(matchesSavedSelection(selected("draft=draft-a"), saved), true);
  assert.equal(matchesSavedSelection(selected("gap=dapper:KnowledgeGap.gap-a"), saved), true);
  assert.equal(matchesSavedSelection(selected("draft=draft-b"), saved), false);
  assert.equal(matchesSavedSelection(selected("gap=other"), saved), false);
  const submitted = { ...saved, job: { id: "job-a" } };
  assert.equal(matchesSavedSelection(selected("job=job-a&draft=draft-a"), submitted), true);
  assert.equal(matchesSavedSelection(selected("job=job-b&draft=draft-a"), submitted), false);
  assert.equal(matchesSavedSelection(selected("job=job-a&draft=draft-b"), submitted), false);
  assert.equal(matchesSavedSelection(selected("job=job-a"), saved), false);
  assert.equal(matchesSavedSelection(selected("draft=draft-a"), null), false);
});

test("selection identity follows job, draft and gap changes but ignores OAuth error metadata", () => {
  const links = ["", "draft=a", "draft=b", "gap=a", "gap=b", "job=a", "job=b", "job=a&draft=a"];
  assert.equal(new Set(links.map(link => selectionKey(selected(link)))).size, links.length);
  assert.equal(selectionKey(selected("draft=a&error=AccessDenied")), selectionKey(selected("draft=a")));
  assert.equal(hasSelection(selected("error=AccessDenied")), false);
  assert.equal(hasSelection(selected("job=a")), true);
});

test("returning from uncertain submission makes an explicit draft recovery link without changing its key", () => {
  const snapshot = { ...saved, submitKey: { binding: "draft-a:3", key: "original-paid-request-key" } };
  const url = selectionUrl(new URL("https://reveal.example/?job=old&gap=old&error=AccessDenied"), questionSelection(snapshot.draft.id, snapshot.gap.object.id));
  assert.equal(url.href, "https://reveal.example/?draft=draft-a");
  assert.equal(matchesSavedSelection(composerSelection(url.searchParams), snapshot), true);
  assert.deepEqual(snapshot.submitKey, { binding: "draft-a:3", key: "original-paid-request-key" });
});

test("a question without a saved draft gets an exact encoded gap link; clearing gets bare home", () => {
  const url = new URL("https://reveal.example/?draft=stale&job=stale&error=bad");
  const target = questionSelection(null, "dapper:KnowledgeGap.a/b?c&d");
  assert.deepEqual(composerSelection(selectionUrl(url, target).searchParams), target);
  assert.equal(selectionUrl(url, questionSelection()).href, "https://reveal.example/");
});
