"use client";
import { useEffect, useId, useRef, useState } from "react";
import { signIn } from "next-auth/react";
import { api, messageOf, type Schema } from "@/lib/client";
import { nextVote, type Vote } from "@/lib/votes";
import { ProviderButtons, useIdentity } from "./Session";
import "./votes.css";

export function VoteControls({ kind, id, initial, onChange, compact = false, vertical = false }: {
  kind: "gap" | "account"; id: string; initial: Schema<"VoteState"> | null;
  onChange?: (value: Schema<"VoteState">) => void; compact?: boolean; vertical?: boolean;
}) {
  const { me, ready } = useIdentity();
  const [value, setValue] = useState(initial), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null), label = useId();
  const retry = useRef<{ vote: Vote; key: string } | null>(null);
  const binding = `${kind}:${id}:${me?.user_id || "visitor"}`;
  const current = useRef(binding); current.current = binding;
  const pending = useRef(false);
  useEffect(() => { setValue(initial); }, [initial]);
  useEffect(() => { retry.current = null; setError(""); setBusy(false); pending.current = false; }, [binding]);
  if (!value) return null;
  const registered = me?.principal_kind === "registered";
  const mine = registered ? value.user_vote : null;
  const noun = kind === "gap" ? "knowledge gap" : "scientific account";
  const cast = async (vote: Vote) => {
    if (pending.current) return;
    if (!registered) { dialog.current?.showModal(); return; }
    if (retry.current?.vote !== vote) retry.current = { vote, key: crypto.randomUUID() };
    const request = retry.current;
    pending.current = true; setBusy(true); setError("");
    try {
      if (kind === "gap") await api.setGapVote(id, vote, request.key);
      else await api.setAccountVote(id, vote, request.key);
      if (current.current !== binding) return;
      // A recovered idempotent response may predate a vote changed in another
      // tab. Read the committed ballot once after this mutation/retry.
      const result = kind === "gap" ? await api.gapVote(id) : await api.accountVote(id);
      if (current.current !== binding) return;
      setValue(result); retry.current = null; onChange?.(result);
    } catch (failure) { if (current.current === binding) setError(messageOf(failure)); }
    finally { if (current.current === binding) { pending.current = false; setBusy(false); } }
  };
  return <div className={`vote-control${compact ? " is-compact" : ""}${vertical ? " is-vertical" : ""}`}>
    <div className="vote-buttons" role="group" aria-label={`Vote on this ${noun}`} aria-busy={busy}>
      <button type="button" className={`vote-up${mine === 1 ? " is-selected" : ""}`} aria-pressed={mine === 1} disabled={!ready || busy} aria-label={`${mine === 1 ? "Remove upvote from" : "Upvote"} this ${noun}`} title={mine === 1 ? "Remove your upvote" : kind === "gap" ? "This question needs an answer" : "This account is useful"} onClick={() => void cast(nextVote(mine, 1))}><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m4 9 4-4 4 4M8 5v8" /></svg></button>
      <output className="vote-score" aria-live="polite" aria-label={`${value.score} net votes, ${value.upvotes} upvotes and ${value.downvotes} downvotes`} title={`${value.upvotes} upvotes · ${value.downvotes} downvotes`}>{value.score > 0 ? "+" : ""}{value.score}</output>
      <button type="button" className={`vote-down${mine === -1 ? " is-selected" : ""}`} aria-pressed={mine === -1} disabled={!ready || busy} aria-label={`${mine === -1 ? "Remove downvote from" : "Downvote"} this ${noun}`} title={mine === -1 ? "Remove your downvote" : "Downvote"} onClick={() => void cast(nextVote(mine, -1))}><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="m4 7 4 4 4-4M8 3v8" /></svg></button>
    </div>
    {error && <p className="vote-error" role="alert">{error} <button type="button" onClick={() => retry.current && void cast(retry.current.vote)}>Retry vote</button></p>}
    <dialog ref={dialog} className="auth-dialog" aria-labelledby={label}><button className="dialog-close" aria-label="Close sign-in choices" onClick={() => dialog.current?.close()}>×</button><h2 id={label}>Sign in to vote</h2><p>Each researcher gets one vote per {noun}. You can change or remove your vote at any time.</p><ProviderButtons onLogin={provider => void signIn(provider, { callbackUrl: window.location.href })} /></dialog>
  </div>;
}
