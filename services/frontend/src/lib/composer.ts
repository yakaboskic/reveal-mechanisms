import type { Schema } from "./client";
export const emptyComposer = (): Schema<"Composer"> => ({ source_gap: null, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: "", model: "cfde-inc-v2", selected_kgs: ["biomarkerkg", "prokn"], research_direction: "", context: "", hypotheses: "", upload_ids: [] });
export const normalizedComposer = (composer: Schema<"Composer">): Schema<"Composer"> => ({ ...emptyComposer(), ...composer });
export const composerEqual = (a: Schema<"Composer">, b: Schema<"Composer">) => JSON.stringify(normalizedComposer(a)) === JSON.stringify(normalizedComposer(b));
export const selectedGap = (gap: Schema<"GapRecord">): Schema<"SelectedGap"> => ({ id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision });
export const factorSelection = (factor: Schema<"EagglFactor">, origin: "manual" | "automatic", suggestion_id: string | null = null): Schema<"Selection"> => ({ reference: { source: "eaggl", source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id }, origin, suggestion_id });
export function removeAnchor(composer: Schema<"Composer">, id: string): Schema<"Composer"> {
  return { ...composer, eaggl_anchors: composer.eaggl_anchors.filter(a => a.reference.source_id !== id), dismissed_source_ids: Array.from(new Set([...composer.dismissed_source_ids, id])) };
}
export function applySuggestions(composer: Schema<"Composer">, response: Schema<"Suggestions">): Schema<"Composer"> {
  const anchors = [...composer.eaggl_anchors];
  let automaticCount = anchors.filter(a => a.origin === "automatic").length;
  for (const { factor } of response.automatic_anchors) {
    if (automaticCount >= 5 || anchors.length >= 10) break;
    if (composer.dismissed_source_ids.includes(factor.source_id) || anchors.some(a => a.reference.source_id === factor.source_id)) continue;
    anchors.push(factorSelection(factor, "automatic", response.suggestion_id)); automaticCount++;
  }
  return { ...composer, eaggl_anchors: anchors };
}
