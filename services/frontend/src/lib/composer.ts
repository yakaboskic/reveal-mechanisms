import { ApiError, type Schema } from "./client";
import { currentReferenceModel } from "./reference";
export const emptyComposer = (): Schema<"Composer"> => ({ source_gap: null, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: "", model: currentReferenceModel(), selected_kgs: ["biomarkerkg", "prokn"], research_direction: "", context: "", hypotheses: "", upload_ids: [] });
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
  return { ...composer, model: response.automatic_anchors[0]?.factor.model || composer.model, eaggl_anchors: anchors };
}
/** Superseded anchors are removed without being dismissed; the composer adopts the current model. */
export function dropOutdatedAnchors(composer: Schema<"Composer">, outdated: ReadonlySet<string>, model = currentReferenceModel()): Schema<"Composer"> {
  return { ...composer, model, eaggl_anchors: composer.eaggl_anchors.filter(a => !outdated.has(a.reference.source_id)), dismissed_source_ids: composer.dismissed_source_ids.filter(id => !outdated.has(id)) };
}
export type CopiedResearchInputs = Partial<Pick<Schema<"Composer">, "selected_kgs" | "mechanism_subquery" | "research_direction" | "context" | "hypotheses" | "upload_ids">>;
/** Only an archive without a private request uses defaults. Owner read failures must stay visible. */
export async function archivedResearchInputs(requestId: string | null | undefined, load: (id: string) => Promise<Schema<"ResearchRequest">>, fallback: CopiedResearchInputs = {}): Promise<CopiedResearchInputs> {
  if (!requestId) return fallback;
  const { composer } = await load(requestId);
  return { selected_kgs: fallback.selected_kgs || composer.selected_kgs, mechanism_subquery: composer.mechanism_subquery,
    research_direction: composer.research_direction, context: composer.context, hypotheses: composer.hypotheses, upload_ids: composer.upload_ids };
}
/** A fresh editor keeps the chosen inquiry and documents while replacing superseded anchors. */
export function currentAnalysisComposer(gap: Schema<"SelectedGap">, settings: CopiedResearchInputs = {}): Schema<"Composer"> {
  const empty = emptyComposer();
  return { ...empty, source_gap: { ...gap }, mechanism_subquery: settings.mechanism_subquery || "", selected_kgs: settings.selected_kgs ? [...settings.selected_kgs] : empty.selected_kgs,
    research_direction: settings.research_direction || "", context: settings.context || "", hypotheses: settings.hypotheses || "", upload_ids: [...(settings.upload_ids || [])] };
}

type DraftClient = {
  draft?: (id: string) => Promise<Schema<"Draft">>;
  saveDraft: (draft: Schema<"Draft">, composer: Schema<"Composer">, key: string, name?: string) => Promise<Schema<"Draft">>;
  createDraft: (composer: Schema<"Composer">, key: string, name?: string) => Promise<Schema<"Draft">>;
};
/**
 * Save a composer snapshot: PATCH the saved draft, or create one. A saved draft that is gone (404:
 * anchored drafts are dropped at a reference cutover, or it was deleted in another tab) keeps the
 * edits as a new draft; `gone` runs first, so the dead draft is never saved again even if that fails.
 * `replaced` names the dead draft. Idempotency keys match those of a first save.
 */
export async function persistDraft(client: DraftClient, previous: Schema<"Draft"> | null, snapshot: Schema<"Composer">, key: (body: string) => string,
  { current = () => true, gone = () => {}, name, attempting }: { current?: () => boolean; gone?: () => void; name?: string; attempting?: (draft: Schema<"Draft"> | null, key: string) => void } = {}): Promise<{ draft: Schema<"Draft">; replaced: string | null }> {
  const persist = (draft: Schema<"Draft"> | null) => {
    const binding = name === undefined
      ? { ...(draft ? { draft: draft.id, version: draft.version } : {}), composer: snapshot }
      : { action: "save", draft: draft?.id, version: draft?.version, composer: snapshot, name };
    const requestKey = key(JSON.stringify(binding));
    attempting?.(draft, requestKey);
    return draft ? client.saveDraft(draft, snapshot, requestKey, name) : client.createDraft(snapshot, requestKey, name);
  };
  if (!previous) return { draft: await persist(null), replaced: null };
  try { return { draft: await persist(previous), replaced: null }; }
  catch (failure) {
    if (!(failure instanceof ApiError && failure.status === 404) || !current()) throw failure;
    // Upload validation can also return 404. Only a missing draft warrants replacement.
    if (client.draft) {
      let draftGone = false;
      try { await client.draft(previous.id); }
      catch (readFailure) { draftGone = readFailure instanceof ApiError && readFailure.status === 404; }
      if (!draftGone || !current()) throw failure;
    }
    gone();
    return { draft: await persist(null), replaced: previous.id };
  }
}

type WorkingDraftClient = {
  draft: (id: string) => Promise<Schema<"Draft">>;
  createWorkingDraft: (composer: Schema<"Composer">, key: string, sourceId?: string, sourceVersion?: number) => Promise<Schema<"Draft">>;
};
/** A deleted saved source cannot supply lineage; ready owner-scoped documents remain explicit inputs. */
export async function createSubmissionDraft(client: WorkingDraftClient, snapshot: Schema<"Composer">, key: string,
  sourceId?: string, sourceVersion?: number, current: () => boolean = () => true): Promise<Schema<"Draft">> {
  try { return await client.createWorkingDraft(snapshot, key, sourceId, sourceVersion); }
  catch (failure) {
    if (!(failure instanceof ApiError && failure.status === 404) || !sourceId || !current()) throw failure;
    let sourceGone = false;
    try { await client.draft(sourceId); }
    catch (readFailure) { sourceGone = readFailure instanceof ApiError && readFailure.status === 404; }
    if (!sourceGone || !current()) throw failure;
    return client.createWorkingDraft(snapshot, `${key}:without-source`);
  }
}
