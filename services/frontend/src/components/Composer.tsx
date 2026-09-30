"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, supersededReference, terminal, type Schema } from "@/lib/client";
import { applySuggestions, dropOutdatedAnchors, emptyComposer, factorSelection, persistDraft, removeAnchor, selectedGap } from "@/lib/composer";
import { ProviderButtons, useIdentity } from "./Session";
import { Activity } from "./Activity";
import { AccountPreview, Record } from "./Scientific";
import { GapBrowser, GapScopeSelector, type GapScope } from "./GapBrowser";
import { GapAccounts } from "./GapAccounts";
import { rememberSubmission, restoreSubmission, type SubmissionAttempt, type SubmissionMethod, type SubmissionStage } from "@/lib/submission";
import { SubmissionProgress } from "./SubmissionProgress";
import { LoadingSurface, LoadingStatus } from "./LoadingSurface";
import { withRequestDeadline } from "@/lib/request-deadline";
import { providerRedirect } from "@/lib/provider-redirect";
import { MechanismLabel } from "./MechanismLabel";
import { mechanismName, mechanismTrait } from "@/lib/mechanism-display";
import { anchorKey, currentComposer, isReferenceReload, observedReferenceModel, outdatedFromAnchor, outdatedFromFactor, referenceRechecker, type OutdatedAnchor, type ReferenceArchive } from "@/lib/reference";
import { ReferenceArchiveBanner } from "./ReferenceArchive";
import { onWorkspaceChange } from "@/lib/workspace-events";

const storageKey = "reveal:composer";
type LocalDraft = { composer: Schema<"Composer">; gap: Schema<"GapRecord"> | null; factors: Record<string, Schema<"EagglFactor">>; draft: Schema<"Draft"> | null; owner: string | null; job?: Schema<"Job"> | null; submitKey?: { binding: string; key: string } | null };
export function Composer({ initialJobId, initialDraftId }: { initialJobId?: string; initialDraftId?: string } = {}) {
  const { me, ready, refresh } = useIdentity();
  const [composer, setComposer] = useState(emptyComposer);
  const [gap, setGap] = useState<Schema<"GapRecord"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Schema<"EagglFactor">>>({});
  // Anchors from a superseded reference generation render from their archive stamp or frozen factor.
  const [outdated, setOutdated] = useState<Record<string, OutdatedAnchor>>({});
  const [requestArchive, setRequestArchive] = useState<ReferenceArchive | null>(null);
  const [draft, setDraft] = useState<Schema<"Draft"> | null>(null);
  // Browsing also autosaves drafts; only opening/restoring one enters editor mode.
  const [draftView, setDraftView] = useState(!!initialDraftId);
  const [restoring, setRestoring] = useState("");
  const [retrievingJob, setRetrievingJob] = useState(!!initialJobId);
  const [jobRestoreError, setJobRestoreError] = useState("");
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [gapScope, setGapScope] = useState<GapScope>("public");
  const [query, setQuery] = useState(""); const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Schema<"GapRecord">[]>([]); const [trending, setTrending] = useState<Schema<"GapRecord">[]>([]);
  const searchBinding = `${gapScope}:${me?.user_id || "visitor"}`;
  const [resultsFor, setResultsFor] = useState("");
  const visibleResults = resultsFor === searchBinding ? results : [];
  const [matches, setMatches] = useState<Schema<"EagglFactor">[]>([]); const [adding, setAdding] = useState(false);
  const [findingMatches, setFindingMatches] = useState(false);
  const [suggesting, setSuggesting] = useState(false); const [limitations, setLimitations] = useState<string[]>([]);
  const [saveState, setSaveState] = useState("Kept in this browser"); const [conflict, setConflict] = useState(false);
  const [error, setError] = useState(""); const [booted, setBooted] = useState(false);
  const [job, setJob] = useState<Schema<"Job"> | null>(null);
  const [submission, setSubmission] = useState<{ stage: SubmissionStage; error?: string } | null>(null);
  const pendingSubmission = useRef<SubmissionAttempt | null>(null);
  const submissionRunning = useRef(false);
  const submissionResumed = useRef(false);
  const [example, setExample] = useState("What would you like to understand?"); const [focused, setFocused] = useState(false);
  const [showResults, setShowResults] = useState(true);
  const pendingResultFocus = useRef(false);
  const [inspection, setInspection] = useState<{ title: string; description?: string | null; value: unknown } | null>(null);
  const inspectionDialog = useRef<HTMLDialogElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const draftRef = useRef(draft); const currentRef = useRef(composer); const loadedOwner = useRef<string | null>(null);
  const saveEpoch = useRef(0);
  const restoreEpoch = useRef(0);
  const mounted = useRef(false);
  const hydrated = useRef(false);
  const suggestionRequest = useRef<AbortController | null>(null);
  const saveQueue = useRef<Promise<Schema<"Draft"> | null>>(Promise.resolve(null));
  const requestKeys = useRef(new Map<string, string>()); const submitKey = useRef<{ binding: string; key: string } | null>(null);
  const getKey = (body: string) => {
    if (!requestKeys.current.has(body)) requestKeys.current.set(body, crypto.randomUUID());
    if (pendingSubmission.current) { pendingSubmission.current.requestKeys = [...requestKeys.current]; rememberSubmission(pendingSubmission.current); }
    return requestKeys.current.get(body)!;
  };
  currentRef.current = composer;
  useEffect(() => { setDraftView(!!initialDraftId); }, [initialDraftId]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; suggestionRequest.current?.abort(); }; }, []);
  useEffect(() => {
    // Fast Refresh replays effects while preserving refs and active promises.
    // Rehydrating then would reset a failed attempt to a spinner without resuming it.
    if (hydrated.current) return;
    hydrated.current = true;
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) || "null") as LocalDraft | null;
      // Anchors kept from a superseded reference model cannot be analysed; discard that snapshot.
      const composer = stored && currentComposer(stored.composer);
      if (stored && !composer) sessionStorage.removeItem(storageKey);
      const local = stored && composer ? { ...stored, composer } : null;
      const params = new URLSearchParams(window.location.search);
      // A submitted snapshot belongs to its explicit link, not the home page.
      // Keep refresh recovery for an unsubmitted question and OAuth below.
      const hasSelectionLink = ["job", "draft", "gap"].some(key => params.get(key));
      const matchesLink = local && (!local.job || hasSelectionLink) &&
        (!params.get("job") || params.get("job") === local.job?.id) &&
        (!params.get("draft") || params.get("draft") === local.draft?.id) &&
        (!params.get("gap") || params.get("gap") === local.gap?.object.id);
      if (local && matchesLink) { setComposer(local.composer); setGap(local.gap); setFactors(local.factors); setDraft(local.draft); draftRef.current = local.draft; loadedOwner.current = local.owner; submitKey.current = local.submitKey || null; if (local.job && params.get("job") === local.job.id) setJob(local.job); }
      if (local?.draft && matchesLink && !hasSelectionLink) setDraftView(true);
    } catch { sessionStorage.removeItem(storageKey); }
    // Explicit workspace links open the requested record. A pending submission
    // may resume only at the unqualified home/OAuth return URL.
    const params = new URLSearchParams(window.location.search);
    const pending = ["job", "draft", "gap"].some(key => params.get(key)) ? null : restoreSubmission();
    if (pending) {
      pendingSubmission.current = pending;
      setComposer(pending.composer); currentRef.current = pending.composer;
      if (pending.gap) setGap(pending.gap);
      setDraft(pending.draft); draftRef.current = pending.draft; loadedOwner.current = pending.owner;
      requestKeys.current = new Map(pending.requestKeys); submitKey.current = pending.submitKey;
      setJob(null); setSubmission({ stage: "signing-in" });
    }
    setBooted(true);

  }, []);
  useEffect(() => { if (ready && !me && gapScope === "workspace") setGapScope("public"); }, [ready, me?.user_id, gapScope]);
  useEffect(() => {
    if (!booted) return;
    sessionStorage.setItem(storageKey, JSON.stringify({ composer, gap, factors, draft, owner: me?.user_id || loadedOwner.current, job, submitKey: submitKey.current } satisfies LocalDraft));
  }, [composer, gap, factors, draft, job, booted, me?.user_id]);
  useEffect(() => {
    if (!ready || !booted || !me) return;
    if (loadedOwner.current && loadedOwner.current !== me.user_id) { saveEpoch.current++; draftRef.current = null; setDraft(null); setJob(null); }
    loadedOwner.current = me.user_id;
  }, [me?.user_id, ready, booted]);
  useEffect(() => {
    if (!booted || !ready || pendingSubmission.current) return;
    const params = new URLSearchParams(window.location.search);
    const draftId = params.get("draft"); const gapId = params.get("gap"); const jobId = params.get("job");
    let active = true; const epoch = restoreEpoch.current;
    const canRestore = () => active && !pendingSubmission.current && epoch === restoreEpoch.current;
    const restore = async () => {
      if (jobId && !me) { setRetrievingJob(false); setJob(null); setJobRestoreError("Sign in with the account that started this job to retrieve its status."); return; }
      if (jobId) { setRetrievingJob(true); setJobRestoreError(""); }
      if ((draftId && me) || gapId) setRestoring(draftId ? "Restoring your saved question" : "Opening knowledge gap");
      const savedJob = jobId && me ? api.job(jobId) : null;
      const restoreJob = async () => {
        if (!savedJob) return;
        try { const value = await savedJob; if (canRestore()) setJob(value); }
        catch (failure) { if (canRestore()) setJobRestoreError(messageOf(failure)); }
        finally { if (canRestore()) setRetrievingJob(false); }
      };
      const restoreSelection = async () => {
      const restoreSubmitted = async () => {
        const value = await savedJob;
        if (!value?.research_request_id) return;
        const frozen = await api.request(value.research_request_id);
        const source = frozen.composer.source_gap ? await api.gap(frozen.composer.source_gap.id) : null;
        if (!canRestore()) return;
        saveEpoch.current++; draftRef.current = null; setDraft(null);
        // Archived requests label their original anchors from the stamp, not the live catalog.
        const archive = frozen.archive || null;
        setRequestArchive(archive);
        if (archive) setOutdated(current => ({ ...current, ...Object.fromEntries(frozen.composer.eaggl_anchors.flatMap(anchor => {
          const stamped = archive.reference.anchors.find(item => item.source_id === anchor.reference.source_id);
          return stamped ? [[anchorKey(anchor.reference), outdatedFromAnchor(stamped)]] : [];
        })) }));
        setComposer(frozen.composer); setGap(source);
      };
      try {
        if (draftId && me) {
          let value: Schema<"Draft">;
          try { value = await api.draft(draftId); }
          catch (failure) {
            if (savedJob && failure instanceof ApiError && failure.status === 404) { await restoreSubmitted(); return; }
            throw failure;
          }
          const source = value.composer.source_gap ? await api.gap(value.composer.source_gap.id) : null;
          if (!canRestore()) return;
          saveEpoch.current++; draftRef.current = value; setDraft(value); setSaveState("Saved"); setComposer(value.composer); setGap(source); setRequestArchive(null); if (!jobId) setJob(null);
          // "Start a new analysis with current factors" opens its new draft here to suggest anchors.
          if (params.get("suggest") === "current") {
            const url = new URL(window.location.href); url.searchParams.delete("suggest"); window.history.replaceState(null, "", url);
            if (value.composer.source_gap && !value.composer.eaggl_anchors.length) void suggest(value.composer);
          }
        } else if (savedJob) {
          await restoreSubmitted();
        } else if (gapId) {
          const source = await api.gap(gapId); if (!canRestore()) return;
          if (currentRef.current.source_gap?.id === source.object.id && currentRef.current.source_gap.source_revision === source.source.source_revision) setGap(source);
          else if (jobId) { setGap(source); setComposer({ ...emptyComposer(), source_gap: selectedGap(source) }); }
          else await selectGap(source, true);
        }
      } catch (failure) { if (canRestore()) setError(messageOf(failure)); }
      finally { if (canRestore()) setRestoring(""); }
      };
      // Status and selection are independent: neither slow source lookup nor
      // draft storage should hold up the live activity stream.
      await Promise.allSettled([restoreJob(), restoreSelection()]);
    };
    void restore(); return () => { active = false; setRestoring(""); };
  }, [booted, ready, me?.user_id, restoreAttempt, initialDraftId, initialJobId]);
  useEffect(() => {
    if (query.trim()) {
      const controller = new AbortController(); setSearching(true); setResults([]);
      const timer = setTimeout(() => { void api.searchGaps(query.trim(), controller.signal, gapScope).then(value => { if (!controller.signal.aborted) { setResults(value.items.map(hit => hit.gap)); setResultsFor(searchBinding); setSearching(false); } }).catch(e => { if (!controller.signal.aborted) { setError(messageOf(e)); setSearching(false); } }); }, 250);
      return () => { clearTimeout(timer); controller.abort(); };
    }
    setResults([]); setSearching(false);
  }, [query, gapScope, me?.user_id]);
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
        // A draft gone since it was saved (dropped at a reference cutover, or deleted elsewhere) is replaced by a new one.
        const { draft: next, replaced } = await persistDraft(api, previous, snapshot, getKey,
          { current: () => epoch === saveEpoch.current, gone: () => { draftRef.current = null; setDraft(null); } });
        if (epoch !== saveEpoch.current) return null;
        draftRef.current = next; setDraft(next); setSaveState("Saved"); setConflict(false);
        if (replaced) {
          const url = new URL(window.location.href);
          if (url.searchParams.get("draft") === replaced) { url.searchParams.set("draft", next.id); window.history.replaceState(null, "", url); }
        }
        return next;
      } catch (failure) {
        setSaveState("Save failed — edits retained locally");
        if (failure instanceof ApiError && failure.status === 409 && !supersededReference(failure)) setConflict(true);
        throw failure;
      }
    }); saveQueue.current = queued; return queued;
  };
  const outdatedAnchors = composer.eaggl_anchors.filter(anchor => outdated[anchorKey(anchor.reference)]);
  useEffect(() => {
    // Outdated anchors cannot be saved; wait until they are replaced with current factors.
    if (!booted || !me || !composer.source_gap || job || retrievingJob || restoring || jobRestoreError || conflict || suggesting || submission || outdatedAnchors.length) return;
    const timer = setTimeout(() => { void save(composer).catch(e => { setError(messageOf(e)); if (supersededReference(e)) void verifyAnchors(); }); }, 1000);
    return () => clearTimeout(timer);
  }, [composer, me?.user_id, booted, job, retrievingJob, restoring, jobRestoreError, conflict, suggesting, submission, outdatedAnchors.length]);
  useEffect(() => {
    if (!booted || !ready || !pendingSubmission.current || submissionResumed.current) return;
    submissionResumed.current = true;
    void provision(pendingSubmission.current);
  }, [booted, ready, me?.user_id]);
  useEffect(() => {
    const missing = composer.eaggl_anchors.filter(anchor => { if (outdated[anchorKey(anchor.reference)]) return false; const factor = factors[anchor.reference.source_id]; return !factor || factor.source_revision !== anchor.reference.source_revision || factor.object.id !== anchor.reference.dapper_id; });
    if (!missing.length) return; let active = true;
    void Promise.allSettled(missing.map(anchor => api.mechanism(anchor.reference.source_id, anchor.reference.source_revision))).then(values => {
      if (!active) return;
      const records = values.flatMap((value, index) => value.status === "fulfilled" && value.value.source === "eaggl" && value.value.source_revision === missing[index].reference.source_revision && value.value.object.id === missing[index].reference.dapper_id ? [value.value] : []);
      if (records.length) setFactors(current => ({ ...current, ...Object.fromEntries(records.map(record => [record.source_id, record])) }));
      // 410: a superseded factor; show its frozen snapshot instead of retrying the live catalog.
      const gone = values.flatMap((value, index) => value.status === "rejected" && supersededReference(value.reason) ? [outdatedEntry(missing[index], value.reason)] : []);
      if (gone.length) setOutdated(current => ({ ...current, ...Object.fromEntries(gone) }));
    });
    return () => { active = false; };
  }, [composer.eaggl_anchors, factors, outdated]);
  const outdatedEntry = (anchor: Schema<"Selection">, failure: ApiError) => {
    const cached = factors[anchor.reference.source_id];
    return [anchorKey(anchor.reference), outdatedFromFactor(anchor.reference.source_id, failure.problem?.archived_reference_factor, cached && { name: mechanismName(cached), trait: mechanismTrait(cached) })] as const;
  };
  // A 409 REFERENCE_GENERATION_SUPERSEDED names no anchor: re-read each one to find the superseded ones.
  // After such a 409 a changed revision is also a superseded generation (KPN ids can recur).
  const verifyAnchors = async (revisions = true) => {
    const anchors = currentRef.current.eaggl_anchors;
    const values = await Promise.allSettled(anchors.map(anchor => api.mechanism(anchor.reference.source_id, anchor.reference.source_revision)));
    const stale = values.flatMap((value, index) => value.status === "rejected" && (supersededReference(value.reason) || (revisions && value.reason instanceof ApiError && value.reason.code === "SOURCE_REVISION_CHANGED")) ? [outdatedEntry(anchors[index], value.reason as ApiError)] : []);
    if (stale.length && mounted.current) setOutdated(current => ({ ...current, ...Object.fromEntries(stale) }));
  };
  const verifyRef = useRef(verifyAnchors); verifyRef.current = verifyAnchors;
  // A reference cutover publishes a `reference` catalog event (publications emit other catalog events);
  // recheck anchors kept in an open composer, now and after the API has swapped catalogs (410 only).
  useEffect(() => {
    const rechecks = referenceRechecker(() => { if (mounted.current && currentRef.current.eaggl_anchors.length) void verifyRef.current(false); });
    const remove = onWorkspaceChange((reset, event) => { if (!reset && isReferenceReload(event)) rechecks.schedule(); });
    return () => { remove(); rechecks.cancel(); };
  }, []);
  const replaceOutdated = () => {
    const next = dropOutdatedAnchors(currentRef.current, new Set(outdatedAnchors.map(anchor => anchor.reference.source_id)));
    setError(""); setConflict(false); setComposer(next); void suggest(next);
  };
  const rememberFactors = (values: Schema<"EagglFactor">[]) => setFactors(current => ({ ...current, ...Object.fromEntries(values.map(f => [f.source_id, f])) }));
  const suggest = async (snapshot: Schema<"Composer">) => {
    if (!snapshot.source_gap) return;
    suggestionRequest.current?.abort();
    const controller = new AbortController(); suggestionRequest.current = controller;
    setSuggesting(true);
    try {
      // The API serves one active reference model; the composer's own model is the fallback.
      const value = await api.suggest({ source_gap: snapshot.source_gap, manual_eaggl_anchors: snapshot.eaggl_anchors.filter(a => a.origin === "manual").map(a => a.reference), dismissed_source_ids: snapshot.dismissed_source_ids, subquery: snapshot.mechanism_subquery, mode: "semantic", model: observedReferenceModel() || snapshot.model }, controller.signal);
      if (controller.signal.aborted) return;
      rememberFactors(value.automatic_anchors.map(item => item.factor)); setLimitations(value.limitations);
      setComposer(current => current.source_gap?.id === snapshot.source_gap?.id && current.source_gap?.source_revision === snapshot.source_gap?.source_revision ? applySuggestions(current, value) : current);
    } catch (failure) { if (!controller.signal.aborted) { setError(messageOf(failure)); if (supersededReference(failure)) void verifyAnchors(); } }
    finally { if (suggestionRequest.current === controller) { suggestionRequest.current = null; setSuggesting(false); } }
  };
  async function selectGap(value: Schema<"GapRecord">, restoringSelection = false) {
    if (!restoringSelection) { restoreEpoch.current++; setRestoring(""); setRetrievingJob(false); setJobRestoreError(""); }
    saveEpoch.current++;
    setGap(value); setQuery(""); setJob(null); setError(""); setConflict(false); submitKey.current = null; setRequestArchive(null); setOutdated({});
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
  const submissionFailed = (failure: unknown) => {
    setSubmission(current => ({ stage: current?.stage || "signing-in", error: messageOf(failure) }));
  };
  const beginSubmission = (method: SubmissionMethod) => {
    if (pendingSubmission.current || submissionRunning.current || !currentRef.current.source_gap || !currentRef.current.eaggl_anchors.length) return null;
    const attempt: SubmissionAttempt = {
      method, question: gap?.object.text || "", gap, composer: structuredClone(currentRef.current),
      draft: draftRef.current, owner: me?.user_id || loadedOwner.current,
      anonymousKey: crypto.randomUUID(), requestKeys: [...requestKeys.current], submitKey: submitKey.current,
    };
    restoreEpoch.current++;
    pendingSubmission.current = attempt; submissionResumed.current = true;
    rememberSubmission(attempt);
    setError(""); dialog.current?.close();
    setSubmission({ stage: method === "session" ? "saving" : "signing-in" });
    return attempt;
  };
  async function provision(attempt: SubmissionAttempt, confirmedIdentity = me) {
    if (submissionRunning.current) return;
    submissionRunning.current = true;
    setSubmission({ stage: "signing-in" });
    try {
      let identity = confirmedIdentity;
      if (attempt.method === "anonymous" && !identity) {
        await withRequestDeadline(async signal => {
          const response = await fetch("/api/session/anonymous", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": attempt.anonymousKey }, body: "{}", signal });
          const value = await response.json(); if (!response.ok) throw new Error(value.detail || "Anonymous continuation is unavailable. Please try again.");
        });
        identity = await refresh();
      } else if (!identity) identity = await refresh();
      if (attempt.method === "google" || attempt.method === "orcid") {
        if (new URLSearchParams(window.location.search).has("error") || identity?.principal_kind !== "registered") throw new Error("Sign-in was not completed. Try signing in again, or return to your question to choose another option.");
      }
      if (!identity) throw new Error("We could not confirm your session. Please try again. Your question and anchors are saved in this browser.");
      // Authentication can change the owner while this function is awaiting a response.
      // Set the owner before saving so the identity effect cannot discard a new draft.
      if (attempt.owner && attempt.owner !== identity.user_id) {
        saveEpoch.current++; draftRef.current = null; setDraft(null); attempt.draft = null;
        requestKeys.current.clear(); submitKey.current = null; attempt.submitKey = null; attempt.requestKeys = [];
      }
      loadedOwner.current = identity.user_id; attempt.owner = identity.user_id;
      rememberSubmission(attempt);
      setSubmission({ stage: "saving" });
      const saved = await save(attempt.composer);
      if (!saved) throw new Error("Your draft changed while it was being saved. Please try again.");
      attempt.draft = saved; rememberSubmission(attempt);
      const binding = `${saved.id}:${saved.version}`;
      if (submitKey.current?.binding !== binding) submitKey.current = { binding, key: crypto.randomUUID() };
      attempt.submitKey = submitKey.current; rememberSubmission(attempt);
      setSubmission({ stage: "submitting" });
      const result = await api.submit({ kind: "analysis", draft_id: saved.id, draft_version: saved.version }, submitKey.current.key);
      setJob(result);
      const url = new URL(window.location.href); url.searchParams.set("draft", saved.id); url.searchParams.set("job", result.id); url.searchParams.delete("error");
      if (mounted.current) window.history.replaceState(null, "", url);
      try { sessionStorage.setItem(storageKey, JSON.stringify({ composer: saved.composer, gap, factors, draft: saved, owner: identity.user_id, job: result, submitKey: submitKey.current } satisfies LocalDraft)); } catch { /* Keep the confirmed job in memory when storage is unavailable. */ }
      pendingSubmission.current = null; rememberSubmission(null); setSubmission(null);
      if (mounted.current) requestAnimationFrame(() => document.getElementById("submitted-question")?.focus());
      // Recording the visit is bookkeeping; it must not delay or block a research job.
      void api.explore({ source_gap: saved.composer.source_gap!, draft_id: saved.id }).catch(() => {});
    } catch (failure) {
      // Retrying cannot help outdated anchors: return to the question and mark them for replacement.
      if (supersededReference(failure)) { pendingSubmission.current = null; rememberSubmission(null); setSubmission(null); setError(messageOf(failure)); void verifyAnchors(); }
      else submissionFailed(failure);
    }
    finally { submissionRunning.current = false; }
  }
  const launch = () => { const attempt = beginSubmission("session"); if (attempt) void provision(attempt); };
  const anonymous = () => { const attempt = beginSubmission("anonymous"); if (attempt) void provision(attempt); };
  const redirectToProvider = async (provider: "google" | "orcid") => {
    if (submissionRunning.current) return;
    submissionRunning.current = true;
    setSubmission({ stage: "signing-in" });
    try { window.location.assign(await providerRedirect(provider, window.location.origin + "/")); }
    catch (failure) { submissionFailed(failure); }
    finally { submissionRunning.current = false; }
  };
  const oauth = (provider: "google" | "orcid") => { if (beginSubmission(provider)) void redirectToProvider(provider); };
  const retrySubmission = async () => {
    const attempt = pendingSubmission.current; if (!attempt || submissionRunning.current) return;
    if (attempt.method === "google" || attempt.method === "orcid") {
      if (new URLSearchParams(window.location.search).has("error")) { void redirectToProvider(attempt.method); return; }
      // A failed session read does not mean the provider sign-in failed.
      if (me?.principal_kind !== "registered") {
        submissionRunning.current = true; setSubmission({ stage: "signing-in" });
        const identity = await refresh(); submissionRunning.current = false;
        if (identity?.principal_kind === "registered") void provision(attempt, identity);
        else void redirectToProvider(attempt.method);
        return;
      }
    }
    void provision(attempt);
  };
  const backToQuestion = () => {
    if (submissionRunning.current) return;
    // Keep the retry binding even when the user dismisses an uncertain submission.
    // Returning here after a reload must recover the same job, not create another.
    try { sessionStorage.setItem(storageKey, JSON.stringify({ composer, gap, factors, draft: draftRef.current, owner: loadedOwner.current, job, submitKey: submitKey.current } satisfies LocalDraft)); } catch { /* The binding stays in memory when storage is unavailable. */ }
    pendingSubmission.current = null; rememberSubmission(null); setSubmission(null); setError("");
    const url = new URL(window.location.href); url.searchParams.delete("error"); window.history.replaceState(null, "", url);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".gap-submit")?.focus());
  };
  const reset = () => { restoreEpoch.current++; setRestoring(""); setRetrievingJob(false); setJobRestoreError(""); setJob(null); submitKey.current = null; const url = new URL(window.location.href); url.searchParams.delete("job"); window.history.replaceState(null, "", url); };
  const accountIds = job?.result?.kind === "analysis" ? job.result.account_ids : [];
  const clearGap = () => {
    setDraftView(false);
    restoreEpoch.current++; setRestoring(""); setRetrievingJob(false); setJobRestoreError("");
    suggestionRequest.current?.abort(); suggestionRequest.current = null; setSuggesting(false); setLimitations([]);
    saveEpoch.current++; setGap(null); setComposer(emptyComposer()); setDraft(null); draftRef.current = null; setError(""); setAdding(false); setQuery(""); setRequestArchive(null); setOutdated({});
    const url = new URL(window.location.href); ["draft", "gap", "job"].forEach(key => url.searchParams.delete(key)); window.history.replaceState(null, "", url);
    requestAnimationFrame(() => document.getElementById("gap-search")?.focus());
  };
  const focusResult = () => { setShowResults(true); pendingResultFocus.current = searching; if (!searching) requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#gap-results-list button")?.focus()); };
  const linkedMechanisms = gap?.attachments.filter(item => item.resolution === "resolved" && item.target?.dapper_id.startsWith("dapper:Mechanism.")) || [];
  const selectedFactor = (id: string) => {
    const anchor = composer.eaggl_anchors.find(item => item.reference.source_id === id); const factor = factors[id];
    return anchor && factor?.source_revision === anchor.reference.source_revision && factor.object.id === anchor.reference.dapper_id ? factor : undefined;
  };
  const outdatedAnchor = (anchor: Schema<"Selection">) => outdated[anchorKey(anchor.reference)];
  const anchorName = (anchor: Schema<"Selection">) => outdatedAnchor(anchor)?.name || mechanismName(selectedFactor(anchor.reference.source_id));
  const inspectAnchor = (anchor: Schema<"Selection">) => {
    const old = outdatedAnchor(anchor), factor = selectedFactor(anchor.reference.source_id);
    if (old) setInspection({ title: old.name, description: "This anchor belongs to an outdated EAGGL reference generation. It is shown from its archived record and can no longer be analysed.", value: { ...old, reference: anchor.reference } });
    else setInspection({ title: factor?.cfde_anchor.label || "Mechanism anchor", description: factor?.object.description, value: factor || anchor });
  };
  const discoveryVisible = !draftView && !job;
  if (submission) return <SubmissionProgress stage={submission.stage} provider={pendingSubmission.current?.method} question={pendingSubmission.current?.question} error={submission.error} onRetry={retrySubmission} onBack={backToQuestion} retryLabel={pendingSubmission.current?.method === "google" || pendingSubmission.current?.method === "orcid" ? "Try again" : "Retry"} />;
  const jobStatusSurface = <LoadingSurface compact={!!job} skeleton={job ? "none" : "rows"} title={jobRestoreError ? "Unable to retrieve job status" : "Retrieving job status"} description="Checking the current stage and reconnecting to recorded activity." error={jobRestoreError} onRetry={() => { setJobRestoreError(""); setRetrievingJob(true); setRestoreAttempt(value => value + 1); }} />;
  if (!job && (retrievingJob || jobRestoreError)) return <main id="main" className="composer-page prototype-composer has-job">{jobStatusSurface}{jobRestoreError && <a className="text-button" href="/">Return to knowledge gaps</a>}</main>;
  if (draftView && !gap && !job) {
    const draftError = error || (ready && !me ? "Sign in to open your saved draft." : draft ? "This draft has no selected knowledge gap." : undefined);
    return <main id="main" className="composer-page prototype-composer">
      <LoadingSurface title={draftError ? "Unable to open this draft" : "Opening your saved draft"} description="Retrieving your saved question and mechanism anchors." error={draftError} onRetry={error ? () => { setError(""); setRestoreAttempt(value => value + 1); } : undefined} />
      <a className="text-button" href="/">Return to knowledge gaps</a>
    </main>;
  }
  return <main id="main" className={`composer-page prototype-composer ${!gap && discoveryVisible ? "is-gap-browsing" : ""} ${gap ? "has-gap" : ""} ${job ? "has-job" : ""} ${job && terminal(job.status) ? "job-complete" : ""}`}>
    {draft && !job && <nav className="composer-draft-nav" aria-label="Draft navigation"><Link href="/workspace?tab=gaps">← Your drafts</Link><strong>{draft.name || `Draft ${draft.id.slice(0, 8)}`}</strong><span role="status">{saveState}</span></nav>}
    {(retrievingJob || jobRestoreError) && jobStatusSurface}
    {restoring && !retrievingJob && <LoadingSurface compact={!!gap || !!job} skeleton={gap || job ? "none" : "rows"} title={restoring} description="Retrieving your saved question and mechanism anchors." />}
    {!gap && discoveryVisible && <p className="invitation">Help us close these <a href="https://dismech.monarchinitiative.org/app/discussions/index.html" target="_blank" rel="noopener noreferrer">knowledge gaps</a></p>}
    {(!job || gap) && <section className={`question-shell ${job ? "submitted" : ""}`} aria-label="Knowledge gap and mechanism anchors">
      {!gap ? <div className="gap-input-wrap"><label className="sr-only" htmlFor="gap-search">Search DisMech knowledge gaps</label><input id="gap-search" type="search" role="combobox" aria-autocomplete="list" aria-expanded={!!query.trim() && showResults && !!visibleResults.length} value={query} autoComplete="off" onChange={e => { setQuery(e.target.value); setResults([]); setSearching(!!e.target.value.trim()); pendingResultFocus.current = false; setError(""); setShowResults(true); }} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)} onKeyDown={e => { if ((e.key === "ArrowDown" || e.key === "Enter") && query.trim()) { e.preventDefault(); focusResult(); } if (e.key === "Escape") { setShowResults(false); pendingResultFocus.current = false; } }} placeholder={focused ? "Search DisMech knowledge gaps" : ""} aria-controls="gap-results-list" />{!query && !focused && <span className="idle-question" aria-hidden="true">{example}</span>}<button className="send search-arrow" disabled={!query.trim()} aria-label="Show matching knowledge gaps" title="Show matching knowledge gaps" onClick={focusResult}><span aria-hidden="true">↑</span></button></div> : <>
        <div className="selected-question-row"><h1 id="submitted-question" tabIndex={job ? -1 : undefined} className="selected-question">{gap.object.text}</h1>{!job && <button className="clear-question" aria-label="Search for a different knowledge gap" onClick={clearGap}>×</button>}</div>
        <div className="inline-context">
          {!job && <><div className="question-meta"><button className="subtle" onClick={() => setInspection({ title: "About this knowledge gap", description: gap.object.gap_description, value: gap })}>About this knowledge gap ↗</button><span>DisMech</span></div><p className="anchor-guidance">Anchor on possible genetic mechanisms to explore evidence for answering this gap.</p><div className="chip-group-label">Mechanism anchors <span>{composer.eaggl_anchors.length}</span>{suggesting && <LoadingStatus>Finding anchors…</LoadingStatus>}{!!limitations.length && <button className="matching-note" onClick={() => setInspection({ title: "About mechanism matching", description: limitations.join(" "), value: { model: composer.model, automatic_anchors: composer.eaggl_anchors.filter(anchor => anchor.origin === "automatic").length } })}>About matching</button>}</div></>}
          <div className="anchor-chips" aria-label="Selected mechanism anchors">{composer.eaggl_anchors.map(anchor => <span className="chip" key={anchor.reference.source_id}><button className="label" title={(outdatedAnchor(anchor) ? [anchorName(anchor), outdatedAnchor(anchor)!.trait, "Outdated reference"] : [anchorName(anchor), selectedFactor(anchor.reference.source_id)?.cfde_anchor.subtitle]).filter(Boolean).join(" · ")} onClick={() => inspectAnchor(anchor)}><MechanismLabel factor={selectedFactor(anchor.reference.source_id)} sourceId={anchor.reference.source_id} outdated={outdatedAnchor(anchor)} /></button>{!job && <button className="remove" aria-label={`Remove ${anchorName(anchor)}`} onClick={() => setComposer(current => removeAnchor(current, anchor.reference.source_id))}>×</button>}</span>)}{!job && <button className="chip-add" aria-expanded={adding} aria-controls="factor-picker" aria-label="Add a mechanism anchor" onClick={() => { setAdding(!adding); if (!adding) requestAnimationFrame(() => document.getElementById("mechanism-search")?.focus()); }}>+</button>}</div>
          {!job && <>
            {!!outdatedAnchors.length && <div className="conflict reference-outdated" role="status"><p>{outdatedAnchors.length === 1 ? "One mechanism anchor comes" : `${outdatedAnchors.length} mechanism anchors come`} from an outdated EAGGL reference and can’t be analysed. Replace {outdatedAnchors.length === 1 ? "it" : "them"} with current factors to continue.</p><button disabled={suggesting} onClick={replaceOutdated}>Replace with current factors</button></div>}
            {!composer.eaggl_anchors.length && !suggesting && <p className="anchor-required" role="status">Add at least one mechanism anchor to continue.</p>}
            <div className="context-disclosures">
              <details id="factor-picker" open={adding} onToggle={e => setAdding(e.currentTarget.open)}><summary>Find more mechanisms</summary><label className="sr-only" htmlFor="mechanism-search">Search possible genetic mechanisms</label><input id="mechanism-search" type="search" className="field" value={composer.mechanism_subquery} onChange={e => setComposer(c => ({ ...c, mechanism_subquery: e.target.value }))} placeholder="Search possible genetic mechanisms" />{matches.map(factor => <button className="mechanism-match" key={factor.source_id} title={factor.cfde_anchor.subtitle || factor.source_id} disabled={composer.eaggl_anchors.length >= 10 || composer.eaggl_anchors.some(a => a.reference.source_id === factor.source_id)} onClick={() => { rememberFactors([factor]); setComposer(c => ({ ...c, model: factor.model, eaggl_anchors: [...c.eaggl_anchors, factorSelection(factor, "manual")], dismissed_source_ids: c.dismissed_source_ids.filter(id => id !== factor.source_id) })); }}><MechanismLabel factor={factor} /><span aria-hidden="true">+</span></button>)}{findingMatches && <LoadingSurface compact skeleton={matches.length ? "none" : "rows"} rows={2} title={matches.length ? "Finding related mechanisms" : "Finding mechanisms"} description="Matching your search to available genetic mechanism anchors." />}{!!composer.mechanism_subquery.trim() && !findingMatches && !matches.length && <p className="muted">No mapped anchors found.</p>}<button className="text-button" disabled={suggesting} onClick={() => { const next = { ...composer, dismissed_source_ids: [], eaggl_anchors: composer.eaggl_anchors.filter(a => a.origin === "manual") }; setComposer(next); void suggest(next); }}>Reset suggestions</button><small>Up to five automatic anchors. Removed anchors stay dismissed until reset.</small></details>
              <details><summary>Linked DisMech mechanisms <span className="disclosure-count">{linkedMechanisms.length} linked</span></summary><p className="muted">Mechanisms linked to this curated knowledge gap. Source context is read-only.</p>{linkedMechanisms.map((item, i) => <button className="linked-mechanism" key={i} onClick={() => setInspection({ title: item.label || "DisMech source context", value: item })}><span>{item.label || item.source_reference}<small>{item.target_kind} · {item.resolution.replaceAll("_", " ")}</small></span><span aria-hidden="true">↗</span></button>)}{!linkedMechanisms.length && <p className="muted">No linked mechanisms in this source observation.</p>}</details>
              <details><summary>Additional knowledge graphs</summary><div className="checks">{(["biomarkerkg", "prokn"] as const).map(kg => <label key={kg}><input type="checkbox" checked={composer.selected_kgs.includes(kg)} onChange={e => setComposer(c => ({ ...c, selected_kgs: e.target.checked ? [...c.selected_kgs, kg] : c.selected_kgs.filter(k => k !== kg) }))} />{kg === "prokn" ? "ProKN" : "BiomarkerKG"}</label>)}</div></details>
            </div>
            {!draft && <span className="sr-only" role="status">{me ? saveState : "Selections kept in this browser"}</span>}
            <p id="research-result-options" className="research-result-options">A completed analysis returns <strong>scientific accounts</strong> supported by the evidence, or a <strong>saved exploration</strong> explaining why an account could not be supported.</p>
            <div className="submit-row"><button className="gap-submit" aria-label="Let’s close this gap" aria-describedby="research-result-options" disabled={!composer.eaggl_anchors.length || suggesting || conflict || !ready || !!outdatedAnchors.length} onClick={() => me ? void launch() : dialog.current?.showModal()}><span>Let’s close this gap</span><span className="send" aria-hidden="true"><span>↑</span></span></button></div>
          </>}
        </div>
      </>}
    </section>}
    {discoveryVisible && <GapScopeSelector scope={gapScope} onChange={scope => { setGapScope(scope); setError(""); }} />}
    {gap && discoveryVisible && <GapAccounts gap={gap} scope={gapScope} />}
    {!gap && discoveryVisible && !!query.trim() && showResults && <section className="search-results" aria-label="Knowledge gap search results">{searching ? <LoadingSurface key={query.trim()} compact rows={2} title="Searching knowledge gaps" description="Looking for matching questions in DisMech." /> : <p className="result-heading" role="status">{error ? "Search unavailable" : `${visibleResults.length} related knowledge gaps`}</p>}<div id="gap-results-list" role="listbox" aria-label="Matching knowledge gaps">{visibleResults.map((value, index) => <button className="gap-result" role="option" aria-selected="false" key={value.source.source_id} onClick={() => void selectGap(value)} onKeyDown={e => { if (e.key === "Escape") { setShowResults(false); document.getElementById("gap-search")?.focus(); } if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (e.key === "ArrowUp" && index === 0) document.getElementById("gap-search")?.focus(); else { const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button"); buttons?.[(index + (e.key === "ArrowDown" ? 1 : -1)) % visibleResults.length]?.focus(); } } }}><span>{value.object.text}</span><small>{value.source.disease_label} · {value.source.status || "Status not specified"}</small></button>)}</div>{!searching && !error && !visibleResults.length && <p className="empty">No matching knowledge gaps. Try a disease, gene or mechanism.</p>}</section>}
    {!gap && discoveryVisible && !query.length && <GapBrowser scope={gapScope} onSelect={value => void selectGap(value)} onRanked={setTrending} />}
    {error && <div className="error" role="alert">{error}</div>}
    {conflict && <div className="conflict"><p>This draft changed in another session. Your edits are retained here.</p><button onClick={async () => { if (!draftRef.current) return; const latest = await api.draft(draftRef.current.id); draftRef.current = latest; setDraft(latest); setComposer(latest.composer); setConflict(false); setError(""); }}>Load saved version</button><button onClick={() => { draftRef.current = null; setDraft(null); setConflict(false); setError(""); void save().catch(e => setError(messageOf(e))); }}>Save my edits as a new draft</button></div>}
    {job && requestArchive && <ReferenceArchiveBanner compact archive={requestArchive} subject="analysis" gapId={requestArchive.gap?.id || composer.source_gap?.id} anchorsOpen={false} settings={async () => ({ selected_kgs: composer.selected_kgs, mechanism_subquery: composer.mechanism_subquery })} />}
    {job && <><Activity key={job.id} initial={job} onJob={setJob} archived={!!requestArchive} />{accountIds.map(id => <AccountPreview key={id} id={id} />)}{terminal(job.status) && <button className="text-button return-to-question" onClick={reset}>Return to question</button>}</>}
    <dialog ref={inspectionDialog} className="inspection-dialog" aria-labelledby="inspection-title" onClose={() => setInspection(null)}><div className="inspection-heading"><h2 id="inspection-title">{inspection?.title}</h2><button aria-label="Close record" onClick={() => inspectionDialog.current?.close()}>×</button></div><div className="inspection-body">{inspection?.title === "About this knowledge gap" && <p>{gap?.object.text}</p>}{inspection?.description && <><h3>{inspection.title === "About this knowledge gap" ? "What remains unknown" : "Context"}</h3><p>{inspection.description}</p></>}<details open={!inspection?.description}><summary>Source evidence and record</summary><Record value={inspection?.value} /></details></div></dialog>
    <dialog ref={dialog} className="auth-dialog" aria-labelledby="auth-title"><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id="auth-title">Continue your exploration</h2><p>Sign in to keep your work, or continue anonymously.</p><ProviderButtons onLogin={oauth} /><div className="or">or</div><button className="provider" onClick={anonymous}>Continue anonymously</button><small>Your selected question and anchors stay with you. Anonymous access depends on this browser session.</small>{error && <p role="alert" className="error">{error}</p>}</dialog>
  </main>;
}
