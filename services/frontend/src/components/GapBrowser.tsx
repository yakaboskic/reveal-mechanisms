"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { loadFeaturedGaps } from "@/lib/featured-gaps";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import { VoteControls } from "./VoteControls";
import { onWorkspaceChange } from "@/lib/workspace-events";
import "./gap-browser.css";

export type GapScope = "public" | "workspace";
export type GapSort = "accounts" | "votes";
export const gapScopeLabel = (scope: GapScope) => scope === "public" ? "Trending knowledge gaps" : "Top questions in your workspace";
export function GapScopeSelector({ scope, onChange, sort, onSort }: { scope: GapScope; onChange: (scope: GapScope) => void; sort?: GapSort; onSort?: (sort: GapSort) => void }) {
  const { me } = useIdentity();
  return <div className="gap-scope-selector">
    <label className="sr-only" htmlFor="gap-list-scope">Knowledge gap list</label>
    <div className="gap-browse-heading"><div className="gap-scope-control">
      {scope === "public" && <svg className="gap-scope-trending-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m3 17 6-6 4 4 8-8M15 7h6v6" /></svg>}
      <select id="gap-list-scope" value={scope} onChange={event => onChange(event.target.value as GapScope)}><option value="public">Trending knowledge gaps</option><option value="workspace" disabled={!me}>Top questions in your workspace</option></select>
    </div>{onSort && <div className="gap-sort"><label htmlFor="gap-sort">sort</label><select id="gap-sort" value={sort} onChange={event => onSort(event.target.value as GapSort)} title={sort === "votes" ? "Upvotes minus downvotes" : scope === "public" ? "Number of published accounts" : "Number of accounts in your workspace"}><option value="accounts">Accounts</option><option value="votes">Votes</option></select></div>}</div>
    <p>{scope === "public" ? "Questions and published scientific accounts across all researchers." : "Questions and scientific accounts saved in your workspace."}</p>
  </div>;
}

export function GapBrowser({ scope, sort, onSelect, onRanked }: { scope: GapScope; sort: GapSort; onSelect: (gap: Schema<"GapRecord">) => void; onRanked: (gaps: Schema<"GapRecord">[]) => void }) {
  const { me, ready } = useIdentity();
  const binding = `${scope}:${sort}:${me?.user_id || "visitor"}`;
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
      const result = await api.gaps(append ? previous?.page.next_cursor || undefined : undefined, scope, abort.signal, sort);
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
  }, [binding, ready, scope, sort, me?.user_id]);
  useEffect(() => {
    serial.current++; controller.current?.abort(); inflight.current = false;
    setCollection(null); setBusy(true); setError(""); ranked.current([]);
    if (scroll.current) scroll.current.scrollTop = 0;
    void load();
    return () => { serial.current++; controller.current?.abort(); inflight.current = false; };
  }, [load]);
  useEffect(() => onWorkspaceChange((reset, event) => {
    if (!reset && event && !event.collections.some(value => ["catalog", "accounts", "gaps", "identity"].includes(value))) return;
    serial.current++; controller.current?.abort(); inflight.current = false;
    void load();
  }), [load]);
  useEffect(() => {
    if (!visible?.page.has_more || busy || error || !scroll.current || !sentinel.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void load(true); }, { root: scroll.current, rootMargin: "0px 0px 100px 0px" });
    observer.observe(sentinel.current); return () => observer.disconnect();
  }, [visible?.page.next_cursor, busy, error, load]);
  return <section className="gap-browser trending" aria-label={gapScopeLabel(scope)}>
    <div className="gap-browser-list" ref={scroll} tabIndex={0} role="region" aria-label={`${gapScopeLabel(scope)} list`}>
      {(visible?.items || []).map(gap => <article className="trend" key={gap.object.id}>
        <VoteControls kind="gap" id={gap.object.id} initial={gap.votes || null} compact vertical />
        <div className="trend-main"><button className="trend-open" onClick={() => onSelect(gap)}><span className="trend-question">{gap.object.text}</span><span className="trend-arrow" aria-hidden="true">↗</span></button><div className="trend-meta">{scope === "public" ? <Link className="trend-accounts" href={`/knowledge-gaps/${encodeURIComponent(gap.object.id)}`}><span className="trend-account-count">{gap.scientific_accounts.count}</span> published scientific account{gap.scientific_accounts.count === 1 ? "" : "s"}</Link> : <span>{gap.scientific_accounts.count} scientific account{gap.scientific_accounts.count === 1 ? "" : "s"} in your workspace</span>}<Link className="trend-info" href={`/knowledge-gaps/${encodeURIComponent(gap.object.id)}`} aria-label={`Information about ${gap.object.text}`}>Info</Link></div></div>
      </article>)}
      {busy && <LoadingSurface compact title={visible?.items.length ? "Loading more knowledge gaps" : "Loading knowledge gaps"} description={visible?.items.length ? undefined : "Retrieving ranked questions to explore."} skeleton={visible?.items.length ? "none" : "rows"} rows={3} />}
      {error && <LoadingSurface compact title="Knowledge gaps are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
      {!busy && !error && visible && !visible.items.length && <p className="muted">No knowledge gaps are available in this view.</p>}
      {visible?.page.has_more && <div className="gap-browser-more" ref={sentinel}>{!busy && !error && <button onClick={() => void load(true)}>Load more questions <span aria-hidden="true">↓</span></button>}</div>}
      {visible && !visible.page.has_more && !!visible.items.length && <p className="gap-browser-end">All {visible.items.length} questions loaded</p>}
    </div>
  </section>;
}
