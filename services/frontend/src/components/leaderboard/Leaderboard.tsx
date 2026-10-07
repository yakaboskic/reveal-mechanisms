"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { LoadingSurface } from "../LoadingSurface";
import { onPageReturn } from "@/lib/page-return";
import { formatLeaderboardNumber as number, leaderboardHref, LeaderboardPageChangedError, leaderboardSorts, leaderboardViews, mergeLeaderboardPages, publicLeaderboardHref, readLeaderboardQuery, verifiedOrcidHref, type LeaderboardEntry, type LeaderboardMetric, type LeaderboardQuery, type LeaderboardSort, type LeaderboardView } from "@/lib/leaderboard";

type Column = { key: keyof LeaderboardEntry["metrics"]; label: string; metric: LeaderboardMetric; sort?: LeaderboardSort; score?: boolean };
const columns: Record<LeaderboardView, Column[]> = {
  researchers: [
    { key: "overall_score", label: "Overall score", metric: "accounts", sort: "overall", score: true },
    { key: "account_count", label: "Public accounts", metric: "accounts", sort: "accounts" },
    { key: "account_gap_count", label: "Gaps covered", metric: "gaps", sort: "gaps" },
    { key: "net_votes", label: "Community votes", metric: "votes", sort: "votes" },
    { key: "explored_gap_count", label: "Gaps explored", metric: "explored", sort: "explored" },
  ],
  accounts: [
    { key: "net_votes", label: "Net votes", metric: "votes", sort: "votes" },
    { key: "upvotes", label: "Upvotes", metric: "votes" },
    { key: "downvotes", label: "Downvotes", metric: "votes" },
    { key: "voter_count", label: "Voters", metric: "votes" },
  ],
  datasets: [
    { key: "account_count", label: "Accounts", metric: "accounts", sort: "accounts" },
    { key: "claim_count", label: "Claims", metric: "claims", sort: "claims" },
    { key: "account_gap_count", label: "Gaps", metric: "gaps", sort: "gaps" },
    { key: "researcher_count", label: "Researchers", metric: "researchers", sort: "researchers" },
  ],
};
const names: Record<LeaderboardView, string> = { researchers: "Researcher", accounts: "Scientific account", datasets: "Dataset" };
type OpenRecords = (entry: LeaderboardEntry, metric: LeaderboardMetric, trigger: HTMLElement) => void;

function usePublicRevalidation(reload: () => void) {
  const latest = useRef(reload); latest.current = reload;
  useEffect(() => onPageReturn(() => latest.current()), []);
}

function Contributor({ entry }: { entry: LeaderboardEntry }) {
  const author = entry.attribution;
  if (!author) return <span>Attribution unavailable</span>;
  const href = leaderboardHref({ view: "researchers", sort: "overall", evidence: "all", selected: author.id, metric: "accounts" });
  return <span>Proposed by <Link href={href}>{author.label}</Link></span>;
}

/** Presentation is separate from fetching so real response fixtures can exercise it in isolation. */
export function LeaderboardTable({ entries, view, sort, onOpen }: { entries: LeaderboardEntry[]; view: LeaderboardView; sort: LeaderboardSort; onOpen: OpenRecords }) {
  const metricCell = (entry: LeaderboardEntry, column: Column): ReactNode => <button type="button" className="leaderboard-number" onClick={event => onOpen(entry, column.metric, event.currentTarget)} aria-label={`${column.label} for ${entry.label}: ${number(entry.metrics[column.key], column.score)}. View contributing public records.`}>
    {view === "accounts" && column.key === "net_votes" && entry.metrics.voter_count === 0 ? <span className="leaderboard-unrated">Unrated</span> : number(entry.metrics[column.key], column.score)}
  </button>;
  return <table className={`leaderboard-table leaderboard-table--${view}`}>
    <caption className="sr-only">{leaderboardViews.find(item => item.id === view)?.label}, ranked by {leaderboardSorts[view].find(item => item.id === sort)?.label.toLowerCase()}. Equal values share rank.</caption>
    <thead><tr><th scope="col" className="leaderboard-rank">Rank</th><th scope="col" className="leaderboard-name">{names[view]}</th>{columns[view].map(column => <th key={column.key} scope="col" className={`leaderboard-metric${column.sort === sort ? " is-selected" : ""}`} aria-sort={column.sort === sort ? "descending" : undefined}>{column.label}{column.sort === sort && <span aria-hidden="true"> ↓</span>}</th>)}</tr></thead>
    <tbody>{entries.map(entry => <tr key={entry.id}>
      <td className="leaderboard-rank">{entry.rank}</td>
      <th scope="row" className="leaderboard-name">
        {view === "researchers" ? <button type="button" className="leaderboard-record-name" onClick={event => onOpen(entry, entry.metrics.account_count ? "accounts" : "explored", event.currentTarget)}>{entry.label}</button> : <Link className="leaderboard-record-name" href={`/${view === "accounts" ? "accounts" : "id"}/${encodeURIComponent(entry.account_id || entry.id)}`}>{entry.label}</Link>}
        {view === "researchers" && verifiedOrcidHref(entry.orcid) && <a className="leaderboard-orcid" href={verifiedOrcidHref(entry.orcid)!} target="_blank" rel="noopener noreferrer">Verified ORCID <span aria-hidden="true">↗</span></a>}
        {view === "accounts" && <div className="leaderboard-record-context"><Contributor entry={entry} />{entry.gap && <Link href={`/knowledge-gaps/${encodeURIComponent(entry.gap.id)}`}>{entry.gap.label}</Link>}<span>{number(entry.metrics.claim_count)} associated {entry.metrics.claim_count === 1 ? "claim" : "claims"}</span></div>}
        {view === "datasets" && <button type="button" className="leaderboard-evidence-link" onClick={event => onOpen(entry, "files", event.currentTarget)}>Evidence and source files</button>}
        <details className="leaderboard-row-metrics"><summary>All metrics</summary><dl>{columns[view].map(column => <div key={column.key}><dt>{column.label}</dt><dd>{metricCell(entry, column)}</dd></div>)}</dl></details>
      </th>
      {columns[view].map(column => <td key={column.key} className={`leaderboard-metric${column.sort === sort ? " is-selected" : ""}`}>{metricCell(entry, column)}</td>)}
    </tr>)}</tbody>
  </table>;
}

function observed(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Observation time unavailable" : new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function MetricDefinition({ view, metric }: { view: LeaderboardView; metric: LeaderboardMetric | "overall" }) {
  let text = "Distinct currently public accounts. Multiple published copies of the same scientific identity count once.";
  if (metric === "overall") text = "A contribution score combining public accounts (30%), community recognition (40%), and gaps covered (30%). It reflects contributions relative to the current researcher cohort, not scientific validity or expertise.";
  else if (metric === "votes") text = view === "researchers" ? "Upvotes minus downvotes across this researcher’s public accounts, excluding their own ballots. Negative totals remain visible; they earn zero recognition points in the overall score." : "Upvotes minus downvotes on this canonical public account. These are community responses, not a scientific quality rating. An account with no votes is unrated.";
  else if (metric === "gaps") text = "Distinct knowledge gaps represented by qualifying public accounts. Contributing to a gap does not establish that it has been closed.";
  else if (metric === "explored") text = "Distinct gaps with a public account or a published exploration by the researcher. Saved drafts and private or repeated research runs add no credit.";
  else if (metric === "claims") text = view === "accounts" ? "Distinct component claims associated with this public scientific account. Each canonical claim identity counts once." : "Distinct component claims informed by this dataset through evidence targeting the claim’s proposition. Repeated paths to the same claim count once.";
  else if (metric === "researchers") text = view === "accounts" ? "The original registered contributor credited to this account. Upstream source authors and software agents do not receive researcher credit here." : "Distinct credited, registered researchers whose public accounts use this dataset as evidence. Anonymous and unavailable attribution add no researcher credit.";
  else if (metric === "files") text = "Concrete files reached by qualifying evidence paths. Other members of the same dataset are not counted just because they share its membership.";
  else if (view === "datasets") text = "Distinct currently public accounts with qualifying evidence paths to this dataset. Different versions retain their canonical identities; matching names do not combine datasets.";
  return <p className="leaderboard-definition">{text}</p>;
}

function ScoreBreakdown({ entry }: { entry: LeaderboardEntry }) {
  return <div className="leaderboard-score-breakdown"><h3>Overall contribution · {number(entry.metrics.overall_score, true)}</h3><dl>
    {([{ key: "accounts", label: "Public accounts", weight: 30 }, { key: "votes", label: "Community recognition", weight: 40 }, { key: "gaps", label: "Gap coverage", weight: 30 }] as const).map(item => <div key={item.key}><dt>{item.label}<span>{item.weight}% weight</span></dt><dd>{number(entry.components[item.key], true)}<span>percentile points</span></dd></div>)}
  </dl><p>Nonpositive values earn zero component points. A sole contributor’s positive component receives 50 points. Ties receive equal points; displayed scores are rounded to one decimal.</p></div>;
}

function Observation({ data }: { data: { as_of: string; score_version: string } }) {
  return <p className="leaderboard-observation">Observed <time dateTime={data.as_of}>{observed(data.as_of)}</time><span>Score version {data.score_version}</span></p>;
}

function RefreshRequired({ records = false, onReload }: { records?: boolean; onReload: () => void }) {
  return <div className="leaderboard-refresh-error" role="alert"><h2>{records ? "Public contributions changed" : "Rankings changed"}</h2><p>A publication or vote changed since this view was opened. Reload the first page to see the current public records.</p><button type="button" onClick={onReload}>{records ? "Reload contributions" : "Reload rankings"}</button></div>;
}

function Methodology({ data }: { data: Schema<"LeaderboardList"> | null }) {
  if (!data) return null;
  return <details className="leaderboard-methodology"><summary>How rankings work</summary>
    <div>
      <p>{data.methodology.scope}</p>
      <p>Overall contribution combines public accounts ({number(data.methodology.weights.accounts * 100)}%), community votes ({number(data.methodology.weights.votes * 100)}%), and gap coverage ({number(data.methodology.weights.gaps * 100)}%). It is a contribution score, not a measure of scientific validity.</p>
      <p>{data.methodology.score}</p>
      <p>{data.methodology.own_votes}</p>
      <p>{data.methodology.evidence}</p>
      <p>{data.cohort_size < 20 ? "Early community rankings. " : ""}{number(data.cohort_size)} eligible {data.cohort_size === 1 ? "researcher" : "researchers"} · {number(data.voting_participants)} voting {data.voting_participants === 1 ? "participant" : "participants"}.</p>
      <Observation data={data} />
    </div>
  </details>;
}

function PublicRecord({ record, metric }: { record: Schema<"LeaderboardRecord">; metric: LeaderboardMetric }) {
  const href = publicLeaderboardHref(record.url);
  const directions = Object.entries(record.directions).filter(([, count]) => count > 0);
  return <li className="leaderboard-public-record"><span className="leaderboard-record-kind">{record.kind.replaceAll("_", " ")}</span>
    {href ? <Link href={href}>{record.label}</Link> : <span>{record.label}</span>}
    {metric === "votes" && <small>Net community votes: {number(record.value)}</small>}
    {!!directions.length && <small>{directions.map(([direction, count]) => `${direction.toLowerCase()}: ${number(count)}`).join(" · ")}</small>}
    {(record.account_ids.length > 0 || record.claim_ids.length > 0 || record.evidence_ids.length > 0) && <details><summary>Contributing evidence paths</summary><div>{record.account_ids.map(id => <Link key={id} href={`/accounts/${encodeURIComponent(id)}`}>Public account <span className="leaderboard-record-id">{id}</span></Link>)}{record.claim_ids.map(id => <Link key={id} href={`/claims/${encodeURIComponent(id)}`}>Component claim <span className="leaderboard-record-id">{id}</span></Link>)}{record.evidence_ids.map(id => <Link key={id} href={`/id/${encodeURIComponent(id)}`}>Evidence interpretation <span className="leaderboard-record-id">{id}</span></Link>)}</div></details>}
  </li>;
}

function Records({ query, onTitle }: { query: LeaderboardQuery; onTitle: (label: string) => void }) {
  const binding = `${query.view}:${query.selected}:${query.metric}:${query.evidence}`;
  const [loaded, setLoaded] = useState<{ binding: string; data: Schema<"LeaderboardRecords"> } | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [restart, setRestart] = useState(false);
  const request = useRef<AbortController | null>(null), sequence = useRef(0);
  const visible = loaded?.binding === binding ? loaded.data : null;
  async function load(append = false) {
    if (!query.selected) return;
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    const serial = ++sequence.current; setBusy(true); setError(""); setRestart(false); if (!append) { setLoaded(null); onTitle(""); }
    try {
      const result = await api.leaderboardRecords(query.view, query.selected, { metric: query.metric, evidence: query.evidence, limit: 20, ...(append && visible?.page.next_cursor ? { cursor: visible.page.next_cursor } : {}) }, controller.signal);
      if (controller.signal.aborted || serial !== sequence.current) return;
      if (result.view !== query.view || result.id !== query.selected || result.metric !== query.metric || result.evidence !== query.evidence) throw new Error("These records do not match the selected contribution. Please retry.");
      if (append && visible?.page.snapshot_id !== result.page.snapshot_id) throw new LeaderboardPageChangedError("The public contributions changed. Reload this view.");
      if (result.page.has_more && (!result.page.next_cursor || (append && result.page.next_cursor === visible?.page.next_cursor))) throw new LeaderboardPageChangedError("The next contribution page could not be verified. Please reload.");
      const items = new Map<string, Schema<"LeaderboardRecord">>();
      for (const item of [...(append ? visible?.items || [] : []), ...result.items]) items.set(`${item.kind}:${item.id}`, item);
      setLoaded({ binding, data: { ...result, items: [...items.values()] } }); onTitle(result.entry.label);
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current) { setError(messageOf(failure)); if (failure instanceof LeaderboardPageChangedError || failure instanceof ApiError && failure.status === 409) { setLoaded(null); setRestart(true); } else if (failure instanceof ApiError && failure.status === 404) setLoaded(null); }
    } finally { if (serial === sequence.current) setBusy(false); }
  }
  useEffect(() => { void load(); return () => { sequence.current++; request.current?.abort(); }; }, [binding]);
  usePublicRevalidation(() => { void load(); });
  return <div className="leaderboard-records">
    <MetricDefinition view={query.view} metric={query.metric} />
    {query.view === "datasets" && <p className="leaderboard-filter-note">{query.evidence === "supporting" ? "Explicit supporting evidence only." : "All qualifying evidence directions."}</p>}
    {visible && <>
      {query.view === "researchers" && <ScoreBreakdown entry={visible.entry} />}
      {query.view === "accounts" && <dl className="leaderboard-vote-breakdown"><div><dt>Upvotes</dt><dd>{number(visible.entry.metrics.upvotes)}</dd></div><div><dt>Downvotes</dt><dd>{number(visible.entry.metrics.downvotes)}</dd></div><div><dt>Voters</dt><dd>{number(visible.entry.metrics.voter_count)}</dd></div></dl>}
      {query.view === "datasets" && <div className="leaderboard-direction-breakdown"><h3>Evidence directions</h3><dl>{Object.entries(visible.entry.directions).map(([direction, count]) => <div key={direction}><dt>{direction.toLowerCase()}</dt><dd>{number(count)}</dd></div>)}</dl><p>Counts describe interpretations targeting a proposition and can overlap.</p></div>}
      <h3 className="leaderboard-records-heading">{number(visible.total)} contributing public {visible.total === 1 ? "record" : "records"}</h3>
      {query.metric === "votes" && <p className="leaderboard-filter-note">Account records show the vote totals contributing to this measure.</p>}
      {visible.items.length ? <ul className="leaderboard-record-list">{visible.items.map(record => <PublicRecord key={`${record.kind}:${record.id}`} record={record} metric={query.metric} />)}</ul> : <p>No public records currently contribute to this measure.</p>}
      <Observation data={visible} />
    </>}
    {busy && <LoadingSurface compact title={visible ? "Loading more public records" : "Loading contributing public records"} skeleton={visible ? "none" : "rows"} rows={2} />}
    {restart ? <RefreshRequired records onReload={() => void load()} /> : error && <LoadingSurface compact skeleton="none" title="Contributions are unavailable" error={error} onRetry={() => void load()} />}
    {visible?.page.has_more && !busy && !error && <button type="button" className="leaderboard-load-more" onClick={() => void load(true)}>Show more records</button>}
  </div>;
}

export function Leaderboard() {
  const router = useRouter(), params = useSearchParams(), query = readLeaderboardQuery(params);
  const binding = `${query.view}:${query.sort}:${query.evidence}`;
  const [loaded, setLoaded] = useState<{ binding: string; data: Schema<"LeaderboardList"> } | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [restart, setRestart] = useState(false);
  const [detailTitle, setDetailTitle] = useState("");
  const request = useRef<AbortController | null>(null), sequence = useRef(0), opener = useRef<HTMLElement | null>(null);
  const dialog = useRef<HTMLDialogElement>(null), tabId = useId(), dialogTitleId = useId();
  const visible = loaded?.binding === binding ? loaded.data : null;
  const titleBinding = `${query.view}:${query.selected}`;
  useEffect(() => { setDetailTitle(""); }, [titleBinding]);
  async function load(append = false) {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    const serial = ++sequence.current; setBusy(true); setError(""); setRestart(false); if (!append) setLoaded(null);
    try {
      const result = await api.leaderboard({ view: query.view, sort: query.sort, evidence: query.evidence, limit: 20, ...(append && visible?.page.next_cursor ? { cursor: visible.page.next_cursor } : {}) }, controller.signal);
      if (controller.signal.aborted || serial !== sequence.current) return;
      setLoaded({ binding, data: mergeLeaderboardPages(append ? visible : null, result, query) });
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current) { setError(messageOf(failure)); if (failure instanceof LeaderboardPageChangedError || failure instanceof ApiError && failure.status === 409) { setLoaded(null); setRestart(true); } }
    } finally { if (serial === sequence.current) setBusy(false); }
  }
  useEffect(() => { void load(); return () => { sequence.current++; request.current?.abort(); }; }, [binding]);
  usePublicRevalidation(() => { void load(); });
  useEffect(() => {
    if (query.selected) { if (!dialog.current?.open) dialog.current?.showModal(); }
    else if (dialog.current?.open) dialog.current.close();
  }, [query.selected]);
  const navigate = (change: Partial<LeaderboardQuery>) => router.push(leaderboardHref(query, change), { scroll: false });
  const changeView = (view: LeaderboardView) => navigate({ view, sort: leaderboardSorts[view][0].id, selected: null });
  const openRecords: OpenRecords = (entry, metric, trigger) => { opener.current = trigger; setDetailTitle(entry.label); navigate({ selected: entry.id, metric }); };
  const closeDialog = () => { if (query.selected) router.replace(leaderboardHref(query, { selected: null }), { scroll: false }); };
  const hasEntries = !!visible?.items.length;
  const showFilters = (hasEntries && query.view !== "accounts") || (query.view === "datasets" && query.evidence === "supporting");
  const emptyTitle = query.view === "researchers" ? "No researchers yet." : query.view === "accounts" ? "No scientific accounts yet." : query.evidence === "supporting" ? "No datasets with supporting evidence yet." : "No datasets yet.";
  return <>
    <div className="leaderboard-tabs" role="tablist" aria-label="Leaderboard views">{leaderboardViews.map((view, index) => <button type="button" key={view.id} id={`${tabId}-${view.id}`} role="tab" aria-selected={query.view === view.id} aria-controls={`${tabId}-panel`} tabIndex={query.view === view.id ? 0 : -1} onClick={() => changeView(view.id)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? leaderboardViews.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + leaderboardViews.length) % leaderboardViews.length;
      changeView(leaderboardViews[next].id); document.getElementById(`${tabId}-${leaderboardViews[next].id}`)?.focus();
    }}>{view.label}</button>)}</div>
    <section id={`${tabId}-panel`} role="tabpanel" aria-labelledby={`${tabId}-${query.view}`}>
      {showFilters && <div className="leaderboard-toolbar">{hasEntries && <label>Rank by<select aria-label="Rank by" value={query.sort} onChange={event => navigate({ sort: event.target.value as LeaderboardSort, selected: null })}>{leaderboardSorts[query.view].map(sort => <option key={sort.id} value={sort.id}>{sort.label}</option>)}</select></label>}
        {query.view === "datasets" && <label>Evidence<select aria-label="Evidence filter" value={query.evidence} onChange={event => navigate({ evidence: event.target.value === "supporting" ? "supporting" : "all", selected: null })}><option value="all">All evidence</option><option value="supporting">Supporting evidence</option></select></label>}
      </div>}
      {hasEntries && visible && <LeaderboardTable entries={visible.items} view={query.view} sort={query.sort} onOpen={openRecords} />}
      {busy && <LoadingSurface title={visible ? "Loading more rankings" : "Loading rankings"} compact skeleton="none" />}
      {restart ? <RefreshRequired onReload={() => void load()} /> : error && <LoadingSurface compact title="Rankings are unavailable" error={error} skeleton="none" onRetry={() => void load()} />}
      {visible && !hasEntries && !busy && !error && <p className="leaderboard-empty">{emptyTitle}</p>}
      {visible?.page.has_more && !busy && !error && <button type="button" className="leaderboard-load-more" onClick={() => void load(true)}>Show more rankings</button>}
    </section>
    {hasEntries && <Methodology data={visible} />}
    <dialog ref={dialog} className="leaderboard-dialog" aria-labelledby={dialogTitleId} onCancel={event => { event.preventDefault(); closeDialog(); }} onClose={() => { if (opener.current?.isConnected) opener.current.focus({ preventScroll: true }); }}>
      <div className="leaderboard-dialog-heading"><h2 id={dialogTitleId}>{detailTitle || `${names[query.view]} contributions`}</h2><button type="button" autoFocus aria-label="Close contribution details" onClick={closeDialog}>×</button></div>
      <div className="leaderboard-dialog-body">{query.selected && <Records key={`${query.view}:${query.selected}:${query.metric}:${query.evidence}`} query={query} onTitle={setDetailTitle} />}</div>
    </dialog>
  </>;
}
