"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { useIdentity } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import { outcomeAuthor, outcomeHref } from "./AnalysisOutcome";
import type { GapScope } from "./GapBrowser";
import "./gap-accounts.css";

type Listing = { binding: string; items: Schema<"AnalysisOutcomeSummary">[]; page: Schema<"Page"> };
export function GapOutcomes({ gap, scope }: { gap: Schema<"GapRecord">; scope: GapScope }) {
  const { me, ready } = useIdentity();
  const binding = `${scope}:${gap.object.id}:${gap.source.source_revision}:${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const sequence = useRef(0), request = useRef<AbortController | null>(null);
  const [listing, setListing] = useState<Listing | null>(null), [busy, setBusy] = useState(true), [error, setError] = useState("");
  const visible = listing?.binding === binding ? listing : null;
  async function load(append = false) {
    if (!ready) return;
    const serial = ++sequence.current;
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError("");
    try {
      const result = await api.gapOutcomes(gap.object.id, gap.source.source_revision, scope, append ? visible?.page.next_cursor || undefined : undefined, controller.signal);
      if (controller.signal.aborted || serial !== sequence.current || current.current !== binding) return;
      if (result.items.some(item => item.knowledge_gap.id !== gap.object.id)) throw new Error("The explorations could not be matched to this knowledge gap.");
      const items = new Map((append && visible ? visible.items : []).map(item => [item.id, item]));
      for (const item of result.items) items.set(item.id, item);
      setListing({ binding, items: [...items.values()], page: result.page });
    } catch (failure) {
      if (!controller.signal.aborted && serial === sequence.current && current.current === binding) {
        if (failure instanceof ApiError && failure.code === "CURSOR_EXPIRED") setListing(null);
        setError(messageOf(failure));
      }
    } finally { if (serial === sequence.current && current.current === binding) setBusy(false); }
  }
  useEffect(() => { setListing(null); setError(""); setBusy(true); void load(); return () => { sequence.current++; request.current?.abort(); }; }, [binding, ready]);
  return <section className="gap-accounts gap-outcomes" aria-labelledby="gap-outcomes-heading">
    <div className="gap-accounts-heading"><div><h2 id="gap-outcomes-heading">Explored, with evidence gaps</h2><p>{scope === "public" ? "Published analyses that found insufficient support for a scientific account. See what was tried before starting another run." : "Saved analyses in your workspace that found insufficient support for a scientific account."}</p></div>{visible && <span className="gap-accounts-count">{visible.items.length}{visible.page.has_more ? "+" : ""}</span>}</div>
    {!visible && busy && <LoadingSurface compact title="Finding previous explorations" description="Checking the evidence gaps recorded for this question." rows={1} />}
    {visible?.items.map(item => <article className="gap-account-card" key={item.id}>
      <div className="gap-account-meta"><span>Explored by <strong>{outcomeAuthor(item)}</strong></span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</time></div>
      <h3><Link href={outcomeHref(item.id)}>No supported scientific account</Link></h3>
      <p className="gap-account-synthesis">{item.summary}</p>
      <div className="gap-account-footer"><span>{item.anchors.length} mechanism{item.anchors.length === 1 ? "" : "s"} explored</span><Link href={outcomeHref(item.id)}>View findings and evidence gaps <span aria-hidden="true">↗</span></Link></div>
    </article>)}
    {visible && !visible.items.length && <p className="gap-accounts-empty">{scope === "public" ? "No explorations have been published for this question yet." : "No insufficient-evidence explorations have been saved for this question yet."}</p>}
    {error && <LoadingSurface compact title="Previous explorations are unavailable" error={error} onRetry={() => void load(!!visible)} skeleton="none" />}
    {visible && busy && <LoadingSurface compact title="Loading more explorations" skeleton="none" />}
    {visible?.page.has_more && !busy && !error && <button className="gap-accounts-more" onClick={() => void load(true)}>Show more explorations</button>}
  </section>;
}
