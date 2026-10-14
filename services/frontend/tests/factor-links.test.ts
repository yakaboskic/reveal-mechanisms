import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { factorHref, geneSetHref, referenceReturnPath, returnLabel, traitHref, traitPageHref } from "../src/lib/factor-links";

test("trait links use the KPN catalog and preserve leading zeroes", () => {
  assert.equal(traitHref("KPN.TRAIT:0001360"), "https://broadinstitute.github.io/kpn-data-models/kpn.trait/0001360/");
  for (const id of ["KPN.TRAIT:1360", "KPN.TRAIT:0001360/other", "trait:0001360"]) assert.equal(traitHref(id), null);
});

test("factor links preserve the exact source revision and archived snapshot", () => {
  const id = "factor:kpn:0012345:eaggl-capped-v1:Factor2";
  const url = new URL(factorHref(id, "abc", { archiveId: "frozen", from: "/runs/run-1" }), "http://localhost");
  assert.equal(decodeURIComponent(url.pathname.split("/").at(-1)!), id);
  assert.equal(url.searchParams.get("source_revision"), "abc");
  assert.equal(url.searchParams.get("archive"), "frozen");
  assert.equal(url.searchParams.get("from"), "/runs/run-1");
});

test("gene-set links pin their reference generation and preserve factor context", () => {
  const factor = factorHref("factor:kpn:0012345:eaggl-capped-v1:Factor2", "abc");
  const url = new URL(geneSetHref("dapper:GeneSet.a_b-c", "generation", factor), "http://localhost");
  assert.equal(url.searchParams.get("generation_id"), "generation");
  assert.equal(url.searchParams.get("from"), factor);
});

test("return links cannot leave the app or lead to API routes", () => {
  for (const value of ["//example.com", "https://example.com", "/\\example.com", "/api/session/logout", "/factors/../api", "/\n/evil"]) assert.equal(referenceReturnPath(value), null);
  assert.equal(referenceReturnPath("/?job=123"), "/?job=123");
});

test("an opened gap without a draft yet returns to its question", () => {
  const url = new URL(factorHref("factor:fixture", "abc", { from: "/?gap=dismech%3Agap" }), "http://localhost");
  const back = referenceReturnPath(url.searchParams.get("from"));
  assert.equal(back, "/?gap=dismech%3Agap");
  assert.equal(returnLabel(back), "Back to knowledge gap");
  assert.equal(returnLabel("/drafts/draft-1"), "Back to draft"); assert.equal(returnLabel("/"), "Explore knowledge gaps");
});

test("trait pages are in-app, carry their return path and accept only KPN trait ids", () => {
  const factor = factorHref("factor:kpn:0000398:eaggl-capped-v1:Factor1", "abc");
  const url = new URL(traitPageHref("KPN.TRAIT:0000398", factor)!, "http://localhost");
  assert.equal(decodeURIComponent(url.pathname), "/traits/KPN.TRAIT:0000398");
  assert.equal(url.searchParams.get("from"), factor);
  assert.equal(traitPageHref("KPN.TRAIT:0000398", "https://example.com"), "/traits/KPN.TRAIT%3A0000398");
  for (const id of ["KPN.TRAIT:398", "T2D", "KPN.TRAIT:0000398/x"]) assert.equal(traitPageHref(id), null);
  const back = referenceReturnPath(url.pathname + url.search);
  assert.equal(returnLabel(back), "Back to trait");
  assert.equal(returnLabel("/gene-sets/dapper%3AGeneSet.x"), "Back to gene set");
  assert.equal(returnLabel("/accounts/dapper%3AScientificAccount.x?view=lineage"), "Back to scientific account");
});

test("the id resolver opens only exact KPN trait ids as trait pages", () => {
  const resolver = readFileSync(new URL("../src/app/id/[id]/page.tsx", import.meta.url), "utf8");
  assert.match(resolver, /const trait = traitPageHref\(exact\);\s*if \(trait\) redirect\(trait\);/);
  assert.doesNotMatch(resolver, /startsWith\("KPN\.TRAIT:"\)/);
  assert.equal(traitPageHref("KPN.TRAIT:0000398"), "/traits/KPN.TRAIT%3A0000398");
  for (const id of ["KPN.TRAIT:", "KPN.TRAIT:12345678", "KPN.TRAIT:0000398 "]) assert.equal(traitPageHref(id), null);
});
