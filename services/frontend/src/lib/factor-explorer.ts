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
