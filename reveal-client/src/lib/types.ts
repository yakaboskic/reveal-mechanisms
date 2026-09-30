import type { components } from "./api.generated";

export type Schema<K extends keyof components["schemas"]> = components["schemas"][K];
export type Me = Schema<"Me">;
export type Draft = Schema<"Draft">;
export type Composer = Schema<"Composer">;
export type Gap = Schema<"GapRecord">;
export type Factor = Schema<"EagglFactor">;
export type Selection = Schema<"Selection">;
export type Job = Schema<"Job">;
export type JobEvent = Schema<"JobEvent">;
export type WorkspaceEvent = Schema<"WorkspaceEvent">;
// OpenAPI permits partial budget overrides; generated defaults are marked required.
export type AnalysisInput = Omit<Schema<"AnalysisJobInput">, "budgets"> & { budgets?: Partial<Schema<"JobBudgets">> };
export type Page<T> = { items: T[]; page: Schema<"Page"> };
export const emptyComposer = (): Composer => ({ source_gap: null, eaggl_anchors: [],
  dismissed_source_ids: [], mechanism_subquery: "", model: "cfde-inc-v2", selected_kgs: ["biomarkerkg"] });
export const terminal = (status: string) => ["succeeded", "failed", "cancelled", "insufficient_evidence"].includes(status);
export const factorSelection = (factor: Factor, suggestionId: string): Selection => ({
  reference: { source: "eaggl", source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id },
  origin: "automatic", suggestion_id: suggestionId,
});
