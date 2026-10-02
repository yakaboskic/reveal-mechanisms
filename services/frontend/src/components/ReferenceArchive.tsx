"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, messageOf, type Schema } from "@/lib/client";
import { archivedResearchInputs, currentAnalysisComposer, selectedGap, type CopiedResearchInputs } from "@/lib/composer";
import { archivedDate, isArchived, outdatedFromAnchor, referenceFactorHref, referenceReloaded, referenceStateLabels, referenceStates, type ArchivedAnchor, type ArchivedFactor, type ReferenceArchive, type ReferenceState } from "@/lib/reference";
import { useIdentity } from "./Session";
import { LoadingStatus } from "./LoadingSurface";
import "./reference-archive.css";

type CopiedSettings = CopiedResearchInputs;
export const newAnalysisLabel = "Start a new analysis on this gap with current factors";

/** "Outdated reference" marker for work built on a superseded EAGGL reference generation. */
export function ReferenceBadge({ archive }: { archive?: ReferenceArchive | null }) {
  if (!archive || !isArchived({ archive })) return null;
  const date = archivedDate(archive);
  return <span className="reference-badge" title={`Built on the ${archive.reference.model} EAGGL reference${date ? `, archived ${date}` : ""}. Readable and publishable; not re-analysable.`}>Outdated reference</span>;
}

/** True once this browser has seen a post-reload reference model (client-only, after hydration). */
export function useReferenceReloaded() {
  const [reloaded, setReloaded] = useState(false);
  useEffect(() => { setReloaded(referenceReloaded()); }, []);
  return reloaded;
}

/** Current/archived/all filter. Shown only where archived work can exist, so legacy deployments look unchanged. */
export function ReferenceFilter({ value, onChange, visible }: { value: ReferenceState; onChange: (value: ReferenceState) => void; visible: boolean }) {
  if (!visible && value === "all") return null;
  return <div className="reference-filter" role="group" aria-label="Filter by EAGGL reference">{referenceStates.map(state => <button key={state} type="button" aria-pressed={value === state} onClick={() => onChange(state)}>{referenceStateLabels[state]}</button>)}</div>;
}

function ArchivedFactorDetails({ anchor }: { anchor: ArchivedAnchor }) {
  const display = outdatedFromAnchor(anchor);
  const [snapshot, setSnapshot] = useState<ArchivedFactor | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState("");
  const requested = useRef(false);
  const load = () => {
    if (requested.current) return;
    requested.current = true; setLoading(true); setError("");
    void api.referenceFactor(anchor.archived_reference_factor_id).then(setSnapshot)
      .catch(failure => { requested.current = false; setError(messageOf(failure)); }).finally(() => setLoading(false));
  };
  const genes = snapshot?.top_genes.slice(0, 12).map(gene => gene.symbol) || [];
  const geneSets = snapshot?.top_gene_sets.slice(0, 5) || [];
  return <details className="reference-anchor" onToggle={event => { if (event.currentTarget.open) load(); }}>
    <summary><span className="mechanism-label"><span className="mechanism-name">{display.name}</span>{display.trait && <span className="mechanism-trait">{display.trait}</span>}</span></summary>
    <div className="reference-anchor-body">
      {loading && <LoadingStatus>Loading the archived factor…</LoadingStatus>}
      {error && <p className="reference-anchor-error" role="alert">{error} <button type="button" onClick={load}>Retry</button></p>}
      {snapshot && <dl>
        <div><dt>EAGGL factor</dt><dd>{snapshot.factor_id} · {snapshot.model}</dd></div>
        {!!genes.length && <div><dt>Top genes</dt><dd>{genes.join(", ")}</dd></div>}
        {!!geneSets.length && <div><dt>Top gene sets</dt><dd><ol>{geneSets.map(set => <li key={`${set.rank}:${set.name}`}>{set.name}</li>)}</ol></dd></div>}
      </dl>}
      <a href={referenceFactorHref(anchor.archived_reference_factor_id)} target="_blank" rel="noopener noreferrer">Archived factor record (JSON) <span aria-hidden="true">↗</span></a>
    </div>
  </details>;
}

/**
 * Opens a temporary draft with the gap, inquiry, documents and knowledge graphs copied and no
 * anchors, then opens it so the composer suggests current factors. Visitors
 * without a session open the gap instead, which suggests anchors in the browser.
 */
export function StartCurrentAnalysis({ gapId, settings }: { gapId: string; settings?: () => Promise<CopiedSettings> }) {
  const { me, ready } = useIdentity(); const router = useRouter();
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const pending = useRef<{ binding: string; key: string } | null>(null);
  const start = async () => {
    if (busy || !ready) return;
    if (!me) { router.push(`/?gap=${encodeURIComponent(gapId)}`); return; }
    setBusy(true); setError("");
    try {
      const [gap, copied] = await Promise.all([api.gap(gapId), settings ? settings() : Promise.resolve({})]);
      const composer = currentAnalysisComposer(selectedGap(gap), copied);
      const binding = JSON.stringify({ user: me.user_id, composer });
      if (pending.current?.binding !== binding) pending.current = { binding, key: crypto.randomUUID() };
      const draft = await api.createWorkingDraft(composer, pending.current.key);
      pending.current = null;
      router.push(`/drafts/${encodeURIComponent(draft.id)}?suggest=current`);
    } catch (failure) { setError(messageOf(failure)); }
    finally { setBusy(false); }
  };
  return <div className="reference-archive-actions">
    <button type="button" className="reference-new-analysis" disabled={busy || !ready} onClick={() => void start()}>{newAnalysisLabel} <span aria-hidden="true">→</span></button>
    {busy && <LoadingStatus>Creating a draft with current factors…</LoadingStatus>}
    {error && <p className="reference-anchor-error" role="alert">{error}</p>}
  </div>;
}

/** Banner for an archived account, exploration or submitted analysis, with its original anchors. */
export function ReferenceArchiveBanner({ archive, subject, gapId, settings, anchorsOpen = true, compact = false }: {
  archive: ReferenceArchive; subject: string; gapId?: string | null; settings?: () => Promise<CopiedSettings>; anchorsOpen?: boolean; compact?: boolean;
}) {
  const id = useId(); const date = archivedDate(archive); const anchors = archive.reference.anchors;
  return <section className={`reference-archive${compact ? " is-compact" : ""}`} aria-labelledby={id}>
    <div className="reference-archive-heading"><ReferenceBadge archive={archive} /><h2 id={id}>Built on an outdated EAGGL reference</h2></div>
    <p>This {subject} used mechanism anchors from the {archive.reference.model} EAGGL reference{date ? <>, archived on <time dateTime={archive.archived_at}>{date}</time></> : ", which has since been replaced"}. It stays readable, downloadable and publishable, but it can’t be re-analysed or have its review retried.</p>
    {!!anchors.length && <details className="reference-archive-anchors" open={anchorsOpen}><summary>Original mechanism anchors <span>{anchors.length}</span></summary><div>{anchors.map((anchor, index) => <ArchivedFactorDetails key={`${anchor.source_id}:${index}`} anchor={anchor} />)}</div></details>}
    {gapId && <StartCurrentAnalysis gapId={gapId} settings={settings} />}
  </section>;
}

/** Copies the frozen request's inputs for its owner; public readers keep defaults. */
export const requestSettings = (archive: ReferenceArchive, fallback: CopiedSettings = {}) => async (): Promise<CopiedSettings> => {
  return archivedResearchInputs(archive.analysis.request_id, api.request, fallback);
};
