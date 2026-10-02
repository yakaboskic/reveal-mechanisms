import type { Schema } from "@/lib/client";

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const entries = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
export const readableTerm = (value: string) => value.toLowerCase().replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
function term(value: unknown): string {
  if (typeof value === "string") return text(value);
  const item = record(value), ontology = record(item.term);
  return text(item.preferred_term) || text(item.name) || text(item.label) || text(ontology.label) || text(ontology.id);
}

export function evidenceUrl(reference: string): string | null {
  if (/^PMID:\d+$/i.test(reference)) return `https://pubmed.ncbi.nlm.nih.gov/${reference.split(":")[1]}/`;
  if (/^PMC(?:ID)?:PMC\d+$/i.test(reference)) return `https://pmc.ncbi.nlm.nih.gov/articles/${reference.split(":")[1]}/`;
  if (/^DOI:10\.\d{4,9}\/.+/i.test(reference)) return `https://doi.org/${reference.slice(4).split("/").map(encodeURIComponent).join("/")}`;
  try {
    const url = new URL(reference);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export type GapEvidence = { reference: string; title: string; href: string | null; badges: string[]; snippet: string; explanation: string };
function evidence(value: unknown): GapEvidence[] {
  return entries(value).flatMap(value => {
    const item = record(value), reference = text(item.reference), snippet = text(item.snippet), explanation = text(item.explanation);
    if (!reference && !snippet && !explanation) return [];
    return [{ reference, title: text(item.reference_title), href: evidenceUrl(reference), snippet, explanation,
      badges: [item.supports, item.directness, item.evidence_source].map(text).filter(Boolean).map(readableTerm) }];
  });
}

export type ExperimentItem = { name: string; description: string; metadata: { label: string; value: string }[] };
const itemFields = {
  target: "Target", organism: "Organism", experimental_model_type: "Model type", tissue_term: "Tissue", cell_types: "Cell types",
  genes: "Genes", chemical_entities: "Chemical entities", biological_processes: "Biological processes", direction: "Direction",
  interpretation: "Interpretation", conditions: "Conditions", cell_source: "Cell source", culture_system: "Culture system",
  accession: "Accession", publication: "Publication", notes: "Notes",
};
function experimentItem(value: unknown): ExperimentItem | null {
  if (typeof value === "string") return text(value) ? { name: text(value), description: "", metadata: [] } : null;
  const item = record(value);
  const name = term(item) || text(item.title) || text(item.experiment_id), description = text(item.description);
  const metadata = Object.entries(itemFields).flatMap(([key, label]) => {
    const value = (Array.isArray(item[key]) ? entries(item[key]).map(term).filter(Boolean).join("; ") : term(item[key]));
    return value ? [{ label, value }] : [];
  });
  return name || description || metadata.length ? { name, description, metadata } : null;
}
const experimentGroups = {
  model_systems: "Model systems", perturbations: "Perturbations", assays: "Assays", readouts: "Readouts", controls: "Controls",
  datasets: "Datasets", would_support: "Would support", would_refute: "Would refute", supporting_outcome: "Supporting outcomes", refuting_outcome: "Refuting outcomes",
};

export function gapContext(gap: Schema<"GapRecord">) {
  const raw = record(gap.source_detail.raw);
  const experiments = entries(raw.proposed_experiments).flatMap(value => {
    const item = record(value), name = text(item.name) || text(item.experiment_id), description = text(item.description);
    if (!name && !description) return [];
    return [{ name: name || "Proposed experiment", description, type: term(item.experiment_type), decision: text(item.decision_criterion), notes: text(item.notes),
      groups: Object.entries(experimentGroups).flatMap(([key, label]) => {
        const items = entries(item[key]).map(experimentItem).filter((value): value is ExperimentItem => value !== null);
        return items.length ? [{ label, items }] : [];
      }), evidence: evidence(item.evidence) }];
  });
  const metadata = [
    { label: "Status", value: gap.source.status ? readableTerm(gap.source.status) : "Not specified" },
    { label: "Kind", value: readableTerm(text(raw.kind) || gap.object.gap_kind || "") },
    { label: "Disease or entry", value: gap.source.disease_label || "" },
    { label: "Scope", value: gap.object.scope && gap.object.scope !== gap.source.disease_label ? gap.object.scope : "" },
    { label: "Posed by", value: text(raw.posed_by) },
    { label: "Posed on", value: text(raw.posed_date) },
    { label: "Resolved on", value: text(raw.resolved_date) },
  ].filter(item => item.value);
  const source = gap.source_detail.source_file;
  const safeSource = /^kb\/[a-z_]+\/[^/\\]+\.ya?ml$/.test(source) && !source.split("/").includes("..");
  const sourceUrl = safeSource ? `https://github.com/monarch-initiative/dismech/blob/main/${source.split("/").map(encodeURIComponent).join("/")}` : null;
  const [, kind, file] = safeSource ? source.split("/") : [];
  // Match DisMech's export/utils.py slugify for disorder pages; module pages use the filename.
  const slug = kind === "disorders" && gap.source.disease_label ? gap.source.disease_label.replaceAll(" ", "_").replaceAll("/", "_").replace(/[()]/g, "") : kind === "modules" ? file.replace(/\.ya?ml$/, "") : "";
  const sourcePage = slug ? `https://dismech.monarchinitiative.org/pages/${kind}/${encodeURIComponent(slug)}.html${text(raw.discussion_id) ? "#" + encodeURIComponent(text(raw.discussion_id)) : ""}` : null;
  return { metadata, context: text(raw.rationale) || gap.object.gap_description, notes: text(raw.notes), resolution: text(raw.resolution_note),
    experiments, evidence: evidence(raw.evidence), sourceUrl, sourcePage };
}
