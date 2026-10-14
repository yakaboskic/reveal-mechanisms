import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError, type Schema } from "../src/lib/client";
import { appendRows, betaKey, linkKey, mechanismHref, metricValue, ownTraitStatus, phenotypeTarget } from "../src/lib/trait-view";

const spec = JSON.parse(readFileSync(new URL("../../../api/openapi.json", import.meta.url), "utf8"));
const example = (path: string, name: string, status = "200") => structuredClone(spec.paths[path].get.responses[status].content[status === "200" ? "application/json" : "application/problem+json"].examples[name].value);

test("atlas phenotypes open in the app; other phenotypes keep only their KPN catalog page", () => {
  const [own, other] = (example("/v1/factor-trait-links", "factor") as Schema<"FactorTraitLinks">).items;
  const from = "/factors/factor%3Akpn%3A0000398%3Aeaggl-capped-v1%3AFactor1";
  const atlas = phenotypeTarget(own, from);
  assert.equal(atlas.page, `/traits/KPN.TRAIT%3A0000398?${new URLSearchParams({ from })}`);
  assert.equal(atlas.catalog, "https://broadinstitute.github.io/kpn-data-models/kpn.trait/0000398/");
  assert.deepEqual(phenotypeTarget(other), { page: null, catalog: "https://broadinstitute.github.io/kpn-data-models/kpn.trait/0001360/" });
  assert.deepEqual(phenotypeTarget({ ...other, phenotype_kpn_trait_id: null }), { page: null, catalog: null });
});

test("a factor's own trait is linked, unlinked above the threshold, or untested", () => {
  assert.equal(ownTraitStatus({ own_trait_q: 4.6e-10 }), "Linked to its own trait (q 4.60e-10)");
  assert.equal(ownTraitStatus({ own_trait_q: 0.2 }), "Not linked to its own trait (q 0.2)");
  assert.equal(ownTraitStatus({ own_trait_q: 0.05 }), "Linked to its own trait (q 0.05)");
  assert.equal(ownTraitStatus({ own_trait_q: null }), "Its own trait was not tested");
  assert.equal(ownTraitStatus(null), null);
});

test("table values are compact, keep exact zeros and never invent a number", () => {
  assert.equal(metricValue(0), "0");
  assert.equal(metricValue(0.0312345), "0.03123");
  assert.equal(metricValue(-0.0004), "-4.00e-4");
  assert.equal(metricValue(12.5), "12.5");
  for (const value of [null, undefined, Number.NaN, Infinity]) assert.equal(metricValue(value), "Not available");
});

test("appended pages keep earlier rows and drop a repeated boundary row", () => {
  const page = example("/v1/trait-gene-sets", "trait") as Schema<"TraitGeneSets">;
  const [first, second] = page.items;
  const rows = appendRows([first], [first, second], betaKey);
  assert.deepEqual(rows.map(betaKey), [betaKey(first), betaKey(second)]);
  assert.equal(rows[0], first);
  const links = (example("/v1/factor-trait-links", "factor") as Schema<"FactorTraitLinks">).items;
  assert.equal(new Set(links.map(linkKey)).size, links.length);
});

test("a Mechanism id opens its current factor, its archived snapshot on 410, and nothing else", () => {
  const current = example("/v1/mechanism-objects/{dapper_id}", "current") as Schema<"MechanismObject">;
  assert.equal(mechanismHref(current, "/accounts/a"), `/factors/${encodeURIComponent(current.source_id)}?from=%2Faccounts%2Fa`);
  const gone = example("/v1/mechanism-objects/{dapper_id}", "reference_generation_superseded", "410") as Schema<"Problem">;
  const archived = gone.archived_reference_factor!;
  const url = new URL(mechanismHref(new ApiError(410, gone.code, gone.detail || "", gone), null)!, "http://localhost");
  assert.equal(decodeURIComponent(url.pathname.split("/").at(-1)!), archived.source_id);
  assert.equal(url.searchParams.get("archive"), archived.archive_id);
  assert.equal(mechanismHref(new ApiError(410, gone.code, "", { ...gone, archived_reference_factor: null })), null);
  assert.equal(mechanismHref(new ApiError(404, "NOT_FOUND", "Not found")), null);
  assert.equal(mechanismHref(new Error("offline")), null);
  assert.equal(mechanismHref({ ...current, status: "archived" }), null);
});
