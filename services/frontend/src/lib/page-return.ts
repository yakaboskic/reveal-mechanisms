type VisibleDocument = EventTarget & { visibilityState: DocumentVisibilityState };
export const pageReturnCoalesceMs = 250;

/** One check per return to a visible page: a tab switch fires visibilitychange and focus together. */
export function onPageReturn(check: () => void, options: { win?: EventTarget; doc?: VisibleDocument; now?: () => number; coalesceMs?: number } = {}) {
  const win = options.win || window, doc = options.doc || document, now = options.now || (() => performance.now());
  const coalesce = options.coalesceMs ?? pageReturnCoalesceMs;
  let last = -Infinity;
  const handle = () => {
    if (doc.visibilityState === "hidden" || now() - last < coalesce) return;
    last = now(); check();
  };
  win.addEventListener("focus", handle); win.addEventListener("online", handle); doc.addEventListener("visibilitychange", handle);
  return () => { win.removeEventListener("focus", handle); win.removeEventListener("online", handle); doc.removeEventListener("visibilitychange", handle); };
}
