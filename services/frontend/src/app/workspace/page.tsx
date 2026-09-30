"use client";
import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { messageOf, type Schema } from "@/lib/client";
import { useIdentity, ProviderButtons } from "@/components/Session";
import { LoadingSurface } from "@/components/LoadingSurface";
import { workspaceRuns } from "@/lib/workspace";
import { WorkspaceGapRow } from "@/components/WorkspaceDrafts";
import { useWorkspaceData } from "@/components/WorkspaceCache";
import { workspaceTabs as tabs, type WorkspaceTab } from "@/lib/workspace-data";
import { outcomeHref } from "@/components/AnalysisOutcome";
import "@/components/workspace-activity.css";

const tabLabels = { gaps: "Knowledge gaps", accounts: "Scientific accounts", explorations: "Explorations" };
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
function PublicationStatus({ publication }: { publication?: Schema<"PublicationState"> }) {
  if (!publication) return <span className="workspace-visibility">Status unavailable</span>;
  const published = publication.visibility === "public";
  return <><span className={`workspace-visibility ${published ? "is-published" : "is-private"}`}>
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">{published ? <><circle cx="8" cy="8" r="6" /><ellipse cx="8" cy="8" rx="2.5" ry="6" /><path d="M2 8h12" /></> : <><rect x="3" y="7" width="10" height="7" rx="1.5" /><path d="M5 7V5a3 3 0 0 1 6 0v2M8 10v1" /></>}</svg>
    {published ? "Published" : "Private"}
  </span>{published && publication.has_unpublished_changes && <span className="workspace-unpublished">Unpublished changes</span>}</>;
}
export default function WorkspacePage() { return <Suspense fallback={<main id="main" className="reading-page workspace-dashboard"><LoadingSurface title="Opening your workspace" description="Retrieving your saved knowledge gaps, scientific accounts, and explorations." /></main>}><Workspace /></Suspense>; }
function Workspace() {
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab: WorkspaceTab = tabs.includes(requestedTab as WorkspaceTab) ? requestedTab as WorkspaceTab : "gaps";
  const [localGaps, setLocalGaps] = useState<Schema<"GapRecord">[]>([]);
  const [searchText, setSearchText] = useState({ accounts: "", explorations: "" });
  const [searches, setSearches] = useState({ accounts: "", explorations: "" });
  const { me, ready } = useIdentity();
  const query = tab === "gaps" ? "" : searches[tab];
  const cached = useWorkspaceData(tab, query);
  const { gaps = [], accounts = [], outcomes = [], drafts = [], jobs = [], requests = [], cursor = null } = cached.data || {};
  const { loading, loadingMore, counts } = cached;
  const error = cached.error ? messageOf(cached.error) : "";
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { try { setLocalGaps(JSON.parse(sessionStorage.getItem("reveal:local-explorations") || "[]")); } catch { /* empty browser history */ } }, []);
  useEffect(() => { setSearchText({ accounts: "", explorations: "" }); setSearches({ accounts: "", explorations: "" }); }, [me?.user_id]);
  const activity = workspaceRuns(jobs, requests, drafts);
  const load = (append = false) => append ? cached.loadMore() : cached.refresh();
  const chooseTab = (value: WorkspaceTab) => { window.history.replaceState(null, "", `?tab=${value}`); };
  const count = (value: WorkspaceTab) => me ? counts[value] : ready ? String(value === "gaps" ? localGaps.length : 0) : undefined;
  const initialLoading = !ready || (!!me && !cached.data && !error);
  const signedIn = me?.principal_kind === "registered";
  const hasCurrentData = !!me && !!cached.data;
  const isEmpty = tab === "gaps" ? (me ? gaps.length : localGaps.length) === 0 : !me || (tab === "accounts" ? accounts.length === 0 : outcomes.length === 0);
  const clearSearch = () => { if (tab !== "gaps") { setSearchText(value => ({ ...value, [tab]: "" })); setSearches(value => ({ ...value, [tab]: "" })); } };
  return <main id="main" className="reading-page workspace-dashboard">
    <nav className="workspace-topbar" aria-label="Workspace navigation"><Link href="/">← Explore knowledge gaps</Link>{me && <button className="text-button workspace-refresh" disabled={loading} onClick={() => void cached.refresh()}>Refresh</button>}</nav>
    <div className="workspace-heading"><h1>Your workspace</h1><p>{signedIn ? "Your knowledge gaps, scientific accounts, and explorations, together." : "A place for the questions you explore and the accounts you build."}</p></div>
    {ready && !signedIn && <div className="workspace-continuity"><div><strong>{me ? "Anonymous workspace" : "Temporary workspace"}</strong><p>{me ? "Access is tied to this browser session. Sign in to keep your work." : "Your selections stay in this tab until you continue anonymously or sign in."}</p>{me?.workspace_expires_at && <small>Session access ends {new Date(me.workspace_expires_at).toLocaleDateString()}.</small>}</div><button onClick={() => dialog.current?.showModal()}>Sign in to keep your work</button></div>}
    {me && cached.connection !== "live" && <p role="status" className="workspace-stream-status">{cached.connection === "expired" ? "Your session has expired. Sign in to refresh your workspace." : cached.connection === "reconnecting" ? "Reconnecting to workspace updates. Saved results remain available." : "Connecting to workspace updates…"}</p>}
    <div className="workspace-tabs" role="tablist" aria-label="Workspace view">{tabs.map((value, index) => <button key={value} id={`workspace-tab-${value}`} role="tab" aria-selected={tab === value} aria-controls="workspace-results" tabIndex={tab === value ? 0 : -1} onClick={() => chooseTab(value)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault();
      const next: WorkspaceTab = event.key === "Home" ? "gaps" : event.key === "End" ? tabs[tabs.length - 1] : tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      chooseTab(next); document.getElementById(`workspace-tab-${next}`)?.focus();
    }}>{tabLabels[value]}{count(value) !== undefined && <span title={count(value)!.endsWith("+") ? "More records are available. Load more to see the remaining history." : undefined}>{count(value)}</span>}</button>)}</div>
    {me && tab !== "gaps" && <form className="workspace-search" role="search" aria-label={`Search ${tabLabels[tab].toLowerCase()}`} onSubmit={event => { event.preventDefault(); setSearches(value => ({ ...value, [tab]: searchText[tab].trim() })); }}>
      <label htmlFor={`workspace-search-${tab}`}>Search {tabLabels[tab].toLowerCase()}</label>
      <div className="workspace-search-controls"><input id={`workspace-search-${tab}`} type="search" maxLength={200} value={searchText[tab]} placeholder={tab === "accounts" ? "Search questions, titles, or findings" : "Search questions, findings, or mechanisms"} onChange={event => { const value = event.target.value; setSearchText(current => ({ ...current, [tab]: value })); if (!value) clearSearch(); }} /><button type="submit">Search</button>{(query || searchText[tab]) && <button type="button" className="workspace-search-clear" onClick={clearSearch}>Clear</button>}</div>
      {query && <p role="status">{loading ? "Searching…" : `${tab === "accounts" ? accounts.length : outcomes.length}${cursor ? "+" : ""} matching ${tabLabels[tab].toLowerCase()}`} for “{query}”</p>}
    </form>}
    <div id="workspace-results" role="tabpanel" aria-labelledby={`workspace-tab-${tab}`} aria-busy={initialLoading || loading}>
      {initialLoading ? <LoadingSurface key={`${me?.user_id || "session"}:${tab}`} title={!ready ? "Opening your workspace" : `Loading your ${tabLabels[tab].toLowerCase()}`} description={!ready ? "Checking this browser’s access to your saved work." : "Your saved explorations will appear here when the request completes."} /> : <>
        {tab === "gaps" ? me ? (hasCurrentData ? gaps : []).map(item => <WorkspaceGapRow key={`${me.user_id}:${item.source_gap.source_id}`} item={item} drafts={drafts.filter(draft => draft.composer.source_gap?.id === item.source_gap.id)} activity={activity} refresh={cached.refresh} />) : localGaps.map(gap => <article className="workspace-gap-row" key={gap.object.id}><Link className="workspace-item-title" href={`/?gap=${encodeURIComponent(gap.object.id)}`}>{gap.object.text}</Link><div className="workspace-item-meta">{gap.source.disease_label && <span>{gap.source.disease_label}</span>}<span>Kept in this browser</span></div></article>) : tab === "explorations" ? (hasCurrentData ? outcomes : []).map(item => <article className="workspace-account-row" key={item.id}>
          <div className="workspace-item-meta"><PublicationStatus publication={item.publication} /><time dateTime={item.created_at}>Explored {shortDate(item.created_at)}</time></div>
          <Link className="workspace-item-title" href={outcomeHref(item.id)}>{item.knowledge_gap.text}</Link>
          <p className="workspace-account-conclusion">{item.summary}</p>
          <div className="workspace-item-meta"><span>No supported scientific account</span><span>{item.anchors.length} mechanism {item.anchors.length === 1 ? "anchor" : "anchors"}</span><Link href={outcomeHref(item.id)}>View findings and evidence gaps</Link></div>
        </article>) : (hasCurrentData ? accounts : []).map(item => <article className="workspace-account-row" key={item.account.id}>
          <div className="workspace-item-meta"><PublicationStatus publication={item.publication} /><time dateTime={item.created_at}>Saved {shortDate(item.created_at)}</time></div>
          <Link className="workspace-item-title workspace-account-summary-link" href={`/accounts/${encodeURIComponent(item.account.id)}`}>{item.account.closing_remarks || item.account.context || item.account.name || item.knowledge_gap.text}</Link>
          <Link className="workspace-account-question" href={`/?gap=${encodeURIComponent(item.knowledge_gap.id)}`}>{item.knowledge_gap.text}</Link>
          <div className="workspace-item-meta"><span>{item.claim_count} associated {item.claim_count === 1 ? "claim" : "claims"}</span><span>Research statement {item.research_statement.status.replaceAll("_", " ")}</span><Link href={`/?gap=${encodeURIComponent(item.knowledge_gap.id)}`}>View knowledge gap</Link></div>
        </article>)}
        {loadingMore && <LoadingSurface compact skeleton="none" title={`Loading more ${tabLabels[tab].toLowerCase()}`} description="The records already shown remain available." />}
        {isEmpty && !loading && !error && <div className="workspace-empty">{query ? <><h2>No matching {tabLabels[tab].toLowerCase()}.</h2><p>Try another term or clear the search to see all your saved work.</p><button className="text-button" onClick={clearSearch}>Clear search</button></> : <><h2>{tab === "gaps" ? "Your next question starts here." : "Room for your findings."}</h2><p>{tab === "gaps" ? "Knowledge gaps you explore will appear here." : tab === "accounts" ? "Scientific accounts will appear here as your analyses finish. Runs without a supported account are saved under Explorations." : "Completed runs without a supported scientific account will appear here with their findings and evidence gaps."}</p><Link href="/">Explore knowledge gaps</Link></>}</div>}
        {me && cursor && <button className="text-button workspace-load-more" disabled={loading} onClick={() => void load(true)}>Load more</button>}
      </>}
    </div>
    {error && <p className="error" role="alert">{cached.data && "Could not refresh; showing previously loaded records. "}{error} <button onClick={() => void load()}>Retry</button></p>}
    <dialog ref={dialog} className="auth-dialog workspace-sign-in" aria-labelledby="workspace-sign-in-title"><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id="workspace-sign-in-title">Keep your work</h2><p>Sign in to return to your knowledge gaps, scientific accounts, and explorations.</p><ProviderButtons /><small>Your saved scientific identities and attribution stay unchanged.</small></dialog>
  </main>;
}
