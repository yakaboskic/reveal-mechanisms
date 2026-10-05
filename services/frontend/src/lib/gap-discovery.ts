import { api, type Schema } from "./client";
import { loadFeaturedGaps } from "./featured-gaps";
import { RevalidationCache, type LoadMode } from "./revalidation-cache";

export type GapScope = "public" | "workspace";
export type GapSort = "accounts" | "votes";
export type GapListKey = `${GapScope}:${GapSort}`;
export type GapCollection = { items: Schema<"GapRecord">[]; page: Schema<"Page"> };
export const gapListKey = (scope: GapScope, sort: GapSort): GapListKey => `${scope}:${sort}`;

export async function loadGapCollection(key: GapListKey, previous: GapCollection | undefined, mode: LoadMode, signal: AbortSignal): Promise<GapCollection> {
  const [scope, sort] = key.split(":") as [GapScope, GapSort];
  if (mode === "append" && previous && !previous.page.next_cursor) return previous;
  const result = await api.gaps(mode === "append" ? previous?.page.next_cursor || undefined : undefined, scope, signal, sort);
  return { items: loadFeaturedGaps([...(mode === "append" ? previous?.items || [] : []), ...result.items]), page: result.page };
}

/** A tab's navigation snapshot has no age-based refresh. Only a new view,
 * explicit refresh, or identity reset loads its first page. */
export class GapDiscoveryCache extends RevalidationCache<GapListKey, GapCollection> {
  private positions = new Map<GapListKey, number>();
  private viewer: string | null = null;
  constructor(loader = loadGapCollection) { super(loader); }
  override bind(viewer: string | null) {
    if (viewer !== this.viewer) { this.positions.clear(); this.viewer = viewer; }
    super.bind(viewer);
  }
  ensure(viewer: string | null, key: GapListKey) {
    const snapshot = this.read(viewer, key);
    if (snapshot.data || snapshot.error) return Promise.resolve();
    return this.revalidate(viewer, key);
  }
  position(viewer: string | null, key: GapListKey) { return viewer === this.viewer ? this.positions.get(key) || 0 : 0; }
  rememberPosition(viewer: string | null, key: GapListKey, top: number) {
    if (viewer && viewer === this.viewer) this.positions.set(key, top);
  }
  vote(viewer: string | null, id: string, votes: Schema<"VoteState">) {
    for (const scope of ["public", "workspace"] as const) for (const sort of ["accounts", "votes"] as const) {
      this.update(viewer, gapListKey(scope, sort), data => data.items.some(gap => gap.object.id === id)
        ? { ...data, items: data.items.map(gap => gap.object.id === id ? { ...gap, votes } : gap) } : data);
    }
  }
}
