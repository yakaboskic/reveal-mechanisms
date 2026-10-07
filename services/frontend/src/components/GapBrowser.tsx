"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { useGapCollection } from "./GapDiscoveryCache";
import type { GapScope, GapSort } from "@/lib/gap-discovery";
import { discoveryLabel, mergePublicAccounts, type DiscoveryView, type AccountSort } from "@/lib/community-discovery";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import { VoteControls } from "./VoteControls";
import { onWorkspaceChange } from "@/lib/workspace-events";
import { onPageReturn } from "@/lib/page-return";
import "./gap-browser.css";

const publicFreshMs = 60_000;

export type { GapScope, GapSort } from "@/lib/gap-discovery";
export const gapScopeLabel = (scope: GapScope) => scope === "public" ? "Trending knowledge gaps" : "Top questions in your workspace";
export function DiscoverySelector({ view, onChange, gapSort, onGapSort, accountSort, onAccountSort, searching }: { view: DiscoveryView; onChange: (view: DiscoveryView) => void; gapSort: GapSort; onGapSort: (sort: GapSort) => void; accountSort: AccountSort; onAccountSort: (sort: AccountSort) => void; searching: boolean }) {
  return <div className="gap-scope-selector">
    <label className="sr-only" htmlFor="gap-list-scope">Community discovery</label>
    <div className="gap-browse-heading"><div className="gap-scope-control">
      <svg className="gap-scope-trending-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m3 17 6-6 4 4 8-8M15 7h6v6" /></svg>
      <select id="gap-list-scope" value={view} onChange={event => onChange(event.target.value as DiscoveryView)}><option value="gaps">Trending knowledge gaps</option><option value="accounts">Trending scientific accounts</option></select>
    </div>{(view === "accounts" || !searching) && <div className="gap-sort"><label htmlFor="gap-sort">sort</label>{view === "accounts"
      ? <select id="gap-sort" value={accountSort} onChange={event => onAccountSort(event.target.value as AccountSort)} title={accountSort === "votes" ? "Upvotes minus downvotes, then publication date" : "Most recently published first"}><option value="votes">Votes</option><option value="recent">Newest</option></select>
      : <select id="gap-sort" value={gapSort} onChange={event => onGapSort(event.target.value as GapSort)} title={`${gapSort === "votes" ? "Upvotes minus downvotes" : "Number of published accounts"}, then number of attached DisMech mechanisms`}><option value="accounts">Accounts</option><option value="votes">Votes</option></select>}</div>}</div>
    <p>{view === "accounts" ? "Published scientific accounts from all researchers." : "Known biomedical questions, ranked across the research community."}</p>
  </div>;
}

export function GapBrowser({ scope, sort, onSelect, onRanked }: { scope: GapScope; sort: GapSort; onSelect: (gap: Schema<"GapRecord">) => void; onRanked: (gaps: Schema<"GapRecord">[]) => void }) {
  const { cache, viewer, key, snapshot } = useGapCollection(scope, sort);
  const binding = `${viewer}:${key}`, current = useRef(binding); current.current = binding;
  const visible = snapshot.data, busy = snapshot.loading || (!viewer && !visible), error = snapshot.error;
  const expired = error instanceof ApiError && error.code === "CURSOR_EXPIRED";
  const scroll = useRef<HTMLDivElement>(null), sentinel = useRef<HTMLDivElement>(null);
  const ranked = useRef(onRanked); ranked.current = onRanked;
  useEffect(() => { ranked.current(visible?.items || []); }, [visible]);
  useLayoutEffect(() => { if (scroll.current) scroll.current.scrollTop = cache.position(viewer, key); }, [cache, viewer, key]);
  const load = useCallback((append = false) => {
    if (append && !cache.read(viewer, key).data?.page.next_cursor) return;
    return cache.revalidate(viewer, key, true, append ? "append" : "refresh");
  }, [cache, viewer, key]);
  const refresh = () => {
    void load(false)?.then(() => {
      if (current.current !== binding || cache.read(viewer, key).stale) return;
      cache.rememberPosition(viewer, key, 0);
      if (scroll.current) scroll.current.scrollTop = 0;
    });
  };
  useEffect(() => {
    if (!visible?.page.has_more || busy || error || !scroll.current || !sentinel.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void load(true); }, { root: scroll.current, rootMargin: "0px 0px 100px 0px" });
    observer.observe(sentinel.current); return () => observer.disconnect();
  }, [visible?.page.next_cursor, busy, error, load]);
  return <section className="gap-browser trending" aria-label={gapScopeLabel(scope)}>
    {visible && <div className="gap-browser-refresh"><span role="status">{snapshot.stale && !error ? "Rankings may have changed" : ""}</span><button type="button" disabled={busy} onClick={refresh}>{busy && !snapshot.loadingMore ? "Refreshing…" : "Refresh rankings"}</button></div>}
    <div className="gap-browser-list" ref={scroll} onScroll={event => cache.rememberPosition(viewer, key, event.currentTarget.scrollTop)} tabIndex={0} role="region" aria-label={`${gapScopeLabel(scope)} list`}>
      {(visible?.items || []).map(gap => <article className="trend" key={gap.object.id}>
        <VoteControls kind="gap" id={gap.object.id} initial={gap.votes || null} onChange={votes => cache.vote(viewer, gap.object.id, votes)} compact vertical />
        <div className="trend-main">
          <div className="trend-question-area">
            <Link className="trend-open" href={`/?gap=${encodeURIComponent(gap.object.id)}`} onClick={event => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); onSelect(gap); }}><span className="trend-question">{gap.object.text}</span></Link>{"\u00a0"}<Link className="trend-info" href={`/knowledge-gaps/${encodeURIComponent(gap.object.id)}`} aria-label={`Information about ${gap.object.text}`}>[info]</Link>
          </div>
          <div className="trend-meta">{scope === "public" ? <Link className="trend-accounts" href={`/knowledge-gaps/${encodeURIComponent(gap.object.id)}`}><span className="trend-account-count">{gap.scientific_accounts.count}</span> published scientific account{gap.scientific_accounts.count === 1 ? "" : "s"}</Link> : <span>{gap.scientific_accounts.count} scientific account{gap.scientific_accounts.count === 1 ? "" : "s"} in your workspace</span>}</div>
        </div>
      </article>)}
      {busy && <LoadingSurface compact title={visible?.items.length ? snapshot.loadingMore ? "Loading more knowledge gaps" : "Refreshing knowledge gaps" : "Loading knowledge gaps"} description={visible?.items.length ? undefined : "Retrieving ranked questions to explore."} skeleton={visible?.items.length ? "none" : "rows"} rows={3} />}
      {!!error && <LoadingSurface compact title={expired ? "Rankings have changed" : "Knowledge gaps are unavailable"} error={expired ? "Refresh the rankings to continue browsing. Your loaded questions are still shown." : messageOf(error)} onRetry={refresh} skeleton="none" />}
      {!busy && !error && visible && !visible.items.length && <p className="muted">No knowledge gaps are available in this view.</p>}
      {visible?.page.has_more && <div className="gap-browser-more" ref={sentinel}>{!busy && !error && <button onClick={() => void load(true)}>Load more questions <span aria-hidden="true">↓</span></button>}</div>}
      {visible && !visible.page.has_more && !!visible.items.length && <p className="gap-browser-end">All {visible.items.length} questions loaded</p>}
    </div>
  </section>;
}

export function TrendingAccounts({ query, sort, onLoaded }: { query: string; sort: AccountSort; onLoaded: () => void }) {
  const { me, ready } = useIdentity();
  const term = query.trim(), binding = `${sort}:${term}:${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const [collection, setCollection] = useState<{ binding: string; items: Schema<"AccountSummary">[]; page: Schema<"Page"> } | null>(null);
  const [busy, setBusy] = useState(true), [error, setError] = useState("");
  const serial = useRef(0), inflight = useRef(false), controller = useRef<AbortController | null>(null), loadedAt = useRef(0);
  const scroll = useRef<HTMLDivElement>(null), sentinel = useRef<HTMLDivElement>(null);
  const loaded = useRef(onLoaded); loaded.current = onLoaded;
  const visible = collection?.binding === binding ? collection : null;
  const snapshot = useRef(visible); snapshot.current = visible;
  const load = useCallback(async (append = false) => {
    if (!ready || inflight.current) return;
    const previous = snapshot.current;
    if (append && !previous?.page.next_cursor) return;
    const sequence = ++serial.current; inflight.current = true;
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError("");
    try {
      const result = await api.publicAccounts(append ? previous?.page.next_cursor || undefined : undefined, abort.signal, term, sort);
      if (abort.signal.aborted || sequence !== serial.current || binding !== current.current) return;
      const items = mergePublicAccounts(append ? previous?.items || [] : [], result.items);
      setCollection({ binding, items, page: result.page }); if (!append) loadedAt.current = Date.now();
      requestAnimationFrame(() => { if (!abort.signal.aborted && binding === current.current) loaded.current(); });
    } catch (failure) {
      if (!abort.signal.aborted && sequence === serial.current && binding === current.current) {
        if (failure instanceof ApiError && (failure.code === "CURSOR_EXPIRED" || [401, 403].includes(failure.status))) { setCollection(null); snapshot.current = null; }
        setError(messageOf(failure));
      }
    } finally {
      if (sequence === serial.current) { inflight.current = false; setBusy(false); }
    }
  }, [binding, ready, term, sort]);
  useEffect(() => {
    serial.current++; controller.current?.abort(); inflight.current = false;
    setCollection(null); snapshot.current = null; setBusy(true); setError("");
    if (scroll.current) scroll.current.scrollTop = 0;
    const timer = setTimeout(() => void load(), term ? 250 : 0);
    return () => { clearTimeout(timer); serial.current++; controller.current?.abort(); inflight.current = false; };
  }, [load]);
  useEffect(() => {
    const refresh = () => {
      serial.current++; controller.current?.abort(); inflight.current = false;
      setCollection(null); snapshot.current = null;
      void load();
    };
    const unsubscribe = onWorkspaceChange((reset, event) => {
      if (reset || !event || event.collections.some(value => ["catalog", "accounts", "identity"].includes(value))) refresh();
    });
    // A tab switch fires visibilitychange and focus together: one check, and rows under a minute old are kept.
    const stop = onPageReturn(() => { if (!inflight.current && (!snapshot.current || Date.now() - loadedAt.current >= publicFreshMs)) refresh(); });
    return () => { unsubscribe(); stop(); };
  }, [load]);
  useEffect(() => {
    if (!visible?.page.has_more || busy || error || !scroll.current || !sentinel.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) void load(true); }, { root: scroll.current, rootMargin: "0px 0px 100px 0px" });
    observer.observe(sentinel.current); return () => observer.disconnect();
  }, [visible?.page.next_cursor, busy, error, load]);
  return <section className="gap-browser trending account-discovery" aria-label={term ? "Scientific account search results" : discoveryLabel("accounts")}>
    <div id="discovery-account-list" className="gap-browser-list" ref={scroll} tabIndex={0} role="region" aria-label="Published scientific accounts list">
      {term && visible && <p className="account-discovery-count" role="status">{visible.items.length}{visible.page.has_more ? "+" : ""} matching published scientific account{visible.items.length === 1 ? "" : "s"}</p>}
      {(visible?.items || []).map(item => <article className="trend" key={item.account.id}>
        <VoteControls key={`${binding}:${item.account.id}`} kind="account" id={item.account.id} initial={item.votes || null} compact vertical />
        <div className="trend-main"><div className="trend-question-area"><Link className="trend-open" href={`/accounts/${encodeURIComponent(item.account.id)}`}><span className="trend-question">{item.account.name || "Scientific account"}</span></Link>{"\u00a0"}<Link className="trend-info" href={`/knowledge-gaps/${encodeURIComponent(item.knowledge_gap.id)}`} aria-label={`Knowledge gap for ${item.account.name || "this scientific account"}`}>[info]</Link></div>
          {item.account.closing_remarks && <p className="trend-account-summary">{item.account.closing_remarks}</p>}
          <div className="trend-meta"><Link href={`/knowledge-gaps/${encodeURIComponent(item.knowledge_gap.id)}`}>{item.knowledge_gap.text}</Link><span>{item.claim_count} claim{item.claim_count === 1 ? "" : "s"}{item.attribution?.display_name ? ` · ${item.attribution.display_name}` : ""}</span></div>
        </div>
      </article>)}
      {busy && <LoadingSurface compact title={visible?.items.length ? "Loading more scientific accounts" : term ? "Searching published scientific accounts" : "Loading published scientific accounts"} description={visible?.items.length ? undefined : "Retrieving published research across the community."} skeleton={visible?.items.length ? "none" : "rows"} rows={3} />}
      {error && <LoadingSurface compact title="Scientific accounts are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
      {!busy && !error && visible && !visible.items.length && <p className="muted">{term ? "No matching published scientific accounts. Try a disease, gene or mechanism." : "No scientific accounts have been published yet."}</p>}
      {visible?.page.has_more && <div className="gap-browser-more" ref={sentinel}>{!busy && !error && <button onClick={() => void load(true)}>Load more scientific accounts <span aria-hidden="true">↓</span></button>}</div>}
      {visible && !visible.page.has_more && !!visible.items.length && <p className="gap-browser-end">All {visible.items.length} published scientific accounts loaded</p>}
    </div>
  </section>;
}
