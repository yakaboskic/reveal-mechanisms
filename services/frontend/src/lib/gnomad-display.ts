import type { Schema } from "./client";

export type LoadingSort = Schema<"FactorLoadings">["sort"];
export type GeneConstraint = Schema<"GnomadGeneConstraint">;
export const loadingSortLabels: Record<LoadingSort, string> = {
  alphabetical: "Alphabetical", loading: "Strongest first", gnomad_pli: "pLI: highest first",
  gnomad_loeuf: "LOEUF: lowest first", gnomad_mis_z: "Missense Z: highest first",
};
export const constraintDefinitions = {
  pli: "pLI: probability of loss-of-function intolerance; higher values indicate stronger constraint.",
  loeuf: "LOEUF: loss-of-function observed/expected upper confidence bound; lower values indicate stronger constraint.",
  mis_z: "Missense Z: depletion of observed missense variation relative to expectation; higher values indicate stronger constraint.",
};
export function constraintValue(annotation: GeneConstraint | null | undefined, metric: "pli" | "loeuf" | "mis_z" | "lof_oe") {
  const value = annotation?.status === "selected" ? annotation[metric] : null;
  return value != null && Number.isFinite(value) ? String(value) : "Not reported";
}
export function constraintSelection(annotation: GeneConstraint | null | undefined) {
  if (!annotation) return "No matching gene in this import";
  if (annotation.status === "selected") return annotation.selection_method === "mane_select" ? "MANE Select transcript" : annotation.selection_method === "canonical" ? "Canonical transcript" : "Selected transcript";
  return ({ ambiguous_gene: "Ambiguous gene mapping", ambiguous_transcript: "Ambiguous transcript selection", no_primary_transcript: "No MANE Select or canonical transcript", no_ensembl_gene: "No Ensembl gene mapping" } as const)[annotation.status];
}
export function constraintSourceHref(value?: string | null) {
  if (!value) return null;
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : null; } catch { return null; }
}
export function constraintLinks(annotation: GeneConstraint | null | undefined, version?: string) {
  const links: { label: string; href: string }[] = [];
  if (annotation?.gene_id && /^ENSG\d+(?:\.\d+)?$/.test(annotation.gene_id)) {
    const url = new URL(`https://gnomad.broadinstitute.org/gene/${encodeURIComponent(annotation.gene_id.split(".")[0])}`);
    if (/^v?4(?:\.|$)/i.test(version || "")) url.searchParams.set("dataset", "gnomad_r4");
    links.push({ label: "gnomAD gene", href: url.href });
  }
  if (annotation?.transcript && /^ENST\d+(?:\.\d+)?$/.test(annotation.transcript)) {
    const url = new URL("https://www.ensembl.org/Homo_sapiens/Transcript/Summary"); url.searchParams.set("t", annotation.transcript);
    links.push({ label: "Ensembl transcript", href: url.href });
  }
  return links;
}
export function constraintCell(annotation: GeneConstraint | null | undefined, source: Schema<"GnomadImport"> | null | undefined) {
  if (!source) return {};
  return {
    details: [{ label: "pLI", value: constraintValue(annotation, "pli") }, { label: "LOEUF", value: constraintValue(annotation, "loeuf") }, { label: "Missense Z", value: constraintValue(annotation, "mis_z") }],
    description: [`gnomAD ${source.version}`, constraintSelection(annotation), annotation?.transcript, annotation?.flags.length ? `Flags: ${annotation.flags.join(", ")}` : null].filter(Boolean).join(" · "),
    links: constraintLinks(annotation, source.version),
  };
}
