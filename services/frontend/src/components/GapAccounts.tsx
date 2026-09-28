"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import type { GapScope } from "./GapBrowser";
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

export function GapAccounts({ gap, scope }: { gap: Schema<"GapRecord">; scope: GapScope }) {
  const { me, ready } = useIdentity();
  const binding = `${scope}\0${gap.object.id}\0${gap.source.source_revision}\0${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const sequence = useRef(0);
  const request = useRef<AbortController | null>(null);
  const [listing, setListing] = useState<Listing | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const visible = listing?.binding === binding ? listing : null;

  async function load(append = false) {
    if (!ready) return;
    const serial = ++sequence.current;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const result = await api.gapAccounts(gap.object.id, gap.source.source_revision, append ? visible?.page.next_cursor || undefined : undefined, controller.signal, scope);
      if (controller.signal.aborted || serial !== sequence.current || current.current !== binding) return;
      if (result.items.some(item => item.knowledge_gap.id !== gap.object.id || item.account.question !== gap.object.id)) {
        throw new Error("The returned accounts could not be matched to this knowledge gap. Please retry.");
      }
      const items = new Map((append && visible ? visible.items : []).map(item => [item.account.id, item]));
      for (const item of result.items) items.set(item.account.id, item);
      setListing({ binding, items: [...items.values()], page: result.page });
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current && current.current === binding) {
        if (failure instanceof ApiError && failure.code === "CURSOR_EXPIRED") setListing(null);
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
  }, [binding, ready]);

  return <section className="gap-accounts" aria-labelledby="gap-accounts-heading">
    <div className="gap-accounts-heading"><div><h2 id="gap-accounts-heading">Scientific accounts</h2><p>{scope === "workspace" ? "Proposed answers in your workspace for this question." : "Published scientific accounts from all researchers for this question."}</p></div>
      {visible && <span className="gap-accounts-count" aria-label={`${visible.items.length}${visible.page.has_more ? " or more" : ""} scientific accounts`}>{visible.items.length}{visible.page.has_more ? "+" : ""}</span>}
    </div>
    {!visible && busy && <LoadingSurface compact title="Loading scientific accounts" description="Finding proposed answers for this knowledge gap." rows={2} />}
    {visible?.items.map(item => {
      const href = `/accounts/${encodeURIComponent(item.account.id)}?view=conclusions`;
      const date = dateLabel(item.created_at);
      return <article className="gap-account-card" key={item.account.id}>
        <div className="gap-account-meta"><span>{item.attribution && "Proposed by "}<strong title={item.attribution?.user_id}>{author(item)}</strong></span>{date && <time dateTime={item.created_at}>{date}</time>}</div>
        <h3><Link href={href}>{item.account.name || "Scientific account"}</Link></h3>
        {item.account.closing_remarks && <p className="gap-account-synthesis">{item.account.closing_remarks}</p>}
        <div className="gap-account-footer"><span>{item.claim_count} claim{item.claim_count === 1 ? "" : "s"}</span><Link href={href}>View scientific account <span aria-hidden="true">↗</span></Link></div>
      </article>;
    })}
    {visible && !visible.items.length && <p className="gap-accounts-empty">{scope === "workspace" ? "No scientific accounts have been proposed in your workspace for this knowledge gap yet." : "No scientific accounts have been published for this knowledge gap yet."}</p>}
    {error && <LoadingSurface compact title="Scientific accounts are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
    {visible && busy && <LoadingSurface compact title="Loading more scientific accounts" skeleton="none" />}
    {visible?.page.has_more && !busy && !error && <button className="gap-accounts-more" onClick={() => void load(true)}>Show more accounts <span aria-hidden="true">↓</span></button>}
  </section>;
}
