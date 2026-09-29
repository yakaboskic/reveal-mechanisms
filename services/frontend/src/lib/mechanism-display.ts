import type { Schema } from "./client";

/** The catalog supplies its exact trait label in `subtitle`, followed by FactorN. */
export function mechanismTrait(factor?: Schema<"EagglFactor">, sourceId?: string): string | null {
  const subtitle = factor?.cfde_anchor.subtitle?.trim();
  const label = subtitle?.replace(/\s*\(Factor\d+\)\s*$/i, "").trim();
  if (label && !/^Factor\s*\d+$/i.test(label) && !label.startsWith("factor:")) return label;
  const native = factor?.cfde_anchor.node_id || factor?.source_id || sourceId;
  return /^factor:portal:([^:]+):[^:]+:Factor\d+$/.exec(native || "")?.[1] || null;
}

export function mechanismName(factor?: Schema<"EagglFactor">): string {
  return factor?.cfde_anchor.label?.trim() || factor?.object.name?.trim() || "Mechanism anchor";
}
