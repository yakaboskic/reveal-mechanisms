/** Claims to CFDE datasets, from the data_sources stored with a scientific account (GET /v1/accounts/{id}): the account is
 * traced once when it is saved, through the reference lineage of the gene sets, mechanisms, traits, files and claims it
 * cites. A trait's GWAS bottom-line artifact is traced too and is marked `gwas`, apart from the CFDE datasets. */
import type { CreditedOrganization } from "./organizations";
import type { Schema } from "./types";

type DataSources = Schema<"AccountDataSources">;
export type DataSourcesStatus = DataSources["status"];
export type CfdeDataset = {
  id: string; name: string; href: string | null; version: string | null; description: string | null;
  /** How the claims use it: data_input (primary data) or metadata_input (gene-identifier mapping). */
  roles: string[];
  /** The graph's claim ids it supports, in graph order. */
  claims: string[];
  /** What the claims reach it through: cited mechanisms, gene sets, files, or another claim of the account. */
  via: string[];
  organizations: CreditedOrganization[];
  /** Set for a KPN trait's GWAS bottom-line artifact (kind gwas_bottom_line), which a claim reaches through a trait
   * (route trait_gwas): the reference release names it and the Translator provenance portal holds it. It is traced
   * lineage, not a CFDE dataset. */
  gwas: { trait: string | null; ancestry: string | null } | null;
};
export type CfdeEdge = { from: string; to: string; key: string };
export type CfdeSources = { status: DataSourcesStatus; datasets: CfdeDataset[]; claimDatasets: Map<string, string[]>; edges: CfdeEdge[]; lowerBound: boolean; unverifiedClaims: Set<string> };

const statuses: DataSourcesStatus[] = ["complete", "incomplete", "unavailable", "not_traced"];
const roleNames: Record<string, string> = { data_input: "primary data", metadata_input: "gene-identifier mapping" };
export const datasetRole = (role: string) => roleNames[role] || role.replaceAll("_", " ");

/** A dataset's public source page; only http(s) locations are linked. */
export function datasetHref(location?: string | null) {
  if (!location) return null;
  try {
    const url = new URL(location);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}

function stored(value: unknown): DataSources | null {
  if (!value || typeof value !== "object") return null;
  return statuses.includes((value as { status?: unknown }).status as DataSourcesStatus) ? value as DataSources : null;
}

/** The CFDE datasets the graph's claims reach. claims: the graph's claims in order, each with the evidence ids it shows.
 * A dataset's edges leave the claim's evidence, which the dataset ultimately supports, or the claim when it has none.
 * Primary data comes first, then the datasets supporting the most claims. */
export function cfdeSources(value: unknown, claims: { id: string; evidence: string[] }[]): CfdeSources | null {
  const sources = stored(value);
  if (!sources) return null;
  const records = new Map((sources.datasets || []).map(item => [item.id, item]));
  const named = new Map((sources.organizations || []).map(item => [item.id, item]));
  const cited = new Map((sources.via || []).map(item => [item.id, item]));
  const graph = new Map(claims.map((claim, index) => [claim.id, { ...claim, number: index + 1 }]));
  const viaLabel = (id: string) => {
    const claim = graph.get(id), item = cited.get(id);
    if (claim) return `claim ${claim.number}`;
    if (item?.name || item?.filename) return item.name || item.filename || id;
    return item?.kind === "claim" ? "a source claim" : item?.kind === "mechanism" ? "a cited mechanism" : item?.kind === "gene_set" ? "a cited gene set" : item?.kind === "trait" ? "a cited trait" : "a cited source";
  };
  const datasets = new Map<string, CfdeDataset & { credits: Map<string, Set<string>> }>();
  const claimDatasets = new Map<string, string[]>(), edges = new Map<string, CfdeEdge>();
  for (const claim of sources.claims || []) {
    const node = graph.get(claim.claim_id);
    if (!node) continue;
    for (const connection of claim.datasets || []) {
      const id = connection.dataset_id, record = records.get(id);
      const dataset = datasets.get(id) || { id, name: record?.name || id, href: datasetHref(record?.location), version: record?.version || null,
        description: record?.description || null, roles: [], claims: [], via: [], organizations: [], credits: new Map<string, Set<string>>(),
        gwas: record?.kind === "gwas_bottom_line" ? { trait: record.kpn_trait_id || null, ancestry: record.ancestry_name || null } : null };
      datasets.set(id, dataset);
      for (const role of connection.input_roles || []) if (!dataset.roles.includes(role)) dataset.roles.push(role);
      if (!dataset.claims.includes(claim.claim_id)) dataset.claims.push(claim.claim_id);
      for (const item of connection.via_ids || []) { const label = viaLabel(item); if (!dataset.via.includes(label)) dataset.via.push(label); }
      for (const credit of connection.organizations || []) {
        const roles = dataset.credits.get(credit.organization_id) || new Set<string>();
        for (const role of credit.roles || []) roles.add(role);
        dataset.credits.set(credit.organization_id, roles);
      }
      const listed = claimDatasets.get(claim.claim_id) || [];
      if (!listed.includes(id)) claimDatasets.set(claim.claim_id, [...listed, id]);
      for (const from of node.evidence.length ? node.evidence.map(item => "evidence:" + item) : ["claim:" + claim.claim_id]) {
        const key = `cfde:${from}>${id}`;
        if (!edges.has(key)) edges.set(key, { from, to: "dataset:" + id, key });
      }
    }
  }
  const primary = (dataset: CfdeDataset) => dataset.gwas ? 2 : dataset.roles.includes("data_input") ? 0 : 1;
  const ordered = [...datasets.values()].map(({ credits, ...dataset }) => ({ ...dataset,
    organizations: [...credits].map(([key, roles]) => ({ id: key, name: named.get(key)?.name || null, ror: named.get(key)?.ror || null, roles: [...roles].sort() })) }))
    .sort((a, b) => primary(a) - primary(b) || b.claims.length - a.claims.length || a.name.localeCompare(b.name));
  // An incomplete claim record, or a trace that never finished, does not show that CFDE support is absent.
  const unverifiedClaims = new Set<string>();
  if (sources.status === "unavailable" || sources.status === "not_traced") {
    for (const claim of claims) unverifiedClaims.add(claim.id);
  } else {
    for (const claim of sources.claims || []) {
      if (!graph.has(claim.claim_id) || (claim.datasets || []).length || claim.resolution !== "incomplete") continue;
      unverifiedClaims.add(claim.claim_id);
    }
  }
  return { status: sources.status, datasets: ordered, claimDatasets, edges: [...edges.values()], unverifiedClaims,
    lowerBound: sources.status === "incomplete" || !!sources.coverage?.counts_are_lower_bounds };
}

/** A dataset node's popup: what the dataset is, how the account's claims use it, and who is credited for it. */
export function datasetTitle(dataset: CfdeDataset, lowerBound: boolean) {
  const claims = `${dataset.claims.length} claim${dataset.claims.length === 1 ? "" : "s"}`;
  const credited = dataset.organizations.map(item => (item.name || item.id) + (item.roles?.length ? ` (${item.roles.map(role => role.replaceAll("_", " ")).join(", ")})` : ""));
  const gwas = dataset.gwas;
  return [
    dataset.name,
    gwas ? `Trait GWAS bottom line${gwas.trait ? ` of ${gwas.trait}` : ""}${gwas.ancestry ? ` (${gwas.ancestry})` : ""}. The reference release names it; the Translator provenance portal holds it.` : "",
    dataset.version ? `Version: ${dataset.version}` : "",
    dataset.description || "",
    gwas ? `Reached by ${claims} through a trait.` : dataset.roles.length ? `Used as ${dataset.roles.map(datasetRole).join(" and ")} by ${claims}.` : `Reached by ${claims}.`,
    dataset.via.length ? `Reached through: ${dataset.via.join("; ")}.` : "",
    credited.length ? `Credited: ${credited.join("; ")}.` : "",
    lowerBound ? "Some cited sources have no recorded lineage, so more datasets may contribute." : "",
  ].filter(Boolean).join("\n");
}
