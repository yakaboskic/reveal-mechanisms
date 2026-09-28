import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mechanismName, mechanismTrait } from "../src/lib/mechanism-display";
import type { Schema } from "../src/lib/client";

const factor = JSON.parse(readFileSync(new URL("../src/lib/fixtures/contract.json", import.meta.url), "utf8")).suggestions.automatic_anchors[0].factor as Schema<"EagglFactor">;
const withAnchor = (values: Partial<Schema<"CfdeAnchor">>) => ({ ...factor, cfde_anchor: { ...factor.cfde_anchor, ...values } });

test("mechanism labels preserve the friendly name and show the exact catalog trait without Factor IDs", () => {
  const record = withAnchor({ label: "Beta Cell Dysfunction and Diabetes", subtitle: "eGFRcys (Factor4)" });
  assert.equal(mechanismName(record), "Beta Cell Dysfunction and Diabetes");
  assert.equal(mechanismTrait(record), "eGFRcys");
  assert.equal(mechanismTrait(withAnchor({ subtitle: "Coronary artery disease (Factor12)" })), "Coronary artery disease");
});

test("restoring frozen anchors uses only a recognized native trait identity while labels load", () => {
  assert.equal(mechanismTrait(undefined, "factor:portal:CAD:cfde-inc-v2:Factor1"), "CAD");
  assert.equal(mechanismName(undefined), "Mechanism anchor");
  assert.equal(mechanismTrait(undefined, "unrecognized:record"), null);
  assert.equal(mechanismTrait(withAnchor({ subtitle: "Factor4", node_id: "unrecognized:record" })), null);
});

test("trait text is not guessed or expanded and numeric content in real trait labels survives", () => {
  assert.equal(mechanismTrait(withAnchor({ subtitle: "Type 2 diabetes (Factor9)" })), "Type 2 diabetes");
  assert.equal(mechanismTrait(withAnchor({ subtitle: "HDL" })), "HDL");
  assert.equal(mechanismTrait(withAnchor({ subtitle: "Trait (subtype) (Factor2)" })), "Trait (subtype)");
});
