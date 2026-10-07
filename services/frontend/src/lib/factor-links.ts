/** KPN trait catalog pages retain the identifier's seven-digit suffix. */
export function traitHref(id: string) {
  const match = /^KPN\.TRAIT:(\d{7})$/.exec(id);
  return match ? `https://broadinstitute.github.io/kpn-data-models/kpn.trait/${match[1]}/` : null;
}

/** A factor's source ID can recur after a reference reload; always carry its revision. */
export function factorHref(sourceId: string, revision?: string | null, options: { archiveId?: string | null; from?: string | null } = {}) {
  const query = new URLSearchParams();
  if (revision) query.set("source_revision", revision);
  if (options.archiveId) query.set("archive", options.archiveId);
  const from = referenceReturnPath(options.from);
  if (from) query.set("from", from);
  return `/factors/${encodeURIComponent(sourceId)}${query.size ? `?${query}` : ""}`;
}

export function geneSetHref(id: string, generation?: string | null, from?: string | null) {
  const query = new URLSearchParams();
  if (generation) query.set("generation_id", generation);
  const back = referenceReturnPath(from);
  if (back) query.set("from", back);
  return `/gene-sets/${encodeURIComponent(id)}${query.size ? `?${query}` : ""}`;
}

/** Only application reading/editing routes can be a return destination. */
export function referenceReturnPath(value?: string | null): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || /[\\\r\n]/.test(value)) return null;
  try {
    const url = new URL(value, "https://reveal.invalid");
    if (url.origin !== "https://reveal.invalid" || !/^(\/|\/(factors|gene-sets|runs|drafts|accounts|claims|knowledge-gaps)\/[^/]+\/?|\/workspace)$/.test(url.pathname)) return null;
    return url.pathname + url.search + url.hash;
  } catch { return null; }
}

export function returnLabel(path?: string | null) {
  if (path?.startsWith("/factors/")) return "Back to factor";
  if (path?.startsWith("/runs/") || path?.includes("?job=")) return "Back to research run";
  if (path?.startsWith("/drafts/")) return "Back to draft";
  if (path?.startsWith("/?gap=")) return "Back to knowledge gap";
  return "Explore knowledge gaps";
}
