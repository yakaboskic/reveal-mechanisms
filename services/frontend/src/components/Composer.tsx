"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, messageOf, supersededReference, terminal, type Schema } from "@/lib/client";
import { applySuggestions, composerEqual, createSubmissionDraft, dropOutdatedAnchors, emptyComposer, normalizedComposer, factorSelection, persistDraft, removeAnchor, selectedGap } from "@/lib/composer";
import { ProviderButtons, useIdentity } from "./Session";
import { Activity } from "./Activity";
import { AccountPreview, Record } from "./Scientific";
import { GapBrowser, DiscoverySelector, TrendingAccounts } from "./GapBrowser";
import { useGapDiscovery } from "./GapDiscoveryCache";
import type { DiscoveryView, AccountSort } from "@/lib/community-discovery";
import { GapAccounts } from "./GapAccounts";
import { VoteControls } from "./VoteControls";
import { onCollectionInvalidation } from "@/lib/collection-events";
import { rememberSubmission, restoreSubmission, submissionActionDisabled, type SubmissionAttempt, type SubmissionMethod, type SubmissionStage } from "@/lib/submission";
import { SubmissionProgress } from "./SubmissionProgress";
import { LoadingSurface, LoadingStatus } from "./LoadingSurface";
import { withRequestDeadline } from "@/lib/request-deadline";
import { providerRedirect } from "@/lib/provider-redirect";
import { MechanismLabel } from "./MechanismLabel";
import { mechanismName, mechanismTrait } from "@/lib/mechanism-display";
import { factorHref } from "@/lib/factor-links";
import { composerSelection, followsSelection, hasSelection, questionSelection, selectionKey, selectionUrl, type AdoptedSelection, type ComposerSelection } from "@/lib/composer-navigation";
import { anchorKey, isReferenceReload, observedReferenceModel, outdatedFromAnchor, outdatedFromFactor, referenceRechecker, type OutdatedAnchor, type ReferenceArchive } from "@/lib/reference";
import { ReferenceArchiveBanner } from "./ReferenceArchive";
import { onWorkspaceChange } from "@/lib/workspace-events";
import { ResearchInputs } from "./ResearchInputs";
import { DraftNavigation } from "./DraftNavigation";
import { rememberDraftSave, restoreDraftSave, type DraftSaveAttempt } from "@/lib/draft-save";
import { analysisAccountResults, localWorkApi, localWorkHref, LocalWorkError } from "@/lib/local-work";
import { ResearchModeMenu } from "./ResearchModeMenu";
import "./draft-editor.css";
import "./local-work.css";

const browserSelection = () => composerSelection(new URLSearchParams(window.location.search), window.location.pathname);
export function Composer({ initialJobId, initialDraftId }: { initialJobId?: string; initialDraftId?: string } = {}) {
  const router = useRouter();
  const requestedMode = useRef<"online" | "local">("online");
  const params = useSearchParams();
  const pathname = usePathname();
  const selection = composerSelection(params, pathname);
  const navigationKey = selectionKey(selection);
  const lastNavigation = useRef(navigationKey);
  // The composer's own switch to a draft replacing one dropped at a reference cutover: the restore
  // effect skips it once, and saves or a submission begun under the dropped draft carry on.
  const adopted = useRef<(AdoptedSelection & { restoreSkipped: boolean }) | null>(null);
  const { me, ready, refresh } = useIdentity();
  const [composer, setComposer] = useState(emptyComposer);
  const [gap, setGap] = useState<Schema<"GapRecord"> | null>(null);
  const selectedGapId = gap?.object.id;
  const selectedVoteBinding = `${selectedGapId}:${me?.user_id || "visitor"}`;
  const [selectedVotes, setSelectedVotes] = useState<{ binding: string; value: Schema<"VoteState"> } | null>(null);
  useEffect(() => {
    if (!selectedGapId || !ready) return;
    let active = true, sequence = 0;
    let controller: AbortController | undefined;
    const refreshVote = () => {
      controller?.abort(); controller = new AbortController();
      const signal = controller.signal, request = ++sequence;
      void api.gapVote(selectedGapId, signal).then(votes => {
        if (active && !signal.aborted && request === sequence) setSelectedVotes({ binding: selectedVoteBinding, value: votes });
      }).catch(() => { /* Keep the last displayed tally; voting reports its own failures. */ });
    };
    refreshVote();
    const unsubscribe = onCollectionInvalidation(["catalog", "gaps"], refreshVote);
    return () => { active = false; controller?.abort(); unsubscribe(); };
  }, [selectedGapId, selectedVoteBinding, ready]);
  const [factors, setFactors] = useState<Record<string, Schema<"EagglFactor">>>({});
  // Anchors from a superseded reference generation render from their archive stamp or frozen factor.
  const [outdated, setOutdated] = useState<Record<string, OutdatedAnchor>>({});
  const [requestArchive, setRequestArchive] = useState<ReferenceArchive | null>(null);
  const [draft, setDraft] = useState<Schema<"Draft"> | null>(null);
  // Temporary editors have an address but are retained only through explicit Save.
  const [draftView, setDraftView] = useState(!!initialDraftId);
  const [restoring, setRestoring] = useState("");
  const [retrievingJob, setRetrievingJob] = useState(!!initialJobId);
  const [jobRestoreError, setJobRestoreError] = useState("");
  const [restoreAttempt, setRestoreAttempt] = useState(0);
  const [discoveryView, setDiscoveryView] = useState<DiscoveryView>("gaps");
  const { sort: gapSort, setSort: setGapSort } = useGapDiscovery();
  const [accountSort, setAccountSort] = useState<AccountSort>("votes");
  const searchLabel = discoveryView === "accounts" ? "Search published scientific accounts" : "Search DisMech knowledge gaps";
  const [query, setQuery] = useState(""); const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Schema<"GapRecord">[]>([]); const [trending, setTrending] = useState<Schema<"GapRecord">[]>([]);
  const searchBinding = `${discoveryView}:${query.trim()}:${me?.user_id || "visitor"}`;
  const [resultsFor, setResultsFor] = useState("");
  const visibleResults = resultsFor === searchBinding ? results : [];
  const [matches, setMatches] = useState<Schema<"EagglFactor">[]>([]); const [adding, setAdding] = useState(false);
  const [findingMatches, setFindingMatches] = useState(false);
  const [suggesting, setSuggesting] = useState(false); const [limitations, setLimitations] = useState<string[]>([]);
  const [saveState, setSaveState] = useState("Not saved"); const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveRecovery, setSaveRecovery] = useState<DraftSaveAttempt | null>(null);
  const savingRef = useRef(false);
  const [saveName, setSaveName] = useState("");
  const [attachmentsBlocked, setAttachmentsBlocked] = useState(false);
  const namingDialog = useRef<HTMLDialogElement>(null);
  const saveAsCopy = useRef(false);
  const openingAttempt = useRef<{ key: string; composer: Schema<"Composer"> } | null>(null);
  const openingGap = useRef(false);
  const freshSelection = useRef<string | null>(null);
  const [error, setError] = useState(""); const [booted, setBooted] = useState(false);
  const [job, setJob] = useState<Schema<"Job"> | null>(null);
  const [runRequest, setRunRequest] = useState<Schema<"ResearchRequest"> | null>(null);
  const [submission, setSubmission] = useState<{ stage: SubmissionStage; error?: string } | null>(null);
  const pendingSubmission = useRef<SubmissionAttempt | null>(null);
  const droppedDrafts = useRef(new Set<string>());
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
  savingRef.current = saving;
  const navigateSelection = (selected: ComposerSelection, adoptedFrom?: string) => {
    lastNavigation.current = selectionKey(selected);
    adopted.current = adoptedFrom ? { from: adoptedFrom, to: lastNavigation.current, restoreSkipped: false } : null;
    window.history.replaceState(null, "", selectionUrl(new URL(window.location.href), selected));
    setDraftView(!!selected.draft);
  };
  const discardTemporary = (value = draftRef.current) => {
    if (value?.lifecycle === "temporary") void api.deleteDraft(value, getKey(`discard:${value.id}:${value.version}`)).catch(() => { /* Server expiration handles an interrupted discard. */ });
  };
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; saveEpoch.current++; restoreEpoch.current++; suggestionRequest.current?.abort();
    // Defer past Strict Mode’s effect replay; real departures discard unsaved work.
    queueMicrotask(() => { if (!mounted.current && !pendingSubmission.current && !savingRef.current) discardTemporary(); });
  }; }, []);
  useEffect(() => {
    if (lastNavigation.current === navigationKey) return;
    lastNavigation.current = navigationKey; adopted.current = null;
    // Fence late reads and explicitly discard the old temporary editor.
    if (!pendingSubmission.current) discardTemporary();
    saveEpoch.current++; restoreEpoch.current++;
    suggestionRequest.current?.abort(); suggestionRequest.current = null;
    draftRef.current = null; currentRef.current = emptyComposer();
    pendingSubmission.current = null; submissionResumed.current = false;
    submitKey.current = null; requestKeys.current.clear();
    setComposer(currentRef.current); setGap(null); setFactors({}); setOutdated({}); setRequestArchive(null); setDraft(null); setJob(null); setRunRequest(null);
    setDraftView(!!selection.draft); setSubmission(null); setRestoring("");
    setRetrievingJob(!!selection.job); setJobRestoreError("");
    setSuggesting(false); setLimitations([]); setError(""); setConflict(false); setAdding(false); setQuery("");
    setSaveState("Not saved"); setSaveRecovery(null);
  }, [navigationKey]);
  useEffect(() => {
    // Fast Refresh replays effects while preserving refs and active promises.
    // Rehydrating then would reset a failed attempt to a spinner without resuming it.
    if (hydrated.current) return;
    hydrated.current = true;
    try { sessionStorage.removeItem("reveal:composer"); } catch { /* No ordinary edit recovery. */ }
    // Explicit workspace links open the requested record. A pending submission
    // may resume only at the unqualified home/OAuth return URL.
    const pending = hasSelection(browserSelection()) ? null : restoreSubmission();
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
  useEffect(() => {
    if (!ready || !booted) return;
    if (loadedOwner.current && loadedOwner.current !== me?.user_id && !pendingSubmission.current) {
      saveEpoch.current++; restoreEpoch.current++; suggestionRequest.current?.abort();
      draftRef.current = null; currentRef.current = emptyComposer();
      setDraft(null); setJob(null); setRunRequest(null); setGap(null); setComposer(currentRef.current); setFactors({}); setOutdated({}); setRequestArchive(null);
      setError(""); setInspection(null); inspectionDialog.current?.close(); namingDialog.current?.close();
      openingAttempt.current = null; requestKeys.current.clear(); rememberDraftSave(null); setSaveRecovery(null); droppedDrafts.current.clear();
    }
    if (!pendingSubmission.current) loadedOwner.current = me?.user_id || null;
  }, [me?.user_id, ready, booted]);
  useEffect(() => {
    // A replacement draft adopted in place is already held here: restoring it would discard newer edits.
    // Only the run its own URL change triggers is skipped.
    const adoption = adopted.current;
    if (adoption && !adoption.restoreSkipped) { adoption.restoreSkipped = true; if (adoption.to === navigationKey) { freshSelection.current = null; return; } }
    if (!booted || !ready || pendingSubmission.current) return;
    const { draft: draftId, gap: gapId, job: jobId } = browserSelection();
    if (freshSelection.current === navigationKey) { freshSelection.current = null; return; }
    let active = true; const epoch = restoreEpoch.current;
    const canRestore = () => active && !pendingSubmission.current && epoch === restoreEpoch.current
      && navigationKey === selectionKey(browserSelection());
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
        if (!canRestore()) return;
        saveEpoch.current++; draftRef.current = null; setDraft(null); setRunRequest(frozen);
        const archive = frozen.archive || null;
        setRequestArchive(archive);
        if (archive) setOutdated(Object.fromEntries(frozen.composer.eaggl_anchors.flatMap(anchor => {
          const stamped = archive.reference.anchors.find(item => item.source_id === anchor.reference.source_id);
          return stamped ? [[anchorKey(anchor.reference), outdatedFromAnchor(stamped)]] : [];
        })));
        setComposer(normalizedComposer(frozen.composer));
        // The frozen question and inputs render even if the current catalog is unavailable.
        try {
          const source = frozen.composer.source_gap ? await api.gap(frozen.composer.source_gap.id) : null;
          if (canRestore()) setGap(source);
        } catch { if (canRestore()) setError("Submitted inputs are available, but the current knowledge gap could not be loaded for editing."); }
      };
      try {
        const interruptedSave = me && restoreDraftSave(navigationKey, me.user_id);
        if (interruptedSave && !savedJob) {
          const source = interruptedSave.composer.source_gap ? await api.gap(interruptedSave.composer.source_gap.id) : null;
          if (!canRestore()) return;
          draftRef.current = interruptedSave.draft; setDraft(interruptedSave.draft);
          currentRef.current = normalizedComposer(interruptedSave.composer); setComposer(currentRef.current); setGap(source);
          await save(interruptedSave.composer, interruptedSave.name, interruptedSave);
        } else if (savedJob) {
          await restoreSubmitted();
        } else if (draftId && me) {
          const value = await api.draft(draftId);
          const source = value.composer.source_gap ? await api.gap(value.composer.source_gap.id) : null;
          if (!canRestore()) return;
          const currentFactors = params.get("suggest") === "current";
          if (value.lifecycle === "temporary" && !currentFactors) {
            discardTemporary(value);
            if (source) await selectGap(source, true);
            else throw new Error("This unsaved editor has expired. Choose a knowledge gap to start again.");
          } else {
            saveEpoch.current++; draftRef.current = value; setDraft(value); setSaveState(value.lifecycle === "temporary" ? "Not saved" : "Saved");
            currentRef.current = normalizedComposer(value.composer); setComposer(currentRef.current); setGap(source); setJob(null); setRequestArchive(null);
            if (currentFactors) {
              const url = new URL(window.location.href); url.searchParams.delete("suggest"); window.history.replaceState(null, "", url);
              if (value.composer.source_gap && !value.composer.eaggl_anchors.length) void suggest(value.composer);
            }
          }
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
  }, [booted, ready, me?.user_id, restoreAttempt, navigationKey]);
  useEffect(() => {
    if (discoveryView === "gaps" && query.trim()) {
      const controller = new AbortController(); setSearching(true); setResults([]);
      const timer = setTimeout(() => { void api.searchGaps(query.trim(), controller.signal, "public").then(value => { if (!controller.signal.aborted) { setResults(value.items.map(hit => hit.gap)); setResultsFor(searchBinding); setSearching(false); } }).catch(e => { if (!controller.signal.aborted) { setError(messageOf(e)); setSearching(false); } }); }, 250);
      return () => { clearTimeout(timer); controller.abort(); };
    }
    setResults([]); setSearching(false);
  }, [query, discoveryView, me?.user_id]);
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
    if (discoveryView === "accounts") { setExample("Search published scientific accounts"); return; }
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
  }, [query, focused, gap, trending, discoveryView]);
  useEffect(() => { if (inspection) inspectionDialog.current?.showModal(); }, [inspection]);
  const save = (snapshot: Schema<"Composer"> = currentRef.current, name = draftRef.current?.name || "", resumed?: DraftSaveAttempt): Promise<Schema<"Draft"> | null> => {
    const epoch = saveEpoch.current;
    const navigation = lastNavigation.current;
    const current = () => mounted.current && epoch === saveEpoch.current && followsSelection(navigation, selectionKey(browserSelection()), adopted.current);
    const queued = saveQueue.current.catch(() => null).then(async () => {
      if (!current()) return null;
      if (!name.trim()) throw new Error("Give this draft a name before saving.");
      const previous = resumed ? resumed.draft : saveAsCopy.current ? null : draftRef.current;
      savingRef.current = true; setSaving(true); setSaveState("Saving…"); setError("");
      try {
        const binding = JSON.stringify({ action: "save", draft: previous?.id, version: previous?.version, composer: snapshot, name: name.trim() });
        if (resumed) requestKeys.current.set(binding, resumed.key);
        let completedKey: string | undefined;
        const { draft: next, replaced } = await persistDraft(api, previous, snapshot, getKey, {
          current, name: name.trim(), gone: () => { draftRef.current = null; setDraft(null); },
          attempting: (target, key) => {
            completedKey = key;
            const receipt = { navigation, owner: loadedOwner.current || me!.user_id, key, draft: target, composer: snapshot, name: name.trim() };
            setSaveRecovery(receipt); rememberDraftSave(receipt);
          },
        });
        rememberDraftSave(null, completedKey);
        if (!current()) return null;
        setSaveRecovery(null);
        draftRef.current = next; setDraft(next); setSaveState("Saved"); setConflict(false);
        saveAsCopy.current = false; namingDialog.current?.close();
        const target = questionSelection(next.id);
        freshSelection.current = selectionKey(target); navigateSelection(target, replaced ? navigation : undefined);
        return next;
      } catch (failure) {
        if (!current()) return null;
        setSaveState("Save failed");
        if (failure instanceof ApiError && failure.status === 409 && !supersededReference(failure)) setConflict(true);
        if (supersededReference(failure)) void verifyAnchors();
        throw failure;
      } finally { savingRef.current = false; if (mounted.current) setSaving(false); }
    }); saveQueue.current = queued; return queued;
  };
  const outdatedAnchors = composer.eaggl_anchors.filter(anchor => outdated[anchorKey(anchor.reference)]);
  const requestSave = () => {
    if (saving || attachmentsBlocked || outdatedAnchors.length || !draftRef.current) return;
    if (draftRef.current.lifecycle === "temporary" || !draftRef.current.name) {
      setSaveName(draftRef.current.name || ""); namingDialog.current?.showModal();
    } else void save().catch(failure => setError(messageOf(failure)));
  };
  useEffect(() => {
    if (saving) return;
    setSaveState(draft && draft.lifecycle !== "temporary" ? composerEqual(draft.composer, composer) ? "Saved" : "Unsaved changes" : "Not saved");
  }, [composer, draft, saving]);
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
  async function selectGap(value: Schema<"GapRecord">, restoringSelection = false, retryOpening = false, seed?: Schema<"Composer">) {
    if (openingGap.current) return;
    openingGap.current = true;
    if (!restoringSelection) restoreEpoch.current++;
    if (!retryOpening) discardTemporary();
    saveEpoch.current++;
    const epoch = saveEpoch.current;
    setRestoring("Opening your draft"); setRetrievingJob(false); setJobRestoreError("");
    setGap(value); setQuery(""); setJob(null); setRunRequest(null); setError(""); setConflict(false); submitKey.current = null; setRequestArchive(null); setOutdated({});
    if (!retryOpening || !openingAttempt.current) openingAttempt.current = { key: crypto.randomUUID(), composer: seed || { ...emptyComposer(), source_gap: selectedGap(value) } };
    const attempt = openingAttempt.current;
    const next = retryOpening ? currentRef.current : attempt.composer;
    currentRef.current = next; setComposer(next); setDraft(null); draftRef.current = null;
    try {
      let identity = me;
      if (!identity) {
        await withRequestDeadline(async signal => {
          const response = await fetch("/api/session/anonymous", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": getKey("editor-session") }, body: "{}", signal });
          if (!response.ok) throw new Error("Could not open your workspace. Please retry.");
        });
        identity = await refresh();
      }
      if (!identity) throw new Error("Your session could not be established. Please retry.");
      if (!mounted.current || epoch !== saveEpoch.current) return;
      loadedOwner.current = identity.user_id;
      const created = await api.createWorkingDraft(attempt.composer, attempt.key);
      if (!mounted.current || epoch !== saveEpoch.current) { discardTemporary(created); return; }
      draftRef.current = created; setDraft(created); setSaveState("Not saved");
      const target = questionSelection(created.id);
      freshSelection.current = selectionKey(target); navigateSelection(target);
      setRestoring("");
      void api.explore({ source_gap: selectedGap(value) }).catch(() => {});
      await suggest(currentRef.current);
    } catch (failure) { if (mounted.current && epoch === saveEpoch.current) setError(messageOf(failure)); }
    finally { openingGap.current = false; if (mounted.current && epoch === saveEpoch.current) setRestoring(""); }
  }
  const submissionFailed = (failure: unknown) => {
    setSubmission(current => ({ stage: current?.stage || "signing-in", error: messageOf(failure) }));
  };
  const beginSubmission = (method: SubmissionMethod) => {
    if (submissionRunning.current || attachmentsBlocked || outdatedAnchors.length || !currentRef.current.source_gap || !currentRef.current.eaggl_anchors.length) return null;
    if (pendingSubmission.current) return pendingSubmission.current;
    const attempt: SubmissionAttempt = {
      method, mode: requestedMode.current, question: gap?.object.text || "", gap, composer: structuredClone(currentRef.current),
      draft: draftRef.current, owner: me?.user_id || loadedOwner.current,
      anonymousKey: crypto.randomUUID(), requestKeys: [...requestKeys.current], submitKey: submitKey.current,
    };
    restoreEpoch.current++;
    pendingSubmission.current = attempt; submissionResumed.current = true;
    rememberSubmission(attempt);
    // The pending record, rather than an older explicit draft link, owns
    // reload/OAuth recovery until this exact submission has been acknowledged.
    navigateSelection(questionSelection());
    setError(""); dialog.current?.close();
    setSubmission({ stage: method === "session" ? "saving" : "signing-in" });
    return attempt;
  };
  async function provision(attempt: SubmissionAttempt, confirmedIdentity = me) {
    if (submissionRunning.current) return;
    submissionRunning.current = true;
    const navigation = lastNavigation.current;
    const current = () => mounted.current && pendingSubmission.current === attempt
      && followsSelection(navigation, selectionKey(browserSelection()), adopted.current);
    setSubmission({ stage: "signing-in" });
    try {
      let identity = confirmedIdentity;
      if (attempt.method === "anonymous" && !identity) {
        await withRequestDeadline(async signal => {
          const response = await fetch("/api/session/anonymous", { method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": attempt.anonymousKey }, body: "{}", signal });
          const value = await response.json(); if (!response.ok) throw new Error(value.detail || "Anonymous continuation is unavailable. Please try again.");
        });
        if (!current()) return;
        identity = await refresh();
      } else if (!identity) identity = await refresh();
      if (!current()) return;
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
      // A submission snapshot is temporary. It never overwrites a named draft.
      const source = attempt.draft;
      let saved = source;
      // Once dispatch is uncertain, reconcile its original draft/version and key exactly.
      // This also covers receipts made before temporary drafts were introduced.
      if (!attempt.submitKey && (!saved || droppedDrafts.current.has(saved.id) || saved.lifecycle !== "temporary" || !composerEqual(saved.composer, attempt.composer))) {
        saved = await createSubmissionDraft(api, attempt.composer, getKey(JSON.stringify({ action: "submit-input", owner: identity.user_id, source: source?.id, composer: attempt.composer })), source?.lifecycle !== "temporary" ? source?.id : source?.source_draft_id || undefined, source?.lifecycle !== "temporary" ? source?.version : source?.source_draft_version || undefined, current);
      }
      if (!current()) return;
      if (!saved) throw new Error("Your draft changed while it was being saved. Please try again.");
      attempt.draft = saved; draftRef.current = saved; setDraft(saved); rememberSubmission(attempt);
      const binding = `${attempt.mode === "local" ? "local:" : ""}${saved.id}:${saved.version}`;
      if (submitKey.current?.binding !== binding) submitKey.current = { binding, key: crypto.randomUUID() };
      attempt.submitKey = submitKey.current; rememberSubmission(attempt);
      setSubmission({ stage: "submitting" });
      if (attempt.mode === "local") {
        const work = await localWorkApi.create({ draft_id: saved.id, draft_version: saved.version }, submitKey.current.key);
        if (!current()) return;
        draftRef.current = null; setDraft(null); pendingSubmission.current = null; rememberSubmission(null);
        setSubmission(null); submitKey.current = null;
        router.push(localWorkHref(work.id));
        return;
      }
      const result = await api.submit({ kind: "analysis", draft_id: saved.id, draft_version: saved.version }, submitKey.current.key);
      if (!current()) return;
      setJob(result);
      if (mounted.current) { const target = { draft: null, job: result.id, gap: null }; freshSelection.current = selectionKey(target); navigateSelection(target); }
      draftRef.current = null; setDraft(null);
      pendingSubmission.current = null; rememberSubmission(null); setSubmission(null);
      if (mounted.current) requestAnimationFrame(() => document.getElementById("submitted-question")?.focus());
      // Recording the visit is bookkeeping; it must not delay or block a research job.
      void api.explore({ source_gap: saved.composer.source_gap! }).catch(() => {});
    } catch (failure) {
      const submissionFailure = failure instanceof LocalWorkError ? new ApiError(failure.status, failure.code, failure.message) : failure;
      let draftGone = false;
      if (current() && submissionFailure instanceof ApiError && submissionFailure.status === 404 && attempt.draft) {
        try { await api.draft(attempt.draft.id); }
        catch (readFailure) { draftGone = readFailure instanceof ApiError && readFailure.status === 404; }
      }
      if (current()) {
        if (supersededReference(submissionFailure) || draftGone) {
          if (draftGone && attempt.draft) droppedDrafts.current.add(attempt.draft.id);
          pendingSubmission.current = null; submitKey.current = null; rememberSubmission(null); setSubmission(null);
          setError(draftGone ? "This draft was removed while the research inputs were being prepared. Your inputs remain here; replace any outdated anchors, then try again." : messageOf(submissionFailure));
          const target = questionSelection(draftRef.current?.id, currentRef.current.source_gap?.id);
          freshSelection.current = selectionKey(target); navigateSelection(target);
          void verifyAnchors();
        } else submissionFailed(submissionFailure);
      }
    }
    finally { submissionRunning.current = false; }
  }
  const launch = (mode: "online" | "local" = "online") => { requestedMode.current = mode; const attempt = beginSubmission("session"); if (attempt) void provision(attempt); };
  const anonymous = () => { const attempt = beginSubmission("anonymous"); if (attempt) void provision(attempt); };
  const redirectToProvider = async (provider: "google" | "orcid") => {
    if (submissionRunning.current) return;
    const attempt = pendingSubmission.current;
    submissionRunning.current = true;
    setSubmission({ stage: "signing-in" });
    try {
      const destination = await providerRedirect(provider, window.location.origin + "/");
      if (mounted.current && pendingSubmission.current === attempt) window.location.assign(destination);
    }
    catch (failure) { if (mounted.current && pendingSubmission.current === attempt) submissionFailed(failure); }
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
    // After dispatch might have committed, retain the exact retry receipt.
    const uncertain = !!pendingSubmission.current?.submitKey;
    if (!uncertain) { pendingSubmission.current = null; rememberSubmission(null); }
    setSubmission(null); setError(uncertain ? "Submission is not confirmed yet. Check the same submission before editing or starting another run." : "");
    if (!uncertain) navigateSelection(questionSelection(draftRef.current?.id, composer.source_gap?.id));
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".gap-submit")?.focus());
  };
  const reset = async () => {
    if (!gap || openingGap.current) return;
    const frozenInputs = structuredClone(composer);
    setJob(null); submitKey.current = null;
    await selectGap(gap, false, false, frozenInputs);
  };
  const accountResults = job?.result?.kind === "analysis" ? analysisAccountResults(job.result) : [];
  const clearGap = () => {
    discardTemporary();
    setDraftView(false);
    restoreEpoch.current++; setRestoring(""); setRetrievingJob(false); setJobRestoreError("");
    suggestionRequest.current?.abort(); suggestionRequest.current = null; setSuggesting(false); setLimitations([]);
    saveEpoch.current++; setGap(null); setComposer(emptyComposer()); setDraft(null); draftRef.current = null; setError(""); setAdding(false); setQuery(""); setRequestArchive(null); setOutdated({});
    navigateSelection(questionSelection());
    requestAnimationFrame(() => document.getElementById("gap-search")?.focus());
  };
  const focusAccountResult = () => {
    if (!pendingResultFocus.current) return;
    const link = document.querySelector<HTMLAnchorElement>("#discovery-account-list .trend-open");
    if (link) { link.focus(); pendingResultFocus.current = false; }
  };
  const focusResult = () => {
    setShowResults(true);
    if (discoveryView === "accounts") { pendingResultFocus.current = true; requestAnimationFrame(focusAccountResult); }
    else { pendingResultFocus.current = searching; if (!searching) requestAnimationFrame(() => document.querySelector<HTMLButtonElement>("#gap-results-list button")?.focus()); }
  };
  const linkedMechanisms = gap?.attachments.filter(item => item.resolution === "resolved" && item.target?.dapper_id.startsWith("dapper:Mechanism.")) || [];
  const selectedFactor = (id: string) => {
    const anchor = composer.eaggl_anchors.find(item => item.reference.source_id === id); const factor = factors[id];
    return anchor && factor?.source_revision === anchor.reference.source_revision && factor.object.id === anchor.reference.dapper_id ? factor : undefined;
  };
  const outdatedAnchor = (anchor: Schema<"Selection">) => outdated[anchorKey(anchor.reference)];
  const anchorName = (anchor: Schema<"Selection">) => outdatedAnchor(anchor)?.name || mechanismName(selectedFactor(anchor.reference.source_id));
  const anchorHref = (anchor: Schema<"Selection">) => factorHref(anchor.reference.source_id, anchor.reference.source_revision, {
    archiveId: outdatedAnchor(anchor)?.archive_id,
    from: job ? `/runs/${encodeURIComponent(job.id)}` : draft ? `/drafts/${encodeURIComponent(draft.id)}` : "/",
  });
  const frozenGap = runRequest?.document?.knowledge_gaps?.find(value => value.id === runRequest.question_id);
  const displayGap = job && frozenGap ? frozenGap : gap?.object;
  const uncertainSubmission = !!pendingSubmission.current?.submitKey;
  const editorLocked = saving || uncertainSubmission;
  const submitDisabled = submissionActionDisabled({ ready, busy: saving || submissionRunning.current, uncertain: uncertainSubmission,
    newInputsValid: !!composer.eaggl_anchors.length && !suggesting && !attachmentsBlocked && !!draft && !outdatedAnchors.length });
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
  if (selection.gap && !gap && !job) return <main id="main" className="composer-page prototype-composer">
    <LoadingSurface title={error ? "Unable to open this knowledge gap" : "Opening knowledge gap"} description="Retrieving the selected question and mechanism anchors." error={error || undefined}
      onRetry={error ? () => { setError(""); setRestoreAttempt(value => value + 1); } : undefined} />
    <Link className="text-button" href="/">Return to knowledge gaps</Link>
  </main>;
  return <main id="main" className={`composer-page prototype-composer ${!gap && discoveryVisible ? "is-gap-browsing" : ""} ${gap ? "has-gap" : ""} ${job ? "has-job" : ""} ${job && terminal(job.status) ? "job-complete" : ""}`}>
    {draft && !job && <nav className="composer-draft-nav" aria-label="Draft navigation"><DraftNavigation /><strong>{draft.name || "New research draft"}</strong><span role="status">{saveState}</span></nav>}
    {job && <nav className="run-navigation" aria-label="Research run navigation"><Link href="/workspace?tab=runs">← Research runs</Link><span>Submitted inputs · read-only</span></nav>}
    {(retrievingJob || jobRestoreError) && jobStatusSurface}
    {restoring && !retrievingJob && <LoadingSurface compact={!!gap || !!job} skeleton={gap || job ? "none" : "rows"} title={restoring} description="Retrieving your saved question and mechanism anchors." />}
    {!gap && discoveryVisible && <p className="invitation">Let’s use <a href="https://cfdeknowledge.org/r/kc_landing" target="_blank" rel="noopener noreferrer">Common Fund Data</a> to close known <a href="https://dismech.monarchinitiative.org/app/discussions/index.html" target="_blank" rel="noopener noreferrer">biomedical knowledge gaps</a>.</p>}
    {(!job || displayGap) && <section className={`question-shell ${job ? "submitted" : ""}`} aria-label="Knowledge gap and mechanism anchors">
      {!displayGap ? <div className="gap-input-wrap"><label className="sr-only" htmlFor="gap-search">{searchLabel}</label><input id="gap-search" type="search" maxLength={200} role={discoveryView === "gaps" ? "combobox" : undefined} aria-autocomplete={discoveryView === "gaps" ? "list" : undefined} aria-expanded={discoveryView === "gaps" ? !!query.trim() && showResults && !!visibleResults.length : undefined} value={query} autoComplete="off" onChange={e => { setQuery(e.target.value); setResults([]); setSearching(discoveryView === "gaps" && !!e.target.value.trim()); pendingResultFocus.current = false; setError(""); setShowResults(true); }} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)} onKeyDown={e => { if ((e.key === "ArrowDown" || e.key === "Enter") && query.trim()) { e.preventDefault(); focusResult(); } if (e.key === "Escape") { setShowResults(false); pendingResultFocus.current = false; } }} placeholder={focused ? searchLabel : ""} aria-controls={discoveryView === "accounts" ? "discovery-account-list" : "gap-results-list"} />{!query && !focused && <span className="idle-question" aria-hidden="true">{example}</span>}<button className="send search-arrow" disabled={!query.trim()} aria-label={discoveryView === "accounts" ? "Show matching scientific accounts" : "Show matching knowledge gaps"} title={discoveryView === "accounts" ? "Show matching scientific accounts" : "Show matching knowledge gaps"} onClick={focusResult}><span aria-hidden="true">↑</span></button></div> : <>
        <div className={`selected-question-row${!job ? " has-vote" : ""}`}>{!job && gap && <VoteControls key={`${gap.object.id}:${me?.user_id || "visitor"}`} kind="gap" id={gap.object.id} initial={selectedVotes?.binding === selectedVoteBinding ? selectedVotes.value : { ...gap.votes, user_vote: null }} compact vertical onChange={votes => setSelectedVotes({ binding: selectedVoteBinding, value: votes })} />}<h1 id="submitted-question" tabIndex={job ? -1 : undefined} className="selected-question">{displayGap.text}</h1>{!job && <button className="clear-question" aria-label="Search for a different knowledge gap" disabled={editorLocked} onClick={clearGap}>×</button>}</div>
        <div className="inline-context">
          {!job && gap && <div className="question-meta"><Link className="subtle" href={`/knowledge-gaps/${encodeURIComponent(gap.object.id)}`}>About this knowledge gap ↗</Link></div>}
          <fieldset className="draft-input-lock" disabled={editorLocked}>
          <section className="draft-mechanisms" aria-label={job ? "Submitted mechanisms and evidence sources" : "Mechanisms and evidence sources"}>
          {!job && <><div className="chip-group-label">Mechanisms <span>{composer.eaggl_anchors.length}</span>{suggesting && <LoadingStatus>Finding anchors…</LoadingStatus>}{!!limitations.length && <button className="matching-note" onClick={() => setInspection({ title: "About mechanism matching", description: limitations.join(" "), value: { model: composer.model, automatic_anchors: composer.eaggl_anchors.filter(anchor => anchor.origin === "automatic").length } })}>About matching</button>}</div></>}
          <div className="anchor-chips" aria-label="Selected mechanism anchors">{composer.eaggl_anchors.map(anchor => <span className="chip" key={anchor.reference.source_id}><Link className="label" href={anchorHref(anchor)} target="_blank" rel="noopener noreferrer" title={`${anchorName(anchor)} — view factor loadings in a new tab`}><MechanismLabel factor={selectedFactor(anchor.reference.source_id)} sourceId={anchor.reference.source_id} outdated={outdatedAnchor(anchor)} /><span className="sr-only"> (opens in a new tab)</span></Link>{!job && <button className="remove" aria-label={`Remove ${anchorName(anchor)}`} onClick={() => setComposer(current => removeAnchor(current, anchor.reference.source_id))}>×</button>}</span>)}{!job && <button className="chip-add" aria-expanded={adding} aria-controls="factor-picker" aria-label="Add a mechanism anchor" onClick={() => { setAdding(!adding); if (!adding) requestAnimationFrame(() => document.getElementById("mechanism-search")?.focus()); }}>+</button>}</div>
          {!job && <>
            {!!outdatedAnchors.length && <div className="conflict reference-outdated" role="status"><p>{outdatedAnchors.length === 1 ? "One mechanism anchor comes" : `${outdatedAnchors.length} mechanism anchors come`} from an outdated EAGGL reference and can’t be analysed. Replace {outdatedAnchors.length === 1 ? "it" : "them"} with current factors to continue.</p><button disabled={suggesting} onClick={replaceOutdated}>Replace with current factors</button></div>}
            {!composer.eaggl_anchors.length && !suggesting && <p className="anchor-required" role="status">Add at least one mechanism anchor to continue.</p>}
            <div className="context-disclosures">
              <details id="factor-picker" open={adding} onToggle={e => setAdding(e.currentTarget.open)}><summary>Find more mechanisms</summary><label className="sr-only" htmlFor="mechanism-search">Search possible genetic mechanisms</label><input id="mechanism-search" type="search" className="field" value={composer.mechanism_subquery} onChange={e => setComposer(c => ({ ...c, mechanism_subquery: e.target.value }))} placeholder="Search possible genetic mechanisms" />{matches.map(factor => <button className="mechanism-match" key={factor.source_id} title={factor.cfde_anchor.subtitle || factor.source_id} disabled={composer.eaggl_anchors.length >= 10 || composer.eaggl_anchors.some(a => a.reference.source_id === factor.source_id)} onClick={() => { rememberFactors([factor]); setComposer(c => ({ ...c, model: factor.model, eaggl_anchors: [...c.eaggl_anchors, factorSelection(factor, "manual")], dismissed_source_ids: c.dismissed_source_ids.filter(id => id !== factor.source_id) })); }}><MechanismLabel factor={factor} /><span aria-hidden="true">+</span></button>)}{findingMatches && <LoadingSurface compact skeleton={matches.length ? "none" : "rows"} rows={2} title={matches.length ? "Finding related mechanisms" : "Finding mechanisms"} description="Matching your search to available genetic mechanism anchors." />}{!!composer.mechanism_subquery.trim() && !findingMatches && !matches.length && <p className="muted">No mapped anchors found.</p>}<button className="text-button" disabled={suggesting} onClick={() => { const next = { ...composer, dismissed_source_ids: [], eaggl_anchors: composer.eaggl_anchors.filter(a => a.origin === "manual") }; setComposer(next); void suggest(next); }}>Reset suggestions</button><small>Up to five automatic anchors. Removed anchors stay dismissed until reset.</small></details>
              <details><summary>Related DisMech evidence <span className="disclosure-count">{linkedMechanisms.length} linked</span></summary><p className="muted">Mechanisms linked to this curated knowledge gap. Source context is read-only.</p>{linkedMechanisms.map((item, i) => <button className="linked-mechanism" key={i} onClick={() => setInspection({ title: item.label || "DisMech source context", value: item })}><span>{item.label || item.source_reference}<small>{item.target_kind} · {item.resolution.replaceAll("_", " ")}</small></span><span aria-hidden="true">↗</span></button>)}{!linkedMechanisms.length && <p className="muted">No linked mechanisms in this source observation.</p>}</details>
              <details><summary>Additional knowledge graphs</summary><div className="checks">{(["biomarkerkg", "prokn"] as const).map(kg => <label key={kg}><input type="checkbox" checked={composer.selected_kgs.includes(kg)} onChange={e => setComposer(c => ({ ...c, selected_kgs: e.target.checked ? [...c.selected_kgs, kg] : c.selected_kgs.filter(k => k !== kg) }))} />{kg === "prokn" ? "ProKN" : "BiomarkerKG"}</label>)}</div></details>
            </div>
          </>}
          {job && <p className="muted">Additional knowledge graphs: {composer.selected_kgs.length ? composer.selected_kgs.join(", ") : "None"}</p>}
          </section>
          <ResearchInputs key={job?.id || draft?.id || "opening"} composer={composer} onChange={setComposer} draftId={draft?.id} readOnly={!!job} onBlockingChange={setAttachmentsBlocked} />
          </fieldset>
          {!job && <>
            <p className="draft-notice">{draft?.lifecycle !== "temporary" && draft ? "Changes are kept only when you save." : "Save a named draft to keep it. Unsaved work is discarded when you leave."}</p>
            <div className="submit-row"><div className="draft-save-group"><button type="button" className="draft-save-button" aria-label="Save draft" title="Save a named draft" disabled={!draft || saving || attachmentsBlocked || uncertainSubmission || conflict || !!outdatedAnchors.length} onClick={requestSave}><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M5 3h12l4 4v14H3V3h2Z"/><path d="M7 3v7h10V3M7 21v-7h10v7M14 5v3"/></svg><span className="sr-only">Save draft</span></button><span className="draft-save-state" role="status">{saveState}</span></div>{uncertainSubmission ? <button type="button" className="gap-submit" disabled={submitDisabled} onClick={() => void retrySubmission()}><span>Check submission</span><span className="send" aria-hidden="true"><span>↑</span></span></button> : <ResearchModeMenu disabled={submitDisabled} onSelect={mode => { requestedMode.current = mode; if (me) void launch(mode); else dialog.current?.showModal(); }} />}</div><p className="local-research-link"><Link href="/workspace?tab=runs">Your research runs</Link></p>
          </>}
        </div>
      </>}
    </section>}
    {!gap && discoveryVisible && <DiscoverySelector view={discoveryView} onChange={view => { setDiscoveryView(view); setResults([]); pendingResultFocus.current = false; setShowResults(true); setError(""); }} gapSort={gapSort} onGapSort={setGapSort} accountSort={accountSort} onAccountSort={setAccountSort} searching={!!query.trim()} />}
    {gap && discoveryVisible && <GapAccounts gap={gap} scope="public" />}
    {!gap && discoveryVisible && discoveryView === "gaps" && !!query.trim() && showResults && <section className="search-results" aria-label="Knowledge gap search results">{searching ? <LoadingSurface key={query.trim()} compact rows={2} title="Searching knowledge gaps" description="Looking for matching questions in DisMech." /> : <p className="result-heading" role="status">{error ? "Search unavailable" : `${visibleResults.length} related knowledge gaps`}</p>}<div id="gap-results-list" role="listbox" aria-label="Matching knowledge gaps">{visibleResults.map((value, index) => <button className="gap-result" role="option" aria-selected="false" key={value.source.source_id} onClick={() => void selectGap(value)} onKeyDown={e => { if (e.key === "Escape") { setShowResults(false); document.getElementById("gap-search")?.focus(); } if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (e.key === "ArrowUp" && index === 0) document.getElementById("gap-search")?.focus(); else { const buttons = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button"); buttons?.[(index + (e.key === "ArrowDown" ? 1 : -1)) % visibleResults.length]?.focus(); } } }}><span>{value.object.text}</span><small>{value.source.disease_label} · {value.source.status || "Status not specified"}</small></button>)}</div>{!searching && !error && !visibleResults.length && <p className="empty">No matching knowledge gaps. Try a disease, gene or mechanism.</p>}</section>}
    {!gap && discoveryVisible && discoveryView === "gaps" && !query.trim() && <GapBrowser scope="public" sort={gapSort} onSelect={value => void selectGap(value)} onRanked={setTrending} />}
    {!gap && discoveryVisible && discoveryView === "accounts" && (!query.trim() || showResults) && <TrendingAccounts query={query} sort={accountSort} onLoaded={focusAccountResult} />}
    {error && <div className="error" role="alert">{error}{saveRecovery && !saving && <button type="button" onClick={() => void save(saveRecovery.composer, saveRecovery.name, saveRecovery).catch(failure => setError(messageOf(failure)))}>Retry save</button>}{gap && !draft && !job && openingAttempt.current && <button type="button" onClick={() => void selectGap(gap, false, true)}>Retry opening editor</button>}</div>}
    {conflict && <div className="conflict"><p>This draft changed in another session. Your edits are retained here.</p><button onClick={async () => { if (!draftRef.current) return; const latest = await api.draft(draftRef.current.id); rememberDraftSave(null); draftRef.current = latest; setDraft(latest); setComposer(latest.composer); setConflict(false); setError(""); }}>Load saved version</button><button onClick={() => { saveAsCopy.current = true; setError(""); setSaveName(""); namingDialog.current?.showModal(); }}>Save my edits as a new draft</button></div>}
    {job && requestArchive && <ReferenceArchiveBanner compact archive={requestArchive} subject="analysis" gapId={requestArchive.gap?.id || composer.source_gap?.id} anchorsOpen={false} settings={async () => composer} />}
    {job && <><Activity key={job.id} initial={job} onJob={setJob} archived={!!requestArchive} />{accountResults.map(({ id, reused }) => reused ? <section key={id} aria-label="Reused scientific account"><p className="local-research-link">Reused accepted account · original authorship retained</p><AccountPreview id={id} /></section> : <AccountPreview key={id} id={id} />)}{terminal(job.status) && gap && !requestArchive && <button className="text-button return-to-question" onClick={() => void reset()}>Edit these inputs</button>}</>}
    <dialog ref={inspectionDialog} className="inspection-dialog" aria-labelledby="inspection-title" onClose={() => setInspection(null)}><div className="inspection-heading"><h2 id="inspection-title">{inspection?.title}</h2><button aria-label="Close record" onClick={() => inspectionDialog.current?.close()}>×</button></div><div className="inspection-body">{inspection?.title === "About this knowledge gap" && <p>{gap?.object.text}</p>}{inspection?.description && <><h3>{inspection.title === "About this knowledge gap" ? "What remains unknown" : "Context"}</h3><p>{inspection.description}</p></>}<details open={!inspection?.description}><summary>Source evidence and record</summary><Record value={inspection?.value} /></details></div></dialog>
    <dialog ref={namingDialog} className="auth-dialog draft-name-dialog" aria-labelledby="draft-name-title" onClose={() => { saveAsCopy.current = false; }} onCancel={event => { if (saving) event.preventDefault(); }}><form onSubmit={event => { event.preventDefault(); void save(currentRef.current, saveName).catch(failure => setError(messageOf(failure))); }}><h2 id="draft-name-title">Save your draft</h2><label htmlFor="draft-name">Draft name</label><input autoFocus className="field" id="draft-name" required maxLength={120} value={saveName} disabled={saving} onChange={event => setSaveName(event.target.value)} placeholder="e.g. Endothelial insulin signaling" />{error && <p className="error" role="alert">{error}</p>}<div className="draft-name-actions"><button type="button" disabled={saving} onClick={() => namingDialog.current?.close()}>Cancel</button><button type="submit" className="primary" disabled={saving || !saveName.trim()}>{saving ? "Saving…" : "Save draft"}</button></div></form></dialog>
    <dialog ref={dialog} className="auth-dialog" aria-labelledby="auth-title"><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id="auth-title">Continue your exploration</h2><p>Sign in to keep your work, or continue anonymously.</p><ProviderButtons onLogin={oauth} /><div className="or">or</div><button className="provider" onClick={anonymous}>Continue anonymously</button><small>Your selected question and anchors stay with you. Anonymous access depends on this browser session.</small>{error && <p role="alert" className="error">{error}</p>}</dialog>
  </main>;
}
