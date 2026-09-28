"use client";
import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, messageOf, type Schema } from "@/lib/client";
import { useIdentity, ProviderButtons } from "@/components/Session";
import { LoadingSurface } from "@/components/LoadingSurface";
import "@/components/workspace-activity.css";

type WorkspaceTab = "gaps" | "accounts";
const tabLabels = { gaps: "Knowledge gaps", accounts: "Scientific accounts" };
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
export default function WorkspacePage() { return <Suspense fallback={<main id="main" className="reading-page workspace-dashboard"><LoadingSurface title="Opening your workspace" description="Retrieving your saved knowledge gaps and scientific accounts." /></main>}><Workspace /></Suspense>; }
function Workspace() {
  const searchParams = useSearchParams();
  const [localGaps, setLocalGaps] = useState<Schema<"GapRecord">[]>([]);
  const { me, ready } = useIdentity(); const [tab, setTab] = useState<WorkspaceTab>("gaps"); const [error, setError] = useState(""); const [loading, setLoading] = useState(false);
  const [gaps, setGaps] = useState<Schema<"Exploration">[]>([]); const [accounts, setAccounts] = useState<Schema<"AccountSummary">[]>([]); const [drafts, setDrafts] = useState<Schema<"Draft">[]>([]); const [jobs, setJobs] = useState<Schema<"Job">[]>([]);
  const [requests, setRequests] = useState<Schema<"ResearchRequest">[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState("");
  const [counts, setCounts] = useState<Partial<Record<WorkspaceTab, string>>>({});
  const loadSequence = useRef(0); const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { setTab(searchParams.get("tab") === "accounts" ? "accounts" : "gaps"); }, [searchParams]);
  useEffect(() => { try { setLocalGaps(JSON.parse(sessionStorage.getItem("reveal:local-explorations") || "[]")); } catch { /* empty browser history */ } }, []);
  const load = async (append = false) => {
    if (!me) return; const sequence = ++loadSequence.current; setLoading(true); setError("");
    try {
      if (tab === "gaps") {
        const [list, saved, runs, frozen] = await Promise.all([api.explorations(append ? cursor || undefined : undefined), api.drafts(), api.jobs(), api.requests()]);
        if (sequence !== loadSequence.current) return;
        const items = append ? [...gaps, ...list.items] : list.items;
        setRequests(frozen.items); setGaps(items); setDrafts(saved.items); setJobs(runs.items); setCursor(list.page.next_cursor);
        setCounts(current => ({ ...current, gaps: `${items.length}${list.page.has_more ? "+" : ""}` }));
      } else {
        const list = await api.accounts(append ? cursor || undefined : undefined); if (sequence !== loadSequence.current) return;
        const items = append ? [...accounts, ...list.items] : list.items;
        setAccounts(items); setCursor(list.page.next_cursor); setCounts(current => ({ ...current, accounts: `${items.length}${list.page.has_more ? "+" : ""}` }));
      }
      setLoadedFor(`${me.user_id}:${tab}`);
    } catch (failure) { if (sequence === loadSequence.current) setError(messageOf(failure)); } finally { if (sequence === loadSequence.current) setLoading(false); }
  };
  useEffect(() => { setCursor(null); void load(); return () => { loadSequence.current++; }; }, [tab, me?.user_id]);
  const chooseTab = (value: WorkspaceTab) => { setTab(value); window.history.replaceState(null, "", `?tab=${value}`); };
  const count = (value: WorkspaceTab) => me ? counts[value] : ready ? String(value === "gaps" ? localGaps.length : 0) : undefined;
  const initialLoading = !ready || (!!me && loadedFor !== `${me.user_id}:${tab}` && !error);
  const signedIn = me?.principal_kind === "registered";
  const isEmpty = tab === "gaps" ? (me ? gaps.length : localGaps.length) === 0 : accounts.length === 0;
  const jobLink = (job: Schema<"Job">, fallback: string | null) => {
    const draft = requests.find(request => request.id === job.research_request_id)?.source_draft_id || fallback;
    const query = new URLSearchParams({ job: job.id }); if (draft) query.set("draft", draft);
    return `/?${query}`;
  };
  return <main id="main" className="reading-page workspace-dashboard">
    <nav className="workspace-topbar" aria-label="Workspace navigation"><Link href="/">← Explore knowledge gaps</Link></nav>
    <div className="workspace-heading"><h1>Your workspace</h1><p>{signedIn ? "Your knowledge gaps and scientific accounts, together." : "A place for the questions you explore and the accounts you build."}</p></div>
    <div className="workspace-continuity"><div><strong>{signedIn ? me.display_name || "Signed-in workspace" : me ? "Anonymous workspace" : "Temporary workspace"}</strong><p>{signedIn ? "Your explorations and scientific accounts are saved to this workspace." : me ? "Access is tied to this browser session. Sign in to keep your work." : "Your selections stay in this tab until you continue anonymously or sign in."}</p>{me?.principal_kind === "anonymous" && me.workspace_expires_at && <small>Session access ends {new Date(me.workspace_expires_at).toLocaleDateString()}.</small>}</div>{!signedIn && <button disabled={!ready} onClick={() => dialog.current?.showModal()}>Sign in to keep your work</button>}</div>
    <div className="workspace-tabs" role="tablist" aria-label="Workspace view">{(["gaps", "accounts"] as const).map((value, index) => <button key={value} id={`workspace-tab-${value}`} role="tab" aria-selected={tab === value} aria-controls="workspace-results" tabIndex={tab === value ? 0 : -1} onClick={() => chooseTab(value)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault();
      const next: WorkspaceTab = event.key === "Home" ? "gaps" : event.key === "End" ? "accounts" : index === 0 ? "accounts" : "gaps";
      chooseTab(next); document.getElementById(`workspace-tab-${next}`)?.focus();
    }}>{tabLabels[value]}{count(value) !== undefined && <span title={count(value)!.endsWith("+") ? "More records are available. Load more to see the remaining history." : undefined}>{count(value)}</span>}</button>)}</div>
    <div id="workspace-results" role="tabpanel" aria-labelledby={`workspace-tab-${tab}`} aria-busy={initialLoading || loading}>
      {initialLoading ? <LoadingSurface key={`${me?.user_id || "session"}:${tab}`} title={!ready ? "Opening your workspace" : `Loading your ${tabLabels[tab].toLowerCase()}`} description={!ready ? "Checking this browser’s access to your saved work." : "Your saved explorations will appear here when the request completes."} /> : <>
        {tab === "gaps" ? me ? gaps.map(item => <article className="workspace-gap-row" key={item.source_gap.source_id}>
          <Link className="workspace-item-title" href={item.draft_id ? `/?draft=${item.draft_id}` : `/?gap=${encodeURIComponent(item.source_gap.id)}`}>{item.knowledge_gap.text}</Link>
          <div className="workspace-item-meta">{item.knowledge_gap.scope && <span>{item.knowledge_gap.scope}</span>}<time dateTime={item.last_explored_at}>Explored {shortDate(item.last_explored_at)}</time>{item.scientific_accounts.count > 0 && <Link href="/workspace?tab=accounts">{item.scientific_accounts.count} scientific {item.scientific_accounts.count === 1 ? "account" : "accounts"}</Link>}{drafts.filter(draft => draft.composer.source_gap?.id === item.source_gap.id).map(draft => <Link key={draft.id} href={`/?draft=${draft.id}`}>{draft.composer.eaggl_anchors.length} anchors · Resume draft</Link>)}{jobs.filter(job => job.kind === "analysis" && requests.some(request => request.id === job.research_request_id && request.composer.source_gap?.id === item.source_gap.id)).slice(0, 1).map(job => <Link key={job.id} href={jobLink(job, item.draft_id)}>Research {job.status.replaceAll("_", " ")}</Link>)}</div>
        </article>) : localGaps.map(gap => <article className="workspace-gap-row" key={gap.object.id}><Link className="workspace-item-title" href={`/?gap=${encodeURIComponent(gap.object.id)}`}>{gap.object.text}</Link><div className="workspace-item-meta">{gap.source.disease_label && <span>{gap.source.disease_label}</span>}<span>Kept in this browser</span></div></article>) : accounts.map(item => <article className="workspace-account-row" key={item.account.id}>
          <div className="workspace-item-meta"><time dateTime={item.created_at}>Saved {shortDate(item.created_at)}</time></div>
          <Link className="workspace-item-title" href={`/accounts/${encodeURIComponent(item.account.id)}`}>{item.account.name || "Scientific account"}</Link>
          <p className="workspace-account-conclusion">{item.account.closing_remarks || item.account.context}</p>
          <Link className="workspace-account-question" href={`/?gap=${encodeURIComponent(item.knowledge_gap.id)}`}>{item.knowledge_gap.text}</Link>
          <div className="workspace-item-meta"><span>{item.claim_count} associated {item.claim_count === 1 ? "claim" : "claims"}</span><span>Research statement {item.research_statement.status.replaceAll("_", " ")}</span><Link href={`/?gap=${encodeURIComponent(item.knowledge_gap.id)}`}>View knowledge gap</Link></div>
        </article>)}
        {loading && <LoadingSurface compact skeleton="none" title={`Loading more ${tabLabels[tab].toLowerCase()}`} description="The records already shown remain available." />}
        {isEmpty && !loading && !error && <div className="workspace-empty"><h2>{tab === "gaps" ? "Your next question starts here." : "Room for your findings."}</h2><p>{tab === "gaps" ? "Knowledge gaps you explore will appear here." : "Scientific accounts will appear here as your analyses finish."}</p><Link href="/">Explore knowledge gaps</Link></div>}
        {me && cursor && <button className="text-button workspace-load-more" disabled={loading} onClick={() => void load(true)}>Load more</button>}
      </>}
    </div>
    {error && <p className="error" role="alert">{error} <button onClick={() => void load()}>Retry</button></p>}
    <dialog ref={dialog} className="auth-dialog workspace-sign-in" aria-labelledby="workspace-sign-in-title"><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id="workspace-sign-in-title">Keep your work</h2><p>Sign in to return to your knowledge gaps and scientific accounts.</p><ProviderButtons /><small>Your saved scientific identities and attribution stay unchanged.</small></dialog>
  </main>;
}
