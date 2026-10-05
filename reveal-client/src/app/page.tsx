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
const gapBody = (gap: Gap) => gap.object.gap_description || gap.object.text || gap.source.source_id;
function GapOption({ gap, selected, disabled, onSelect }: { gap: Gap; selected: boolean; disabled: boolean; onSelect: () => void }) {
  const bodyRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const body = gapBody(gap);
  useEffect(() => {
    const node = bodyRef.current;
    if (!node || open) return;
    const measure = () => setOverflows(node.scrollHeight > node.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [body, open]);
  return <div className={selected ? "gap-option selected" : "gap-option"} role="button" tabIndex={disabled ? -1 : 0} aria-pressed={selected} aria-disabled={disabled} onClick={() => { if (!disabled) onSelect(); }} onKeyDown={event => { if (event.target !== event.currentTarget || disabled) return; if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); } }}>
    <strong>{gapTitle(gap)}</strong>
    <span ref={bodyRef} className={open ? "expanded" : undefined}>{body}</span>
    {overflows && !open && <button type="button" className="read-more" onClick={event => { event.stopPropagation(); setOpen(true); }}>Read more</button>}
  </div>;
}
const factorTitle = (factor: Factor) => factor.cfde_anchor.label || factor.object.name || factor.source_id;
function setLocation(kind: "draft" | "job", id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set(kind, id); else url.searchParams.delete(kind);
  window.history.replaceState(null, "", url);
}
const DRAFT_PAGE_SIZE = 10;
const evidenceName = (id: "biomarkerkg" | "prokn") => id === "biomarkerkg" ? "BiomarkerKG" : "ProKN";
function SavedDrafts({ drafts, jobs, requests, gapLabels, factorLabels, ready, requestsReady, page, deletingId, onPage, onOpen, onDelete, onClose }: {
  drafts: Draft[]; jobs: Job[]; requests: Schema<"ResearchRequest">[]; gapLabels: Record<string, string>; factorLabels: Record<string, string>;
  ready: boolean; requestsReady: boolean; page: number; deletingId: string; onPage: (page: number) => void; onOpen: (id: string) => void; onDelete: (draft: Draft) => void; onClose: () => void;
}) {
  const pages = Math.max(1, Math.ceil(drafts.length / DRAFT_PAGE_SIZE));
  const rows = drafts.slice(page * DRAFT_PAGE_SIZE, page * DRAFT_PAGE_SIZE + DRAFT_PAGE_SIZE);
  return <div className="warning-stage" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="draft-picker" role="dialog" aria-modal="true" aria-labelledby="draft-picker-heading">
      <div className="draft-picker-heading"><h2 id="draft-picker-heading">Saved drafts</h2><button type="button" className="quiet" onClick={onClose}>Close</button></div>
      <div className="draft-table-wrap"><table className="draft-table">
        <thead><tr><th scope="col">Draft</th><th scope="col">Knowledge gap</th><th scope="col">Factors</th><th scope="col">Evidence</th><th scope="col">Investigation</th><th scope="col">Select</th><th scope="col">Delete</th></tr></thead>
        <tbody>{rows.length === 0 ? <tr><td colSpan={7}>No saved drafts.</td></tr> : rows.map(draft => {
          const gapId = draft.composer.source_gap?.id;
          const factorIds = draft.composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
          const linked = requests.filter(request => request.source_draft_id === draft.id).sort((a, b) => b.submitted_at.localeCompare(a.submitted_at));
          const investigations = linked.flatMap(request => {
            const matches = jobs.filter(job => job.research_request_id === request.id);
            return matches.length ? matches.map(job => `${job.kind === "analysis" ? "Analysis" : "Research paragraph"} — ${readable(job.status)} — ${date(job.created_at)}`) : [`Analysis — ${date(request.submitted_at)}`];
          });
          return <tr key={draft.id}>
            <td>{draft.name || "Untitled draft"} ({date(draft.updated_at)})</td>
            <td>{gapId ? gapLabels[gapId] || (ready ? "—" : "Loading…") : "—"}</td>
            <td>{factorIds.length ? factorIds.map(id => factorLabels[id] || (ready ? "—" : "Loading…")).join(", ") : "—"}</td>
            <td>{draft.composer.selected_kgs.map(evidenceName).join(", ") || "—"}</td>
            <td>{!requestsReady ? "Loading…" : investigations.length ? <span className="draft-investigations">{investigations.map((line, index) => <span key={index}>{line}</span>)}</span> : "—"}</td>
            <td><button type="button" onClick={() => onOpen(draft.id)}>Open</button></td>
            <td><button type="button" className="draft-delete" aria-label={`Delete ${draft.name || "Untitled draft"}`} disabled={!!deletingId} onClick={() => onDelete(draft)}>{deletingId === draft.id ? "Deleting…" : "Delete"}</button></td>
          </tr>;
        })}</tbody>
      </table></div>
      {drafts.length > DRAFT_PAGE_SIZE && <nav className="draft-pages" aria-label="Saved draft pages"><button type="button" className="secondary" disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button>{Array.from({ length: pages }, (_, index) => <button type="button" className={index === page ? undefined : "secondary"} aria-current={index === page ? "page" : undefined} key={index} onClick={() => onPage(index)}>{index + 1}</button>)}<button type="button" className="secondary" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>Next</button></nav>}
    </section>
  </div>;
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
  const [query, setQuery] = useState("");
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestion, setSuggestion] = useState<Schema<"Suggestions"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Factor>>({});
  const [factorMisses, setFactorMisses] = useState<Record<string, true>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [activity, setActivity] = useState<JobEvent[]>([]);
  const [streamState, setStreamState] = useState("");
  const [, setWorkspaceState] = useState("");
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const [panelView, setPanelView] = useState<"menu" | "draft" | "copy">("menu");
  const [step, setStep] = useState<"gap" | "anchors" | "investigate" | null>("gap");
  const [activityOpen, setActivityOpen] = useState(false);
  const [draftPicker, setDraftPicker] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [draftPage, setDraftPage] = useState(0);
  const [gapLabels, setGapLabels] = useState<Record<string, string>>({});
  const [factorLabels, setFactorLabels] = useState<Record<string, string>>({});
  const [draftRequests, setDraftRequests] = useState<Schema<"ResearchRequest">[]>([]);
  const [catalogReady, setCatalogReady] = useState(false);
  const [requestsReady, setRequestsReady] = useState(false);
  const [sessionOpen, setSessionOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [unsavedPrompt, setUnsavedPrompt] = useState(false);
  const [draftPrompt, setDraftPrompt] = useState<null | "save" | "start">(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const editorGeneration = useRef(0), jobGeneration = useRef(0);
  const collections = useRef<{ drafts: boolean; jobs: boolean; flight: Promise<void> | null }>({ drafts: false, jobs: false, flight: null });
  const identity = useRef<string | null>(null);
  const searchAbort = useRef<AbortController | null>(null), suggestionAbort = useRef<AbortController | null>(null);
  const mutationKeys = useRef(createMutationKeys());
  const currentJob = useRef<Job | null>(null);
  const startFocus = useRef<string | null>(null);
  const sessionMenu = useRef<HTMLDivElement>(null);
  const helpMenu = useRef<HTMLDivElement>(null);
  const keptName = useRef("");
  const dirty = !draft || JSON.stringify(draft.composer) !== JSON.stringify(composer) || (draft.name || "") !== name;
  const mutable = Boolean(principal) && !busy;
  const gapChosen = Boolean(composer.source_gap);
  const anchorChosen = composer.eaggl_anchors.length > 0;
  const suggestedIds = suggestion?.automatic_anchors.map(anchor => anchor.factor.source_id) ?? [];
  const anchorIds = suggestedIds.length ? Array.from(new Set([...composer.eaggl_anchors.map(anchor => anchor.reference.source_id), ...suggestedIds])) : composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
  const missingFactorKey = composer.eaggl_anchors.map(anchor => anchor.reference.source_id).filter(id => !factors[id] && !factorMisses[id]).join("\n");

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
      setLocation("draft", id); setNotice("Saved draft loaded."); setError(""); setWelcomeOpen(false); setStep(value.composer.source_gap ? "anchors" : "gap");
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
    if (params.get("job")) { setActivityOpen(true); void openJob(params.get("job")!); }
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
  }, [principal?.user_id, refresh]);
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
    if (id === "job-select" && !activityOpen) return;
    if (id === "gap-search" && step !== "gap") return;
    startFocus.current = null;
    const element = document.getElementById(id);
    element?.scrollIntoView({ block: "center" });
    element?.focus();
  }, [welcomeOpen, activityOpen, step, focusNonce]);
  useEffect(() => {
    if (step === "anchors" && !gapChosen) setStep("gap");
    else if (step === "investigate" && !(gapChosen && anchorChosen)) setStep(gapChosen ? "anchors" : "gap");
  }, [step, gapChosen, anchorChosen]);
  useEffect(() => {
    if (!sessionOpen && !helpOpen) return;
    function close(event: MouseEvent) {
      const target = event.target as Node;
      if (!sessionMenu.current?.contains(target)) setSessionOpen(false);
      if (!helpMenu.current?.contains(target)) setHelpOpen(false);
    }
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { setSessionOpen(false); setHelpOpen(false); } }
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", onKey); };
  }, [sessionOpen, helpOpen]);
  useEffect(() => {
    if (!unsavedPrompt && !draftPrompt) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { setUnsavedPrompt(false); setDraftPrompt(null); } }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [unsavedPrompt, draftPrompt]);
  useEffect(() => {
    if (!draftPicker) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") setDraftPicker(false); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [draftPicker]);
  useEffect(() => {
    if (!draftPicker) return;
    const last = Math.max(0, Math.ceil(drafts.length / DRAFT_PAGE_SIZE) - 1);
    if (draftPage > last) setDraftPage(last);
  }, [drafts.length, draftPage, draftPicker]);
  useEffect(() => {
    if (!draftPicker) return;
    const controller = new AbortController();
    setCatalogReady(false); setRequestsReady(false);
    const gapIds = [...new Set(drafts.flatMap(item => item.composer.source_gap ? [item.composer.source_gap.id] : []))];
    const factorIds = [...new Set(drafts.flatMap(item => item.composer.eaggl_anchors.map(anchor => anchor.reference.source_id)))];
    async function labelsFor(ids: string[], read: (id: string) => Promise<string>, apply: (labels: Record<string, string>) => void) {
      const labels: Record<string, string> = {};
      for (let index = 0; index < ids.length; index += 8) {
        if (controller.signal.aborted) return;
        const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
          try { return [id, await read(id)] as const; }
          catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, "Unavailable"] as const; }
        }));
        for (const [id, label] of batch) if (label) labels[id] = label;
        if (!controller.signal.aborted) apply({ ...labels });
      }
    }
    void (async () => {
      try {
        await Promise.all([
          labelsFor(gapIds, async id => { const gap = await api.gap(id, controller.signal); return gap.object.text || gapTitle(gap); }, setGapLabels),
          labelsFor(factorIds, async id => factorTitle(await api.factor(id, controller.signal)), setFactorLabels),
          api.requests(controller.signal).then(page => { if (!controller.signal.aborted) setDraftRequests(page.items); }).catch(error => {
            if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error;
          }).finally(() => { if (!controller.signal.aborted) setRequestsReady(true); }),
        ]);
        if (!controller.signal.aborted) setCatalogReady(true);
      } catch { /* A newer list replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [draftPicker, drafts]);
  useEffect(() => {
    if (!missingFactorKey) return;
    const ids = missingFactorKey.split("\n");
    const controller = new AbortController();
    void (async () => {
      const loaded: Record<string, Factor> = {};
      const missed: string[] = [];
      try {
        for (let index = 0; index < ids.length; index += 8) {
          if (controller.signal.aborted) return;
          const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
            try { return [id, await api.factor(id, controller.signal)] as const; }
            catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, null] as const; }
          }));
          for (const [id, factor] of batch) { if (factor) loaded[id] = factor; else missed.push(id); }
        }
        if (controller.signal.aborted) return;
        if (Object.keys(loaded).length) setFactors(current => ({ ...current, ...loaded }));
        if (missed.length) setFactorMisses(current => ({ ...current, ...Object.fromEntries(missed.map(id => [id, true])) }));
      } catch { /* A newer selection replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [missingFactorKey]);

  async function connect() {
    setBusy("connect"); setError("");
    try {
      const value = await api.connect(); setPrincipal(value.principal); setNotice(""); setPanelView("menu");
      const params = new URLSearchParams(window.location.search);
      setWelcomeOpen(!params.has("draft") && !params.has("job"));
    }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function disconnect() {
    setBusy("disconnect");
    try {
      await api.disconnect(); setSessionOpen(false); setDraftPicker(false); setHelpOpen(false); setWelcomeOpen(false); setPanelView("menu"); setStep("gap"); setActivityOpen(false); setPrincipal(null); currentJob.current = null; setJob(null); setDraft(null); setJobs([]); setDrafts([]);
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
    if (composer.source_gap?.source_id === value.source.source_id) return;
    ++editorGeneration.current;
    const next: Composer = { ...composer, source_gap: { id: value.object.id, source_id: value.source.source_id, source_revision: value.source.source_revision }, eaggl_anchors: [], dismissed_source_ids: [] };
    setGap(value); setComposer(next); setSuggestion(null); setNotice(""); setStep("anchors");
    void suggest(next, false);
  }
  function newDraft() {
    if (dirty && (composer.source_gap || name) && !window.confirm("Start a new draft? Unsaved changes will be discarded.")) return false;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setLocation("draft", null); setNotice(""); setError(""); setStep("gap"); setActivityOpen(false);
    return true;
  }
  function beginWithoutDraft() {
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Leave this draft and choose a knowledge gap? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null);
    setQuery(""); setGaps([]); setSearched(false); setLocation("draft", null); setNotice(""); setError("");
    setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); setUnsavedPrompt(false); setDraftPrompt(null);
    startFocus.current = "gap-search"; setFocusNonce(value => value + 1);
  }
  function startFrom(action: "draft" | "gap" | "jobs") {
    setSessionOpen(false);
    if (action === "draft") { if (newDraft()) { setPanelView("draft"); setWelcomeOpen(true); } return; }
    if (action === "gap") { beginWithoutDraft(); return; }
    if (panelView === "copy") setName(keptName.current);
    if (action === "jobs") { setActivityOpen(true); startFocus.current = "job-select"; }
    else { setStep("gap"); startFocus.current = "gap-search"; }
    setWelcomeOpen(false); setPanelView("menu");
  }
  function openCopy() {
    setSessionOpen(false);
    if (panelView !== "copy") keptName.current = name;
    setName(""); setPanelView("copy"); setWelcomeOpen(true);
  }
  function showDrafts() {
    setSessionOpen(false); setHelpOpen(false); setDraftPage(0); setDraftPicker(true);
    void refresh(["drafts", "jobs"]);
  }
  function chooseDraft(id: string) {
    if (dirty && (composer.source_gap || name) && !window.confirm("Load the saved draft and discard unsaved changes?")) return;
    setDraftPicker(false); setWelcomeOpen(false); setPanelView("menu");
    void openDraft(id);
  }
  async function removeDraft(value: Draft) {
    const label = value.name || "Untitled draft";
    if (!window.confirm(`Delete "${label}"? A submitted investigation from this draft stays available.`)) return;
    setDeletingId(value.id); setError("");
    try {
      await mutationKeys.current.run(["delete", value.id, value.version], key => api.deleteDraft(value, key));
      if (draft?.id === value.id) {
        ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
        setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setLocation("draft", null); setStep("gap"); setActivityOpen(false);
      }
      await refresh(["drafts"]);
    } catch (error) { setError(errorMessage(error)); }
    finally { setDeletingId(""); }
  }
  async function save(asNew = false): Promise<Draft | null> {
    const fromStart = welcomeOpen && panelView === "draft";
    const fromCopy = asNew || (welcomeOpen && panelView === "copy");
    setBusy("save"); setError("");
    try {
      const value = await mutationKeys.current.run(["save", fromCopy ? null : draft?.id, fromCopy ? null : draft?.version, composer, name], key => api.save(fromCopy ? null : draft, composer, name, key));
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setLocation("draft", value.id);
      if (fromStart) { setGaps([]); setSearched(false); setNotice(""); setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); }
      else if (fromCopy) { setNotice(""); setWelcomeOpen(false); setPanelView("menu"); setStep(value.composer.source_gap ? "anchors" : "gap"); }
      else { setUnsavedPrompt(false); setDraftPrompt(null); }
      await refresh();
      return value;
    } catch (error) { setError(error instanceof ApiError && error.status === 409 && error.code !== "REFERENCE_GENERATION_SUPERSEDED" ? "This draft changed on the server. Open the saved draft again to review it before saving. Your local selections remain visible." : errorMessage(error)); return null; }
    finally { setBusy(""); }
  }
  async function beginAnalysis(saved?: Draft) {
    if (!principal) return;
    const source = saved || draft;
    if (!pending && (!source || (!saved && dirty))) return;
    if (!pending && (!source?.composer.source_gap || !source.composer.eaggl_anchors.length)) return;
    const intent = pending || { body: { kind: "analysis" as const, draft_id: source!.id, draft_version: source!.version, budgets: { max_accounts: 1 } }, key: crypto.randomUUID() };
    setPending(intent); sessionStorage.setItem("reveal-submit:" + principal.user_id, JSON.stringify(intent));
    setBusy("submit"); setError("");
    try {
      const value = await api.submit(intent.body, intent.key);
      sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", value.id); setActivityOpen(true); setNotice("Analysis submitted. Live activity will appear alongside your draft."); await refresh();
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
      <div className="connection">
        {principal && <div className="session-menu" ref={sessionMenu}><button className="secondary" aria-expanded={sessionOpen} aria-haspopup="menu" aria-controls="session-menu" onClick={() => { setHelpOpen(false); setSessionOpen(open => !open); }}>Session</button>
          {sessionOpen && <div id="session-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={() => startFrom("draft")}>Start a new draft</button><button role="menuitem" onClick={openCopy} disabled={!draft}>Save as new draft</button><button role="menuitem" onClick={showDrafts} disabled={!drafts.length}>Open saved draft</button><button role="menuitem" onClick={() => startFrom("gap")}>Choose a knowledge gap</button><button role="menuitem" onClick={() => startFrom("jobs")}>Workspace jobs</button></div>}
        </div>}
        {principal && <div className="session-menu" ref={helpMenu}><button className="secondary" aria-expanded={helpOpen} aria-haspopup="menu" aria-controls="help-menu" onClick={() => { setSessionOpen(false); setHelpOpen(open => !open); }}>Help</button>
          {helpOpen && <div id="help-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={() => setHelpOpen(false)}>Learn REVEAL client</button><button role="menuitem" onClick={() => setHelpOpen(false)}>Quick start tutorial</button></div>}
        </div>}
        {principal ? <button onClick={disconnect} disabled={!!busy}>{busy === "disconnect" ? "Disconnecting…" : "Disconnect workspace"}</button> : <span className="connection-status">Disconnected</span>}
      </div></header>
    <main>
      {principal && !welcomeOpen && (draft ? <div className="draft-tools">{dirty && <button type="button" className="save-updates" onClick={() => save()} disabled={!mutable || suggesting}>{busy === "save" ? "Saving…" : "Save updates"}</button>}{name.trim() && <p className="draft-badge">{name.trim()}</p>}</div> : <div className="draft-tools"><button type="button" className="save-updates" onClick={() => setDraftPrompt("save")} disabled={!mutable || suggesting}>Save draft</button></div>)}
      {error && <div className="notice error" role="alert"><span>{error}</span><button className="quiet" onClick={() => setError("")} aria-label="Dismiss error">Dismiss</button></div>}
      {!principal ? <section className="welcome"><div className="welcome-lead"><h2>Choose the gap. Ground the claim.</h2><button onClick={connect} disabled={!!busy || checking}>{checking ? "Checking workspace…" : busy === "connect" ? "Connecting…" : "Connect workspace"}</button></div><div className="welcome-cards"><a className="welcome-card" href="#learn-reveal-client" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><path d="M36 12v32M14 16c7 5 15 5 22-2 7 7 15 7 22 2v28c-7 5-15 5-22-2-7 7-15 7-22 2V16z" /></svg><strong>Learn REVEAL client</strong><span>A guide to the workspace, from a knowledge gap to a grounded claim.</span></a><a className="welcome-card" href="#quick-start-demo" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><rect x="14" y="12" width="44" height="32" rx="3" /><path className="card-icon-fill" d="M33 22l12 6-12 6z" /></svg><strong>Watch quick start demo</strong><span>A short walkthrough of connecting and starting an investigation.</span></a></div></section> : welcomeOpen ? (panelView === "draft" || panelView === "copy" ? <div className="welcome-panel-stage"><section className="welcome-panel" role="dialog" aria-modal="true" aria-labelledby="welcome-heading"><div className="draft-start"><h2 id="welcome-heading">{panelView === "copy" ? "Save as a new draft" : "Start a draft"}</h2><label htmlFor="draft-name">Draft name</label><input id="draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" autoFocus />{notice && <p className="notice" role="status">{notice}</p>}<div className="actions"><button onClick={() => save(panelView === "copy")} disabled={!mutable || !name.trim() || (panelView !== "copy" && !dirty)}>{busy === "save" ? "Saving…" : "Save draft"}</button></div></div></section></div> : <div className="welcome-panel-stage"><section className="welcome-panel" aria-labelledby="welcome-menu-heading"><h2 id="welcome-menu-heading">Welcome to your workspace</h2><div className="welcome-menu"><button type="button" onClick={showDrafts} disabled={!drafts.length}>Open saved draft</button></div></section></div>) : <>
        <div className="steps">
          <section className={step === "gap" ? "step open" : "step"}>
            <button type="button" className="step-toggle" aria-expanded={step === "gap"} aria-describedby="step-gap-guide" onClick={() => setStep(current => current === "gap" ? null : "gap")}><span className="step-number">1</span>Choose a knowledge gap</button>
            {step !== "gap" && gap ? <p id="step-gap-guide" className="step-chosen">{gap.object.text || gapTitle(gap)}</p> : <p id="step-gap-guide" className="step-guide">Find an existing scientific question that evidence still leaves unexplained. The gap you select becomes the question this investigation will try to ground.</p>}
            {step === "gap" && <div className="step-body"><form className="search-control" onSubmit={event => { event.preventDefault(); void search(); }}><label className="sr-only" htmlFor="gap-search">Search knowledge gaps</label><input id="gap-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search a disease or research question" /><button type="submit" className="secondary" disabled={searching}>{searching ? "Searching…" : "Search"}</button></form>
              {gaps.length > 0 && <><p className="gap-guide">{gaps.length} knowledge gap{gaps.length === 1 ? "" : "s"} found. Click one to select for the next step.</p><div className="gap-list" aria-label="Knowledge gap search results">{gaps.map(value => <GapOption key={value.source.source_id} gap={value} selected={composer.source_gap?.source_id === value.source.source_id} disabled={!mutable} onSelect={() => selectGap(value)} />)}</div></>}
              {searched && !searching && !gaps.length && <p className="empty">No matching gaps. Try a broader disease name.</p>}
              {composer.source_gap && <div className="selected-question"><span className="small muted">Selected question</span><p>{gap ? gap.object.text || gapTitle(gap) : composer.source_gap.id}</p>{gap?.object.gap_description && <details><summary>Source context</summary><p>{gap.object.gap_description}</p></details>}</div>}
</div>}
          </section>
          <section className={step === "anchors" ? "step open" : gapChosen ? "step" : "step inactive"}>
            <button type="button" className="step-toggle" aria-expanded={step === "anchors"} aria-describedby="step-anchors-guide" disabled={!gapChosen} onClick={() => { if (gapChosen) setStep(current => current === "anchors" ? null : "anchors"); }}><span className="step-number">2</span>Select mechanism anchors</button>
            {step !== "anchors" && anchorChosen ? <p id="step-anchors-guide" className="step-chosen">{composer.eaggl_anchors.map(anchor => { const factor = factors[anchor.reference.source_id]; return factor ? factorTitle(factor) : anchor.reference.source_id; }).join(", ")}</p> : <p id="step-anchors-guide" className="step-guide">Choose genetic factors that may help explain the selected gap. At least one factor is required to start an investigation, and none is selected for you.{suggestion?.limitations.length ? ` ${suggestion.limitations.join(" ")}` : ""}</p>}
            {step === "anchors" && <div className="step-body">{!composer.source_gap ? <p className="empty">Select a question to find related genetic mechanisms.</p> : <>
                {!anchorChosen && <p className="gap-guide">At least one factor has to be selected to initiate investigation.</p>}
                <div className="anchor-list">{anchorIds.map(sourceId => {
                  const factor = factors[sourceId], selected = composer.eaggl_anchors.some(value => value.reference.source_id === sourceId);
                  const title = factor ? factorTitle(factor) : factorMisses[sourceId] ? sourceId : "Loading…";
                  return <label className={selected ? "anchor selected" : "anchor"} key={sourceId}><input type="checkbox" checked={selected} disabled={!mutable || (!selected && composer.eaggl_anchors.length >= 10)} onChange={() => setComposer(current => selected ? { ...current, eaggl_anchors: current.eaggl_anchors.filter(value => value.reference.source_id !== sourceId) } : factor && suggestion ? withFactors(current, [factor], suggestion.suggestion_id) : current)} /><span><strong>{title}</strong>{factor?.cfde_anchor.subtitle && <small>{factor.cfde_anchor.subtitle}</small>}</span></label>;
                })}{!suggestedIds.length && (suggesting ? <p role="status" className="loading">Finding relevant mechanisms…</p> : <button type="button" className="quiet suggestion-refresh" disabled={!mutable} onClick={() => void suggest(composer, false)}>Show suggestions</button>)}</div>
                {anchorChosen && <button type="button" className="step-next" onClick={() => setStep("investigate")}>Select evidence and initiate investigation in step 3.</button>}
              </>}
</div>}
          </section>
          <section className={step === "investigate" ? "step open" : gapChosen && anchorChosen ? "step" : "step inactive"}>
            <button type="button" className="step-toggle" aria-expanded={step === "investigate"} aria-describedby="step-investigate-guide" disabled={!(gapChosen && anchorChosen)} onClick={() => { if (gapChosen && anchorChosen) setStep(current => current === "investigate" ? null : "investigate"); }}><span className="step-number">3</span>Investigate</button>
            <p id="step-investigate-guide" className="step-guide">Start an analysis of the gap and the factors you selected. Choose any extra knowledge-graph evidence, then begin the investigation.</p>
            {step === "investigate" && <div className="step-body"><p className="step-local">These options add knowledge-graph evidence the analysis can search along with your gap and factors. BiomarkerKG supplies literature-linked biomarkers and diseases; ProKN supplies genes, proteins, pathways, and diseases.</p><fieldset disabled={!mutable}><legend>Knowledge graph evidence</legend>{(["biomarkerkg", "prokn"] as const).map(value => <label className="check" key={value}><input type="checkbox" checked={composer.selected_kgs.includes(value)} onChange={event => setComposer(current => ({ ...current, selected_kgs: event.target.checked ? [...current.selected_kgs, value] : current.selected_kgs.filter(kg => kg !== value) }))} />{value === "biomarkerkg" ? "BiomarkerKG" : "ProKN"}</label>)}</fieldset>
              <p className="step-follow">The analysis freezes that evidence, searches the graphs you leave checked, and drafts one scientific account. If the account is accepted, a research paragraph follows, and you can follow the run in Research activity.</p>
              {pending && <div className="notice"><span>A submission needs confirmation. Recover it with the original request key before starting another.</span><button className="quiet small" onClick={discardSubmission} disabled={!!busy}>Discard recovery</button></div>}
              <div className="actions"><button onClick={() => { if (!pending && !draft) { setDraftPrompt("start"); return; } if (!pending && dirty) { setUnsavedPrompt(true); return; } void beginAnalysis(); }} disabled={!mutable || suggesting || (!pending && (!composer.source_gap || !composer.eaggl_anchors.length))}>{busy === "submit" ? "Submitting…" : pending ? "Recover submission" : "Start analysis"}</button></div>
</div>}
          </section>
        </div>
        {activityOpen ? <section className="activity-float activity-pane" aria-labelledby="activity-heading"><div className="section-heading"><div><p className="step-label">Follow the evidence</p><h2 id="activity-heading">Research activity</h2></div><div className="activity-heading-actions">{job && <span className={"status " + job.status}>{readable(job.status)}</span>}<button type="button" className="quiet" onClick={() => setActivityOpen(false)}>Collapse</button></div></div><label htmlFor="job-select">Workspace jobs</label><select id="job-select" value={job?.id || ""} disabled={!!busy} onChange={event => { if (event.target.value) void openJob(event.target.value); }}><option value="">Choose a job to follow</option>{jobs.map(value => <option key={value.id} value={value.id}>{value.kind === "analysis" ? "Analysis" : "Paragraph"} — {readable(value.status)} — {date(value.created_at)}</option>)}</select>
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
</section> : job ? <button type="button" className="activity-launcher" onClick={() => setActivityOpen(true)}>Research activity</button> : null}
      </>}
    </main><footer className="site-footer"><a href="https://api-qa.hugeampkpnbi.org/api/reveal/docs" target="_blank" rel="noreferrer">API reference</a></footer>
    {unsavedPrompt && <div className="warning-stage"><section className="warning-panel" role="alertdialog" aria-modal="true" aria-labelledby="unsaved-heading"><h2 id="unsaved-heading">Save updates first</h2><p>This draft has unsaved changes. Saving them starts the investigation.</p><div className="actions"><button type="button" className="save-updates" onClick={async () => { const saved = await save(); if (saved) await beginAnalysis(saved); }} disabled={!mutable || suggesting}>{busy === "save" ? "Saving…" : busy === "submit" ? "Starting…" : "Save updates"}</button><button type="button" className="quiet" onClick={() => setUnsavedPrompt(false)}>Cancel</button></div></section></div>}
    {draftPrompt && <div className="warning-stage"><section className="warning-panel" role="dialog" aria-modal="true" aria-labelledby="draft-save-heading"><h2 id="draft-save-heading">{draftPrompt === "start" ? "Save a draft first" : "Save a draft"}</h2><p>{draftPrompt === "start" ? "Name this draft to keep the gap and factors you selected. Saving it starts the investigation." : "Name this draft to keep the gap and factors you selected."}</p><label htmlFor="gap-draft-name">Draft name</label><input id="gap-draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" autoFocus /><div className="actions"><button type="button" className="save-updates" onClick={async () => { const startAnalysis = draftPrompt === "start"; const saved = await save(); if (saved && startAnalysis) await beginAnalysis(saved); }} disabled={!mutable || suggesting || !name.trim()}>{busy === "save" ? "Saving…" : busy === "submit" ? "Starting…" : "Save draft"}</button><button type="button" className="quiet" onClick={() => setDraftPrompt(null)}>Cancel</button></div></section></div>}
    {draftPicker && <SavedDrafts drafts={drafts} jobs={jobs} requests={draftRequests} gapLabels={gapLabels} factorLabels={factorLabels} ready={catalogReady} requestsReady={requestsReady} page={draftPage} deletingId={deletingId} onPage={setDraftPage} onOpen={chooseDraft} onDelete={draftValue => void removeDraft(draftValue)} onClose={() => setDraftPicker(false)} />}
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
