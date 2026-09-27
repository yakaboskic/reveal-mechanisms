"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, messageOf, terminal, type Schema } from "@/lib/client";
import { applySuggestions, emptyComposer, factorSelection, removeAnchor, selectedGap } from "@/lib/composer";
import { ProviderButtons, useIdentity } from "./Session";
import { Activity } from "./Activity";
import { AccountPreview, Record } from "./Scientific";
import { loadFeaturedGaps } from "@/lib/featured-gaps";

const storageKey = "reveal:composer";
type LocalDraft = { composer: Schema<"Composer">; gap: Schema<"GapRecord"> | null; factors: Record<string, Schema<"EagglFactor">>; draft: Schema<"Draft"> | null; owner: string | null; job?: Schema<"Job"> | null };
export function Composer() {
  const { me, ready, refresh } = useIdentity();
  const [composer, setComposer] = useState(emptyComposer);
  const [gap, setGap] = useState<Schema<"GapRecord"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Schema<"EagglFactor">>>({});
  const [draft, setDraft] = useState<Schema<"Draft"> | null>(null);
  const [query, setQuery] = useState(""); const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Schema<"GapRecord">[]>([]); const [trending, setTrending] = useState<Schema<"GapRecord">[]>([]);
  const [matches, setMatches] = useState<Schema<"EagglFactor">[]>([]); const [adding, setAdding] = useState(false);
  const [findingMatches, setFindingMatches] = useState(false);
  const [suggesting, setSuggesting] = useState(false); const [limitations, setLimitations] = useState<string[]>([]);
  const [saveState, setSaveState] = useState("Kept in this browser"); const [conflict, setConflict] = useState(false);
  const [error, setError] = useState(""); const [booted, setBooted] = useState(false);
  const [job, setJob] = useState<Schema<"Job"> | null>(null); const [submitting, setSubmitting] = useState(false);
  const [example, setExample] = useState("What would you like to understand?"); const [focused, setFocused] = useState(false);
  const [showResults, setShowResults] = useState(true);
  const pendingResultFocus = useRef(false);
  const [inspection, setInspection] = useState<{ title: string; description?: string | null; value: unknown } | null>(null);
  const inspectionDialog = useRef<HTMLDialogElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const draftRef = useRef(draft); const currentRef = useRef(composer); const loadedOwner = useRef<string | null>(null);
  const saveEpoch = useRef(0);
  const suggestionRequest = useRef<AbortController | null>(null);
  const saveQueue = useRef<Promise<Schema<"Draft"> | null>>(Promise.resolve(null));
  const requestKeys = useRef(new Map<string, string>()); const submitKey = useRef<{ binding: string; key: string } | null>(null); const anonymousKey = useRef<string | null>(null);
  const getKey = (body: string) => { if (!requestKeys.current.has(body)) requestKeys.current.set(body, crypto.randomUUID()); return requestKeys.current.get(body)!; };
  currentRef.current = composer;
  useEffect(() => () => { suggestionRequest.current?.abort(); }, []);
  useEffect(() => {
    try {
      const local = JSON.parse(sessionStorage.getItem(storageKey) || "null") as LocalDraft | null;
      if (local) { setComposer(local.composer); setGap(local.gap); setFactors(local.factors); setDraft(local.draft); draftRef.current = local.draft; loadedOwner.current = local.owner; if (local.job) setJob(local.job); }
    } catch { sessionStorage.removeItem(storageKey); }
    setBooted(true);
    void api.gaps().then(value => loadFeaturedGaps(api, value.items)).then(setTrending).catch(e => setError(messageOf(e)));
  }, []);
  useEffect(() => {
    if (!booted) return;
    sessionStorage.setItem(storageKey, JSON.stringify({ composer, gap, factors, draft, owner: me?.user_id || loadedOwner.current, job } satisfies LocalDraft));
  }, [composer, gap, factors, draft, job, booted, me?.user_id]);
  useEffect(() => {
    if (!ready || !booted || !me) return;
    if (loadedOwner.current && loadedOwner.current !== me.user_id) { saveEpoch.current++; draftRef.current = null; setDraft(null); setJob(null); }
    loadedOwner.current = me.user_id;
  }, [me?.user_id, ready, booted]);
  useEffect(() => {
    if (!booted || !ready) return;
    const params = new URLSearchParams(window.location.search);
    const draftId = params.get("draft"); const gapId = params.get("gap"); const jobId = params.get("job");
    let active = true;
    const restore = async () => {
      try {
        if (draftId && me) {
          const value = await api.draft(draftId);
          const source = value.composer.source_gap ? await api.gap(value.composer.source_gap.id) : null;
          if (!active) return;
          saveEpoch.current++; draftRef.current = value; setDraft(value); setSaveState("Saved"); setComposer(value.composer); setGap(source); setJob(null);
        } else if (gapId) {
          const source = await api.gap(gapId); if (!active) return;
          if (currentRef.current.source_gap?.id === source.object.id && currentRef.current.source_gap.source_revision === source.source.source_revision) setGap(source);
          else await selectGap(source);
        }
        if (jobId && me) { const value = await api.job(jobId); if (active) setJob(value); }
      } catch (failure) { if (active) setError(messageOf(failure)); }
    };
    void restore(); return () => { active = false; };
  }, [booted, ready, me?.user_id]);
  useEffect(() => {
    if (query.trim()) {
      const controller = new AbortController(); setSearching(true); setResults([]);
      const timer = setTimeout(() => { void api.searchGaps(query.trim(), controller.signal).then(value => { if (!controller.signal.aborted) { setResults(value.items.map(hit => hit.gap)); setSearching(false); } }).catch(e => { if (!controller.signal.aborted) { setError(messageOf(e)); setSearching(false); } }); }, 250);
      return () => { clearTimeout(timer); controller.abort(); };
    }
    setResults([]); setSearching(false);
  }, [query]);
  useEffect(() => {
    if (pendingResultFocus.current && !searching && results.length) {
      pendingResultFocus.current = false;
      document.querySelector<HTMLButtonElement>("#gap-results-list button")?.focus();
    }
  }, [results, searching]);
  useEffect(() => {
    setMatches([]);
    if (!adding || !composer.mechanism_subquery.trim()) { setFindingMatches(false); return; }
    const controller = new AbortController(); setFindingMatches(true);
    let semanticFinished = false;
    // Show cheap label matches while semantic retrieval runs after typing settles.
    const labelsTimer = setTimeout(() => void api.mechanisms(composer.mechanism_subquery, controller.signal, "lexical").then(value => {
      if (!controller.signal.aborted && !semanticFinished) setMatches(value.items.flatMap(hit => hit.record.source === "eaggl" ? [hit.record] : []));
    }).catch(() => { /* The combined search below reports any retrieval failure. */ }), 250);
    const relatedTimer = setTimeout(() => void api.mechanisms(composer.mechanism_subquery, controller.signal, "hybrid").then(value => {
      if (!controller.signal.aborted) { semanticFinished = true; setMatches(value.items.flatMap(hit => hit.record.source === "eaggl" ? [hit.record] : [])); }
    }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => {
      if (!controller.signal.aborted) setFindingMatches(false);
    }), 800);
    return () => { clearTimeout(labelsTimer); clearTimeout(relatedTimer); controller.abort(); };
  }, [composer.mechanism_subquery, adding]);
  useEffect(() => {
    if (query || focused || gap) return;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const questions = trending.map(item => item.object.text).filter(text => text.length <= 300);
    let timer: ReturnType<typeof setTimeout>; let index = 0; let stopped = false;
    let text = "What would you like to understand?";
    const schedule = (callback: () => void, delay: number) => { timer = setTimeout(() => { if (!stopped && !document.hidden) callback(); }, delay); };
    const type = (target: string, offset = 0) => {
      text = target.slice(0, offset); setExample(text);
      if (offset < target.length) schedule(() => type(target, offset + 1), 22);
      else schedule(erase, 2800);
    };
    const erase = () => {
      text = text.slice(0, -1); setExample(text);
      if (text.length) schedule(erase, 10);
      else schedule(() => type(questions[index++ % questions.length]), 300);
    };
    const start = () => {
      clearTimeout(timer);
      if (motion.matches || !questions.length) { setExample("Search DisMech knowledge gaps"); return; }
      if (!document.hidden) { text = "What would you like to understand?"; setExample(text); schedule(erase, 1700); }
    };
    start(); document.addEventListener("visibilitychange", start); motion.addEventListener("change", start);
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", start); motion.removeEventListener("change", start); };
  }, [query, focused, gap, trending]);
  useEffect(() => { if (inspection) inspectionDialog.current?.showModal(); }, [inspection]);
  const save = (snapshot: Schema<"Composer"> = currentRef.current): Promise<Schema<"Draft"> | null> => {
    const epoch = saveEpoch.current;
    const queued = saveQueue.current.catch(() => null).then(async () => {
      if (epoch !== saveEpoch.current) return null;
      const previous = draftRef.current;
      if (previous && JSON.stringify(previous.composer) === JSON.stringify(snapshot)) { setSaveState("Saved"); return previous; }
      setSaveState("Saving…");
      try {
        const key = getKey(JSON.stringify({ draft: previous?.id, version: previous?.version, composer: snapshot }));
        const next = previous ? await api.saveDraft(previous, snapshot, key) : await api.createDraft(snapshot, key);
        if (epoch !== saveEpoch.current) return null;
        draftRef.current = next; setDraft(next); setSaveState("Saved"); setConflict(false);
        return next;
      } catch (failure) {
        setSaveState("Save failed — edits retained locally");
        if (failure instanceof ApiError && failure.status === 409) setConflict(true);
        throw failure;
      }
    }); saveQueue.current = queued; return queued;
  };
  useEffect(() => {
    if (!booted || !me || !composer.source_gap || job || conflict || suggesting) return;
    const timer = setTimeout(() => { void save(composer).catch(e => setError(messageOf(e))); }, 1000);
    return () => clearTimeout(timer);
  }, [composer, me?.user_id, booted, job, conflict, suggesting]);
  useEffect(() => {
    if (!booted || !ready || !me || !sessionStorage.getItem("reveal:submit-after-login")) return;
    sessionStorage.removeItem("reveal:submit-after-login"); void launch();
  }, [booted, ready, me?.user_id]);
  useEffect(() => {
    const missing = composer.eaggl_anchors.filter(anchor => { const factor = factors[anchor.reference.source_id]; return !factor || factor.source_revision !== anchor.reference.source_revision || factor.object.id !== anchor.reference.dapper_id; });
    if (!missing.length) return; let active = true;
    void Promise.allSettled(missing.map(anchor => api.mechanism(anchor.reference.source_id, anchor.reference.source_revision))).then(values => {
      if (!active) return;
      const records = values.flatMap((value, index) => value.status === "fulfilled" && value.value.source === "eaggl" && value.value.source_revision === missing[index].reference.source_revision && value.value.object.id === missing[index].reference.dapper_id ? [value.value] : []);
      if (records.length) setFactors(current => ({ ...current, ...Object.fromEntries(records.map(record => [record.source_id, record])) }));
    });
    return () => { active = false; };
  }, [composer.eaggl_anchors, factors]);
  const rememberFactors = (values: Schema<"EagglFactor">[]) => setFactors(current => ({ ...current, ...Object.fromEntries(values.map(f => [f.source_id, f])) }));
  const suggest = async (snapshot: Schema<"Composer">) => {
    if (!snapshot.source_gap) return;
    suggestionRequest.current?.abort();
    const controller = new AbortController(); suggestionRequest.current = controller;
    setSuggesting(true);
    try {
      const value = await api.suggest({ source_gap: snapshot.source_gap, manual_eaggl_anchors: snapshot.eaggl_anchors.filter(a => a.origin === "manual").map(a => a.reference), dismissed_source_ids: snapshot.dismissed_source_ids, subquery: snapshot.mechanism_subquery, mode: "semantic", model: "cfde-inc-v2" }, controller.signal);
      if (controller.signal.aborted) return;
      rememberFactors(value.automatic_anchors.map(item => item.factor)); setLimitations(value.limitations);
      setComposer(current => current.source_gap?.id === snapshot.source_gap?.id && current.source_gap?.source_revision === snapshot.source_gap?.source_revision ? applySuggestions(current, value) : current);
    } catch (failure) { if (!controller.signal.aborted) setError(messageOf(failure)); }
    finally { if (suggestionRequest.current === controller) { suggestionRequest.current = null; setSuggesting(false); } }
  };
  async function selectGap(value: Schema<"GapRecord">) {
    saveEpoch.current++;
    setGap(value); setQuery(""); setJob(null); setError(""); setConflict(false); submitKey.current = null;
    const next = { ...emptyComposer(), source_gap: selectedGap(value) };
    setComposer(next); setDraft(null); draftRef.current = null;
    if (me) void api.explore({ source_gap: selectedGap(value) }).catch(e => setError(messageOf(e)));
    else {
      try {
        const visits = JSON.parse(sessionStorage.getItem("reveal:local-explorations") || "[]") as Schema<"GapRecord">[];
        sessionStorage.setItem("reveal:local-explorations", JSON.stringify([value, ...visits.filter(visit => visit.source.source_id !== value.source.source_id)].slice(0, 100)));
      } catch { /* browser storage may be restricted; the current selection stays in memory */ }
    }
    await suggest(next);
  }
  const launch = async () => {
    if (submitting || !currentRef.current.source_gap || !currentRef.current.eaggl_anchors.length) return;
    setSubmitting(true); setError("");
    try {
      const saved = await save(); if (!saved) throw new Error("Save the draft before submitting.");
      await api.explore({ source_gap: saved.composer.source_gap!, draft_id: saved.id });
      const binding = `${saved.id}:${saved.version}`;
      if (submitKey.current?.binding !== binding) submitKey.current = { binding, key: crypto.randomUUID() };
      const result = await api.submit({ kind: "analysis", draft_id: saved.id, draft_version: saved.version }, submitKey.current.key);
      setJob(result);
      const url = new URL(window.location.href); url.searchParams.set("draft", saved.id); url.searchParams.set("job", result.id);
      window.history.replaceState(null, "", url); dialog.current?.close();
    } catch (failure) { setError(messageOf(failure)); } finally { setSubmitting(false); }
  };
  const anonymous = async () => {
    setSubmitting(true); setError("");
    try {
      anonymousKey.current ||= crypto.randomUUID();
      const response = await fetch("/api/session/anonymous", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": anonymousKey.current }, body: "{}" });
      const value = await response.json(); if (!response.ok) throw new Error(value.detail);
      await refresh(); dialog.current?.close(); setSubmitting(false); await launch();
    } catch (failure) { setError(messageOf(failure)); setSubmitting(false); }
  };
  const reset = () => { setJob(null); submitKey.current = null; const url = new URL(window.location.href); url.searchParams.delete("job"); window.history.replaceState(null, "", url); };
  const accountIds = job?.result?.kind === "analysis" ? job.result.account_ids : [];
  const clearGap = () => {
    suggestionRequest.current?.abort(); suggestionRequest.current = null; setSuggesting(false); setLimitations([]);
    saveEpoch.current++; setGap(null); setComposer(emptyComposer()); setDraft(null); draftRef.current = null; setError(""); setAdding(false); setQuery("");
    const url = new URL(window.location.href); ["draft", "gap", "job"].forEach(key => url.searchParams.delete(key)); window.history.replaceState(null, "", url);
    requestAnimationFrame(() => document.getElementById("gap-search")?.focus());
  };
  const focusResult = () => { setShowResults(true); pendingResultFocus.current = searching; if (!searching) requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#gap-results-list button")?.focus()); };
  const linkedMechanisms = gap?.attachments.filter(item => item.resolution === "resolved" && item.target?.dapper_id.startsWith("dapper:Mechanism.")) || [];
  const selectedFactor = (id: string) => {
    const anchor = composer.eaggl_anchors.find(item => item.reference.source_id === id); const factor = factors[id];
    return anchor && factor?.source_revision === anchor.reference.source_revision && factor.object.id === anchor.reference.dapper_id ? factor : undefined;
  };
  const anchorName = (id: string) => selectedFactor(id)?.cfde_anchor.label || selectedFactor(id)?.object.name || selectedFactor(id)?.cfde_anchor.subtitle || id;
  return <main id="main" className={`composer-page prototype-composer ${gap ? "has-gap" : ""} ${job ? "has-job" : ""} ${job && terminal(job.status) ? "job-complete" : ""}`}>
    {!gap && <p className="invitation">Help us close these <a href="https://dismech.monarchinitiative.org/app/discussions/index.html" target="_blank" rel="noopener noreferrer">knowledge gaps</a></p>}
    <section className={`question-shell ${job ? "submitted" : ""}`} aria-label="Knowledge gap and mechanism anchors">
      {!gap ? <div className="gap-input-wrap"><label className="sr-only" htmlFor="gap-search">Search DisMech knowledge gaps</label><input id="gap-search" type="search" role="combobox" aria-autocomplete="list" aria-expanded={!!query.trim() && showResults && !!results.length} value={query} autoComplete="off" onChange={e => { setQuery(e.target.value); setResults([]); setSearching(!!e.target.value.trim()); pendingResultFocus.current = false; setError(""); setShowResults(true); }} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)} onKeyDown={e => { if ((e.key === "ArrowDown" || e.key === "Enter") && query.trim()) { e.preventDefault(); focusResult(); } if (e.key === "Escape") { setShowResults(false); pendingResultFocus.current = false; } }} placeholder={focused ? "Search DisMech knowledge gaps" : ""} aria-controls="gap-results-list" />{!query && !focused && <span className="idle-question" aria-hidden="true">{example}</span>}<button className="send search-arrow" disabled={!query.trim()} aria-label="Show matching knowledge gaps" title="Show matching knowledge gaps" onClick={focusResult}><span aria-hidden="true">↑</span></button></div> : <>
        <div className="selected-question-row"><h1 className="selected-question">{gap.object.text}</h1>{!job && <button className="clear-question" aria-label="Search for a different knowledge gap" onClick={clearGap}>×</button>}</div>
        <div className="inline-context">
          {!job && <><div className="question-meta"><button className="subtle" onClick={() => setInspection({ title: "About this knowledge gap", description: gap.object.gap_description, value: gap })}>About this knowledge gap ↗</button><span>DisMech</span></div><p className="anchor-guidance">Anchor on possible genetic mechanisms to explore evidence for answering this gap.</p><div className="chip-group-label">Mechanism anchors <span>{composer.eaggl_anchors.length}</span>{suggesting && <span role="status">Finding anchors…</span>}{!!limitations.length && <button className="matching-note" onClick={() => setInspection({ title: "About mechanism matching", description: limitations.join(" "), value: { model: composer.model, automatic_anchors: composer.eaggl_anchors.filter(anchor => anchor.origin === "automatic").length } })}>About matching</button>}</div></>}
          <div className="anchor-chips" aria-label="Selected mechanism anchors">{composer.eaggl_anchors.map(anchor => <span className="chip" key={anchor.reference.source_id}><button className="label" title={[anchorName(anchor.reference.source_id), selectedFactor(anchor.reference.source_id)?.cfde_anchor.subtitle].filter(Boolean).join(" · ")} onClick={() => setInspection({ title: selectedFactor(anchor.reference.source_id)?.cfde_anchor.label || "Mechanism anchor", description: selectedFactor(anchor.reference.source_id)?.object.description, value: selectedFactor(anchor.reference.source_id) || anchor })}>{anchorName(anchor.reference.source_id)}</button>{!job && <button className="remove" aria-label={`Remove ${anchorName(anchor.reference.source_id)}`} onClick={() => setComposer(current => removeAnchor(current, anchor.reference.source_id))}>×</button>}</span>)}{!job && <button className="chip-add" aria-expanded={adding} aria-controls="factor-picker" aria-label="Add a mechanism anchor" onClick={() => { setAdding(!adding); if (!adding) requestAnimationFrame(() => document.getElementById("mechanism-search")?.focus()); }}>+</button>}</div>
          {!job && <>
            {!composer.eaggl_anchors.length && !suggesting && <p className="anchor-required" role="status">Add at least one mechanism anchor to continue.</p>}
            <div className="context-disclosures">
              <details id="factor-picker" open={adding} onToggle={e => setAdding(e.currentTarget.open)}><summary>Find more mechanisms</summary><label className="sr-only" htmlFor="mechanism-search">Search possible genetic mechanisms</label><input id="mechanism-search" type="search" className="field" value={composer.mechanism_subquery} onChange={e => setComposer(c => ({ ...c, mechanism_subquery: e.target.value }))} placeholder="Search possible genetic mechanisms" />{matches.map(factor => <button className="mechanism-match" key={factor.source_id} title={factor.cfde_anchor.subtitle || factor.source_id} disabled={composer.eaggl_anchors.length >= 10 || composer.eaggl_anchors.some(a => a.reference.source_id === factor.source_id)} onClick={() => { rememberFactors([factor]); setComposer(c => ({ ...c, eaggl_anchors: [...c.eaggl_anchors, factorSelection(factor, "manual")], dismissed_source_ids: c.dismissed_source_ids.filter(id => id !== factor.source_id) })); }}><span>{factor.cfde_anchor.label || factor.object.name || factor.cfde_anchor.subtitle}</span><span aria-hidden="true">+</span></button>)}{findingMatches && <p className="muted" role="status">{matches.length ? "Finding related mechanisms…" : "Finding mechanisms…"}</p>}{!!composer.mechanism_subquery.trim() && !findingMatches && !matches.length && <p className="muted">No mapped anchors found.</p>}<button className="text-button" disabled={suggesting} onClick={() => { const next = { ...composer, dismissed_source_ids: [], eaggl_anchors: composer.eaggl_anchors.filter(a => a.origin === "manual") }; setComposer(next); void suggest(next); }}>Reset suggestions</button><small>Up to five automatic anchors. Removed anchors stay dismissed until reset.</small></details>
              <details><summary>Linked DisMech mechanisms <span className="disclosure-count">{linkedMechanisms.length} linked</span></summary><p className="muted">Mechanisms linked to this curated knowledge gap. Source context is read-only.</p>{linkedMechanisms.map((item, i) => <button className="linked-mechanism" key={i} onClick={() => setInspection({ title: item.label || "DisMech source context", value: item })}><span>{item.label || item.source_reference}<small>{item.target_kind} · {item.resolution.replaceAll("_", " ")}</small></span><span aria-hidden="true">↗</span></button>)}{!linkedMechanisms.length && <p className="muted">No linked mechanisms in this source observation.</p>}</details>
              <details><summary>Additional knowledge graphs</summary><div className="checks">{(["biomarkerkg", "prokn"] as const).map(kg => <label key={kg}><input type="checkbox" checked={composer.selected_kgs.includes(kg)} onChange={e => setComposer(c => ({ ...c, selected_kgs: e.target.checked ? [...c.selected_kgs, kg] : c.selected_kgs.filter(k => k !== kg) }))} />{kg === "prokn" ? "ProKN" : "BiomarkerKG"}</label>)}</div></details>
            </div>
            <span className="sr-only" role="status">{me ? saveState : "Selections kept in this browser"}</span>
            <div className="submit-row"><button className="gap-submit" aria-label={submitting ? "Submitting analysis" : "Let’s close this gap"} disabled={!composer.eaggl_anchors.length || suggesting || submitting || conflict || !ready} onClick={() => me ? void launch() : dialog.current?.showModal()}><span>{submitting ? "Submitting…" : "Let’s close this gap"}</span><span className="send" aria-hidden="true"><span>↑</span></span></button></div>
          </>}
        </div>
      </>}
    </section>
    {!gap && !!query.trim() && showResults && <section className="search-results" aria-label="Knowledge gap search results"><p className="result-heading" role="status">{searching ? "Searching knowledge gaps…" : error ? "Search unavailable" : `${results.length} related knowledge gaps`}</p><div id="gap-results-list" role="listbox" aria-label="Matching knowledge gaps">{results.map((value, index) => <button className="gap-result" role="option" aria-selected="false" key={value.source.source_id} onClick={() => void selectGap(value)} onKeyDown={e => { if (e.key === "Escape") { setShowResults(false); document.getElementById("gap-search")?.focus(); } if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (e.key === "ArrowUp" && index === 0) document.getElementById("gap-search")?.focus(); else { const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button"); buttons?.[(index + (e.key === "ArrowDown" ? 1 : -1)) % results.length]?.focus(); } } }}><span>{value.object.text}</span><small>{value.source.disease_label} · {value.source.status || "Status not specified"}</small></button>)}</div>{!searching && !error && !results.length && <p className="empty">No matching knowledge gaps. Try a disease, gene or mechanism.</p>}</section>}
    {!gap && !query.length && <section className="trending" aria-label="Trending knowledge gaps"><div className="trending-heading"><h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m3 17 6-6 4 4 8-10" /><path d="M15 5h6v6" /></svg>Trending knowledge gaps</h2><span>Curated from DisMech</span></div>{trending.map(value => <button className="trend" key={value.source.source_id} onClick={() => void selectGap(value)}><span className="trend-question">{value.object.text}<small>{value.scientific_accounts.count} scientific account{value.scientific_accounts.count === 1 ? "" : "s"} {value.scientific_accounts.scope === "owner_exact_gap" ? "in your workspace" : "publicly available"}</small></span><span className="trend-arrow" aria-hidden="true">↗</span></button>)}{!trending.length && !error && <p className="muted">Loading knowledge gaps…</p>}</section>}
    {error && <div className="error" role="alert">{error}{!gap && <button onClick={() => { setError(""); void api.gaps().then(value => loadFeaturedGaps(api, value.items)).then(setTrending).catch(e => setError(messageOf(e))); }}>Retry</button>}</div>}
    {conflict && <div className="conflict"><p>This draft changed in another session. Your edits are retained here.</p><button onClick={async () => { if (!draftRef.current) return; const latest = await api.draft(draftRef.current.id); draftRef.current = latest; setDraft(latest); setComposer(latest.composer); setConflict(false); setError(""); }}>Load saved version</button><button onClick={() => { draftRef.current = null; setDraft(null); setConflict(false); setError(""); void save().catch(e => setError(messageOf(e))); }}>Save my edits as a new draft</button></div>}
    {job && <><Activity key={job.id} initial={job} onJob={setJob} />{accountIds.map(id => <AccountPreview key={id} id={id} />)}{terminal(job.status) && <button className="text-button return-to-question" onClick={reset}>Return to question</button>}</>}
    <dialog ref={inspectionDialog} className="inspection-dialog" aria-labelledby="inspection-title" onClose={() => setInspection(null)}><div className="inspection-heading"><h2 id="inspection-title">{inspection?.title}</h2><button aria-label="Close record" onClick={() => inspectionDialog.current?.close()}>×</button></div><div className="inspection-body">{inspection?.title === "About this knowledge gap" && <p>{gap?.object.text}</p>}{inspection?.description && <><h3>{inspection.title === "About this knowledge gap" ? "What remains unknown" : "Context"}</h3><p>{inspection.description}</p></>}<details open={!inspection?.description}><summary>Source evidence and record</summary><Record value={inspection?.value} /></details></div></dialog>
    <dialog ref={dialog} className="auth-dialog" aria-labelledby="auth-title"><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id="auth-title">Continue your exploration</h2><p>Sign in to keep your work, or continue anonymously.</p><ProviderButtons beforeLogin={() => sessionStorage.setItem("reveal:submit-after-login", "1")} /><div className="or">or</div><button className="provider" disabled={submitting} onClick={anonymous}>Continue anonymously</button><small>Your selected question and anchors stay with you. Anonymous access depends on this browser session.</small>{error && <p role="alert" className="error">{error}</p>}</dialog>
  </main>;
}
