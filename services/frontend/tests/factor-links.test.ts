import test from "node:test";
import assert from "node:assert/strict";
import { factorHref, geneSetHref, referenceReturnPath, returnLabel, traitHref } from "../src/lib/factor-links";

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
