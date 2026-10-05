/** A tile is one observed loading, not a bin or an unobserved zero. */
export type LoadingCell = {
  id: string;
  label: string;
  loading: number | null;
  rank: number;
  description?: string;
  href?: string;
  /** Omit when membership has not been established, rather than implying false. */
  isMember?: boolean;
};

export const loadingColors = ["#f1f8f3", "#c6e9ce", "#85ce9b", "#42a875", "#116b45"] as const;

/** Bounds belong to the complete result, so a search cannot change a tile's meaning. */
export function loadingLevel(value: number | null, min: number | null, max: number | null): number | null {
  if (value === null || min === null || max === null || ![value, min, max].every(Number.isFinite) || max < min) return null;
  if (max === min) return value === 0 ? 0 : 4;
  const fraction = Math.max(0, Math.min(1, (value - min) / (max - min)));
  return Math.min(4, Math.floor(fraction * 5));
}

/** Preserve the reported number, including small values and genuine zeroes. */
export const loadingValue = (value: number | null) => value !== null && Number.isFinite(value) ? String(value) : "Not available";

/** Membership annotates a loading without changing its numeric color scale. */
export function loadingAppearance(item: LoadingCell, min: number | null, max: number | null, overlayLabel?: string) {
  const level = loadingLevel(item.loading, min, max), overlay = overlayLabel?.trim();
  if (!overlay) return { level, membership: null, membershipLabel: null };
  const membership = item.isMember === true ? "member" : item.isMember === false ? "nonmember" : "unknown";
  const membershipLabel = membership === "member" ? `Member of ${overlay}`
    : membership === "nonmember" ? `Not a member of ${overlay}` : `Membership in ${overlay} is not available`;
  return { level, membership, membershipLabel };
}

/** Counts describe only supplied rows, including members with unknown loadings. */
export function loadingMembershipCounts(items: readonly LoadingCell[]) {
  return items.reduce((counts, item) => {
    if (item.isMember === true) counts.members++;
    else if (item.isMember === false) counts.nonmembers++;
    else counts.unknown++;
    counts.total++;
    return counts;
  }, { members: 0, nonmembers: 0, unknown: 0, total: 0 });
}

/** Navigation is spatial; an incomplete last row clamps to its final tile. */
export function loadingNavigation(index: number, key: string, count: number, columns: number, control = false): number | null {
  if (count < 1 || columns < 1 || index < 0 || index >= count) return null;
  if (key === "ArrowLeft") return Math.max(0, index - 1);
  if (key === "ArrowRight") return Math.min(count - 1, index + 1);
  if (key === "ArrowUp") return index < columns ? index : index - columns;
  if (key === "ArrowDown") return Math.floor(index / columns) === Math.floor((count - 1) / columns) ? index : Math.min(count - 1, index + columns);
  if (key === "Home") return control ? 0 : Math.floor(index / columns) * columns;
  if (key === "End") return control ? count - 1 : Math.min(count - 1, (Math.floor(index / columns) + 1) * columns - 1);
  return null;
}
