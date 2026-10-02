import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { discoveryLabel, mergePublicAccounts } from "../src/lib/community-discovery";
import type { Schema } from "../src/lib/client";

function account(): Schema<"AccountSummary"> {
  const fixtures = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8"));
  const row = structuredClone(fixtures.gapAccounts.items[0]) as Schema<"AccountSummary">;
  row.publication = { visibility: "public", version: 1, published_at: null, updated_at: null, can_manage: false, has_unpublished_changes: false };
  row.job_id = null; row.research_statement.job_id = null;
  return row;
}

test("community discovery offers knowledge gaps and scientific accounts", () => {
  assert.equal(discoveryLabel("gaps"), "Trending knowledge gaps");
  assert.equal(discoveryLabel("accounts"), "Trending scientific accounts");
});

test("public account pages preserve server ranking and canonical identity across overlap", () => {
  const first = account(), second = account(); second.account.id += "-second";
  const updated = structuredClone(first); updated.account.name = "Current published summary";
  const merged = mergePublicAccounts([first], [updated, second]);
  assert.deepEqual(merged.map(row => row.account.id), [first.account.id, second.account.id]);
  assert.equal(merged[0].account.name, updated.account.name);
  assert.notEqual(first.account.name, updated.account.name);
});

test("public discovery refuses private records, job metadata and inconsistent gap identities", () => {
  for (const change of [
    (row: Schema<"AccountSummary">) => { row.publication.visibility = "private"; },
    (row: Schema<"AccountSummary">) => { row.publication.can_manage = true; },
    (row: Schema<"AccountSummary">) => { row.publication.has_unpublished_changes = true; },
    (row: Schema<"AccountSummary">) => { row.job_id = "private-job"; },
    (row: Schema<"AccountSummary">) => { row.research_statement.job_id = "private-paragraph-job"; },
    (row: Schema<"AccountSummary">) => { row.account.question = "another-gap"; },
  ]) {
    const invalid = account(); change(invalid);
    assert.throws(() => mergePublicAccounts([account()], [invalid]), /could not be verified/);
  }
});
