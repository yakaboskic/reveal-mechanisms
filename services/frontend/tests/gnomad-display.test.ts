import test from "node:test";
import assert from "node:assert/strict";
import { constraintCell, constraintDefinitions, constraintLinks, constraintSelection, constraintSourceHref, constraintValue, type GeneConstraint } from "../src/lib/gnomad-display";
import { loadingAppearance } from "../src/lib/loading-visual";

const annotation: GeneConstraint = { symbol: "NRXN1", gene_id: "ENSG00000179915", transcript: "ENST00000406316.7", status: "selected", selection_method: "mane_select", selection_reason: "Unique Ensembl MANE Select transcript", pli: 0, loeuf: 0.3123456789, mis_z: -0.2, lof_oe: null, flags: ["low_coverage"] };
const source = { import_id: "fixture-import", version: "4.1.1", source_sha256: "a".repeat(64), source_url: "https://example.org/constraint.tsv", selection_policy: "Unique Ensembl MANE Select, otherwise unique canonical" };

test("constraint labels preserve zero, signed scores and exact precision without implying variant probability", () => {
  assert.equal(constraintValue(annotation, "pli"), "0");
  assert.equal(constraintValue(annotation, "loeuf"), "0.3123456789");
  assert.equal(constraintValue(annotation, "mis_z"), "-0.2");
  assert.equal(constraintValue(annotation, "lof_oe"), "Not reported");
  assert.match(constraintDefinitions.pli, /probability of loss-of-function intolerance/);
  assert.match(constraintDefinitions.loeuf, /lower values indicate stronger constraint/);
  assert.match(constraintDefinitions.mis_z, /higher values indicate stronger constraint/);
});

test("unmatched and ambiguous mappings never expose an arbitrary transcript's metric", () => {
  assert.equal(constraintValue(null, "pli"), "Not reported");
  for (const status of ["ambiguous_gene", "ambiguous_transcript", "no_primary_transcript", "no_ensembl_gene"] as const) {
    assert.equal(constraintValue({ ...annotation, status }, "pli"), "Not reported");
    assert.doesNotMatch(constraintSelection({ ...annotation, status }), /^MANE Select/);
  }
  assert.equal(constraintValue({ ...annotation, mis_z: NaN }, "mis_z"), "Not reported");
  assert.match(constraintSelection({ ...annotation, selection_method: "canonical" }), /Canonical/);
});

test("annotations add source context and links while leaving the factor color unchanged", () => {
  const tile = { id: "NRXN1", label: "NRXN1", loading: 0.6, rank: 40 };
  const annotated = { ...tile, ...constraintCell(annotation, source) };
  assert.equal(loadingAppearance(annotated, 0, 0.9).level, loadingAppearance(tile, 0, 0.9).level);
  assert.equal(annotated.rank, 40);
  assert.deepEqual(annotated.details?.map(row => row.value), ["0", "0.3123456789", "-0.2"]);
  assert.match(annotated.description!, /gnomAD 4\.1\.1.*MANE Select.*ENST00000406316\.7.*low_coverage/);
  const links = constraintLinks(annotation, source.version);
  assert.equal(new URL(links[0].href).searchParams.get("dataset"), "gnomad_r4");
  assert.equal(new URL(links[1].href).searchParams.get("t"), annotation.transcript);
  assert.deepEqual(constraintCell(annotation, undefined), {});
});

test("source links require safe absolute URLs and transcript links require Ensembl identities", () => {
  assert.equal(constraintSourceHref("javascript:alert(1)"), null);
  assert.equal(constraintSourceHref("/relative.tsv"), null);
  assert.equal(constraintSourceHref(source.source_url), source.source_url);
  assert.deepEqual(constraintLinks({ ...annotation, gene_id: "not-a-gene", transcript: "NM_001000" }), []);
  assert.deepEqual(constraintLinks(null), []);
});
