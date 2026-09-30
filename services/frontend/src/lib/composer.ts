import { ApiError, type Schema } from "./client";
import { currentReferenceModel } from "./reference";
// The model follows the reference generation the API serves (legacy cfde-inc-v2 until a reload).
export const emptyComposer = (): Schema<"Composer"> => ({ source_gap: null, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: "", model: currentReferenceModel(), selected_kgs: ["biomarkerkg", "prokn"] });
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
  return { ...composer, model: response.automatic_anchors[0]?.factor.model || composer.model, eaggl_anchors: anchors };
}
/** Superseded anchors are removed without being dismissed; the composer adopts the current model. */
export function dropOutdatedAnchors(composer: Schema<"Composer">, outdated: ReadonlySet<string>, model = currentReferenceModel()): Schema<"Composer"> {
  return { ...composer, model, eaggl_anchors: composer.eaggl_anchors.filter(a => !outdated.has(a.reference.source_id)), dismissed_source_ids: composer.dismissed_source_ids.filter(id => !outdated.has(id)) };
}
/** "Start a new analysis on this gap with current factors": gap, inquiry and KGs copied, no anchors. */
export function currentAnalysisComposer(gap: Schema<"SelectedGap">, settings: Partial<Pick<Schema<"Composer">, "selected_kgs" | "mechanism_subquery">> = {}): Schema<"Composer"> {
  const empty = emptyComposer();
  return { ...empty, source_gap: { ...gap }, mechanism_subquery: settings.mechanism_subquery || "", selected_kgs: settings.selected_kgs ? [...settings.selected_kgs] : empty.selected_kgs };
}

type DraftClient = {
  saveDraft: (draft: Schema<"Draft">, composer: Schema<"Composer">, key: string) => Promise<Schema<"Draft">>;
  createDraft: (composer: Schema<"Composer">, key: string) => Promise<Schema<"Draft">>;
};
/**
 * Save a composer snapshot: PATCH the saved draft, or create one. A saved draft that is gone (404:
 * anchored drafts are dropped at a reference cutover, or it was deleted in another tab) keeps the
 * edits as a new draft; `gone` runs first, so the dead draft is never saved again even if that fails.
 * `replaced` names the dead draft. Idempotency keys match those of a first save.
 */
export async function persistDraft(client: DraftClient, previous: Schema<"Draft"> | null, snapshot: Schema<"Composer">, key: (body: string) => string,
  { current = () => true, gone = () => {} }: { current?: () => boolean; gone?: () => void } = {}): Promise<{ draft: Schema<"Draft">; replaced: string | null }> {
  const create = () => client.createDraft(snapshot, key(JSON.stringify({ composer: snapshot })));
  if (!previous) return { draft: await create(), replaced: null };
  try { return { draft: await client.saveDraft(previous, snapshot, key(JSON.stringify({ draft: previous.id, version: previous.version, composer: snapshot }))), replaced: null }; }
  catch (failure) {
    if (!(failure instanceof ApiError && failure.status === 404) || !current()) throw failure;
    gone();
    return { draft: await create(), replaced: previous.id };
  }
}
