import type { components } from "./api.generated";

/**
 * Reference generations (docs/reference-reload.md). Work built on a superseded
 * EAGGL reference carries an `archive` stamp; deployments that never reloaded
 * reference data serve the legacy model and never stamp anything.
 */
type Schemas = components["schemas"];
export type ReferenceModel = Schemas["EagglFactor"]["model"];
export type ReferenceArchive = Schemas["ReferenceArchive"];
export type ArchivedAnchor = Schemas["ReferenceArchiveAnchor"];
export type ArchivedFactor = Schemas["ArchivedReferenceFactor"];
export type ReferenceState = "all" | "current" | "archived";
export const referenceModels: readonly ReferenceModel[] = ["cfde-inc-v2", "eaggl-capped-v1"];
/** Served until the first reference reload. */
export const legacyReferenceModel: ReferenceModel = "cfde-inc-v2";
export const referenceStates: readonly ReferenceState[] = ["all", "current", "archived"];
export const referenceStateLabels: Record<ReferenceState, string> = { all: "All", current: "Current", archived: "Outdated reference" };
const modelKey = "reveal:reference-model";

export const isReferenceModel = (value: unknown): value is ReferenceModel => referenceModels.includes(value as ReferenceModel);
export const isArchived = (item?: { archive?: ReferenceArchive | null } | null) => item?.archive?.status === "archived";
/** The model segment of a public factor id: factor:{group}:{trait}:{model}:{FactorN}. */
export function modelOfSourceId(sourceId?: string | null): ReferenceModel | null {
  const model = /^factor:[^:]+:[^:]+:([^:]+):Factor\d+$/.exec(sourceId || "")?.[1];
  return isReferenceModel(model) ? model : null;
}
/** Only the listed states are sent; `all` is the API default and is omitted. */
export const referenceQuery = (state?: ReferenceState) => state && state !== "all" ? state : undefined;
export const parseReferenceState = (value?: string | null): ReferenceState => referenceStates.includes(value as ReferenceState) ? value as ReferenceState : "all";

/** The model the API last served in this browser; null until a factor record is seen. */
export function observedReferenceModel(): ReferenceModel | null {
  try { const value = localStorage.getItem(modelKey); return isReferenceModel(value) ? value : null; }
  catch { return null; }
}
export function observeReferenceModel(model?: string | null) {
  if (!isReferenceModel(model)) return;
  try { if (localStorage.getItem(modelKey) !== model) localStorage.setItem(modelKey, model); } catch { /* Storage is optional. */ }
}
/** Records only EAGGL factors: every one comes from the active reference generation. */
export function observeFactors(records: readonly { source: string; model?: string }[]) {
  const factor = records.find(record => record.source === "eaggl" && isReferenceModel(record.model));
  if (factor) observeReferenceModel(factor.model);
}
export const currentReferenceModel = (): ReferenceModel => observedReferenceModel() || legacyReferenceModel;
/** True once this browser has seen a deployment serve a post-reload model. */
export const referenceReloaded = () => { const model = observedReferenceModel(); return !!model && model !== legacyReferenceModel; };

type ModelComposer = { model: ReferenceModel; eaggl_anchors: { reference: { source_id: string } }[] };
/**
 * A browser-stored composer from a superseded model is discarded (null) when it
 * holds anchors; one without anchors keeps its gap and adopts the current model.
 */
export function currentComposer<T extends ModelComposer>(composer: T, current = observedReferenceModel()): T | null {
  if (!current) return composer;
  const stale = composer.model !== current || composer.eaggl_anchors.some(anchor => (modelOfSourceId(anchor.reference.source_id) || current) !== current);
  if (!stale) return composer;
  return composer.eaggl_anchors.length ? null : { ...composer, model: current };
}

/** User-facing copy for reference reload problems; null keeps the server detail. */
export function referenceProblemMessage(status: number, code?: string | null): string | null {
  if (code === "REFERENCE_RELOAD_IN_PROGRESS") return "EAGGL reference data is being updated. Your work is kept; please try again in a few minutes.";
  if (code !== "REFERENCE_GENERATION_SUPERSEDED") return null;
  if (status === 410) return "This mechanism belongs to an outdated EAGGL reference and is no longer served.";
  return "These mechanism anchors come from an outdated EAGGL reference and can no longer be analysed. Start a new analysis on this knowledge gap with current factors.";
}

export type OutdatedAnchor = { source_id: string; name: string; trait: string | null; archive_id: string | null };
const subtitleTrait = (subtitle: unknown) => typeof subtitle === "string" ? subtitle.replace(/\s*\(Factor\d+\)\s*$/i, "").trim() || null : null;
/** A KPN snapshot's phenotype name (reference_factors.metadata.kpn); its `trait` is the EAGGL/portal trait code. */
const kpnPhenotype = (metadata: unknown) => {
  const name = (metadata as { kpn?: { phenotype_name?: unknown } } | null | undefined)?.kpn?.phenotype_name;
  return typeof name === "string" && name.trim() || null;
};
export function traitOfSourceId(sourceId?: string | null): string | null {
  const id = sourceId || "";
  const kpn = /^factor:kpn:(\d{7}):[^:]+:Factor\d+$/.exec(id)?.[1];
  return kpn ? `KPN.TRAIT:${kpn}` : /^factor:portal:([^:]+):[^:]+:Factor\d+$/.exec(id)?.[1] || null;
}
/** Display for an original anchor frozen in an archive stamp. */
export const outdatedFromAnchor = (anchor: ArchivedAnchor): OutdatedAnchor => ({
  source_id: anchor.source_id, name: anchor.label?.trim() || anchor.name?.trim() || "Mechanism anchor",
  trait: anchor.trait?.trim() || traitOfSourceId(anchor.source_id), archive_id: anchor.archived_reference_factor_id,
});
/** Display for the frozen factor a 410 mechanism read returns (null when none was captured). */
export const outdatedFromFactor = (sourceId: string, factor?: ArchivedFactor | null, fallback?: { name?: string | null; trait?: string | null }): OutdatedAnchor => ({
  source_id: sourceId, name: factor?.label?.trim() || factor?.mechanism.name?.trim() || fallback?.name?.trim() || "Mechanism anchor",
  trait: subtitleTrait(factor?.metadata.subtitle) || kpnPhenotype(factor?.metadata) || fallback?.trait?.trim() || factor?.trait || traitOfSourceId(sourceId),
  archive_id: factor?.archive_id || null,
});
export const archivedDate = (archive: ReferenceArchive) => {
  const date = new Date(archive.archived_at);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
};
/** Public, reference-data download of a frozen factor snapshot through the gateway. */
export const referenceFactorHref = (archiveId: string) => `/api/backend/v1/reference-factors/${encodeURIComponent(archiveId)}`;

/**
 * A reference cutover's public catalog event. The backend names it `reference` (a vector_active change);
 * publishing or unpublishing work emits other catalog events, which never need an anchor recheck.
 */
export const isReferenceReload = (event?: { collections: readonly string[]; entity_id: string } | null) =>
  !!event && event.collections.includes("catalog") && event.entity_id === "reference";
/**
 * Each API process notices a cutover on its next generation poll (every 5 s) and then reloads its catalog,
 * so the event can arrive before the new generation is served: recheck now and again after the swap.
 */
export const referenceRecheckDelaysMs: readonly number[] = [0, 7_000, 30_000, 120_000];
export function referenceRechecker(check: () => void, delays: readonly number[] = referenceRecheckDelaysMs) {
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const cancel = () => { timers.forEach(clearTimeout); timers.clear(); };
  return {
    /** A new cutover event restarts the schedule. */
    schedule() {
      cancel();
      for (const ms of delays) { const timer = setTimeout(() => { timers.delete(timer); check(); }, ms); timers.add(timer); }
    },
    cancel,
  };
}

/** Outdated anchors are keyed by exact revision: a later generation may reuse a KPN public id. */
export const anchorKey = (reference: { source_id: string; source_revision: string }) => `${reference.source_id}\u0000${reference.source_revision}`;
