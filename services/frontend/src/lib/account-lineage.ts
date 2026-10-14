import type { Schema } from "./client";
import { publicSourceHref } from "./provenance-view";

export type Lineage = Schema<"AccountProvenance">;
export type LineageRoute = Schema<"ProvenanceTrace">["route_classifications"][number];
/** Routes overlap: one claim can reach a dataset by several, so their counts are never added. */
export const routeLabels: Record<LineageRoute, string> = {
  proposition_reference: "Cited by the proposition",
  evidence_derivation: "Derived evidence",
  inherited_source_claim: "Inherited from a source claim",
  factor_projection: "Factor gene-set projection",
  trait_gwas: "Published GWAS of the trait",
};
export const lineageRoutes = Object.keys(routeLabels) as LineageRoute[];
const human = (code: string) => code.toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase());
const issueLabels: Record<string, string> = {
  MISSING_LINEAGE: "Lineage not recorded", REFERENCE_COVERAGE_INCOMPLETE: "Reference coverage incomplete",
  external_provenance_unavailable: "Portal GWAS lineage unavailable", reference_release_superseded: "Reference release superseded",
  trait_unavailable: "Trait not in the reference release", unlinked_inputs: "Inputs without recorded lineage", reference_tables_unavailable: "Reference tables unavailable",
};
export const issueLabel = (code: string) => issueLabels[code] || human(code);
const gapLabels: Record<string, string> = {
  not_a_gwas_trait: "Rare-disease gene list, with no GWAS", not_on_portal: "No bottom-line GWAS on the provenance portal",
  no_gwas_ancestry_artifact: "Bottom-line GWAS on the portal, but none of the ancestry the trace uses (Mixed)", no_upstream_organizations: "GWAS dataset names no organization",
};
export const gapLabel = (code: string) => gapLabels[code] || human(code);

/** A recorded ROR identifier as its public registry page. */
export function rorHref(value?: string | null) {
  const id = /^(?:ror:|https:\/\/ror\.org\/)([0-9a-z]{9})$/.exec(value || "")?.[1];
  return id ? `https://ror.org/${id}` : null;
}

/** Names and public pages of the traced nodes. A node href is only ever a public http(s) page. */
export function lineageIndex(value: Pick<Lineage, "nodes">) {
  const nodes = new Map(value.nodes.map(node => [node.id, node]));
  return {
    name: (id: string) => nodes.get(id)?.name || nodes.get(id)?.filename || id,
    href: (id: string) => publicSourceHref(nodes.get(id)?.href) || rorHref(nodes.get(id)?.ror),
    kind: (id: string) => nodes.get(id)?.kind || null,
  };
}

/** Coverage issues by code, most frequent first. */
export function issueCounts(issues: readonly { [key: string]: unknown }[]) {
  const counts = new Map<string, number>();
  for (const issue of issues) { const code = typeof issue.code === "string" ? issue.code : "unknown"; counts.set(code, (counts.get(code) || 0) + 1); }
  return [...counts].map(([code, count]) => ({ code, count })).sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

export const routeCounts = (row: Schema<"DatasetReuse">) => lineageRoutes.flatMap(route => row.classification_claim_counts[route] ? [{ route, label: routeLabels[route], count: row.classification_claim_counts[route] }] : []);

/** Reached datasets cover the whole account, not only the loaded page of traces. */
export const claimDatasets = (value: Pick<Lineage, "claims">, claimId: string) => value.claims.find(claim => claim.record.id === claimId)?.provenance.dataset_ids || null;

export class LineageChangedError extends Error {}
/** Later pages add traces; totals, reuse and the shared graph come complete with every page. */
export function appendLineagePage(previous: Lineage, next: Lineage): Lineage {
  if (previous.snapshot.id !== next.snapshot.id) throw new LineageChangedError("The account’s lineage changed while loading. Reload the first page.");
  const seen = new Set(previous.traces.map(trace => trace.id));
  return { ...next, traces: [...previous.traces, ...next.traces.filter(trace => !seen.has(trace.id))] };
}
