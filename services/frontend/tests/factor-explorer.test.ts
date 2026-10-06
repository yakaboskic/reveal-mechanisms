import test from "node:test";
import assert from "node:assert/strict";
import { overlayMembers, appendLoadings } from "../src/lib/factor-explorer";
import type { Schema } from "../src/lib/client";

test("overlay matches exact HGNC symbols, deduplicates members and never guesses another namespace", () => {
  const members = overlayMembers({ members: ["HGNC.SYMBOL:NRXN1", "NRXN1", "HGNC.SYMBOL:SHANK3", "HGNC:8008", "HGNC.SYMBOL:", 5] })!;
  assert.deepEqual([...members.symbols], ["NRXN1", "SHANK3"]);
  assert.equal(members.symbols.has("NRXN1_ALT"), false);
  assert.equal(members.symbols.has("nrxn1"), false);
  assert.equal(members.unsupported, 3);
});

test("missing membership stays unknown and is distinct from an explicitly empty gene set", () => {
  assert.equal(overlayMembers(null), null);
  assert.equal(overlayMembers({}), null);
  assert.equal(overlayMembers({ members: null }), null);
  assert.deepEqual(overlayMembers({ members: [] }), { symbols: new Set(), unsupported: 0 });
});

test("appending retains previous order, ranks and values without duplicate boundary tiles", () => {
  const row = (id: string, rank: number, loading: number | null): Schema<"FactorLoading"> => ({ id, label: id, rank, loading });
  const first = [row("ALPHA", 30, 0.4), row("BETA", 1, 1)];
  const rows = appendLoadings(first, [row("BETA", 1, 1), row("GAMMA", 9, null), row("GAMMA", 9, null)]);
  assert.equal(rows[0], first[0]); assert.equal(rows[1], first[1]);
  assert.deepEqual(rows.map(value => [value.id, value.rank, value.loading]), [["ALPHA", 30, 0.4], ["BETA", 1, 1], ["GAMMA", 9, null]]);
});
