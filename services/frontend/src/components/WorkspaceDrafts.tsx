"use client";
import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { emptyComposer } from "@/lib/composer";
import { draftHref, isSavedDraft, type workspaceRuns } from "@/lib/workspace";

type Draft = Schema<"Draft">;
type Action = "create" | "copy" | "rename" | "delete";
const draftName = (draft: Draft) => draft.name || `Draft ${draft.id.slice(0, 8)}`;
const shortDate = (value: string) => new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });

function DraftActions({ draft, sourceGap, refresh }: {
  draft?: Draft; sourceGap: Schema<"SelectedGap"> | null; refresh: () => Promise<unknown>;
}) {
  const id = useId(), dialog = useRef<HTMLDialogElement>(null);
  const [action, setAction] = useState<Action | null>(null), [name, setName] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [created, setCreated] = useState<Draft | null>(null);
  const retry = useRef<{ binding: string; key: string } | null>(null);
  const start = (next: Action) => {
    setError(""); retry.current = null;
    setName(next === "rename" && draft ? draftName(draft) : ""); setAction(next);
  };
  useEffect(() => { if (action) dialog.current?.showModal(); else dialog.current?.close(); }, [action]);
  const submit = async () => {
    if (!action || busy) return;
    const composer = action === "copy" && draft ? draft.composer : { ...emptyComposer(), source_gap: sourceGap };
    const binding = JSON.stringify({ action, draft, name: name.trim(), composer });
    if (retry.current?.binding !== binding) retry.current = { binding, key: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      if (action === "create" || action === "copy") {
        setCreated(await api.createDraft(composer, retry.current.key, name.trim()));
      } else if (action === "rename" && draft) {
        await api.renameDraft(draft, name.trim(), retry.current.key);
      } else if (action === "delete" && draft) {
        await api.deleteDraft(draft, retry.current.key);
      }
      setAction(null); retry.current = null; await refresh();
    } catch (failure) {
      setError(messageOf(failure));
      if (failure instanceof ApiError && [404, 409].includes(failure.status)) { setAction(null); await refresh(); }
    } finally { setBusy(false); }
  };
  return <>
    <div className="workspace-drafts">
      {draft ? <>
        <Link className="workspace-open-draft" href={draftHref(draft.id)}>Open draft <span aria-hidden="true">→</span></Link>
        <button type="button" disabled={busy} onClick={() => start("copy")}>Copy</button>
        <button type="button" disabled={busy} onClick={() => start("rename")}>Rename</button>
        <button type="button" disabled={busy} onClick={() => start("delete")}>Delete</button>
      </> : <button type="button" disabled={busy} onClick={() => start("create")}>+ New saved draft</button>}
      {created && <Link href={draftHref(created.id)}>Open “{draftName(created)}” →</Link>}
    </div>
    {error && !action && <p className="error" role="alert">{error}</p>}
    <dialog ref={dialog} className="auth-dialog workspace-draft-dialog" aria-labelledby={`${id}-title`}
      onCancel={event => { if (busy) event.preventDefault(); else setAction(null); }} onClose={() => { if (!busy) setAction(null); }}>
      {action && <form onSubmit={event => { event.preventDefault(); void submit(); }}>
        <h2 id={`${id}-title`}>{action === "delete" ? "Delete saved draft?" : action === "rename" ? "Rename draft" : action === "copy" ? "Save a copy" : "New saved draft"}</h2>
        {action === "delete" ? <p>Delete “{draftName(draft!)}”? Your research runs and their frozen inputs will remain available.</p> : <>
          <label htmlFor={`${id}-name`}>Draft name</label>
          <input id={`${id}-name`} className="field" autoFocus required maxLength={120} value={name} disabled={busy}
            placeholder="e.g. BMPR2 modifier hypothesis" onChange={event => setName(event.target.value)} />
          {action === "copy" && <p>Copy the saved research direction, context, hypotheses, document references, and source settings from “{draftName(draft!)}”.</p>}
        </>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="workspace-draft-actions"><button type="button" disabled={busy} onClick={() => setAction(null)}>Cancel</button>
          <button type="submit" className={action === "delete" ? "workspace-delete-draft" : "primary"} disabled={busy || (action !== "delete" && !name.trim())}>
            {busy ? "Saving…" : action === "delete" ? "Delete draft" : action === "rename" ? "Save name" : "Save draft"}
          </button></div>
      </form>}
    </dialog>
  </>;
}

export function WorkspaceDraftRow({ draft, refresh }: { draft: Draft; refresh: () => Promise<unknown> }) {
  const preview = draft.composer.research_direction || draft.composer.context;
  return <article className="workspace-draft-row workspace-gap-row" data-draft-id={draft.id}>
    <Link className="workspace-item-title" href={draftHref(draft.id)}>{draftName(draft)}</Link>
    {preview && <p className="workspace-account-conclusion">{preview}</p>}
    <div className="workspace-item-meta"><time dateTime={draft.updated_at}>Saved {shortDate(draft.updated_at)}</time>
      <span>{draft.composer.eaggl_anchors.length} mechanism anchors</span>
      {!!draft.composer.upload_ids?.length && <span>{draft.composer.upload_ids.length} attached documents</span>}
      {draft.composer.source_gap && <Link href={`/knowledge-gaps/${encodeURIComponent(draft.composer.source_gap.id)}`}>View knowledge gap</Link>}
    </div>
    <DraftActions draft={draft} sourceGap={draft.composer.source_gap} refresh={refresh} />
  </article>;
}

export function WorkspaceGapRow({ item, drafts, activity, refresh }: {
  item: Schema<"Exploration">; drafts: Draft[]; activity: ReturnType<typeof workspaceRuns>; refresh: () => Promise<unknown>;
}) {
  const saved = drafts.filter(isSavedDraft), runs = activity.byGap.get(item.source_gap.id) || [];
  return <article className="workspace-gap-row">
    <Link className="workspace-item-title" href={`/knowledge-gaps/${encodeURIComponent(item.source_gap.id)}`}>{item.knowledge_gap.text}</Link>
    <div className="workspace-item-meta">
      {item.knowledge_gap.scope && <span>{item.knowledge_gap.scope}</span>}
      <time dateTime={item.last_explored_at}>Explored {shortDate(item.last_explored_at)}</time>
      <Link href={`/?gap=${encodeURIComponent(item.source_gap.id)}`}>Explore again →</Link>
      {item.scientific_accounts.count > 0 && <Link href={`/knowledge-gaps/${encodeURIComponent(item.source_gap.id)}`}>{item.scientific_accounts.count} scientific accounts</Link>}
    </div>
    {!!saved.length && <div className="workspace-item-meta" aria-label="Saved drafts for this gap"><span>Saved drafts</span>
      {saved.map(draft => <Link key={draft.id} href={draftHref(draft.id)}>{draftName(draft)}</Link>)}
    </div>}
    {!!runs.length && <div className="workspace-item-meta" aria-label="Research runs for this gap"><span>Research runs</span>
      {runs.map(job => <Link key={job.id} href={activity.href(job)}>{job.status.replaceAll("_", " ")} · {shortDate(job.created_at)}</Link>)}
    </div>}
    <DraftActions sourceGap={item.source_gap} refresh={refresh} />
  </article>;
}
