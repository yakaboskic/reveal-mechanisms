import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { responseError } from "../src/lib/api";
import { emptyComposer, legacyReferenceModel, withFactors, type Factor } from "../src/lib/types";

const spec = JSON.parse(readFileSync(new URL("../openapi.json", import.meta.url), "utf8"));
const examples = spec.paths["/v1/mechanisms/{source_id}"].get.responses["200"].content["application/json"].examples;
const legacy = examples.eaggl_factor.value as Factor, kpn = examples.kpn_factor.value as Factor;

test("the composer keeps the legacy model until a factor from a reloaded reference is selected", () => {
  assert.equal(emptyComposer().model, legacyReferenceModel);
  const first = withFactors(emptyComposer(), [legacy], "suggestion-1", true);
  assert.equal(first.model, "cfde-inc-v2"); assert.deepEqual(Object.keys(first), Object.keys(emptyComposer()));
  const reloaded = withFactors(emptyComposer(), [kpn], "suggestion-2", true);
  assert.equal(reloaded.model, "eaggl-capped-v1");
  assert.deepEqual(reloaded.eaggl_anchors.map(anchor => anchor.reference.source_id), [kpn.source_id]);
  assert.equal(withFactors(reloaded, [], "suggestion-3", true).model, "eaggl-capped-v1");
  assert.equal(withFactors(first, [kpn], "suggestion-4").eaggl_anchors.length, 2);
});

test("reference reload problems read as clear copy; other problems keep the server detail", async () => {
  const problem = (code: string, status: number) => new Response(JSON.stringify({ code, detail: "server detail" }), { status });
  const superseded = await responseError(problem("REFERENCE_GENERATION_SUPERSEDED", 409));
  assert.equal(superseded.code, "REFERENCE_GENERATION_SUPERSEDED"); assert.match(superseded.message, /outdated EAGGL reference/);
  assert.match((await responseError(problem("REFERENCE_RELOAD_IN_PROGRESS", 503))).message, /being updated/);
  assert.equal((await responseError(problem("VERSION_CONFLICT", 409))).message, "server detail");
});
