import type { ResearchClient, Schema } from "./client";

type Gap = Schema<"GapRecord">;
const topics = ["AIP", "diabetes", "Alzheimer"] as const;
const normalize = (value: string | null | undefined) => (value || "").normalize("NFKD")
  .replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const sourceKey = (gap: Gap) => `${gap.source.source}\0${gap.source.source_id}`;
const stableKey = (gap: Gap) => `${sourceKey(gap)}\0${gap.source.source_revision}\0${gap.object.id}`;
const compare = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;
const diseaseKey = (gap: Gap) => normalize(gap.source.disease_label);

function relevance(gap: Gap, topic: string): number {
  const term = ` ${normalize(topic)} `;
  // Require an actual source-text match; a fuzzy near-match is not a featured topic.
  // Disease identity takes precedence over a topic mentioned only in the rationale.
  return [gap.source.disease_label, gap.object.scope, gap.object.text, gap.object.gap_description]
    .findIndex(value => ` ${normalize(value)} `.includes(term));
}

/** Select up to three unchanged source records; fewer only if real records run out.
 * Three bounded read-only searches run concurrently. Editorial topics are not a
 * popularity ranking, account counts, or scientific evidence.
 */
export async function loadFeaturedGaps(
  client: Pick<ResearchClient, "searchGaps">,
  fallback: readonly Gap[],
): Promise<Gap[]> {
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout>;
  const deadline = new Promise<never>((_, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      reject(new Error("Featured gap search timed out"));
    }, 5000);
  });
  const searches = await Promise.allSettled(topics.map(topic => Promise.race([
    Promise.resolve().then(() => client.searchGaps(topic, controller.signal)), deadline,
  ]))).finally(() => clearTimeout(timeout));

  const selected: Gap[] = [];
  const sources = new Set<string>();
  const identities = new Set<string>();
  const diseases = new Set<string>();
  const add = (gap: Gap) => {
    if (selected.length === topics.length || sources.has(sourceKey(gap)) || identities.has(gap.object.id)) return false;
    selected.push(gap);
    sources.add(sourceKey(gap)); identities.add(gap.object.id);
    if (diseaseKey(gap)) diseases.add(diseaseKey(gap));
    return true;
  };
  for (const [index, result] of searches.entries()) {
    if (result.status !== "fulfilled") continue;
    const candidates = result.value.items.slice(0, 20).map(hit => ({ gap: hit.gap, relevance: relevance(hit.gap, topics[index]) }))
      .filter(hit => hit.relevance >= 0)
      .sort((a, b) => a.relevance - b.relevance || compare(stableKey(a.gap), stableKey(b.gap)));
    for (const candidate of candidates) if (add(candidate.gap)) break;
  }
  // Prefer a different source-reported disease for each remaining slot. Missing
  // labels are not treated as distinct diseases; they can still fill a last slot.
  const catalog = [...fallback].sort((a, b) => compare(diseaseKey(a), diseaseKey(b)) || compare(stableKey(a), stableKey(b)));
  for (const gap of catalog) if (diseaseKey(gap) && !diseases.has(diseaseKey(gap))) add(gap);
  for (const gap of catalog) add(gap);
  return selected;
}
