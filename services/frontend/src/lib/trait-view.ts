import { ApiError, type Schema } from "./client";
import { factorHref, traitHref, traitPageHref } from "./factor-links";

export type BetaSort = Schema<"TraitGeneSets">["sort"];
export type LinkSort = Schema<"FactorTraitLinks">["sort"];
export type LinkScope = Schema<"FactorTraitLinks">["scope"];
export const betaSortLabels: Record<BetaSort, string> = { beta_uncorrected: "Uncorrected beta", beta: "Corrected beta", avg_postp: "Average posterior probability" };
export const linkSortLabels: Record<LinkSort, string> = { phewas_q: "PheWAS q, lowest first", phewas_beta: "PheWAS beta, highest first", nnls_loading: "NNLS loading, highest first" };
/** The release's PIGEAN betas mode and eaggl trait linkage; distinct from BioIndex small-model values. */
export const metricDefinitions = {
  beta_uncorrected: "PIGEAN betas-mode effect of the gene set on the trait before PIGEAN’s correction (one fit per CFDE library). Not the BioIndex small-model value.",
  beta: "PIGEAN betas-mode effect after PIGEAN’s correction for correlated gene sets. Many kept gene sets are exactly 0.",
  avg_postp: "PIGEAN betas-mode average posterior probability that the gene set is associated with the trait.",
  library_rank: "Rank of the gene set within its library for the trait, by uncorrected beta.",
  nnls_loading: "eaggl trait linkage: the phenotype’s support projected onto the fitted trait’s factors by fixed-W NNLS. Descriptive, with no p-value.",
  phewas_beta: "eaggl factor-PheWAS coefficient of the phenotype’s hit genes on the factor’s gene loadings, adjusted for the fitted trait’s own support.",
  phewas_p_onesided: "eaggl factor-PheWAS one-sided p-value for enrichment of the phenotype in the factor.",
  phewas_q: "Benjamini–Hochberg q of the one-sided p over every factor–phenotype test in the release. Links are kept at q ≤ 0.05.",
} as const;

/** Compact for tables; the exact reported number stays available as a title. */
export function metricValue(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "Not available";
  return value === 0 || Math.abs(value) >= 1e-3 ? String(Number(value.toPrecision(4))) : value.toExponential(2);
}

export const gwasReasons: Record<Schema<"TraitDetail">["gwas"]["status"], string> = {
  available: "Bottom-line GWAS on the Translator provenance portal",
  not_a_gwas_trait: "This trait is a rare-disease gene list, so it has no GWAS.",
  not_on_portal: "The Translator provenance portal lists no bottom-line GWAS for this trait.",
  no_gwas_ancestry_artifact: "The portal lists bottom-line GWAS for this trait, but none of the ancestry the trace uses (Mixed).",
};

/** Atlas traits open in the app; other phenotypes are text, with the KPN catalog page when they have a KPN id. */
export function phenotypeTarget(link: Pick<Schema<"FactorTraitLink">, "phenotype_kpn_trait_id" | "is_atlas_trait">, from?: string | null) {
  const id = link.phenotype_kpn_trait_id;
  if (!id) return { page: null, catalog: null };
  return { page: link.is_atlas_trait ? traitPageHref(id, from) : null, catalog: traitHref(id) };
}

/** The release keeps links at this Benjamini–Hochberg q. */
export const linkThreshold = 0.05;
/** A factor's own fitted trait is reported apart: its q is recorded even above the threshold, and most factors pass it. */
export function ownTraitStatus(summary?: { own_trait_q: number | null } | null) {
  if (!summary) return null;
  const q = summary.own_trait_q;
  return q == null ? "Its own trait was not tested" : q <= linkThreshold ? `Linked to its own trait (q ${metricValue(q)})` : `Not linked to its own trait (q ${metricValue(q)})`;
}

/** Pages are offset-based: a boundary row repeated by a concurrent publish is shown once. */
export function appendRows<T>(previous: readonly T[], incoming: readonly T[], key: (row: T) => string) {
  const seen = new Set(previous.map(key));
  return [...previous, ...incoming.filter(row => { const id = key(row); if (seen.has(id)) return false; seen.add(id); return true; })];
}
export const betaKey = (row: Schema<"TraitGeneSet">) => `${row.kpn_trait_id}|${row.gene_set_id}`;
export const linkKey = (row: Schema<"FactorTraitLink">) => `${row.source_id}|${row.phenotype}`;

/** A Mechanism id opens its factor: current, an archived snapshot (410), or null to keep the raw record (404 and other errors). */
export function mechanismHref(outcome: Schema<"MechanismObject"> | unknown, from?: string | null): string | null {
  if (outcome instanceof ApiError) {
    const archived = outcome.status === 410 ? outcome.problem?.archived_reference_factor : null;
    return archived ? factorHref(archived.source_id, null, { archiveId: archived.archive_id, from }) : null;
  }
  const current = outcome as Partial<Schema<"MechanismObject">> | null;
  return current?.status === "current" && typeof current.source_id === "string" ? factorHref(current.source_id, null, { from }) : null;
}
