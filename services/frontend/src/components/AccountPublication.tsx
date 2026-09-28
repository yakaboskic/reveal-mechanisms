"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { LoadingStatus } from "./LoadingSurface";
import "./account-publication.css";

export function AccountPublication({ id, publication, onRefresh }: { id: string; publication?: Schema<"PublicationState">; onRefresh: () => void }) {
  const [current, setCurrent] = useState(publication), [confirm, setConfirm] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const pending = useRef<{ visibility: "public" | "private"; version: number; key: string } | null>(null);
  const active = useRef(true), inflight = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => { setCurrent(previous => !previous || !publication || publication.version >= previous.version ? publication : previous); }, [publication]);
  const mutate = async (visibility: "public" | "private", retry = false) => {
    if (!current?.can_manage || inflight.current) return;
    if (!retry || !pending.current) pending.current = { visibility, version: current.version, key: crypto.randomUUID() };
    const attempt = pending.current;
    inflight.current = true; setBusy(true); setError("");
    try {
      const value = await api.setPublication(id, { visibility: attempt.visibility, expected_version: attempt.version }, attempt.key);
      if (!active.current) return;
      setCurrent(value); pending.current = null; setConfirm(false); onRefresh();
    } catch (failure) {
      if (!active.current) return;
      if (failure instanceof ApiError && failure.status === 409) {
        pending.current = null;
        try { const value = await api.publication(id); if (active.current) setCurrent(value); } catch { /* Original conflict remains actionable. */ }
        if (active.current) setError("Publication changed in another session. Review its current visibility and try again.");
      } else setError(messageOf(failure));
    } finally { if (active.current) { setBusy(false); inflight.current = false; } }
  };
  if (!current) return null;
  if (!current.can_manage) return current.visibility === "public" ? <p className="account-public-badge">Published scientific account</p> : null;
  return <section className="account-publication" aria-label="Account publication">
    <div className="account-publication-heading"><div><h2>{current.visibility === "public" ? "Published account" : "Private account"}</h2><p>{current.visibility === "public" ? "A saved snapshot is publicly viewable. New statements stay private until you update it." : "This workspace has not published a snapshot of this account."}</p></div>
      {!confirm && <button disabled={busy} onClick={() => { setConfirm(true); setError(""); }}>{current.visibility === "public" ? "Update published snapshot" : "Publish…"}</button>}
    </div>
    {confirm && <div className="publication-confirmation"><p>Publishing makes this account, its claims, original author attribution, supporting evidence and provenance, and its currently accepted research statement and citations publicly viewable and downloadable. Anyone can open the published account without signing in.</p><p>Job activity and workspace drafts stay private. Statements accepted later are included only when you update the published snapshot.</p><div><button className="publication-confirm" disabled={busy} onClick={() => void mutate("public")}>{current.visibility === "public" ? "Update public snapshot" : "Publish account"}</button><button disabled={busy} onClick={() => { setConfirm(false); setError(""); }}>Cancel</button></div></div>}
    {current.visibility === "public" && current.has_unpublished_changes && <p className="publication-new-statement">A newer accepted research statement is still private. Update the published snapshot to include it.</p>}
    {current.visibility === "public" && <div className="publication-unpublish"><p>Unpublishing removes this workspace’s public snapshot. Independently published copies can remain available.</p><button disabled={busy} onClick={() => void mutate("private")}>Unpublish</button></div>}
    {busy && <LoadingStatus>Saving publication settings…</LoadingStatus>}
    {error && <div role="alert" className="error"><span>{error}</span>{pending.current && <button disabled={busy} onClick={() => void mutate(pending.current!.visibility, true)}>Retry</button>}</div>}
  </section>;
}
