"use client";

import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { LoadingStatus } from "./LoadingSurface";
import { useIdentity } from "./Session";
import { PublicationSignIn } from "./PublicationSignIn";
import "./account-publication.css";

type Visibility = "public" | "private";

export function AccountPublication({ id, publication, onRefresh }: { id: string; publication?: Schema<"PublicationState">; onRefresh: () => void }) {
  const { me, ready } = useIdentity();
  const signedIn = ready && me?.principal_kind === "registered";
  const [current, setCurrent] = useState(publication), [confirmation, setConfirmation] = useState<Visibility | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const pending = useRef<{ visibility: Visibility; version: number; key: string } | null>(null);
  const active = useRef(true), inflight = useRef(false), dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null), primary = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => { setCurrent(previous => !previous || !publication || publication.version >= previous.version ? publication : previous); }, [publication]);
  useEffect(() => {
    if (confirmation && current?.can_manage) {
      if (!dialog.current?.open) dialog.current?.showModal();
    } else if (dialog.current?.open) dialog.current.close();
  }, [confirmation, current?.can_manage]);
  const open = (visibility: Visibility, button: HTMLButtonElement) => {
    if (!ready || inflight.current) return;
    trigger.current = button;
    if (pending.current?.visibility !== visibility || pending.current.version !== current?.version) pending.current = null;
    setError(""); setConfirmation(visibility);
  };
  const close = () => { if (!inflight.current) { setConfirmation(null); setError(""); } };
  const restoreFocus = () => {
    if (inflight.current) return;
    setConfirmation(null);
    (trigger.current?.isConnected ? trigger.current : primary.current)?.focus();
  };
  const mutate = async (visibility: Visibility) => {
    if (!ready || !current?.can_manage || inflight.current || (visibility === "public" && !signedIn)) return;
    // Reuse an uncertain request even after closing/reopening the dialog.
    if (!pending.current || pending.current.visibility !== visibility || pending.current.version !== current.version) {
      pending.current = { visibility, version: current.version, key: crypto.randomUUID() };
    }
    const attempt = pending.current;
    inflight.current = true; setBusy(true); setError("");
    try {
      const value = await api.setPublication(id, { visibility: attempt.visibility, expected_version: attempt.version }, attempt.key);
      if (!active.current) return;
      setCurrent(previous => !previous || value.version >= previous.version ? value : previous);
      pending.current = null; setConfirmation(null); onRefresh();
    } catch (failure) {
      if (!active.current) return;
      if (failure instanceof ApiError && failure.status === 409 && ["PUBLICATION_VERSION_CONFLICT", "VERSION_CONFLICT"].includes(failure.code)) {
        pending.current = null;
        try { const value = await api.publication(id); if (active.current) setCurrent(value); } catch { /* Original conflict remains actionable. */ }
        if (active.current) setError("Publication changed in another session. Review its current visibility and try again.");
      } else setError(messageOf(failure));
    } finally { inflight.current = false; if (active.current) setBusy(false); }
  };
  if (!current) return null;
  if (!current.can_manage) return current.visibility === "public" ? <p className="account-public-badge">Published scientific account</p> : null;
  const published = current.visibility === "public";
  const removing = confirmation === "private";
  const needsSignIn = confirmation === "public" && !signedIn;
  const alreadyPrivate = removing && !published;
  const title = removing ? "Unpublish account?" : published ? "Update published snapshot?" : "Publish account?";
  const confirmLabel = removing ? "Unpublish account" : published ? "Update public snapshot" : "Publish account";
  return <section className="account-publication" aria-label="Account publication">
    <button ref={primary} className="account-publication-control" disabled={!ready || busy} aria-haspopup="dialog"
      onClick={event => open(published ? "private" : "public", event.currentTarget)}>{published ? "Unpublish" : "Publish"}</button>
    {published && current.has_unpublished_changes && <button className="account-publication-update" disabled={!ready || busy} aria-haspopup="dialog"
      aria-label="Update published snapshot" onClick={event => open("public", event.currentTarget)}>Update snapshot</button>}
    <dialog ref={dialog} className="account-publication-dialog" aria-labelledby={titleId}
      onCancel={event => { event.preventDefault(); close(); }} onClose={restoreFocus}>
      {confirmation && <div className="publication-confirmation">
        <h2 id={titleId}>{title}</h2>
        {removing ? <>
          <p>{alreadyPrivate ? "This account is already private." : "Unpublishing removes this workspace’s public snapshot. Independently published copies can remain available."}</p>
          {!alreadyPrivate && <p>Your account and its research history remain in your workspace.</p>}
        </> : <>
          <p>{published ? "The existing snapshot is publicly viewable. Confirm an update to replace it with this account’s currently accepted content." : "This account is currently private."}</p>
          <p>Publishing makes this account, its claims, original author attribution, supporting evidence and provenance, and its currently accepted research statement and citations publicly viewable and downloadable. Anyone can open the published account without signing in.</p>
          <p>Job activity and workspace drafts stay private. Statements accepted later are included only when you update the published snapshot.</p>
          {published && current.has_unpublished_changes && <p className="publication-new-statement">A newer accepted research statement is still private. Update the published snapshot to include it.</p>}
        </>}
        {needsSignIn && <PublicationSignIn kind="scientific account" published={published} />}
        {busy && <LoadingStatus>Saving publication settings…</LoadingStatus>}
        {error && <div role="alert" className="error"><span>{error}</span></div>}
        <div className="account-publication-actions">
          <button type="button" disabled={busy} autoFocus onClick={close}>Cancel</button>
          {!needsSignIn && !alreadyPrivate && <button type="button" className="publication-confirm" disabled={!ready || busy}
            onClick={() => void mutate(confirmation)}>{busy ? "Saving…" : error && pending.current ? "Retry" : confirmLabel}</button>}
        </div>
      </div>}
    </dialog>
  </section>;
}
