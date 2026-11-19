/** View state for the admin console lives in the URL (as in the claims console's use-url-state.ts): filters, the page
 * and the open item. These are the pure parts; components/admin/use-url-state.ts wires them to the router. */

export type UrlValue = string | number | null | undefined;
export type UrlParams = Pick<URLSearchParams, "get" | "toString">;

/** The URL for `pathname` with `updates` applied to `current`: an empty, null or undefined value removes the key.
 * Keys keep their order, so the same view always has the same URL. */
export function urlWith(pathname: string, current: UrlParams | string, updates: Record<string, UrlValue>): string {
  const next = new URLSearchParams(typeof current === "string" ? current : current.toString());
  for (const [key, value] of Object.entries(updates)) {
    if (value === undefined || value === null || value === "") next.delete(key); else next.set(key, String(value));
  }
  const query = next.toString();
  return query ? `${pathname}?${query}` : pathname;
}

/** A page number from the URL: a whole number of at least 1, else 1. */
export function pageNumber(value: string | null | undefined): number {
  if (!value || !/^\d+$/.test(value)) return 1;
  return Math.max(1, Number(value));
}

/** One page of a list held in memory. A page past the end shows the last page, so a stale URL never shows nothing. */
export function pageOf<T>(items: T[], page: number, size: number): { items: T[]; page: number; pages: number; total: number } {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(1, page), pages);
  return { items: items.slice((current - 1) * size, current * size), page: current, pages, total: items.length };
}

/** Where one job opens: its own page, /admin/jobs/[id]. */
export const jobHref = (id: string) => `/admin/jobs/${encodeURIComponent(id)}`;
