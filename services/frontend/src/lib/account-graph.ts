import type { Schema } from "./client";

// The inspector also accepts richer scientific types, while the overview only
// renders account, claim, dataset and file nodes.
export type AccountGraphKind = "account" | "claim" | "evidence" | "dataset" | "file" | "activity" | "record";
export type AccountGraphNode = {
  id: string; objectId: string; label: string; kind: AccountGraphKind;
  relation: string; direction?: string; children: AccountGraphNode[];
  missing: boolean; cycle: boolean; limited: boolean; shared: number;
  missingReferences: string[];
  shortLabel?: string;
};
type Node = { id: string; [key: string]: unknown };
type Entry = { node: Node; kind: AccountGraphKind };
const kinds: Record<string, AccountGraphKind> = { scientific_accounts: "account", claims: "claim", evidence_items: "evidence", datasets: "dataset", files: "file", c2m2_files: "file", activities: "activity" };
const ids = (value: unknown): string[] => (Array.isArray(value) ? value : [value]).filter((item): item is string => typeof item === "string" && !!item);
const text = (value: unknown) => typeof value === "string" ? value : "";
export const graphKindLabel = (kind: AccountGraphKind) => ({ account: "Scientific account", claim: "Claim", evidence: "Evidence use", dataset: "Dataset", file: "File", activity: "Activity", record: "Source record" })[kind];
export function accountGraphLabel(node: AccountGraphNode, length = 48) {
  const label = (node.shortLabel || node.label).replace(/\s+/g, " ").trim();
  if (label.length <= length) return label;
  const prefix = label.slice(0, length - 1), boundary = prefix.lastIndexOf(" ");
  return `${prefix.slice(0, boundary > length / 2 ? boundary : prefix.length)}…`;
}

/** Evidence and activity paths are traversed but never drawn as extra circles.
 * Dataset containment uses actual membership, not proximity or a shared job.
 * Repeated display occurrences retain one canonical scientific identity. */
export function buildAccountHierarchy(result: Schema<"AccountResult">, maxDepth = 3, maxOccurrences = 600): AccountGraphNode {
  const records = new Map<string, Entry>();
  for (const [group, rows] of Object.entries(result.document)) {
    if (!Array.isArray(rows) || group.endsWith("_edges")) continue;
    for (const node of rows) if (node && typeof node === "object" && "id" in node && typeof node.id === "string") records.set(node.id, { node: node as Node, kind: kinds[group] || "record" });
  }
  const edges = Object.entries(result.document).flatMap(([group, rows]) => group.endsWith("_edges") && Array.isArray(rows) ? rows as { subject: string; predicate: string; object: string }[] : []);
  const outgoing = new Map<string, typeof edges>();
  for (const edge of edges) outgoing.set(edge.subject, [...(outgoing.get(edge.subject) || []), edge]);
  const inferKind = (id: string): AccountGraphKind => id.startsWith("dapper:Claim.") ? "claim" : id.startsWith("dapper:EvidenceItem.") ? "evidence" : id.startsWith("dapper:Dataset.") ? "dataset" : id.startsWith("dapper:File.") || id.startsWith("dapper:C2M2File.") ? "file" : id.startsWith("dapper:Activity.") ? "activity" : "record";
  const kindOf = (id: string) => records.get(id)?.kind || inferKind(id);
  const entityLabel = (id: string): string => {
    const entity = records.get(id)?.node;
    const name = text(entity?.name) || text(entity?.label);
    if (id.startsWith("dapper:Mechanism.")) return name.match(/\bFactor\s*\d+\b/i)?.[0] || name;
    if (id.startsWith("dapper:GeneSet.")) {
      const module = name.match(/\bgeneM\d+\b/i)?.[0];
      if (module) return module;
      return [...new Set(name.split("|").map(part => part.split("/")[0].trim()))].join("/");
    }
    if (name) return name;
    if (id.startsWith("urn:cfde:gene:") || id.startsWith("urn:cfde:trait:")) return id.split(":").at(-1)!.replace(/([A-Z])in([A-Z])/g, "$1-in-$2");
    return "";
  };
  const members = (id: string) => {
    const node = records.get(id)?.node;
    return [...new Set([...ids(node?.has_file), ...ids(node?.has_drs_object), ...(outgoing.get(id) || []).filter(edge => ["prov:hadMember", "dapper:hasFile"].includes(edge.predicate)).map(edge => edge.object)])].filter(id => kindOf(id) === "file");
  };
  const datasetsForFile = new Map<string, string[]>();
  for (const id of new Set([...records.keys(), ...outgoing.keys()])) if (kindOf(id) === "dataset") for (const file of members(id)) datasetsForFile.set(file, [...(datasetsForFile.get(file) || []), id]);
  const references = (id: string) => {
    const entry = records.get(id), node = entry?.node;
    if (!node) return [];
    const links: { id: string; computational: boolean }[] = [];
    const add = (value: unknown, computational = false) => ids(value).forEach(id => links.push({ id, computational }));
    if (entry.kind === "claim") add(node.has_evidence);
    if (entry.kind === "evidence") { add(node.source_claims); if (records.has(text(node.reference)) || text(node.reference).startsWith("dapper:")) add(node.reference); }
    if (entry.kind === "dataset") add(members(id));
    add(node.was_derived_from); add(node.was_generated_by, true);
    for (const edge of outgoing.get(id) || []) {
      if (["prov:wasDerivedFrom", "prov:hadMember", "dapper:hasEvidence", "dapper:hasFile"].includes(edge.predicate)) add(edge.object);
      if (["prov:wasGeneratedBy", "prov:used"].includes(edge.predicate)) add(edge.object, true);
    }
    return links;
  };
  const display = (objectId: string, path: string, relation: string): AccountGraphNode => {
    const entry = records.get(objectId), node = entry?.node, kind = kindOf(objectId);
    const proposition = node && records.get(text(node.proposition))?.node;
    const label = node ? text(node.name) || text(node.filename) || (kind === "claim" ? text(proposition?.statement) || text(node.statement) : text(node.reference_title)) || graphKindLabel(kind) : graphKindLabel(kind);
    const subject = entityLabel(text(proposition?.subject_entity)), object = entityLabel(text(proposition?.object_entity));
    const shortLabel = kind === "claim" && subject && object ? `${subject}\n→ ${object}` : undefined;
    return { id: path, objectId, label, shortLabel, kind, relation, direction: text(node?.direction) || undefined, children: [], missing: !node, cycle: false, limited: false, shared: 1, missingReferences: [] };
  };
  const root = display(result.root_id, "account", "Account synthesis");
  const componentClaims = [...new Set(ids(records.get(result.root_id)?.node.component_claims))];
  const claims = componentClaims.slice(0, Math.max(0, maxOccurrences - 1));
  root.limited = claims.length < componentClaims.length;
  let remaining = Math.max(0, maxOccurrences - 1 - claims.length);
  if (maxDepth < 1) { root.limited = claims.length > 0; return root; }
  for (const id of claims) {
    const claim = display(id, `account/${encodeURIComponent(id)}`, "Component claim"); root.children.push(claim);
    if (claim.missing) continue;
    const found = new Map<string, boolean>(), visited = new Map<string, boolean>();
    const missing = new Set<string>(); let traversed = 0;
    const walk = (id: string, computational: boolean, ancestors: Set<string>) => {
      if (ancestors.has(id)) { claim.cycle = true; return; }
      if (visited.has(id) && (visited.get(id) === false || computational)) return;
      if (++traversed > 2048 || ancestors.size > 32) { claim.limited = true; return; }
      visited.set(id, computational);
      const kind = kindOf(id);
      if (kind === "dataset" || kind === "file") found.set(id, found.has(id) ? found.get(id)! && computational : computational);
      // Collect a visible source, then follow its own lineage through hidden
      // activities/records. Upstream sources become siblings, never false members.
      if (!records.has(id)) { if (kind !== "dataset" && kind !== "file") missing.add(id); return; }
      const next = new Set(ancestors).add(id);
      for (const link of references(id)) walk(link.id, computational || link.computational, next);
    };
    walk(id, false, new Set());
    // Follow the lineage of a dataset discovered through an explicitly recorded
    // member file. A fresh path plus the visited map avoids inventing a cycle
    // for the reverse membership lookup itself.
    for (const [sourceId, computational] of found) if (kindOf(sourceId) === "file") {
      for (const parent of datasetsForFile.get(sourceId) || []) walk(parent, computational, new Set());
    }
    claim.missingReferences = [...missing];
    // A source file may identify its dataset through an explicit reverse
    // membership lookup. This never turns a computational input into evidence.
    const datasets = new Map<string, boolean>(), standalone = new Map<string, boolean>();
    const associate = (target: Map<string, boolean>, id: string, computational: boolean) => target.set(id, target.has(id) ? target.get(id)! && computational : computational);
    for (const [id, computational] of found) {
      if (kindOf(id) === "dataset") associate(datasets, id, computational);
      else {
        const parents = datasetsForFile.get(id) || [];
        if (parents.length) parents.forEach(parent => associate(datasets, parent, computational));
        else associate(standalone, id, computational);
      }
    }
    if (maxDepth < 2) { claim.limited ||= datasets.size + standalone.size > 0; continue; }
    const append = (parent: AccountGraphNode, id: string, relation: string) => {
      if (remaining <= 0) { parent.limited = true; return null; }
      remaining--; const node = display(id, `${parent.id}/${encodeURIComponent(id)}`, relation); parent.children.push(node); return node;
    };
    for (const [id, computational] of datasets) {
      const dataset = append(claim, id, computational ? "Computational source · provenance path" : "Source dataset · recorded source path");
      if (!dataset) continue;
      const files = members(id);
      if (maxDepth < 3) { dataset.limited = files.length > 0; continue; }
      for (const file of files) append(dataset, file, "Dataset file");
    }
    // An ungrouped file remains truthful: do not invent a dataset for it.
    for (const [id, computational] of standalone) append(claim, id, computational ? "Computational input · no recorded dataset membership" : "Source file · no recorded dataset membership");
  }
  const occurrences = flattenAccountHierarchy(root), counts = new Map<string, number>();
  for (const node of occurrences) counts.set(node.objectId, (counts.get(node.objectId) || 0) + 1);
  for (const node of occurrences) node.shared = counts.get(node.objectId) || 1;
  return root;
}

export function flattenAccountHierarchy(root: AccountGraphNode): AccountGraphNode[] {
  return [root, ...root.children.flatMap(flattenAccountHierarchy)];
}

/** Pages are from the account's signed continuation, never independent reads
 * that could silently select another published observation of a shared object. */
export function mergeAccountPages(previous: Schema<"AccountResult">, next: Schema<"AccountResult">): Schema<"AccountResult"> {
  if (previous.root_id !== next.root_id || JSON.stringify(previous.schema) !== JSON.stringify(next.schema)) throw new Error("The account source changed. Reload the account to continue.");
  const payloads = new Map(previous.payloads.map(payload => [payload.object_id, payload]));
  for (const payload of next.payloads) {
    if (payloads.has(payload.object_id) && payloads.get(payload.object_id)!.payload_sha256 !== payload.payload_sha256) throw new Error("The account source changed. Reload the account to continue.");
    payloads.set(payload.object_id, payload);
  }
  const document: Record<string, unknown> = { ...previous.document };
  const loaded = new Set<string>();
  for (const group of new Set([...Object.keys(previous.document), ...Object.keys(next.document)])) {
    const oldRows = previous.document[group as keyof typeof previous.document], newRows = next.document[group as keyof typeof next.document];
    if (!Array.isArray(oldRows) && !Array.isArray(newRows)) continue;
    const rows = new Map<string, unknown>();
    for (const row of [...(Array.isArray(oldRows) ? oldRows : []), ...(Array.isArray(newRows) ? newRows : [])]) {
      const id = row && typeof row === "object" && "id" in row ? String(row.id) : null;
      if (id) loaded.add(id);
      rows.set(id || JSON.stringify(row), row);
    }
    document[group] = [...rows.values()];
  }
  const missing_ids = [...new Set([...previous.coverage.missing_ids, ...next.coverage.missing_ids])].filter(id => !loaded.has(id));
  return { ...previous, document: document as Schema<"DapperDocument"> & { scientific_accounts: unknown }, payloads: [...payloads.values()],
    artifacts: [...new Map([...previous.artifacts, ...next.artifacts].map(value => [value.file.id, value])).values()],
    citation_metadata: [...new Map([...previous.citation_metadata, ...next.citation_metadata].map(value => [`${value.target_id}:${value.metadata_revision}`, value])).values()],
    coverage: { ...next.coverage, missing_ids, complete: next.coverage.complete && !missing_ids.length } };
}
