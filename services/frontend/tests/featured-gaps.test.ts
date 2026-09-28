import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loadFeaturedGaps } from "../src/lib/featured-gaps";
import type { Schema } from "../src/lib/client";

const base = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8")).gaps.items[0] as Schema<"GapRecord">;
test("trending preserves server account-count order and exact source records without editorial selection", () => {
  const ranked = [9, 5, 2, 0].map((count, i) => ({ ...base, object: { ...base.object, id: `${base.object.id}_${i}` }, scientific_accounts: { ...base.scientific_accounts, count } }));
  assert.deepEqual(loadFeaturedGaps([ranked[0], ranked[0], ...ranked.slice(1)]), ranked.slice(0, 3));
  assert.equal(loadFeaturedGaps(ranked)[0], ranked[0]);
  assert.deepEqual(loadFeaturedGaps([]), []);
});
