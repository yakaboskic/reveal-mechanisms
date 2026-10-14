import { test } from "node:test";
import assert from "node:assert/strict";
import { cfdeSources, datasetHref, datasetTitle } from "../src/lib/data-sources";

// Invented identifiers in the shape GET /v1/accounts/{id} serves: claim 1 cites an EAGGL mechanism whose factor
// projection reaches three datasets, claim 4 inherits them from claim 1, and claims 2 and 3 cite no CFDE lineage.
const organization = (id: string, roles: string[]) => ({ organization_id: id, roles });
const connection = (dataset_id: string, input_roles: string[], via_ids: string[], organizations: ReturnType<typeof organization>[]) =>
  ({ dataset_id, input_roles, via_ids, organizations, routes: ["factor_projection" as const], curation_ids: [], trace_count: 1 });
const reached = (via: string) => [
  connection("dapper:Dataset.gtex", ["data_input"], [via], [organization("dapper:Organization.gtex", ["attribution"]), organization("dapper:Organization.broad", ["publisher"])]),
  connection("dapper:Dataset.ncbi", ["metadata_input"], [via], [organization("dapper:Organization.ncbi", ["publisher"])]),
  connection("dapper:Dataset.lincs", ["data_input"], [via], [organization("dapper:Organization.lab", ["attribution"])]),
];
const dataSources = {
  status: "incomplete", traced_at: "2026-10-12T10:00:00Z",
  coverage: { trace_count: 6, counts_are_lower_bounds: true, issue_counts: { MISSING_LINEAGE: 3 } },
  claims: [
    { claim_id: "dapper:Claim.one", resolution: "incomplete", datasets: reached("dapper:Mechanism.factor2") },
    { claim_id: "dapper:Claim.two", resolution: "incomplete", datasets: [] },
    { claim_id: "dapper:Claim.four", resolution: "incomplete", datasets: reached("dapper:Claim.one") },
    { claim_id: "dapper:Claim.elsewhere", resolution: "complete", datasets: reached("dapper:Mechanism.factor2") },
  ],
  datasets: [
    { id: "dapper:Dataset.gtex", name: "GTEx Analysis V8 bulk tissue RNA-seq", version: "v8", location: "https://gtexportal.org/home/downloads/adult-gtex/bulk_tissue_expression" },
    { id: "dapper:Dataset.ncbi", name: "NCBI Gene Homo_sapiens.gene_info", location: "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/" },
    { id: "dapper:Dataset.lincs", name: "LINCS L1000 consensus signatures", location: "javascript:alert(1)" },
  ],
  organizations: [
    { id: "dapper:Organization.gtex", name: "GTEx Consortium" }, { id: "dapper:Organization.broad", name: "Broad Institute", ror: "ror:05a0ya142" },
    { id: "dapper:Organization.ncbi", name: "National Center for Biotechnology Information", ror: "ror:02meqm098" },
  ],
  via: [{ id: "dapper:Mechanism.factor2", kind: "mechanism", name: "Psychiatric disorders mechanism Factor2" }, { id: "dapper:Claim.one", kind: "claim" }],
};
const claims = [
  { id: "dapper:Claim.one", evidence: ["dapper:EvidenceItem.loading"] },
  { id: "dapper:Claim.two", evidence: ["dapper:EvidenceItem.cohort"] },
  { id: "dapper:Claim.three", evidence: [] },
  { id: "dapper:Claim.four", evidence: [] },
];

test("stored data sources put each claim's CFDE datasets in the Source column, primary data first", () => {
  const sources = cfdeSources(dataSources, claims)!;
  assert.equal(sources.status, "incomplete");
  assert.equal(sources.lowerBound, true);
  assert.deepEqual(sources.datasets.map(item => item.id), ["dapper:Dataset.gtex", "dapper:Dataset.lincs", "dapper:Dataset.ncbi"]);
  assert.deepEqual(Object.fromEntries(sources.claimDatasets), {
    "dapper:Claim.one": ["dapper:Dataset.gtex", "dapper:Dataset.ncbi", "dapper:Dataset.lincs"],
    "dapper:Claim.four": ["dapper:Dataset.gtex", "dapper:Dataset.ncbi", "dapper:Dataset.lincs"],
  });
  assert.deepEqual([...sources.unverifiedClaims], ["dapper:Claim.two"]);
  const gtex = sources.datasets[0];
  assert.deepEqual(gtex.claims, ["dapper:Claim.one", "dapper:Claim.four"]);
  assert.deepEqual(gtex.via, ["Psychiatric disorders mechanism Factor2", "claim 1"]);
  assert.deepEqual(gtex.organizations, [
    { id: "dapper:Organization.gtex", name: "GTEx Consortium", ror: null, roles: ["attribution"] },
    { id: "dapper:Organization.broad", name: "Broad Institute", ror: "ror:05a0ya142", roles: ["publisher"] },
  ]);
  assert.equal(gtex.href, "https://gtexportal.org/home/downloads/adult-gtex/bulk_tissue_expression");
  assert.equal(sources.datasets[1].href, null, "only http(s) locations are linked");
  assert.deepEqual(sources.datasets[1].organizations, [{ id: "dapper:Organization.lab", name: null, ror: null, roles: ["attribution"] }]);
});

test("dataset edges leave the claim's evidence, or the claim itself when it shows none, once each", () => {
  const edges = cfdeSources(dataSources, claims)!.edges;
  assert.equal(edges.length, 6);
  assert.equal(new Set(edges.map(edge => edge.key)).size, edges.length);
  assert.deepEqual(edges.filter(edge => edge.to === "dataset:dapper:Dataset.gtex").map(edge => edge.from),
    ["evidence:dapper:EvidenceItem.loading", "claim:dapper:Claim.four"]);
  assert.ok(edges.every(edge => !edge.from.includes("elsewhere")), "claims outside the graph draw nothing");
});

test("untraced, failed and malformed data sources draw no datasets", () => {
  assert.equal(cfdeSources(undefined, claims), null);
  assert.equal(cfdeSources({ status: "pending" }, claims), null);
  for (const status of ["not_traced", "unavailable"]) {
    const sources = cfdeSources({ status, ...(status === "unavailable" ? { error: { code: "TRACE_TIMEOUT" } } : {}) }, claims)!;
    assert.equal(sources.status, status);
    assert.deepEqual([sources.datasets, sources.edges, sources.claimDatasets.size], [[], [], 0]);
    assert.deepEqual([...sources.unverifiedClaims].sort(), claims.map(claim => claim.id).sort());
  }
  const complete = cfdeSources({ ...dataSources, status: "complete", coverage: { trace_count: 6, counts_are_lower_bounds: false, issue_counts: {} } }, claims)!;
  assert.equal(complete.lowerBound, false);
});

test("a trait's GWAS bottom line is traced lineage, kept apart from and after the CFDE datasets", () => {
  const gwasId = "gwas-bottom-line:T2D.bottom-line.EU";
  const withGwas = {
    ...dataSources,
    claims: [...dataSources.claims.filter(claim => claim.claim_id !== "dapper:Claim.two"), { claim_id: "dapper:Claim.two", resolution: "complete", datasets: [
      { dataset_id: gwasId, input_roles: ["data_input"], routes: ["trait_gwas" as const], via_ids: ["trait:portal:T2D"], organizations: [], curation_ids: [], trace_count: 1 },
    ] }],
    datasets: [...dataSources.datasets, { id: gwasId, kind: "gwas_bottom_line", name: "Type 2 diabetes GWAS bottom line", kpn_trait_id: "portal:T2D", ancestry_name: "European", location: "https://example.org/artifact" }],
    via: [...dataSources.via, { id: "trait:portal:T2D", kind: "trait" }],
  };
  const sources = cfdeSources(withGwas, claims)!;
  assert.deepEqual(sources.datasets.map(item => item.id), ["dapper:Dataset.gtex", "dapper:Dataset.lincs", "dapper:Dataset.ncbi", gwasId]);
  const gwas = sources.datasets.at(-1)!;
  assert.deepEqual(gwas.gwas, { trait: "portal:T2D", ancestry: "European" });
  assert.ok(sources.datasets.slice(0, -1).every(item => item.gwas === null));
  assert.deepEqual(sources.claimDatasets.get("dapper:Claim.two"), [gwasId]);
  assert.equal(sources.unverifiedClaims.has("dapper:Claim.two"), false);
  assert.equal(datasetTitle(gwas, false), [
    "Type 2 diabetes GWAS bottom line",
    "Trait GWAS bottom line of portal:T2D (European). The reference release names it; the Translator provenance portal holds it.",
    "Reached by 1 claim through a trait.",
    "Reached through: a cited trait.",
  ].join("\n"));
});

test("a dataset's popup states its use, its route and its credit", () => {
  const [gtex] = cfdeSources(dataSources, claims)!.datasets;
  assert.equal(datasetTitle(gtex, true), [
    "GTEx Analysis V8 bulk tissue RNA-seq",
    "Version: v8",
    "Used as primary data by 2 claims.",
    "Reached through: Psychiatric disorders mechanism Factor2; claim 1.",
    "Credited: GTEx Consortium (attribution); Broad Institute (publisher).",
    "Some cited sources have no recorded lineage, so more datasets may contribute.",
  ].join("\n"));
  assert.equal(datasetHref("ftp://example.org/data"), null);
  assert.equal(datasetHref("not a url"), null);
});
