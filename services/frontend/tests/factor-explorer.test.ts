import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { overlayMembers, appendLoadings, firstGenePage, prefetchedLoadings } from "../src/lib/factor-explorer";
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

const genePage = (overrides: Partial<Schema<"FactorLoadings">> = {}): Schema<"FactorLoadings"> => ({
  source_id: "factor:fixture", generation_id: "generation-a", kind: "gene", metric: "joint", sort: "alphabetical", items: [], total: 0, offset: 0, limit: 200,
  next_offset: null, summary: { total: 0, min: null, max: null, coverage: "" } as unknown as Schema<"LoadingSummary">, gnomad: { import_id: "a".repeat(64) } as Schema<"GnomadImport">, ...overrides,
});
const firstRequest = { source_id: "factor:fixture", generation_id: "generation-a", gnomad_import_id: "a".repeat(64), ...firstGenePage };

test("the gene page read alongside the detail is used only when it answers the panel's first pinned request", () => {
  const page = genePage();
  assert.equal(prefetchedLoadings(page, firstRequest), page);
  assert.equal(prefetchedLoadings(genePage({ gnomad: null }), { ...firstRequest, gnomad_import_id: "none" })?.gnomad, null, "No gnomAD import on either side still matches");
  for (const [value, request, reason] of [
    [null, firstRequest, "the early read failed"],
    [genePage({ generation_id: "generation-b" }), firstRequest, "a cutover landed between the two reads"],
    [genePage({ gnomad: { import_id: "b".repeat(64) } as Schema<"GnomadImport"> }), firstRequest, "the gnomAD pin moved"],
    [genePage({ gnomad: null }), firstRequest, "the gnomAD import disappeared"],
    [genePage({ source_id: "factor:other" }), firstRequest, "another factor"],
    [genePage({ sort: "loading" }), firstRequest, "another order"],
    [page, { ...firstRequest, sort: "loading" }, "the panel no longer asks for the first page"],
    [page, { ...firstRequest, q: "BRCA" }, "a search"],
    [page, { ...firstRequest, offset: 200 }, "a later page"],
    [page, { ...firstRequest, kind: "gene_set", limit: 50 }, "the gene-set panel"],
  ] as const) assert.equal(prefetchedLoadings(value, request), null, reason);
});

test("a factor page starts its first gene page before awaiting the detail and hands it to the gene panel only", () => {
  const view = readFileSync(new URL("../src/components/FactorView.tsx", import.meta.url), "utf8");
  const explorer = readFileSync(new URL("../src/components/FactorExplorer.tsx", import.meta.url), "utf8");
  assert.ok(view.indexOf("factorApi.loadings({ source_id: sourceId, source_revision: revision, ...firstGenePage }") < view.indexOf("await factorApi.detail("));
  assert.match(explorer, /kind="gene" self=\{self\} overlay=\{overlay\} initial=\{genes\}/);
  assert.doesNotMatch(explorer, /kind="gene_set"[^>]*initial=/);
});
