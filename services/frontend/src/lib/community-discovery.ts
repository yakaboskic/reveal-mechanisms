import type { Schema } from "./client";

export type DiscoveryView = "gaps" | "accounts";
export type AccountSort = "votes" | "recent";
export const discoveryLabel = (view: DiscoveryView) => view === "accounts" ? "Trending scientific accounts" : "Trending knowledge gaps";

/** A public discovery page must never absorb a private workspace response. */
export function mergePublicAccounts(previous: Schema<"AccountSummary">[], incoming: Schema<"AccountSummary">[]) {
  const items = new Map<string, Schema<"AccountSummary">>();
  for (const item of [...previous, ...incoming]) {
    if (item.publication.visibility !== "public" || item.publication.can_manage || item.publication.has_unpublished_changes || item.job_id || item.research_statement.job_id || item.account.question !== item.knowledge_gap.id) {
      throw new Error("The published accounts could not be verified. Please retry.");
    }
    items.set(item.account.id, item);
  }
  return [...items.values()];
}
