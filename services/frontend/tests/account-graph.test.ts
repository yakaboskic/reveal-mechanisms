import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { accountGraphLabel, buildAccountHierarchy, flattenAccountHierarchy, mergeAccountPages } from "../src/lib/account-graph";
import type { Schema } from "../src/lib/client";

const fixture = () => JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8")).account as Schema<"AccountResult">;

test("only explicit component claims are account children, preserving scientific order", () => {
  const result = fixture(), account = result.document.scientific_accounts![0];
  result.document.claims!.push({ ...result.document.claims![0], id: "dapper:Claim.unrelated" });
  const graph = buildAccountHierarchy(result);
  assert.deepEqual(graph.children.map(node => node.objectId), account.component_claims);
  assert.equal(graph.children.some(node => node.objectId === "dapper:Claim.unrelated"), false);
});

test("shared source files retain canonical identity and distinct display occurrences", () => {
  const nodes = flattenAccountHierarchy(buildAccountHierarchy(fixture()));
  const files = nodes.filter(node => node.kind === "file");
  const shared = files.filter(node => node.objectId === files[0].objectId);
  assert.ok(shared.length > 1);
  assert.equal(new Set(shared.map(node => node.id)).size, shared.length);
  assert.ok(shared.every(node => node.shared === shared.length));
  assert.ok(nodes.some(node => node.relation.startsWith("Computational input")));
  assert.ok(nodes.every(node => ["account", "claim", "dataset", "file"].includes(node.kind)));
  assert.ok(files.every(node => !node.children.length));
});

test("dataset membership follows has_file and provenance edges without including unrelated files", () => {
  const result = fixture(), file = result.document.files![0], claim = result.document.claims![0], evidence = result.document.evidence_items!.find(node => node.id === claim.has_evidence![0])!;
  const dataset = { id: "dapper:Dataset.example", name: "Captured observations", has_file: [file.id], location: "s3://development/example/" };
  result.document.datasets = [dataset]; evidence.was_derived_from = [dataset.id];
  const datasetNode = flattenAccountHierarchy(buildAccountHierarchy(result)).find(node => node.kind === "dataset")!;
  assert.deepEqual(datasetNode.children.map(node => [node.objectId, node.relation]), [[file.id, "Dataset file"]]);
  assert.notEqual(datasetNode.children.length, result.document.files!.length);
});

test("bounded records and cyclic lineage remain explicit and terminate", () => {
  const result = fixture(), first = result.document.claims![0];
  first.was_derived_from = [first.id, "dapper:File.omitted"];
  result.coverage.complete = false; result.coverage.missing_ids = ["dapper:File.omitted"];
  const nodes = flattenAccountHierarchy(buildAccountHierarchy(result));
  assert.ok(nodes.some(node => node.objectId === first.id && node.cycle));
  assert.equal(nodes.filter(node => node.objectId === first.id).length, 1);
  assert.ok(nodes.some(node => node.objectId === "dapper:File.omitted" && node.missing));
  const limited = buildAccountHierarchy(result, 1, 18);
  assert.deepEqual(limited.children.map(node => node.objectId), result.document.scientific_accounts![0].component_claims);
  assert.ok(flattenAccountHierarchy(limited).some(node => node.limited));
});

test("account continuation merges records and clears only omissions actually loaded", () => {
  const full = fixture(), [firstClaim, secondClaim] = full.document.claims!, [firstEvidence] = full.document.evidence_items!;
  const first = { ...full, document: { scientific_accounts: full.document.scientific_accounts, claims: [firstClaim] }, artifacts: [], coverage: { ...full.coverage, complete: false, missing_ids: [secondClaim.id, firstEvidence.id], next_cursor: "cursor" } };
  const second = { ...full, document: { scientific_accounts: full.document.scientific_accounts, claims: [secondClaim] }, artifacts: [], coverage: { ...full.coverage, complete: false, missing_ids: [firstEvidence.id], next_cursor: null } };
  const merged = mergeAccountPages(first, second);
  assert.deepEqual(merged.document.claims!.map(node => node.id), [firstClaim.id, secondClaim.id]);
  assert.equal(merged.document.scientific_accounts!.length, 1);
  assert.deepEqual(merged.coverage.missing_ids, [firstEvidence.id]);
  assert.equal(merged.coverage.complete, false);
  assert.equal(merged.coverage.next_cursor, null);
});

test("account continuation refuses a different root or payload observation", () => {
  const first = fixture(), changed = fixture();
  changed.payloads[0].payload_sha256 = "f".repeat(64);
  assert.throws(() => mergeAccountPages(first, changed), /source changed/);
  assert.throws(() => mergeAccountPages(first, { ...fixture(), root_id: "dapper:ScientificAccount.other" }), /source changed/);
});

test("canonical overview contains only claims, actual datasets and their member files", () => {
  const result = JSON.parse(readFileSync(new URL("../../../data/fixtures/bubble-account-v1/account-envelope.json", import.meta.url), "utf8")) as Schema<"AccountResult">;
  const graph = buildAccountHierarchy(result), nodes = flattenAccountHierarchy(graph);
  assert.equal(graph.children.length, 12);
  assert.equal(new Set(nodes.filter(node => node.kind === "dataset").map(node => node.objectId)).size, 4);
  assert.equal(new Set(nodes.filter(node => node.kind === "file").map(node => node.objectId)).size, result.document.files!.length);
  assert.ok(nodes.length <= 600);
  assert.ok(nodes.every(node => ["account", "claim", "dataset", "file"].includes(node.kind)));
  for (const dataset of nodes.filter(node => node.kind === "dataset")) {
    const source = result.document.datasets!.find(value => value.id === dataset.objectId)!;
    assert.deepEqual(dataset.children.map(node => node.objectId), source.has_file);
  }
  assert.ok(nodes.some(node => node.kind === "dataset" && node.children.some(child => child.kind === "file" && child.relation === "Dataset file")));
  assert.ok(nodes.every(node => !node.missing));
  assert.equal(graph.children[0].shortLabel, "SHH\n→ Factor1");
  assert.equal(graph.children[5].shortLabel, "SHH\n→ CAD-in-T2D");
  assert.equal(new Set(graph.children.map(node => node.shortLabel)).size, 12);
  assert.ok(graph.children.every(node => accountGraphLabel(node).length > 10 && !/^C\d+$/.test(accountGraphLabel(node))));
});

test("missing hidden evidence remains explicit without adding an evidence circle", () => {
  const result = fixture(), claim = result.document.claims![0];
  claim.has_evidence = ["dapper:EvidenceItem.not-loaded"];
  const display = buildAccountHierarchy(result).children[0];
  assert.equal(display.missing, false, "The loaded claim remains readable");
  assert.deepEqual(display.missingReferences, ["dapper:EvidenceItem.not-loaded"]);
  assert.equal(flattenAccountHierarchy(display).some(node => node.kind === "evidence"), false);
});

test("activity inputs remain computational sources and do not imply evidence or dataset membership", () => {
  const result = fixture(), claim = result.document.claims![0], [file, standalone] = result.document.files!;
  const activity = "dapper:Activity.overview-inputs", dataset = "dapper:Dataset.overview-inputs";
  claim.has_evidence = []; claim.was_generated_by = activity;
  result.document.activities = [{ id: activity, name: "Input processing" }];
  result.document.datasets = [{ id: dataset, name: "Observed input collection", has_file: [file.id] }];
  result.document.used_edges = [file, standalone].map(value => ({ subject: activity, predicate: "prov:used", object: value.id }));
  const display = buildAccountHierarchy(result).children[0];
  assert.match(display.children.find(node => node.objectId === dataset)!.relation, /^Computational source/);
  assert.match(display.children.find(node => node.objectId === standalone.id)!.relation, /^Computational input/);
  assert.deepEqual(display.children.find(node => node.objectId === dataset)!.children.map(node => node.objectId), [file.id]);
  assert.equal(display.children.some(node => node.objectId === activity), false);
});

test("display identities survive newly loaded earlier source references", () => {
  const result = fixture(), claim = result.document.claims![0], evidence = result.document.evidence_items!.find(node => node.id === claim.has_evidence![0])!;
  const before = flattenAccountHierarchy(buildAccountHierarchy(result));
  const dataset = { id: "dapper:Dataset.loaded-later", name: "Earlier source now loaded", has_file: [] };
  result.document.datasets = [...(result.document.datasets || []), dataset];
  evidence.was_derived_from = [dataset.id, ...(evidence.was_derived_from || [])];
  const after = flattenAccountHierarchy(buildAccountHierarchy(result));
  for (const node of before) assert.ok(after.some(value => value.id === node.id && value.objectId === node.objectId));
});

test("file lineage discovers upstream datasets without nesting upstream files in the derived dataset", () => {
  const result = fixture(), claim = result.document.claims![0], [derived, input] = result.document.files!;
  const dataset = "dapper:Dataset.derived", upstream = "dapper:Dataset.upstream", activity = "dapper:Activity.transform";
  claim.has_evidence = []; claim.was_derived_from = [dataset];
  result.document.datasets = [{ id: dataset, name: "Derived dataset", has_file: [derived.id] }, { id: upstream, name: "Original observations", has_file: [input.id] }];
  result.document.activities = [{ id: activity, name: "Transformation" }];
  result.document.was_generated_by_edges = [{ subject: derived.id, predicate: "prov:wasGeneratedBy", object: activity }];
  result.document.used_edges = [{ subject: activity, predicate: "prov:used", object: input.id }];
  const display = buildAccountHierarchy(result).children[0];
  assert.deepEqual(display.children.find(node => node.objectId === dataset)!.children.map(node => node.objectId), [derived.id]);
  const source = display.children.find(node => node.objectId === upstream)!;
  assert.deepEqual(source.children.map(node => node.objectId), [input.id]);
  assert.match(source.relation, /^Computational source/);
});

test("schema edge-only evidence and C2M2 file membership remain visible", () => {
  const result = fixture(), claim = result.document.claims![0], evidence = result.document.evidence_items![0], file = result.document.files![0];
  const dataset = "dapper:Dataset.edge-only", c2m2 = "dapper:C2M2File.edge-only", drs = "dapper:DrsObject.bundle";
  claim.has_evidence = []; evidence.was_derived_from = [dataset];
  result.document.has_evidence_edges = [{ subject: claim.id, predicate: "dapper:hasEvidence", object: evidence.id }];
  result.document.datasets = [{ id: dataset, name: "Edge-only membership", has_drs_object: [drs] }];
  result.document.c2m2_files = [{ ...file, id: c2m2 }];
  result.document.has_file_edges = [{ subject: dataset, predicate: "dapper:hasFile", object: c2m2 }];
  const display = buildAccountHierarchy(result).children[0].children.find(node => node.objectId === dataset)!;
  assert.deepEqual(display.children.map(node => [node.objectId, node.kind, node.missing]), [[c2m2, "file", false]]);
  assert.equal(flattenAccountHierarchy(display).some(node => node.objectId === drs), false, "A DRS bundle is not falsely labeled a file");
  result.document.c2m2_files = [];
  assert.equal(buildAccountHierarchy(result).children[0].children.find(node => node.objectId === dataset)!.children[0].missing, true);
});

test("a dataset found through a source file retains its own upstream lineage without a false cycle", () => {
  const result = fixture(), claim = result.document.claims![0], file = result.document.files![0];
  const parent = "dapper:Dataset.file-parent", upstream = "dapper:Dataset.parent-source";
  claim.has_evidence = []; claim.was_generated_by = "dapper:Activity.unloaded"; claim.was_derived_from = [file.id];
  result.document.datasets = [{ id: parent, name: "File parent", has_file: [file.id], was_derived_from: [upstream] }, { id: upstream, name: "Parent source" }];
  result.document.was_generated_by_edges = []; result.document.was_derived_from_edges = []; result.document.used_edges = [];
  const display = buildAccountHierarchy(result).children[0];
  assert.deepEqual(display.children.filter(node => node.kind === "dataset").map(node => node.objectId), [parent, upstream]);
  assert.equal(display.cycle, false);
});

test("an oversized component collection stays inside the total display budget", () => {
  const result = fixture();
  result.document.scientific_accounts![0].component_claims = Array.from({ length: 1000 }, (_, index) => `dapper:Claim.missing-${index}`);
  const graph = buildAccountHierarchy(result, 3, 80);
  assert.equal(flattenAccountHierarchy(graph).length, 80);
  assert.equal(graph.children.length, 79); assert.equal(graph.limited, true);
});
