import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { gapPageId, gapExploreHref, mergeGapAccounts } from "../src/components/gap-reading";
import type { Schema } from "../src/lib/client";

const fixtures = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8"));
const id: string = fixtures.gaps.items[0].object.id;
function account(): Schema<"AccountSummary"> {
  const row = structuredClone(fixtures.gapAccounts.items[0]) as Schema<"AccountSummary">;
  row.publication = { visibility: "public", version: 1, published_at: null, updated_at: null, can_manage: false, has_unpublished_changes: false };
  return row;
}

test("gap reading routes preserve case-sensitive DAPPER IDs and encode the explicit exploration link", () => {
  assert.equal(gapPageId(id), id);
  assert.equal(gapPageId(encodeURIComponent(id)), id);
  const link = gapExploreHref(id);
  assert.ok(link.startsWith("/?gap=dapper%3AKnowledgeGap."));
  assert.equal(new URL(link, "https://example.test").searchParams.get("gap"), id);
  for (const invalid of ["%", "../workspace", id + "/extra", id + "?draft=other", "dapper:ScientificAccount." + "a".repeat(32)]) {
    assert.equal(gapPageId(invalid), null);
  }
});

test("public account pagination deduplicates overlap and keeps the latest visible record", () => {
  const first = account(), second = account(); second.account.id = "dapper:ScientificAccount." + "b".repeat(32);
  const updated = structuredClone(first); updated.account.name = "Updated published name";
  const rows = mergeGapAccounts(id, "public", [first], [updated, second]);
  assert.deepEqual(rows.map(row => row.account.id), [first.account.id, second.account.id]);
  assert.equal(rows[0].account.name, "Updated published name");
  assert.notEqual(first.account.name, rows[0].account.name);
});

test("public reading rejects private and cross-gap results before exposing a page", () => {
  const privateRow = account(); privateRow.publication.visibility = "private";
  assert.throws(() => mergeGapAccounts(id, "public", [], [privateRow]), /published accounts could not be verified/);
  assert.equal(mergeGapAccounts(id, "workspace", [], [privateRow]).length, 1);
  for (const target of ["account", "knowledge_gap"] as const) {
    const wrong = account();
    if (target === "account") wrong.account.question = "dapper:KnowledgeGap." + "x".repeat(32);
    else wrong.knowledge_gap.id = "dapper:KnowledgeGap." + "x".repeat(32);
    assert.throws(() => mergeGapAccounts(id, "public", [account()], [wrong]), /matched to this knowledge gap/);
  }
});
