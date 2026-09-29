"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { loadFeaturedGaps } from "@/lib/featured-gaps";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import "./gap-browser.css";

export type GapScope = "public" | "workspace";
export const gapScopeLabel = (scope: GapScope) => scope === "public" ? "Trending knowledge gaps" : "Top questions in your workspace";
export function GapScopeSelector({ scope, onChange }: { scope: GapScope; onChange: (scope: GapScope) => void }) {
  const { me } = useIdentity();
  return <div className="gap-scope-selector">
    <label className="sr-only" htmlFor="gap-list-scope">Knowledge gap list</label>
    <div className="gap-scope-control">
      {scope === "public" && <svg className="gap-scope-trending-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m3 17 6-6 4 4 8-8M15 7h6v6" /></svg>}
      <select id="gap-list-scope" value={scope} onChange={event => onChange(event.target.value as GapScope)}><option value="public">Trending knowledge gaps</option><option value="workspace" disabled={!me}>Top questions in your workspace</option></select>
    </div>
    <p>{scope === "public" ? "Ranked by published scientific accounts across all researchers." : "Ranked by scientific accounts saved in your workspace."}</p>
  </div>;
}

export function GapBrowser({ scope, onSelect, onRanked }: { scope: GapScope; onSelect: (gap: Schema<"GapRecord">) => void; onRanked: (gaps: Schema<"GapRecord">[]) => void }) {
  const { me, ready } = useIdentity();
  const binding = `${scope}:${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const [collection, setCollection] = useState<{ binding: string; items: Schema<"GapRecord">[]; page: Schema<"Page"> } | null>(null);
  const [busy, setBusy] = useState(true), [error, setError] = useState("");
  const serial = useRef(0), inflight = useRef(false), controller = useRef<AbortController | null>(null);
  const scroll = useRef<HTMLDivElement>(null), sentinel = useRef<HTMLDivElement>(null);
  const ranked = useRef(onRanked); ranked.current = onRanked;
  const visible = collection?.binding === binding ? collection : null;
  const snapshot = useRef(visible); snapshot.current = visible;
  const load = useCallback(async (append = false) => {
    if (!ready || inflight.current || (scope === "workspace" && !me)) return;
    const previous = snapshot.current;
    if (append && !previous?.page.next_cursor) return;
    const sequence = ++serial.current; inflight.current = true;
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError("");
    try {
      const result = await api.gaps(append ? previous?.page.next_cursor || undefined : undefined, scope, abort.signal);
      if (abort.signal.aborted || sequence !== serial.current || binding !== current.current) return;
      const items = loadFeaturedGaps([...(append ? previous?.items || [] : []), ...result.items]);
      setCollection({ binding, items, page: result.page }); ranked.current(items);
    } catch (failure) {
      if (!abort.signal.aborted && sequence === serial.current && binding === current.current) {
        if (failure instanceof ApiError && failure.code === "CURSOR_EXPIRED") { setCollection(null); snapshot.current = null; ranked.current([]); }
        setError(messageOf(failure));
      }
    } finally {
      if (sequence === serial.current) { inflight.current = false; setBusy(false); }
    }
  }, [binding, ready, scope, me?.user_id]);
  useEffect(() => {
    serial.current++; controller.current?.abort(); inflight.current = false;
    setCollection(null); setBusy(true); setError(""); ranked.current([]);
    if (scroll.current) scroll.current.scrollTop = 0;
    void load();
    return () => { serial.current++; controller.current?.abort(); inflight.current = false; };
  }, [load]);
  useEffect(() => {
    if (!visible?.page.has_more || busy || error || !scroll.current || !sentinel.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void load(true); }, { root: scroll.current, rootMargin: "0px 0px 100px 0px" });
    observer.observe(sentinel.current); return () => observer.disconnect();
  }, [visible?.page.next_cursor, busy, error, load]);
  return <section className="gap-browser trending" aria-label={gapScopeLabel(scope)}>
    <div className="gap-browser-list" ref={scroll} tabIndex={0} role="region" aria-label={`${gapScopeLabel(scope)} list`}>
      {(visible?.items || []).map(gap => <button className="trend" key={gap.object.id} onClick={() => onSelect(gap)}><span className="trend-question">{gap.object.text}<small>{gap.scientific_accounts.count} {scope === "public" ? "published " : ""}scientific account{gap.scientific_accounts.count === 1 ? "" : "s"}{scope === "workspace" ? " in your workspace" : ""}</small></span><span className="trend-arrow" aria-hidden="true">↗</span></button>)}
      {busy && <LoadingSurface compact title={visible?.items.length ? "Loading more knowledge gaps" : "Loading knowledge gaps"} description={visible?.items.length ? undefined : "Retrieving ranked questions to explore."} skeleton={visible?.items.length ? "none" : "rows"} rows={3} />}
      {error && <LoadingSurface compact title="Knowledge gaps are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
      {!busy && !error && visible && !visible.items.length && <p className="muted">No knowledge gaps are available in this view.</p>}
      {visible?.page.has_more && <div className="gap-browser-more" ref={sentinel}>{!busy && !error && <button onClick={() => void load(true)}>Load more questions <span aria-hidden="true">↓</span></button>}</div>}
      {visible && !visible.page.has_more && !!visible.items.length && <p className="gap-browser-end">All {visible.items.length} questions loaded</p>}
    </div>
  </section>;
}
