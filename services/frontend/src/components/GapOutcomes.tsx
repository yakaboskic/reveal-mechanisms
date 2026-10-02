"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import { outcomeAuthor, outcomeHref } from "./AnalysisOutcome";
import type { GapScope } from "./GapBrowser";
import { onCollectionInvalidation } from "@/lib/collection-events";
import { ReferenceBadge } from "./ReferenceArchive";
import { isArchived, type ReferenceState } from "@/lib/reference";
import "./gap-accounts.css";

type Listing = { binding: string; items: Schema<"AnalysisOutcomeSummary">[]; page: Schema<"Page"> };
function OutcomeList({ gap, scope, reference, filter, onCount, onArchived }: {
  gap: Schema<"GapRecord">; scope: GapScope; reference: ReferenceState; filter?: ReactNode;
  onCount: (count: number, more: boolean) => void; onArchived?: () => void;
}) {
  const { me, ready } = useIdentity();
  const binding = `${scope}:${gap.object.id}:${gap.source.source_revision}:${me?.user_id || "visitor"}:${reference}`;
  const current = useRef(binding); current.current = binding;
  const sequence = useRef(0), request = useRef<AbortController | null>(null);
  const [listing, setListing] = useState<Listing | null>(null), [busy, setBusy] = useState(true), [error, setError] = useState("");
  const visible = listing?.binding === binding ? listing : null;
  async function load(append = false) {
    if (scope === "workspace" && !ready) return;
    const serial = ++sequence.current;
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const result = await api.gapOutcomes(gap.object.id, gap.source.source_revision, scope, append ? visible?.page.next_cursor || undefined : undefined, controller.signal, reference);
      if (controller.signal.aborted || serial !== sequence.current || current.current !== binding) return;
      if (result.items.some(item => item.knowledge_gap.id !== gap.object.id)) throw new Error("The explorations could not be matched to this knowledge gap.");
      if (scope === "public" && result.items.some(item => item.publication.visibility !== "public")) throw new Error("The published explorations could not be verified. Please retry.");
      const items = new Map((append && visible ? visible.items : []).map(item => [item.id, item]));
      for (const item of result.items) items.set(item.id, item);
      setListing({ binding, items: [...items.values()], page: result.page });
      onCount(items.size, result.page.has_more);
      if ([...items.values()].some(isArchived)) onArchived?.();
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current && current.current === binding) {
        if (failure instanceof ApiError && (failure.code === "CURSOR_EXPIRED" || [401, 403].includes(failure.status))) setListing(null);
        setError(messageOf(failure));
      }
    } finally { if (serial === sequence.current && current.current === binding) setBusy(false); }
  }
  useEffect(() => { setListing(null); setError(""); setBusy(true); void load(); return () => { sequence.current++; request.current?.abort(); }; }, [binding, scope === "public" || ready]);
  useEffect(() => onCollectionInvalidation(["catalog", "explorations", "gaps"], reset => {
    sequence.current++; request.current?.abort();
    if (reset) { setListing(null); setError(""); setBusy(false); if (scope !== "public") return; }
    void load();
  }), [binding, scope === "public" || ready]);
  return <div className="gap-collection-content">
    <p className="gap-collection-description">{scope === "public" ? "Published analyses with insufficient support for a scientific account. See what was tried and which evidence is missing." : "Saved analyses in your workspace with insufficient support for a scientific account."}</p>
    {filter}
    {!visible && busy && <LoadingSurface compact title="Finding previous explorations" description="Checking the evidence gaps recorded for this question." rows={1} />}
    {visible?.items.map(item => <article className="gap-account-card" key={item.id}>
      <div className="gap-account-meta"><span>Explored by <strong>{outcomeAuthor(item)}</strong></span><span className="gap-account-dates"><ReferenceBadge archive={item.archive} /><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</time></span></div>
      <h3><Link href={outcomeHref(item.id)}>No supported scientific account</Link></h3>
      <p className="gap-account-synthesis">{item.summary}</p>
      <div className="gap-account-footer"><span>{item.anchors.length} mechanism{item.anchors.length === 1 ? "" : "s"} explored</span><Link href={outcomeHref(item.id)}>View findings and evidence gaps <span aria-hidden="true">↗</span></Link></div>
    </article>)}
    {visible && !visible.items.length && <p className="gap-accounts-empty">{reference === "archived" ? "No explorations of this question were built on an outdated EAGGL reference." : reference === "current" ? "No explorations of this question use the current EAGGL reference yet." : scope === "public" ? "No explorations have been published for this question yet." : "No insufficient-evidence explorations have been saved for this question yet."}</p>}
    {error && <LoadingSurface compact title="Previous explorations are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
    {visible && busy && <LoadingSurface compact title="Loading more explorations" skeleton="none" />}
    {visible?.page.has_more && !busy && !error && <button className="gap-accounts-more" onClick={() => void load(true)}>Show more explorations</button>}
  </div>;
}

/** The reference filter and its state belong to GapAccounts, which lists both collections for a gap. */
export function GapOutcomes({ gap, scope, reference = "all", filter, onArchived }: {
  gap: Schema<"GapRecord">; scope: GapScope; reference?: ReferenceState; filter?: ReactNode; onArchived?: () => void;
}) {
  const { me } = useIdentity();
  const heading = useId();
  const binding = `${scope}\0${gap.object.id}\0${gap.source.source_revision}\0${me?.user_id || "visitor"}`;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [observed, setObserved] = useState<{ binding: string; count: number; more: boolean } | null>(null);
  const open = expanded === binding;
  const listed = `${binding}\0${reference}`;
  return <details className="gap-accounts gap-outcomes gap-collection-disclosure" open={open} aria-labelledby={heading}
    onToggle={event => setExpanded(event.currentTarget.open ? binding : null)}>
    <summary><span className="gap-collection-caret" aria-hidden="true">›</span><h2 id={heading}>Explorations with evidence gaps</h2>{observed?.binding === listed && <span className="gap-accounts-count">{observed.count}{observed.more ? "+" : ""}</span>}</summary>
    {open && <OutcomeList key={binding} gap={gap} scope={scope} reference={reference} filter={filter} onArchived={onArchived}
      onCount={(count, more) => setObserved({ binding: listed, count, more })} />}
  </details>;
}
