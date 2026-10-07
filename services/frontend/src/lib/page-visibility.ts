/**
 * Long-lived event streams park in hidden tabs. Over HTTP/1.1 (local development) a browser profile has six
 * connections per origin across all tabs, and each open stream holds one. A tab hidden for a minute releases its
 * stream and resumes from its cursor once it is shown again; a visible tab never parks.
 */
export type PageVisibility = {
  hidden(): boolean;
  /** Resolves once the page is visible or the signal aborts. */
  untilVisible(signal: AbortSignal): Promise<void>;
  /** Calls back after the page has stayed hidden for `ms`; returns the cleanup. */
  onHiddenFor(ms: number, callback: () => void): () => void;
};
export const parkHiddenAfterMs = 60_000;
export const alwaysVisible: PageVisibility = { hidden: () => false, untilVisible: async () => {}, onHiddenFor: () => () => {} };

type VisibilityDocument = EventTarget & { visibilityState: DocumentVisibilityState };
export function documentVisibility(doc: VisibilityDocument | undefined = typeof document === "undefined" ? undefined : document): PageVisibility {
  if (!doc) return alwaysVisible;
  const hidden = () => doc.visibilityState === "hidden";
  return {
    hidden,
    untilVisible: signal => new Promise<void>(resolve => {
      if (!hidden() || signal.aborted) return resolve();
      const check = () => {
        if (hidden() && !signal.aborted) return;
        doc.removeEventListener("visibilitychange", check); signal.removeEventListener("abort", check); resolve();
      };
      doc.addEventListener("visibilitychange", check); signal.addEventListener("abort", check);
    }),
    onHiddenFor: (ms, callback) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const change = () => { clearTimeout(timer); timer = hidden() ? setTimeout(callback, ms) : undefined; };
      change(); doc.addEventListener("visibilitychange", change);
      return () => { clearTimeout(timer); doc.removeEventListener("visibilitychange", change); };
    },
  };
}

/** One stream attempt: aborts with the owner's signal, or when the page parks it. `parked` tells the two apart. */
export function streamAttempt(signal: AbortSignal, page: PageVisibility) {
  const controller = new AbortController(), abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  const unpark = page.onHiddenFor(parkHiddenAfterMs, abort);
  return {
    signal: controller.signal, abort,
    parked: () => controller.signal.aborted && !signal.aborted,
    end: () => { unpark(); signal.removeEventListener("abort", abort); },
  };
}

export const untilAborted = (signal: AbortSignal) => new Promise<void>(resolve => {
  if (signal.aborted) resolve(); else signal.addEventListener("abort", () => resolve(), { once: true });
});
