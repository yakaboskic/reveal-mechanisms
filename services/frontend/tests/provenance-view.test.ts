import test from "node:test";
import assert from "node:assert/strict";
import { publicSourceHref } from "../src/lib/provenance-view";

test("untrusted source locators only become links for public HTTP schemes", () => {
  assert.equal(publicSourceHref("https://example.org/source?q=1"), "https://example.org/source?q=1");
  for (const value of ["javascript:alert(1)", "data:text/html,hello", "file:///humgen/data", "s3://bucket/file", "https://user:password@example.org", {}, null]) assert.equal(publicSourceHref(value), null);
});
