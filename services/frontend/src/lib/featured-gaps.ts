import type { Schema } from "./client";

/** Keep the server's scoped account-count ranking and unchanged source records. */
export function loadFeaturedGaps(ranked: readonly Schema<"GapRecord">[]): Schema<"GapRecord">[] {
  const selected: Schema<"GapRecord">[] = [];
  const identities = new Set<string>();
  for (const gap of ranked) {
    if (identities.has(gap.object.id)) continue;
    identities.add(gap.object.id);
    selected.push(gap);
  }
  return selected;
}
