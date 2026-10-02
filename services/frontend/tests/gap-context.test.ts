import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { gapContext, evidenceUrl } from "../src/components/gap-context";
import { GapContext } from "../src/components/GapContext";
import type { Schema } from "../src/lib/client";

const fixture = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8")).gaps.items[0] as Schema<"GapRecord">;

test("DisMech context keeps experiments, source evidence and dates separate from the scientific question", () => {
  const gap = structuredClone(fixture);
  gap.source_detail.raw = { kind: "HUMAN_MODEL_MISMATCH", rationale: "The human and model observations differ.", posed_date: "2026-07-14T00:00:00Z", notes: "Curation note",
    proposed_experiments: [{ name: "Matched perturbation", description: "Compare the responses.", experiment_type: { preferred_term: "Intervention" },
      readouts: [{ name: "EEG", target: "phenotypes#Sleep", direction: "INCREASED", interpretation: "A discriminating response." }],
      model_systems: [{ name: "Organoid", organism: { term: { id: "NCBITaxon:9606", label: "Homo sapiens" } } }],
      decision_criterion: "Concordant change", supporting_outcome: ["Responses match."],
      evidence: [{ reference: "PMID:39014349", snippet: "Experimental support." }] }],
    evidence: [{ reference: "PMID:40163633", reference_title: "Source study", supports: "SUPPORT", evidence_source: "HUMAN_CLINICAL", snippet: "Source quotation.", explanation: "Why this matters." }] };
  const before = structuredClone(gap), value = gapContext(gap);
  assert.deepEqual(gap, before);
  assert.equal(value.context, "The human and model observations differ.");
  assert.ok(value.metadata.some(item => item.label === "Posed on" && item.value === "2026-07-14T00:00:00Z"));
  assert.ok(value.metadata.some(item => item.label === "Kind" && item.value === "Human model mismatch"));
  assert.equal(value.experiments[0].groups.find(group => group.label === "Readouts")?.items[0].metadata.find(item => item.label === "Interpretation")?.value, "A discriminating response.");
  assert.equal(value.experiments[0].groups.find(group => group.label === "Model systems")?.items[0].metadata[0].value, "Homo sapiens");
  assert.equal(value.experiments[0].evidence[0].reference, "PMID:39014349");
  assert.equal(value.evidence[0].reference, "PMID:40163633");
});

test("missing or malformed optional sidecar fields produce useful fallback text without JSON coercion", () => {
  const gap = structuredClone(fixture);
  gap.source_detail.raw = { rationale: {}, evidence: [null, "invalid", { reference: {}, snippet: [] }], proposed_experiments: [null, {}, { name: "Valid", readouts: [null, false] }], posed_date: {} };
  const context = gapContext(gap);
  assert.equal(context.context, gap.object.gap_description);
  assert.deepEqual(context.evidence, []);
  assert.equal(context.experiments.length, 1);
  assert.deepEqual(context.experiments[0].groups, []);
  assert.equal(context.metadata.some(item => item.label === "Posed on"), false);
  assert.equal(JSON.stringify(context).includes("[object Object]"), false);
});

test("source links follow DisMech page slugs and reject unsafe evidence protocols or path traversal", () => {
  const gap = structuredClone(fixture);
  gap.source.disease_label = "Example (Disease)/Syndrome";
  gap.source_detail.source_file = "kb/disorders/Example.yaml";
  gap.source_detail.raw = { discussion_id: "question:one" };
  assert.equal(gapContext(gap).sourcePage, "https://dismech.monarchinitiative.org/pages/disorders/Example_Disease_Syndrome.html#question%3Aone");
  assert.equal(gapContext(gap).sourceUrl, "https://github.com/monarch-initiative/dismech/blob/main/kb/disorders/Example.yaml");
  gap.source_detail.source_file = "kb/../../private.yaml";
  assert.equal(gapContext(gap).sourceUrl, null);
  assert.equal(gapContext(gap).sourcePage, null);
  assert.equal(evidenceUrl("PMID:12345"), "https://pubmed.ncbi.nlm.nih.gov/12345/");
  assert.equal(evidenceUrl("DOI:10.1234/abc"), "https://doi.org/10.1234/abc");
  for (const input of ["javascript:alert(1)", "data:text/html,test", "https://user:secret@example.test", "PMID:123/../../x"]) assert.equal(evidenceUrl(input), null);
});

test("auxiliary details start collapsed and render source text without interpreting markup", () => {
  const gap = structuredClone(fixture);
  gap.source_detail.raw = { rationale: "<script>untrusted source text</script>", evidence: [{ reference: "javascript:alert(1)", snippet: "Evidence quotation" }], proposed_experiments: [{ name: "Test experiment", description: "A comparison." }] };
  const html = renderToStaticMarkup(createElement(GapContext, { gap }));
  assert.ok(html.includes("Proposed experiments"));
  assert.ok(html.includes("Source evidence"));
  assert.ok(html.includes("DisMech source and provenance"));
  assert.equal(/<details[^>]*\sopen(?:[\s=>])/.test(html), false);
  assert.ok(html.includes("&lt;script&gt;untrusted source text&lt;/script&gt;"));
  assert.equal(html.includes('<a href="javascript:'), false);
  assert.equal(html.includes('"rationale":'), false);
});
