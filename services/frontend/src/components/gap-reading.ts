import type { Schema } from "../lib/client";

/** Next decodes route parameters; tolerate one explicit encoded path value too. */
export function gapPageId(value: string): string | null {
  try {
    const id = decodeURIComponent(value);
    return /^dapper:KnowledgeGap\.[A-Za-z0-9_-]{32}$/.test(id) ? id : null;
  } catch { return null; }
}

export const gapExploreHref = (id: string) => `/?gap=${encodeURIComponent(id)}`;

/** Reject a misbound/private response before adding anything to a public list. */
export function mergeGapAccounts(gapId: string, scope: "public" | "workspace",
  previous: Schema<"AccountSummary">[], incoming: Schema<"AccountSummary">[]) {
  const items = new Map<string, Schema<"AccountSummary">>();
  for (const item of [...previous, ...incoming]) {
    if (item.knowledge_gap.id !== gapId || item.account.question !== gapId) {
      throw new Error("The returned accounts could not be matched to this knowledge gap. Please retry.");
    }
    if (scope === "public" && item.publication.visibility !== "public") {
      throw new Error("The published accounts could not be verified. Please retry.");
    }
    items.set(item.account.id, item);
  }
  return [...items.values()];
}
