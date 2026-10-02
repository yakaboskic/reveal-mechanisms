import assert from "node:assert/strict";
import test from "node:test";
import { composerSelection, followsSelection, hasSelection, matchesSavedSelection, questionSelection, selectionKey, selectionUrl } from "../src/lib/composer-navigation";

const selected = (search: string, path = "/") => composerSelection(new URLSearchParams(search), path);

test("the home page has no implicit editor or run selection", () => {
  assert.equal(hasSelection(selected("")), false);
  assert.equal(hasSelection(selected("draft=&gap=&job=")), false);
  assert.equal(hasSelection(selected("error=AccessDenied")), false);
});

test("canonical routes and legacy links resolve to the same independent identities", () => {
  assert.deepEqual(selected("", "/drafts/draft-a"), selected("draft=draft-a"));
  assert.deepEqual(selected("", "/runs/job-a"), selected("job=job-a"));
  assert.deepEqual(selected("job=job-a&draft=deleted&gap=changed"), { job: "job-a", draft: null, gap: null });
  assert.deepEqual(selected("draft=deleted&gap=changed", "/runs/job-a"), { job: "job-a", draft: null, gap: null });
});

test("selection identity ignores irrelevant mutable draft and OAuth metadata on run links", () => {
  assert.equal(selectionKey(selected("job=a&draft=old")), selectionKey(selected("job=a&draft=new")));
  assert.notEqual(selectionKey(selected("draft=a")), selectionKey(selected("draft=b")));
  assert.notEqual(selectionKey(selected("job=a")), selectionKey(selected("job=b")));
  assert.equal(selectionKey(selected("draft=a&error=AccessDenied")), selectionKey(selected("draft=a")));
});

test("navigation writes canonical draft and run URLs without stale identities", () => {
  const url = new URL("https://reveal.example/?job=old&gap=old&error=AccessDenied");
  const target = questionSelection("draft-a", "dapper:KnowledgeGap.gap-a");
  const draftUrl = selectionUrl(url, target);
  assert.equal(draftUrl.href, "https://reveal.example/drafts/draft-a");
  assert.deepEqual(composerSelection(draftUrl.searchParams, draftUrl.pathname), target);
  const runUrl = selectionUrl(draftUrl, { job: "job-a", draft: null, gap: null });
  assert.equal(runUrl.href, "https://reveal.example/runs/job-a");
  assert.equal(selectionUrl(runUrl, questionSelection()).href, "https://reveal.example/");
});

test("a question without an editor gets an exact encoded gap link", () => {
  const url = new URL("https://reveal.example/drafts/stale?job=stale&error=bad");
  const target = questionSelection(null, "dapper:KnowledgeGap.a/b?c&d");
  const result = selectionUrl(url, target);
  assert.deepEqual(composerSelection(result.searchParams, result.pathname), target);
});

test("work begun under a draft dropped at a cutover follows only its adopted replacement", () => {
  const dropped = selectionKey(selected("draft=draft-a")), replacement = selectionKey(selected("draft=draft-b"));
  const adopted = { from: dropped, to: replacement };
  assert.equal(followsSelection(dropped, dropped, null), true);
  assert.equal(followsSelection(dropped, replacement, null), false);
  assert.equal(followsSelection(dropped, replacement, adopted), true);
  assert.equal(followsSelection(replacement, replacement, adopted), true);
  // Any other navigation still fences it.
  assert.equal(followsSelection(dropped, selectionKey(selected("")), adopted), false);
  assert.equal(followsSelection(dropped, selectionKey(selected("draft=draft-c")), adopted), false);
  assert.equal(followsSelection(selectionKey(selected("gap=a")), replacement, adopted), false);
});
