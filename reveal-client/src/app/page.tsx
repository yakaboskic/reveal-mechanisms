"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, backend, errorMessage, request } from "../lib/api";
import { followJob, followWorkspace } from "../lib/events";
import { createMutationKeys } from "../lib/mutations";
import { emptyComposer, terminal, withFactors, type AnalysisInput, type Composer, type Draft, type Factor, type Gap, type Job, type JobEvent, type Me, type Schema } from "../lib/types";

type PendingSubmission = { body: AnalysisInput; key: string };
const readable = (value: string) => value.replaceAll("_", " ");
const date = (value: string) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const gapTitle = (gap: Gap) => gap.object.name || gap.object.text || "Knowledge gap";
const factorTitle = (factor: Factor) => factor.cfde_anchor.label || factor.object.name || factor.source_id;
function setLocation(kind: "draft" | "job", id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set(kind, id); else url.searchParams.delete(kind);
  window.history.replaceState(null, "", url);
}
function storedIntent(user: string): PendingSubmission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem("reveal-submit:" + user) || "null");
    return value?.body?.kind === "analysis" && typeof value.body.draft_id === "string" && Number.isInteger(value.body.draft_version) && typeof value.key === "string" ? value : null;
  } catch { return null; }
}

export default function Home() {
  const [principal, setPrincipal] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [moreRecords, setMoreRecords] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [name, setName] = useState("");
  const [composer, setComposer] = useState<Composer>(emptyComposer);
  const [gap, setGap] = useState<Gap | null>(null);
  const [query, setQuery] = useState("coronary artery disease");
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestion, setSuggestion] = useState<Schema<"Suggestions"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Factor>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [activity, setActivity] = useState<JobEvent[]>([]);
  const [streamState, setStreamState] = useState("");
  const [workspaceState, setWorkspaceState] = useState("");
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [workspaceAttempt, setWorkspaceAttempt] = useState(0);
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const editorGeneration = useRef(0), jobGeneration = useRef(0);
  const collections = useRef<{ drafts: boolean; jobs: boolean; flight: Promise<void> | null }>({ drafts: false, jobs: false, flight: null });
  const identity = useRef<string | null>(null);
  const searchAbort = useRef<AbortController | null>(null), suggestionAbort = useRef<AbortController | null>(null);
  const mutationKeys = useRef(createMutationKeys());
  const currentJob = useRef<Job | null>(null);
  const startFocus = useRef<string | null>(null);
  const dirty = !draft || JSON.stringify(draft.composer) !== JSON.stringify(composer) || (draft.name || "") !== name;
  const mutable = Boolean(principal) && !busy;

  const refresh = useCallback((kinds: string[] = ["drafts", "jobs"]): Promise<void> => {
    if (!identity.current) return Promise.resolve();
    const state = collections.current;
    state.drafts ||= kinds.includes("drafts"); state.jobs ||= kinds.includes("jobs");
    if (state.flight) return state.flight;
    state.flight = (async () => {
      try {
        // Mutations during an in-flight read schedule one further read, never overlap it.
        while (identity.current && (state.drafts || state.jobs)) {
          const owner = identity.current, draftsNeeded = state.drafts, jobsNeeded = state.jobs;
          state.drafts = false; state.jobs = false;
          const [saved, recent] = await Promise.all([draftsNeeded ? api.drafts() : null, jobsNeeded ? api.jobs() : null]);
          if (identity.current !== owner) continue;
          if (saved) setDrafts(saved.items);
          if (recent) setJobs(recent.items);
          if (saved?.page.has_more || recent?.page.has_more) setMoreRecords(true);
        }
      } finally { state.flight = null; }
    })();
    return state.flight;
  }, []);
  const openJob = useCallback(async (id: string) => {
    const generation = ++jobGeneration.current, owner = identity.current;
    try {
      const value = await api.job(id);
      if (generation !== jobGeneration.current || owner !== identity.current) return;
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", id); setError("");
    } catch (error) { if (generation === jobGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, []);
  const openDraft = useCallback(async (id: string) => {
    const generation = ++editorGeneration.current, owner = identity.current;
    suggestionAbort.current?.abort(); setSuggesting(false);
    try {
      const value = await api.draft(id);
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setSuggestion(null); setGap(null);
      setLocation("draft", id); setNotice("Saved draft loaded."); setError("");
      if (value.composer.source_gap) {
        const selected = await api.gap(value.composer.source_gap.id);
        if (generation === editorGeneration.current) setGap(selected);
      }
    } catch (error) { if (generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, []);

  useEffect(() => {
    let active = true;
    api.session().then(value => { if (active) setPrincipal(value.principal); }).catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    identity.current = principal?.user_id || null;
    if (!principal) return;
    setPending(storedIntent(principal.user_id));
    void refresh().catch(error => setError(errorMessage(error)));
    const params = new URLSearchParams(window.location.search);
    if (params.get("draft")) void openDraft(params.get("draft")!);
    if (params.get("job")) void openJob(params.get("job")!);
    return () => { identity.current = null; ++editorGeneration.current; ++jobGeneration.current; };
  }, [principal?.user_id, refresh, openDraft, openJob]);
  useEffect(() => {
    if (!principal) return;
    const controller = new AbortController(); let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const invalidated = new Set<string>();
    const scheduleRefresh = (kinds: string[]) => {
      for (const kind of kinds) invalidated.add(kind);
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        const kinds = [...invalidated]; invalidated.clear();
        void refresh(kinds).catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); });
      }, 120);
    };
    void followWorkspace({ signal: controller.signal, onState: setWorkspaceState, onChange: change => {
      const kinds = change ? change.collections.filter(value => ["drafts", "jobs"].includes(value)) : ["drafts", "jobs"];
      if (kinds.length) scheduleRefresh(kinds);
    } }).catch(error => { if (!controller.signal.aborted) setWorkspaceState(errorMessage(error)); });
    return () => { controller.abort(); if (refreshTimer) clearTimeout(refreshTimer); };
  }, [principal?.user_id, workspaceAttempt, refresh]);
  useEffect(() => {
    if (!principal || !job) return;
    const id = job.id, controller = new AbortController();
    setStreamState("Connecting to live activity…");
    async function readLatest() {
      const value = await api.job(id);
      if (!controller.signal.aborted && currentJob.current?.id === id) { currentJob.current = value; setJob(value); }
      return value;
    }
    void followJob(id, { signal: controller.signal, onState: setStreamState,
      onResync: async () => { const value = await readLatest(); setNotice("Older activity is unavailable. The current saved job was reloaded."); return { cursor: value.last_event_id, terminal: terminal(value.status) }; },
      onEvent: event => {
        if (controller.signal.aborted) return;
        setActivity(items => items.some(item => item.id === event.id) ? items : [...items, event].slice(-1000));
        setJob(value => value?.id === id ? { ...value, status: event.status, stage: event.stage, result: event.result || value.result,
          updated_at: event.occurred_at, last_event_id: event.id } : value);
        if (terminal(event.status)) { setStreamState("Activity complete"); void readLatest().then(() => refresh(["jobs"])).catch(error => setError(errorMessage(error))); }
      },
    }).then(() => { if (!controller.signal.aborted) setStreamState("Activity complete"); })
      .catch(error => { if (!controller.signal.aborted) setStreamState(errorMessage(error)); });
    return () => controller.abort();
  // The stream owns status changes; only selecting a different job reconnects it.
  }, [principal?.user_id, job?.id, streamAttempt, refresh]);
  useEffect(() => () => { searchAbort.current?.abort(); suggestionAbort.current?.abort(); }, []);
  useEffect(() => {
    if (welcomeOpen || !startFocus.current) return;
    const id = startFocus.current;
    startFocus.current = null;
    const element = document.getElementById(id);
    element?.scrollIntoView({ block: "center" });
    element?.focus();
  }, [welcomeOpen]);

  async function connect() {
    setBusy("connect"); setError("");
    try {
      const value = await api.connect(); setPrincipal(value.principal); setNotice("");
      const params = new URLSearchParams(window.location.search);
      setWelcomeOpen(!params.has("draft") && !params.has("job"));
    }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function disconnect() {
    setBusy("disconnect");
    try {
      await api.disconnect(); setWelcomeOpen(false); setPrincipal(null); currentJob.current = null; setJob(null); setDraft(null); setJobs([]); setDrafts([]);
      setComposer(emptyComposer()); setName(""); setGap(null); setSuggestion(null); setActivity([]); setPending(null);
      setNotice("Disconnected. Saved work and running jobs remain on the server.");
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function search() {
    searchAbort.current?.abort(); const controller = new AbortController(); searchAbort.current = controller;
    setSearching(true); setError("");
    try { const values = await api.gaps(query, controller.signal); if (!controller.signal.aborted) { setGaps(values); setSearched(true); } }
    catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  async function suggest(next: Composer, replaceSelection: boolean) {
    suggestionAbort.current?.abort(); const controller = new AbortController(); suggestionAbort.current = controller;
    const generation = editorGeneration.current; setSuggesting(true); setError("");
    try {
      const value = await api.suggest(next, controller.signal);
      if (controller.signal.aborted || generation !== editorGeneration.current) return;
      setSuggestion(value); setFactors(values => ({ ...values, ...Object.fromEntries(value.automatic_anchors.map(anchor => [anchor.factor.source_id, anchor.factor])) }));
      if (replaceSelection) setComposer(current => withFactors(current, value.automatic_anchors.slice(0, 1).map(anchor => anchor.factor), value.suggestion_id, true));
    } catch (error) { if (!controller.signal.aborted && generation === editorGeneration.current) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSuggesting(false); }
  }
  function selectGap(value: Gap) {
    ++editorGeneration.current;
    const next: Composer = { ...composer, source_gap: { id: value.object.id, source_id: value.source.source_id, source_revision: value.source.source_revision }, eaggl_anchors: [], dismissed_source_ids: [] };
    setGap(value); setComposer(next); setSuggestion(null); setNotice(""); setGaps([]); setSearched(false);
    void suggest(next, true);
  }
  function newDraft() {
    if (dirty && (composer.source_gap || name) && !window.confirm("Start a new draft? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setLocation("draft", null); setNotice(""); setError("");
  }
  function startFrom(action: "draft" | "saved" | "gap" | "jobs") {
    if (action === "draft") newDraft();
    startFocus.current = action === "draft" ? "draft-name" : action === "saved" ? "saved-draft" : action === "gap" ? "gap-search" : "job-select";
    setWelcomeOpen(false);
  }
  async function save() {
    setBusy("save"); setError("");
    try {
      const value = await mutationKeys.current.run(["save", draft?.id, draft?.version, composer, name], key => api.save(draft, composer, name, key));
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setLocation("draft", value.id); setNotice("Draft saved. You can now start the analysis."); await refresh();
    } catch (error) { setError(error instanceof ApiError && error.status === 409 && error.code !== "REFERENCE_GENERATION_SUPERSEDED" ? "This draft changed on the server. Reload the saved draft below to review it before saving again. Your local selections remain visible." : errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function submit() {
    if (!principal || (!pending && (!draft || dirty))) return;
    const intent = pending || { body: { kind: "analysis" as const, draft_id: draft!.id, draft_version: draft!.version, budgets: { max_accounts: 1 } }, key: crypto.randomUUID() };
    setPending(intent); sessionStorage.setItem("reveal-submit:" + principal.user_id, JSON.stringify(intent));
    setBusy("submit"); setError("");
    try {
      const value = await api.submit(intent.body, intent.key);
      sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", value.id); setNotice("Analysis submitted. Live activity will appear alongside your draft."); await refresh();
    } catch (error) {
      // A later refusal cannot disprove an earlier committed request whose response was lost.
      setError(errorMessage(error));
    } finally { setBusy(""); }
  }
  function discardSubmission() {
    if (!principal || !window.confirm("The original request may already have created a job. Check Workspace jobs first. Discard this recovery key and allow a new submission?")) return;
    sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
  }
  async function cancel() {
    if (!job) return; const id = job.id; setBusy("cancel"); setError("");
    try { const value = await api.cancel(id); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); } setNotice("Stop requested. The service will preserve captured work and clean up the research sandbox."); await refresh(); }
    catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }
  async function retryReview() {
    if (!job) return; const id = job.id; setBusy("review"); setError("");
    try {
      const value = await mutationKeys.current.run(["review", job.id, job.last_event_id], key => api.retryReview(job, key)); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); setActivity([]); setStreamAttempt(value => value + 1); } await refresh();
    } catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }

  return <>
    <header className="site-header"><div className="brand"><span className="brand-mark" aria-hidden="true">R</span><div><h1>REVEAL client</h1><p>Scientific questions, evidence, and live research</p></div></div>
      <div className="connection"><span className={principal ? "connection-dot connected" : "connection-dot"} /><span>{principal ? "QA workspace connected" : checking ? "Checking workspace…" : "QA workspace"}</span>
        {principal ? <button onClick={disconnect} disabled={!!busy}>{busy === "disconnect" ? "Disconnecting…" : "Disconnect workspace"}</button> : <span className="connection-status">Disconnected</span>}
      </div></header>
    <main>
      {error && <div className="notice error" role="alert"><span>{error}</span><button className="quiet" onClick={() => setError("")} aria-label="Dismiss error">Dismiss</button></div>}
      {principal && notice && <p className="notice" role="status">{notice}</p>}
      {!principal ? <section className="welcome"><div className="welcome-lead"><h2>Choose the gap. Ground the claim.</h2><button onClick={connect} disabled={!!busy || checking}>{checking ? "Checking workspace…" : busy === "connect" ? "Connecting…" : "Connect workspace"}</button></div><div className="welcome-cards"><a className="welcome-card" href="#learn-reveal-client" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><path d="M36 12v32M14 16c7 5 15 5 22-2 7 7 15 7 22 2v28c-7 5-15 5-22-2-7 7-15 7-22 2V16z" /></svg><strong>Learn REVEAL client</strong><span>A guide to the workspace, from a knowledge gap to a grounded claim.</span></a><a className="welcome-card" href="#quick-start-demo" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><rect x="14" y="12" width="44" height="32" rx="3" /><path className="card-icon-fill" d="M33 22l12 6-12 6z" /></svg><strong>Watch quick start demo</strong><span>A short walkthrough of connecting and starting an investigation.</span></a></div></section> : welcomeOpen ? <div className="welcome-panel-stage"><section className="welcome-panel" role="dialog" aria-modal="true" aria-labelledby="welcome-heading"><h2 id="welcome-heading">Welcome to your workspace</h2><div className="welcome-options"><button className="secondary" onClick={() => startFrom("draft")}>Start a new draft</button><button className="secondary" onClick={() => startFrom("saved")} disabled={!drafts.length}>Open saved draft</button><button className="secondary" onClick={() => startFrom("gap")}>Choose a knowledge gap</button><button className="secondary" onClick={() => startFrom("jobs")}>Workspace jobs</button></div></section></div> : <>
        <div className="workspace-toolbar"><span>{principal.display_name || "Research workspace"}</span><div><span className="muted small">{workspaceState || "Connecting workspace updates…"}</span><button className="quiet small" onClick={() => { setWorkspaceAttempt(value => value + 1); void refresh().catch(error => setError(errorMessage(error))); }}>Reconnect updates</button></div></div>
        <div className="workspace-grid">
          <section className="draft-pane" aria-labelledby="draft-heading">
            <div className="section-heading"><div><p className="step-label">Prepare your research</p><h2 id="draft-heading">{draft ? "Research draft" : "New research draft"}</h2></div><button className="secondary" onClick={newDraft} disabled={!mutable}>New draft</button></div>
            <div className="saved-drafts"><label htmlFor="saved-draft">Saved drafts</label><div className="inline-control"><select id="saved-draft" value={draft?.id || ""} disabled={!mutable} onChange={event => { if (event.target.value && (!dirty || window.confirm("Load the saved draft and discard unsaved changes?"))) void openDraft(event.target.value); }}><option value="">Choose a saved draft</option>{drafts.map(value => <option key={value.id} value={value.id}>{value.name || "Untitled draft"} — {date(value.updated_at)}</option>)}</select>{draft && <button className="quiet" disabled={!mutable} onClick={() => { if (!dirty || window.confirm("Reload the server version and discard unsaved changes?")) void openDraft(draft.id); }}>Reload</button>}</div><span className="small muted">{draft ? `Version ${draft.version}${dirty ? " · Unsaved changes" : " · Saved"}` : `${drafts.length} saved drafts in this workspace`}</span></div>
            <label htmlFor="draft-name">Draft name</label><input id="draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" />
            <section className="editor-section"><h3><span className="step-number">1</span> Choose a knowledge gap</h3><form className="search-control" onSubmit={event => { event.preventDefault(); void search(); }}><label className="sr-only" htmlFor="gap-search">Search knowledge gaps</label><input id="gap-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search a disease or research question" /><button type="submit" className="secondary" disabled={searching}>{searching ? "Searching…" : "Search"}</button></form>
              {gaps.length > 0 && <div className="gap-list" aria-label="Knowledge gap search results">{gaps.map(value => <button key={value.source.source_id} className={composer.source_gap?.source_id === value.source.source_id ? "gap-option selected" : "gap-option"} onClick={() => selectGap(value)} disabled={!mutable}><strong>{gapTitle(value)}</strong><span>{value.object.gap_description || value.object.text || value.source.source_id}</span><small>{composer.source_gap?.source_id === value.source.source_id ? "Selected" : "Select gap"}</small></button>)}</div>}
              {searched && !searching && !gaps.length && <p className="empty">No matching gaps. Try a broader disease name.</p>}
              {composer.source_gap && <div className="selected-question"><span className="small muted">Selected question</span><p>{gap ? gap.object.text || gapTitle(gap) : composer.source_gap.id}</p>{gap?.object.gap_description && <details><summary>Source context</summary><p>{gap.object.gap_description}</p></details>}</div>}
            </section>
            <section className="editor-section"><div className="section-heading compact"><h3><span className="step-number">2</span> Select mechanism anchors</h3>{composer.source_gap && <button className="quiet small" disabled={!mutable || suggesting} onClick={() => void suggest(composer, false)}>Refresh suggestions</button>}</div>
              {!composer.source_gap ? <p className="empty">Select a question to find related genetic mechanisms.</p> : suggesting ? <p role="status" className="loading">Finding relevant mechanisms…</p> : <>
                <p className="muted small">One anchor is selected by default. Add only the mechanisms you want to investigate.</p>
                <div className="anchor-list">{Array.from(new Set([...composer.eaggl_anchors.map(value => value.reference.source_id), ...(suggestion?.automatic_anchors.map(value => value.factor.source_id) || [])])).map(sourceId => {
                  const factor = factors[sourceId], selected = composer.eaggl_anchors.some(value => value.reference.source_id === sourceId);
                  return <label className={selected ? "anchor selected" : "anchor"} key={sourceId}><input type="checkbox" checked={selected} disabled={!mutable || (!selected && composer.eaggl_anchors.length >= 10)} onChange={() => setComposer(current => selected ? { ...current, eaggl_anchors: current.eaggl_anchors.filter(value => value.reference.source_id !== sourceId) } : factor && suggestion ? withFactors(current, [factor], suggestion.suggestion_id) : current)} /><span><strong>{factor ? factorTitle(factor) : sourceId}</strong>{factor?.cfde_anchor.subtitle && <small>{factor.cfde_anchor.subtitle}</small>}</span></label>;
                })}</div>
                {!composer.eaggl_anchors.length && !suggestion?.automatic_anchors.length && <p className="empty">No anchors selected. Refresh suggestions or try another question.</p>}
                {!!suggestion?.limitations.length && <details><summary>About these suggestions</summary><ul>{suggestion.limitations.map(value => <li key={value}>{value}</li>)}</ul></details>}
              </>}
            </section>
            <section className="editor-section"><h3><span className="step-number">3</span> Save and investigate</h3><fieldset disabled={!mutable}><legend>Knowledge graph evidence</legend>{(["biomarkerkg", "prokn"] as const).map(value => <label className="check" key={value}><input type="checkbox" checked={composer.selected_kgs.includes(value)} onChange={event => setComposer(current => ({ ...current, selected_kgs: event.target.checked ? [...current.selected_kgs, value] : current.selected_kgs.filter(kg => kg !== value) }))} />{value === "biomarkerkg" ? "BiomarkerKG" : "ProKN"}</label>)}</fieldset>
              <p className="muted small">This client requests one scientific account. Accepted accounts automatically receive a research paragraph. Starting research uses the configured model budget.</p>
              {pending && <div className="notice"><span>A submission needs confirmation. Recover it with the original request key before starting another.</span><button className="quiet small" onClick={discardSubmission} disabled={!!busy}>Discard recovery</button></div>}
              <div className="actions"><button className="secondary" onClick={save} disabled={!mutable || suggesting || !dirty}>{busy === "save" ? "Saving…" : "Save draft"}</button><button onClick={submit} disabled={!mutable || suggesting || (!pending && (!draft || dirty || !composer.source_gap || !composer.eaggl_anchors.length))}>{busy === "submit" ? "Submitting…" : pending ? "Recover submission" : "Start analysis"}</button></div>{dirty && <p className="small muted">Save your draft before starting an analysis.</p>}
            </section>
          </section>
          <section className="activity-pane" aria-labelledby="activity-heading">
            <div className="section-heading"><div><p className="step-label">Follow the evidence</p><h2 id="activity-heading">Research activity</h2></div>{job && <span className={"status " + job.status}>{readable(job.status)}</span>}</div>
            <label htmlFor="job-select">Workspace jobs</label><select id="job-select" value={job?.id || ""} disabled={!!busy} onChange={event => { if (event.target.value) void openJob(event.target.value); }}><option value="">Choose a job to follow</option>{jobs.map(value => <option key={value.id} value={value.id}>{value.kind === "analysis" ? "Analysis" : "Paragraph"} — {readable(value.status)} — {date(value.created_at)}</option>)}</select>
            {moreRecords && <p className="small muted">Showing the most recent 100 drafts and jobs. A saved page URL can reopen an older item.</p>}
            {!job ? <div className="activity-empty"><svg viewBox="0 0 64 64" aria-hidden="true"><path d="M12 43h8l7-22 10 32 7-23 4 13h8" /><path d="M8 12v44h48" /></svg><h3>Your investigation will appear here.</h3><p>Start an analysis or select an existing job. Events arrive live as evidence is collected and reviewed.</p></div> : <>
              <div className="job-meta"><p>{job.kind === "analysis" ? "Analysis" : "Research paragraph"} started {date(job.created_at)}</p><span className="small muted">{readable(job.stage)}</span></div>
              <div className="stream-toolbar"><span className="small muted" role="status">{streamState}</span><div><button className="quiet small" onClick={() => setStreamAttempt(value => value + 1)}>Reconnect</button>{!terminal(job.status) && <button className="danger small" onClick={cancel} disabled={!!busy || job.status === "cancel_requested"}>{job.status === "cancel_requested" ? "Stopping…" : "Stop job"}</button>}</div></div>
              {!!job.warnings.length && <div className="notice"><ul>{job.warnings.map(value => <li key={value}>{value}</li>)}</ul></div>}
              {job.failure && <div className="notice error" role="alert"><div><strong>Research could not complete</strong><p>{job.failure.message}</p><span className="small">{job.failure.code}</span>{job.failure.code.startsWith("REVIEW_") && job.failure.retryable && <p><button className="secondary" onClick={retryReview} disabled={!!busy}>{busy === "review" ? "Requesting review…" : "Retry saved review"}</button></p>}</div></div>}
              {job.status === "cancelled" && <p className="notice">This job was stopped. No successful result is implied.</p>}
              <div className="event-log" aria-label="Job activity events">{!activity.length && <p className="empty">Loading saved activity…</p>}{activity.map(event => <article className={"event " + event.event_type} key={event.id}><div className="event-header"><span>{event.detail?.tool_name || readable(event.event_type)}</span><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleTimeString()}</time></div><p>{event.message}</p>{event.detail?.output_excerpt && <details><summary>Captured output</summary><pre>{event.detail.output_excerpt}</pre></details>}{event.detail?.artifact_sha256 && <a href={backend("artifacts/" + event.detail.artifact_sha256)} target="_blank" rel="noreferrer">Open captured artifact</a>}</article>)}</div>
              {job.result && <ResultView job={job} openJob={openJob} />}
              <details className="record-details"><summary>Job record and evidence</summary><p className="small">Job ID: {job.id}</p><div className="result-links"><a href={backend("jobs/" + job.id)} target="_blank" rel="noreferrer">Job JSON</a><a href={backend("jobs/" + job.id + "/evidence-package")} target="_blank" rel="noreferrer">Frozen evidence package</a></div><pre>{JSON.stringify(job, null, 2)}</pre></details>
            </>}
          </section>
        </div>
      </>}
    </main><footer className="site-footer"><a href="https://api-qa.hugeampkpnbi.org/api/reveal/docs" target="_blank" rel="noreferrer">API reference</a></footer>
  </>;
}

type ResultRecord = { path: string; title: string; data?: Record<string, unknown>; error?: string };
function textValue(value: unknown): string { return typeof value === "string" ? value : ""; }
function ResultView({ job, openJob }: { job: Job; openJob: (id: string) => Promise<void> }) {
  const [records, setRecords] = useState<ResultRecord[]>([]);
  const result = job.result;
  useEffect(() => {
    if (!result) return;
    let active = true;
    const paths = result.kind === "analysis" ? result.account_ids.map(id => ({ path: "accounts/" + encodeURIComponent(id), title: "Scientific account" }))
      : result.kind === "paragraph" ? [{ path: "paragraphs/" + encodeURIComponent(result.paragraph_id), title: "Research paragraph" }]
      : [{ path: "analysis-outcomes/" + encodeURIComponent(result.outcome_id), title: "Insufficient evidence" }];
    setRecords(paths);
    void Promise.all(paths.map(async record => {
      try { return { ...record, data: await request<Record<string, unknown>>(backend(record.path)) }; }
      catch (error) { return { ...record, error: errorMessage(error) }; }
    })).then(values => { if (active) setRecords(values); });
    return () => { active = false; };
  }, [job.id, JSON.stringify(result)]);
  if (!result) return null;
  return <section className="results"><h3>{result.kind === "analysis_outcome" ? "Investigation outcome" : "Saved results"}</h3>
    {records.map(record => {
      const document = record.data?.document as Record<string, Record<string, unknown>[]> | undefined;
      const object = document?.scientific_accounts?.[0];
      const paragraphs = document?.paragraphs || [];
      const artifacts = (record.data?.artifacts || []) as Schema<"ArtifactAccess">[];
      return <article className="result-record" key={record.path}><h4>{textValue(object?.name) || record.title}</h4>{record.error ? <p role="alert" className="error-text">{record.error} <a href={backend(record.path)} target="_blank" rel="noreferrer">Open result</a></p> : !record.data ? <p className="muted">Loading saved result…</p> : <>
        {result.kind === "analysis_outcome" && <><p>{textValue(record.data.summary)}</p><p>{textValue(record.data.reason)}</p>
          {([['missing_evidence', 'Missing evidence'], ['limitations', 'Limitations'], ['next_steps', 'Possible next steps']] as const).map(([field, title]) => Array.isArray(record.data![field]) && (record.data![field] as unknown[]).length > 0 ? <div className="outcome-section" key={field}><strong>{title}</strong><ul>{(record.data![field] as unknown[]).map((value, index) => <li key={index}>{textValue(value)}</li>)}</ul></div> : null)}
          {textValue(record.data.scope_note) && <p className="scope-note">{textValue(record.data.scope_note)}</p>}
        </>}
        {object?.closing_remarks && <p>{textValue(object.closing_remarks)}</p>}
        {paragraphs.map((paragraph, index) => <p className="research-text" key={textValue(paragraph.id) || index}>{textValue(paragraph.text)}</p>)}
        <div className="result-links"><a href={backend(record.path)} target="_blank" rel="noreferrer">Open saved JSON</a>{result.kind === "paragraph" && <a href={backend("paragraphs/" + encodeURIComponent(result.paragraph_id) + "/export?format=markdown")} target="_blank" rel="noreferrer">Export paragraph Markdown</a>}{artifacts.filter(artifact => artifact.availability === "available" && /^[a-f0-9]{64}$/.test(artifact.file.sha256 || "")).map(artifact => <a key={artifact.file.id} href={backend("artifacts/" + artifact.file.sha256)} target="_blank" rel="noreferrer">{artifact.file.filename || artifact.file.name || "Evidence artifact"}</a>)}</div>
        <details><summary>Inspect result</summary><pre>{JSON.stringify(record.data, null, 2)}</pre></details>
      </>}</article>;
    })}
    {result.kind === "analysis" && result.paragraph_job_ids.length > 0 && <div className="paragraph-followup"><h4>Research paragraphs</h4><p className="small muted">Paragraphs run as separate jobs after account validation.</p>{result.paragraph_job_ids.map((id, index) => <button className="secondary" key={id} onClick={() => void openJob(id)}>Follow paragraph {index + 1}</button>)}</div>}
  </section>;
}
