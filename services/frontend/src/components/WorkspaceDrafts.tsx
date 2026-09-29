"use client";
import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, terminal, type Schema } from "@/lib/client";
import { emptyComposer } from "@/lib/composer";
import type { workspaceRuns } from "@/lib/workspace";

type Draft = Schema<"Draft">;
type Action = { kind: "create" | "rename" | "delete"; draft?: Draft };
const draftName = (draft: Draft) => draft.name || `Draft ${draft.id.slice(0, 8)}`;
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });

export function WorkspaceGapRow({ item, drafts, activity, refresh }: {
  item: Schema<"Exploration">; drafts: Draft[]; activity: ReturnType<typeof workspaceRuns>; refresh: () => Promise<unknown>;
}) {
  const id = useId(), dialog = useRef<HTMLDialogElement>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [name, setName] = useState("");
  const [copy, setCopy] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const retry = useRef<{ binding: string; key: string } | null>(null);
  const sorted = [...drafts].sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id.localeCompare(a.id));
  const selected = sorted.find(draft => draft.id === selectedId)
    || sorted.find(draft => activity.activeDrafts.has(draft.id))
    || sorted.find(draft => draft.id === item.draft_id) || sorted[0];
  const runs = selected ? activity.byDraft.get(selected.id) || [] : activity.byGap.get(item.source_gap.id) || [];
  const job = runs[0], running = !!job && !terminal(job.status);
  const href = running ? activity.href(job) : selected ? `/?draft=${selected.id}` : `/?gap=${encodeURIComponent(item.source_gap.id)}`;
  const start = (kind: Action["kind"]) => {
    setError(""); retry.current = null; setCopy(!!selected);
    setName(kind === "create" ? "" : selected ? draftName(selected) : "");
    setAction({ kind, draft: selected });
  };
  useEffect(() => { if (action) dialog.current?.showModal(); else dialog.current?.close(); }, [action]);
  const submit = async () => {
    if (!action || busy) return;
    const composer = copy && action.draft ? action.draft.composer : { ...emptyComposer(), source_gap: item.source_gap };
    const binding = JSON.stringify({ action, name: name.trim(), composer });
    if (retry.current?.binding !== binding) retry.current = { binding, key: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      if (action.kind === "create") {
        const created = await api.createDraft(composer, retry.current.key, name.trim());
        setSelectedId(created.id);
      } else if (action.kind === "rename" && action.draft) {
        const updated = await api.renameDraft(action.draft, name.trim(), retry.current.key);
        setSelectedId(updated.id);
      } else if (action.kind === "delete" && action.draft) {
        await api.deleteDraft(action.draft, retry.current.key); setSelectedId(null);
      }
      setAction(null); retry.current = null;
      await refresh();
    } catch (failure) {
      setError(messageOf(failure));
      if (failure instanceof ApiError && [404, 409].includes(failure.status)) { setAction(null); await refresh(); }
    } finally { setBusy(false); }
  };
  return <article className="workspace-gap-row">
    <Link className="workspace-item-title" href={href}>{item.knowledge_gap.text}</Link>
    <div className="workspace-item-meta">
      {item.knowledge_gap.scope && <span>{item.knowledge_gap.scope}</span>}
      <time dateTime={item.last_explored_at}>Explored {shortDate(item.last_explored_at)}</time>
      {item.scientific_accounts.count > 0 && <Link href="/workspace?tab=accounts">{item.scientific_accounts.count} scientific {item.scientific_accounts.count === 1 ? "account" : "accounts"}</Link>}
    </div>
    <details className="workspace-draft-panel">
      <summary><span className="workspace-draft-caret" aria-hidden="true">›</span>Drafts ({drafts.length})</summary>
    <div className="workspace-drafts">
      {selected ? <>
        <label className="sr-only" htmlFor={`${id}-draft`}>Drafts</label>
        <select id={`${id}-draft`} value={selected.id} disabled={busy} onChange={event => { setSelectedId(event.target.value); setError(""); }}>
          {sorted.map(draft => <option key={draft.id} value={draft.id}>{draftName(draft)} · {shortDate(draft.updated_at)}{activity.activeDrafts.has(draft.id) ? " · Research running" : ""}</option>)}
        </select>
        <Link className="workspace-open-draft" href={href}>{running ? "View run" : "Open draft"}<span aria-hidden="true">→</span></Link>
        <button type="button" disabled={busy} onClick={() => start("rename")}>Rename</button>
        <button type="button" disabled={busy || running} title={running ? "Wait for this draft’s research to finish before deleting it." : undefined} onClick={() => start("delete")}>Delete</button>
      </> : <span className="workspace-no-drafts">No drafts yet</span>}
      <button type="button" className="workspace-new-draft" disabled={busy} onClick={() => start("create")}>+ New draft</button>
    </div>
    {(selected || job) && <div className="workspace-item-meta workspace-draft-detail">
      {selected && <><span>{selected.composer.eaggl_anchors.length} mechanism {selected.composer.eaggl_anchors.length === 1 ? "anchor" : "anchors"}</span><time dateTime={selected.updated_at}>Updated {shortDate(selected.updated_at)}</time></>}
      {job && <Link href={activity.href(job)}>{job.status === "insufficient_evidence" ? "Exploration saved" : `Research ${job.status.replaceAll("_", " ")}`}</Link>}
    </div>}
    </details>
    {error && !action && <p className="error" role="alert">{error}</p>}
    <dialog ref={dialog} className="auth-dialog workspace-draft-dialog" aria-labelledby={`${id}-title`} onCancel={event => { if (busy) event.preventDefault(); else setAction(null); }} onClose={() => { if (!busy) setAction(null); }}>
      {action && <form onSubmit={event => { event.preventDefault(); void submit(); }}>
        <h2 id={`${id}-title`}>{action.kind === "create" ? "New draft" : action.kind === "rename" ? "Rename draft" : "Delete draft?"}</h2>
        {action.kind === "delete" ? <p>Delete “{draftName(action.draft!)}”? Your research runs, scientific accounts, and saved explorations will remain available.</p> : <>
          <label htmlFor={`${id}-name`}>Draft name</label>
          <input id={`${id}-name`} className="field" autoFocus required maxLength={120} value={name} disabled={busy} placeholder="e.g. BMPR2 modifier hypothesis" onChange={event => setName(event.target.value)} />
          {action.kind === "create" && action.draft && <label className="workspace-copy-draft"><input type="checkbox" checked={copy} disabled={busy} onChange={event => setCopy(event.target.checked)} />Copy anchors and settings from “{draftName(action.draft)}”</label>}
          {action.kind === "create" && !copy && <p>Start with this knowledge gap and choose new mechanism anchors in the editor.</p>}
        </>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="workspace-draft-actions"><button type="button" disabled={busy} onClick={() => setAction(null)}>Cancel</button><button type="submit" className={action.kind === "delete" ? "workspace-delete-draft" : "primary"} disabled={busy || (action.kind !== "delete" && !name.trim())}>{busy ? "Saving…" : action.kind === "delete" ? "Delete draft" : action.kind === "create" ? "Create draft" : "Save name"}</button></div>
      </form>}
    </dialog>
  </article>;
}
