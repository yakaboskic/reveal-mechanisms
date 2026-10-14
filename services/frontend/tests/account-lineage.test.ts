import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { Schema } from "../src/lib/client";
import { appendLineagePage, claimDatasets, gapLabel, issueCounts, issueLabel, LineageChangedError, lineageIndex, lineageRoutes, rorHref, routeCounts, routeLabels, type Lineage } from "../src/lib/account-lineage";

const spec = JSON.parse(readFileSync(new URL("../../../api/openapi.json", import.meta.url), "utf8"));
const lineage = () => structuredClone(spec.paths["/v1/accounts/{account_id}/provenance"].get.responses["200"].content["application/json"].examples.account.value) as Lineage;

test("every contract route has a label, including the trait's published GWAS", () => {
  assert.deepEqual([...lineageRoutes].sort(), [...spec.components.schemas.ProvenanceTrace.properties.route_classifications.items.enum].sort());
  assert.equal(routeLabels.trait_gwas, "Published GWAS of the trait");
});

test("dataset routes list only the routes that reached it, and never a sum", () => {
  const row: Schema<"DatasetReuse"> = { dataset_id: "dapper:Dataset.x", claim_count: 2, claim_ids: ["a", "b"], trace_count: 3, input_roles: [],
    classification_claim_counts: { proposition_reference: 0, evidence_derivation: 1, inherited_source_claim: 0, factor_projection: 2, trait_gwas: 2 },
    classification_claim_ids: { proposition_reference: [], evidence_derivation: ["a"], inherited_source_claim: [], factor_projection: ["a", "b"], trait_gwas: ["a", "b"] } };
  assert.deepEqual(routeCounts(row).map(item => [item.route, item.count]), [["evidence_derivation", 1], ["factor_projection", 2], ["trait_gwas", 2]]);
});

test("issues group by code, most frequent first, with readable labels", () => {
  const value = lineage();
  const counts = issueCounts([...value.coverage.issues, { code: "external_provenance_unavailable", object_id: "KPN.TRAIT:0000398" }]);
  assert.equal(counts[0].code, "MISSING_LINEAGE");
  assert.equal(counts[0].count, value.coverage.issues.length);
  assert.deepEqual(counts.at(-1), { code: "external_provenance_unavailable", count: 1 });
  assert.equal(issueLabel("external_provenance_unavailable"), "Portal GWAS lineage unavailable");
  assert.equal(issueLabel("SOMETHING_NEW"), "Something new");
  assert.equal(gapLabel("not_a_gwas_trait"), "Rare-disease gene list, with no GWAS");
});

test("node names and pages come from the traced graph; only public pages are links", () => {
  const value = lineage();
  value.nodes.push({ id: "KPN.TRAIT:0000398", kind: "trait", status: "resolved", name: "Type 2 diabetes", href: "https://translator.broadinstitute.org/artifact_provenance/bottom_line/T2D" },
    { id: "dapper:Organization.x", kind: "organization", status: "resolved", name: "GlyGen", ror: "ror:00y4zzh67" },
    { id: "dapper:Dataset.local", kind: "dataset", status: "resolved", href: "file:///humgen/data" });
  const index = lineageIndex(value);
  assert.equal(index.name("KPN.TRAIT:0000398"), "Type 2 diabetes");
  assert.equal(index.href("KPN.TRAIT:0000398"), "https://translator.broadinstitute.org/artifact_provenance/bottom_line/T2D");
  assert.equal(index.href("dapper:Organization.x"), "https://ror.org/00y4zzh67");
  assert.equal(index.href("dapper:Dataset.local"), null);
  assert.equal(index.name("dapper:Dataset.unknown"), "dapper:Dataset.unknown");
  assert.equal(rorHref("https://ror.org/00y4zzh67"), "https://ror.org/00y4zzh67");
  assert.equal(rorHref("ror:../evil"), null);
});

test("a claim's datasets cover the whole account; an unknown claim has none", () => {
  const value = lineage(), claim = value.claims[0];
  claim.provenance.dataset_ids = ["dapper:Dataset.a"];
  assert.deepEqual(claimDatasets(value, claim.record.id), ["dapper:Dataset.a"]);
  assert.equal(claimDatasets(value, "dapper:Claim.unknown"), null);
});

test("later pages add traces once, and a changed snapshot is refused", () => {
  const trace = (id: string): Schema<"ProvenanceTrace"> => ({ id, claim_id: null, proposition_id: null, evidence_item_id: null, origin_id: "o", source_field: "f", source_id: "s", dataset_id: "d", route_classifications: [], input_roles: [], organization_ids: [], organization_attributions: [], path_steps: [], path_edge_ids: [] });
  const first = { ...lineage(), traces: [trace("t1"), trace("t2")] }, second = { ...lineage(), traces: [trace("t2"), trace("t3")] };
  assert.deepEqual(appendLineagePage(first, second).traces.map(item => item.id), ["t1", "t2", "t3"]);
  assert.throws(() => appendLineagePage(first, { ...second, snapshot: { ...second.snapshot, id: "changed" } }), LineageChangedError);
});
