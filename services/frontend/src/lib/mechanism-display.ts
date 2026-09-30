import type { Schema } from "./client";
import { traitOfSourceId } from "./reference";

/**
 * KPN factors (factor:kpn:NNNNNNN:eaggl-capped-v1:FactorN) carry their phenotype
 * in `kpn_trait`. Otherwise the catalog supplies its exact trait label in
 * `subtitle`, followed by FactorN.
 */
export function mechanismTrait(factor?: Schema<"EagglFactor">, sourceId?: string): string | null {
  const kpn = factor?.kpn_trait?.name?.trim();
  if (kpn) return kpn;
  const subtitle = factor?.cfde_anchor.subtitle?.trim();
  const label = subtitle?.replace(/\s*\(Factor\d+\)\s*$/i, "").trim();
  if (label && !/^Factor\s*\d+$/i.test(label) && !label.startsWith("factor:")) return label;
  return traitOfSourceId(factor?.cfde_anchor.node_id || factor?.source_id || sourceId);
}

export function mechanismName(factor?: Schema<"EagglFactor">): string {
  return factor?.cfde_anchor.label?.trim() || factor?.object.name?.trim() || "Mechanism anchor";
}
