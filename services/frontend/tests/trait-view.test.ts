import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError, type Schema } from "../src/lib/client";
import { appendRows, betaKey, gwasLinks, gwasReasons, linkKey, mechanismHref, metricValue, ownTraitStatus, phenotypeTarget, releaseLacks } from "../src/lib/trait-view";

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

test("a trait's GWAS links out whenever the portal has artifacts, says why the trace has none, and only links public pages", () => {
  const atlas = (example("/v1/traits/{kpn_trait_id}", "atlas_trait") as Schema<"TraitDetail">).gwas;
  const available = gwasLinks(atlas);
  assert.equal(available.note, null);
  assert.equal(available.listing, atlas.portal_href);
  assert.deepEqual(available.artifacts.map(item => [item.name, item.href, item.traced]), atlas.artifacts.map(item => [item.ancestry_name, item.href, item.artifact_id === atlas.gwas_artifact_id]));
  assert.equal(available.artifacts.filter(item => item.traced).length, 1);
  // Artifacts, but none of the traced ancestry: the portal pages remain reachable, with the reason.
  const other = gwasLinks({ ...atlas, status: "no_gwas_ancestry_artifact", gwas_artifact_id: null, artifacts: atlas.artifacts.filter(item => item.ancestry_code !== "Mixed") });
  assert.equal(other.note, gwasReasons.no_gwas_ancestry_artifact);
  assert.equal(other.listing, atlas.portal_href);
  assert.equal(other.artifacts.length, atlas.artifacts.length - 1);
  assert.ok(other.artifacts.every(item => item.href && !item.traced));
  const linked = gwasLinks((example("/v1/traits/{kpn_trait_id}", "linked_phenotype") as Schema<"TraitDetail">).gwas);
  assert.deepEqual(linked, { note: gwasReasons.not_on_portal, listing: null, artifacts: [] });
  // A listing with no artifacts is not offered; unsafe schemes are never links.
  assert.equal(gwasLinks({ ...atlas, status: "not_on_portal", gwas_artifact_id: null, artifacts: [] }).listing, null);
  const unsafe = gwasLinks({ ...atlas, portal_href: "javascript:alert(1)", artifacts: atlas.artifacts.map(item => ({ ...item, href: "file:///etc/passwd" })) });
  assert.equal(unsafe.listing, null);
  assert.equal(unsafe.note, "The GWAS listing is not available as a public link.");
  assert.ok(unsafe.artifacts.every(item => item.href === null));
});

test("a listing the release does not include is stated once, not retried; other failures stay retryable", () => {
  const missing = example("/v1/trait-gene-sets", "not_found", "404") as Schema<"Problem">;
  assert.equal(releaseLacks(new ApiError(404, missing.code, missing.detail, missing)), true);
  assert.equal(releaseLacks(new ApiError(503, "SOURCE_NOT_READY", "No reference release is published for this environment.")), false);
  assert.equal(releaseLacks(new ApiError(422, "INVALID_QUERY", "Invalid")), false);
  assert.equal(releaseLacks(new Error("offline")), false);
  for (const file of ["TraitView.tsx", "TraitLinks.tsx"]) {
    const source = readFileSync(new URL(`../src/components/${file}`, import.meta.url), "utf8");
    assert.match(source, /absent: offset === 0 && releaseLacks\(error\)/, `${file} records an absent listing only for its first page`);
    assert.match(source, /if \(absent && !result\) return /, `${file} renders an absent listing without a retry`);
  }
});
