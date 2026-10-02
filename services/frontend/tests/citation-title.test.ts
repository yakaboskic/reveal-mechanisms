import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { citationTitle } from "../src/lib/citation-title";
import { CitationReference } from "../src/components/CitationReference";

test("short titles and an exact word boundary retain the complete word", () => {
  assert.deepEqual(citationTitle("A concise claim"), { text: "A concise claim", truncated: false });
  assert.deepEqual(citationTitle("One complete word", 12), { text: "One complete", truncated: true });
  assert.deepEqual(citationTitle("One complete word", 17), { text: "One complete word", truncated: false });
});

test("long claim titles stop before the partial word within the default 60-character budget", () => {
  const prefix = "The observed association supports the selected Factor3";
  const full = `${prefix} factor_value observation and a much longer qualification.`;
  assert.deepEqual(citationTitle(full), { text: prefix, truncated: true });
});

test("long individual tokens preserve Unicode grapheme boundaries", () => {
  for (const unit of ["🧬", "e\u0301", "👩‍🔬"]) {
    assert.deepEqual(citationTitle(unit.repeat(61)), { text: unit.repeat(60), truncated: true });
  }
  assert.deepEqual(citationTitle("αβγ δεζη θικλ", 7), { text: "αβγ", truncated: true });
});

test("rendered reference keeps ellipsis outside its link and the full linked identity and title", () => {
  const title = "The observed association supports the selected Factor3 factor_value observation and qualification.";
  const id = "dapper:Claim.ExactScientificIdentity";
  const href = `/claims/${encodeURIComponent(id)}?account=${encodeURIComponent("dapper:ScientificAccount.Context")}`;
  const rendered = renderToStaticMarkup(createElement(CitationReference, { title, href, targetId: id }));
  assert.match(rendered, /href="\/claims\/dapper%3AClaim.ExactScientificIdentity\?account=dapper%3AScientificAccount.Context"/);
  assert.ok(rendered.includes(`title="${title}"`));
  assert.ok(rendered.includes(`aria-label="Inspect cited record: ${title}"`));
  assert.ok(rendered.includes(`>The observed association supports the selected Factor3</a>… <span class="reference-id">[${id}]</span>`));
});

test("short titles get no ellipsis and untrusted citation text stays escaped", () => {
  const rendered = renderToStaticMarkup(createElement(CitationReference,
    { title: "<em>Claim & scope</em>", href: "/id/exact", targetId: "dapper:Claim.<test>" }));
  assert.ok(rendered.includes("&lt;em&gt;Claim &amp; scope&lt;/em&gt;</a> <span"));
  assert.ok(rendered.includes("[dapper:Claim.&lt;test&gt;]"));
  assert.ok(!rendered.includes("…"));
  assert.ok(!rendered.includes("<em>"));
});
