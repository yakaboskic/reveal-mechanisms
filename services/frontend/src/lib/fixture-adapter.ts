import type { ResearchClient, Schema } from "./client";

// Tests or an explicitly chosen development harness may inject this adapter.
// Production views import `api` and never import fixture bytes or this module.
export type ValidatedFixtures = {
  notice: string; contractSha256: string;
  gaps: Schema<"GapList">; suggestions: Schema<"Suggestions">;
  account: Schema<"AccountResult">; claim: Schema<"ClaimResult">;
  paragraph: Schema<"ParagraphObjectResult">; events: Schema<"JobEvents">;
};
export function createFixtureAdapter(fixtures: ValidatedFixtures, mode: "test" | "development"):
  Pick<ResearchClient, "gaps" | "gap" | "suggest" | "account" | "claim" | "paragraph"> {
  if (process.env.NODE_ENV === "production" || !["test", "development"].includes(mode)) throw new Error("Scientific fixtures are restricted to explicit development/test use");
  const clone = <T,>(value: T): T => structuredClone(value);
  return {
    gaps: async () => clone(fixtures.gaps),
    gap: async id => { const gap = fixtures.gaps.items.find(gap => gap.object.id === id); if (!gap) throw new Error("Unknown fixture gap"); return clone(gap); },
    suggest: async input => ({ ...clone(fixtures.suggestions), automatic_anchors: fixtures.suggestions.automatic_anchors.filter(item => !input.dismissed_source_ids.includes(item.factor.source_id) && !input.manual_eaggl_anchors.some(anchor => anchor.source_id === item.factor.source_id)) }),
    account: async id => { if (id !== fixtures.account.root_id) throw new Error("Fixture account cannot be associated with a different scientific identity"); return clone(fixtures.account); },
    claim: async id => { if (id !== fixtures.claim.root_id) throw new Error("Unknown fixture claim"); return clone(fixtures.claim); },
    paragraph: async id => { if (id !== fixtures.paragraph.root_id) throw new Error("Unknown fixture paragraph"); return clone(fixtures.paragraph); },
  };
}
