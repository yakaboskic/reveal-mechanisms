"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import type { GapScope } from "./GapBrowser";
import { GapOutcomes } from "./GapOutcomes";
import { VoteControls } from "./VoteControls";
import { mergeGapAccounts } from "./gap-reading";
import { onCollectionInvalidation } from "@/lib/collection-events";
import { ReferenceBadge, ReferenceFilter, useReferenceReloaded } from "./ReferenceArchive";
import { isArchived, listedAccountCount, type ReferenceState } from "@/lib/reference";
import "./gap-accounts.css";

type Listing = { binding: string; items: Schema<"AccountSummary">[]; page: Schema<"Page"> };

function author(item: Schema<"AccountSummary">) {
  const attribution = item.attribution;
  if (!attribution) return "Attribution unavailable";
  return attribution.display_name || (attribution.principal_kind === "anonymous" ? "Anonymous researcher" : "Researcher");
}

function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : new Intl.DateTimeFormat("en", { dateStyle: "medium" }).format(date);
}

function AccountList({ gap, scope, reference, filter, onCount, onArchived }: {
  gap: Schema<"GapRecord">; scope: GapScope; reference: ReferenceState; filter: ReactNode;
  onCount: (count: number, more: boolean) => void; onArchived: () => void;
}) {
  const { me, ready } = useIdentity();
  const binding = `${scope}\0${gap.object.id}\0${gap.source.source_revision}\0${me?.user_id || "visitor"}\0${reference}`;
  const current = useRef(binding); current.current = binding;
  const sequence = useRef(0);
  const request = useRef<AbortController | null>(null);
  const [listing, setListing] = useState<Listing | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const visible = listing?.binding === binding ? listing : null;

  async function load(append = false) {
    if (scope === "workspace" && !ready) return;
    const serial = ++sequence.current;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const result = await api.gapAccounts(gap.object.id, gap.source.source_revision, append ? visible?.page.next_cursor || undefined : undefined, controller.signal, scope, reference);
      if (controller.signal.aborted || serial !== sequence.current || current.current !== binding) return;
      const items = mergeGapAccounts(gap.object.id, scope, append && visible ? visible.items : [], result.items);
      setListing({ binding, items, page: result.page });
      onCount(items.length, result.page.has_more);
      if (items.some(isArchived)) onArchived();
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current && current.current === binding) {
        if (failure instanceof ApiError && (failure.code === "CURSOR_EXPIRED" || [401, 403].includes(failure.status))) setListing(null);
        setError(messageOf(failure));
      }
    } finally {
      if (serial === sequence.current && current.current === binding) setBusy(false);
    }
  }

  useEffect(() => {
    setError(""); setListing(null); setBusy(true);
    void load();
    return () => { sequence.current++; request.current?.abort(); };
  }, [binding, scope === "public" || ready]);
  useEffect(() => onCollectionInvalidation(["catalog", "accounts", "gaps"], reset => {
    sequence.current++; request.current?.abort();
    if (reset) {
      setListing(null); setError(""); setBusy(false);
      if (scope !== "public") return;
    }
    void load();
  }), [binding, scope === "public" || ready]);

  return <div className="gap-collection-content">
    <p className="gap-collection-description">{scope === "workspace" ? "Proposed answers saved in your workspace." : "Published scientific accounts from all researchers."}</p>
    {filter}
    {!visible && busy && <LoadingSurface compact title="Loading scientific accounts" description="Finding proposed answers for this knowledge gap." rows={2} />}
    {visible?.items.map(item => {
      const href = `/accounts/${encodeURIComponent(item.account.id)}?view=conclusions`;
      const date = dateLabel(item.created_at);
      return <article className={`gap-account-card${item.votes ? " has-votes" : ""}`} key={item.account.id}>
        {item.votes && <div className="gap-account-votes"><VoteControls key={`${binding}\0${item.account.id}`} compact vertical kind="account" id={item.account.id} initial={item.votes}
          onChange={votes => setListing(previous => previous?.binding === binding ? { ...previous, items: previous.items.map(row => row.account.id === item.account.id ? { ...row, votes } : row) } : previous)} /></div>}
        <div className="gap-account-body">
        <div className="gap-account-meta"><span>{item.attribution && "Proposed by "}<strong title={item.attribution?.user_id}>{author(item)}</strong></span><span className="gap-account-dates"><ReferenceBadge archive={item.archive} />{date && <time dateTime={item.created_at}>{date}</time>}</span></div>
        <p className="gap-account-summary"><Link href={href}>{item.account.closing_remarks?.trim() || item.account.context?.trim() || item.account.name || "View saved account"}</Link></p>
        <div className="gap-account-footer"><span>{item.claim_count} claim{item.claim_count === 1 ? "" : "s"}</span></div>
        </div>
      </article>;
    })}
    {visible && !visible.items.length && <p className="gap-accounts-empty">{reference === "archived" ? "No scientific accounts for this knowledge gap were built on an outdated EAGGL reference." : reference === "current" ? "No scientific accounts for this knowledge gap use the current EAGGL reference yet." : scope === "workspace" ? "No scientific accounts have been proposed in your workspace for this knowledge gap yet." : "No scientific accounts have been published for this knowledge gap yet."}</p>}
    {error && <LoadingSurface compact title="Scientific accounts are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
    {visible && busy && <LoadingSurface compact title="Loading more scientific accounts" skeleton="none" />}
    {visible?.page.has_more && !busy && !error && <button className="gap-accounts-more" onClick={() => void load(true)}>Show more accounts <span aria-hidden="true">↓</span></button>}
  </div>;
}

export function GapAccounts({ gap, scope, includeOutcomes = true }: { gap: Schema<"GapRecord">; scope: GapScope; includeOutcomes?: boolean }) {
  const { me } = useIdentity();
  const heading = useId();
  const binding = `${scope}\0${gap.object.id}\0${gap.source.source_revision}\0${me?.user_id || "visitor"}`;
  // One reference filter serves both listings; current work is listed first either way.
  // Changing it reloads the open lists in place, so neither section collapses.
  const [reference, setReference] = useState<ReferenceState>("all");
  const [archivedSeen, setArchivedSeen] = useState<string | null>(null);
  const reloaded = useReferenceReloaded();
  const listed = `${binding}\0${reference}`;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [observed, setObserved] = useState<{ binding: string; count: number; more: boolean } | null>(null);
  const open = expanded === binding;
  // The gap counts current work (which ranks gaps) and archived work apart; the collapsed
  // summary shows the count of the listing the selected filter would open.
  const counted = gap.scientific_accounts;
  const known = counted.scope === (scope === "public" ? "public_exact_gap" : "owner_exact_gap") ? listedAccountCount(counted, reference) : undefined;
  const count = observed?.binding === listed ? `${observed.count}${observed.more ? "+" : ""}` : known;
  const onArchived = () => setArchivedSeen(binding);
  const filter = <ReferenceFilter value={reference} visible={reloaded || !!counted.archived_count || archivedSeen === binding} onChange={setReference} />;
  return <><details className="gap-accounts gap-collection-disclosure" open={open} aria-labelledby={heading}
    onToggle={event => setExpanded(event.currentTarget.open ? binding : null)}>
    <summary><span className="gap-collection-caret" aria-hidden="true">›</span><h2 id={heading}>Scientific accounts</h2>{count !== undefined && <span className="gap-accounts-count">{count}</span>}</summary>
    {open && <AccountList key={binding} gap={gap} scope={scope} reference={reference} filter={filter} onArchived={onArchived}
      onCount={(count, more) => setObserved({ binding: listed, count, more })} />}
  </details>{includeOutcomes && <GapOutcomes gap={gap} scope={scope} reference={reference} filter={filter} onArchived={onArchived} />}</>;
}
