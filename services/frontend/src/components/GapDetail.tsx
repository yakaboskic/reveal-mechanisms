"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { GapAccounts } from "./GapAccounts";
import { GapContext } from "./GapContext";
import { LoadingSurface } from "./LoadingSurface";
import { VoteControls } from "./VoteControls";
import { useIdentity } from "./Session";
import { gapExploreHref } from "./gap-reading";
import { onCollectionInvalidation } from "@/lib/collection-events";
import "./gap-detail.css";

export function GapDetail({ id }: { id: string }) {
  const { me } = useIdentity();
  const binding = `${id}\0${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const [record, setRecord] = useState<{ binding: string; gap: Schema<"GapRecord"> } | null>(null);
  const [failure, setFailure] = useState<{ binding: string; message: string; missing: boolean } | null>(null);
  const request = useRef<AbortController | null>(null), serial = useRef(0);

  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const sequence = ++serial.current;
    setFailure(null);
    void api.gap(id, controller.signal).then(gap => {
      if (controller.signal.aborted || sequence !== serial.current || current.current !== binding) return;
      if (gap.object.id !== id) throw new Error("The returned question did not match this knowledge gap. Please retry.");
      setRecord({ binding, gap });
    }).catch(error => {
      if (!controller.signal.aborted && sequence === serial.current && current.current === binding) {
        setFailure({ binding, message: messageOf(error), missing: error instanceof ApiError && error.status === 404 });
      }
    });
  }, [binding, id]);
  useEffect(() => {
    load();
    return () => { serial.current++; request.current?.abort(); };
  }, [load]);
  useEffect(() => onCollectionInvalidation(["catalog", "gaps", "accounts"], reset => {
    if (reset) { serial.current++; request.current?.abort(); setRecord(null); setFailure(null); }
    load();
  }), [load]);

  const gap = record?.binding === binding ? record.gap : null;
  const error = failure?.binding === binding ? failure : null;
  return <>
    <nav className="gap-detail-nav" aria-label="Knowledge gap navigation"><Link href="/">All knowledge gaps</Link></nav>
    {!gap ? <LoadingSurface title={error?.missing ? "Knowledge gap not found" : error ? "Knowledge gap unavailable" : "Opening the knowledge gap"}
      description="Retrieving the question and its source." error={error?.missing ? "This knowledge gap is not available. Return to all knowledge gaps to choose another question." : error?.message}
      onRetry={error?.missing ? undefined : load} skeleton="record" /> : <article className="gap-detail">
      <header className="gap-detail-header">
        <div className="gap-detail-question">
          <VoteControls key={binding} kind="gap" id={gap.object.id} initial={gap.votes} compact vertical
            onChange={votes => setRecord(previous => previous?.binding === binding ? { ...previous, gap: { ...previous.gap, votes } } : previous)} />
          <div className="gap-detail-title"><p className="gap-detail-name">{gap.object.name || gap.source.disease_label || gap.object.scope || "Knowledge gap"}</p><h1>{gap.object.text}</h1></div>
        </div>
        <div className="gap-detail-actions">
          <p className="gap-detail-source">Source: <strong>DisMech</strong></p>
          <Link className="gap-detail-explore" href={gapExploreHref(gap.object.id)}>Explore this gap</Link>
        </div>
      </header>
      <GapContext key={gap.object.id} gap={gap} />
      <GapAccounts key={binding} gap={gap} scope="public" />
    </article>}
  </>;
}
