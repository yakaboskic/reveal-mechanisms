import type { Schema } from "./client";

/** Membership identifiers are not fuzzy gene searches. Other identifier
 * namespaces need a mapping and must never silently become negative matches. */
export function overlayMembers(object: { [key: string]: unknown } | null) {
  if (!Array.isArray(object?.members)) return null;
  const symbols = new Set<string>(); let unsupported = 0;
  for (const member of object.members) {
    if (typeof member !== "string") { unsupported++; continue; }
    const symbol = member.startsWith("HGNC.SYMBOL:") ? member.slice("HGNC.SYMBOL:".length) : member;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(symbol)) unsupported++;
    else symbols.add(symbol);
  }
  return { symbols, unsupported };
}

export function appendLoadings(previous: readonly Schema<"FactorLoading">[], incoming: readonly Schema<"FactorLoading">[]) {
  const ids = new Set(previous.map(row => row.id));
  return [...previous, ...incoming.filter(row => { if (ids.has(row.id)) return false; ids.add(row.id); return true; })];
}

/** The gene page a factor page opens on. It needs only the factor, so it is read alongside the detail. */
export const firstGenePage = { kind: "gene", metric: "joint", sort: "alphabetical", q: "", offset: 0, limit: 200 } as const;
type PageRequest = { source_id: string; generation_id: string | null; gnomad_import_id?: string; kind: string; metric: string; sort: string; q: string; offset: number; limit: number };
/** The early, unpinned read stands in for the panel's first pinned read only when it answers exactly that request
 * from the detail's generation and gnomAD import. Otherwise the pinned read runs, and a changed reference still
 * reports its own 409. */
export function prefetchedLoadings(value: Schema<"FactorLoadings"> | null, request: PageRequest): Schema<"FactorLoadings"> | null {
  const first = firstGenePage;
  if (!value || request.kind !== first.kind || request.metric !== first.metric || request.sort !== first.sort || request.q !== first.q
    || request.offset !== first.offset || request.limit !== first.limit) return null;
  return value.source_id === request.source_id && value.kind === first.kind && value.metric === first.metric && value.sort === first.sort
    && value.offset === first.offset && value.limit === first.limit && value.generation_id === request.generation_id
    && (value.gnomad?.import_id || "none") === (request.gnomad_import_id || "none") ? value : null;
}
