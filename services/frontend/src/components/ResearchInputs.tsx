"use client";
import { useEffect, useRef, useState } from "react";
import { messageOf, type Schema } from "@/lib/client";
import { fileDigest, transferFile, uploadRequest, type Upload, type UploadTicket } from "@/lib/uploads";

type Pending = { key: string; file: File; progress: number; error?: string; upload?: Upload; controller?: AbortController; ticket?: UploadTicket };
/** `draft` resolves the editor's draft id, creating its temporary draft on the first attachment; null once the editor is gone. */
type Props = { composer: Schema<"Composer">; onChange: (change: (value: Schema<"Composer">) => Schema<"Composer">) => void; draft?: () => Promise<string | null>; readOnly?: boolean; onBlockingChange: (blocked: boolean) => void };
const sizeLabel = (bytes: number) => bytes < 1_000_000 ? `${Math.ceil(bytes / 1000)} KB` : `${(bytes / 1_000_000).toFixed(1)} MB`;

export function ResearchInputs({ composer, onChange, draft, readOnly = false, onBlockingChange }: Props) {
  const ids = composer.upload_ids || [];
  const legacyFields = useRef({ direction: !!composer.research_direction, hypotheses: !!composer.hypotheses });
  legacyFields.current.direction ||= !!composer.research_direction;
  legacyFields.current.hypotheses ||= !!composer.hypotheses;
  const [records, setRecords] = useState<Record<string, Upload>>({});
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [pending, setPending] = useState<Pending[]>([]);
  const active = useRef(true), transfers = useRef(new Map<string, AbortController>());
  const change = useRef(onChange); change.current = onChange;
  useEffect(() => { active.current = true; return () => { active.current = false; for (const controller of transfers.current.values()) controller.abort(); }; }, []);
  const idsKey = JSON.stringify(ids);
  useEffect(() => {
    const controller = new AbortController(); setLoadError("");
    const missing = ids.filter(id => !records[id]);
    void Promise.all(missing.map(id => uploadRequest<Upload>(`/v1/uploads/${encodeURIComponent(id)}`, "GET", undefined, undefined, controller.signal))).then(uploads => {
      if (!controller.signal.aborted) setRecords(previous => ({ ...previous, ...Object.fromEntries(uploads.map(upload => [upload.id, upload])) }));
    }).catch(error => { if (!controller.signal.aborted) setLoadError(messageOf(error)); });
    return () => controller.abort();
  // The requested IDs, not changing cached rows, define the read lifetime.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey, loadAttempt]);
  const blocked = pending.length > 0 || !!loadError || ids.some(id => !records[id] || records[id].status !== "ready");
  useEffect(() => { onBlockingChange(blocked); }, [blocked, onBlockingChange]);
  const updatePending = (key: string, patch: Partial<Pending>) => { if (active.current) setPending(previous => previous.map(item => item.key === key ? { ...item, ...patch } : item)); };
  const release = (id: string) => { void uploadRequest(`/v1/uploads/${encodeURIComponent(id)}`, "DELETE", undefined, `detach-${id}`).catch(() => { /* Saved drafts and frozen runs retain referenced files; expiry reclaims abandoned uploads. */ }); };
  async function attach(item: Pending) {
    if (!draft || readOnly) return;
    const controller = new AbortController(); transfers.current.set(item.key, controller);
    updatePending(item.key, { progress: 0, error: undefined });
    let upload = item.upload;
    try {
      if (upload) upload = await uploadRequest<Upload>(`/v1/uploads/${upload.id}`, "GET", undefined, undefined, controller.signal);
      if (upload?.status !== "ready") {
        const draftId = await draft();
        if (!active.current || controller.signal.aborted) return;
        if (!draftId) throw new Error("This editor closed before the document could be attached.");
        const ticket = await uploadRequest<UploadTicket>("/v1/uploads", "POST", { draft_id: draftId, filename: item.file.name, media_type: item.file.type || "application/octet-stream", size_bytes: item.file.size, sha256: await fileDigest(item.file) }, item.key);
        upload = ticket.upload;
        if (!active.current || controller.signal.aborted) { release(upload.id); return; }
        updatePending(item.key, { ticket, upload });
        if (upload.status !== "ready") await transferFile(ticket, item.file, progress => updatePending(item.key, { progress }), controller.signal);
        upload = await uploadRequest<Upload>(`/v1/uploads/${upload.id}/complete`, "POST", {}, `${item.key}-complete`, controller.signal);
      }
      if (upload.status !== "ready") throw new Error(upload.error || "The document could not be prepared for the agent.");
      if (!active.current || controller.signal.aborted) { release(upload.id); return; }
      const readyUpload = upload;
      setRecords(previous => ({ ...previous, [readyUpload.id]: readyUpload }));
      change.current(previous => ({ ...previous, upload_ids: [...new Set([...(previous.upload_ids || []), readyUpload.id])] }));
      setPending(previous => previous.filter(value => value.key !== item.key));
    } catch (error) {
      if (!controller.signal.aborted) {
        if (upload) upload = await uploadRequest<Upload>(`/v1/uploads/${upload.id}`).catch(() => upload);
        updatePending(item.key, { error: messageOf(error), upload });
      }
    }
    finally { transfers.current.delete(item.key); }
  }
  const retry = (item: Pending) => {
    if (item.upload && (["failed", "removed"].includes(item.upload.status) || new Date(item.upload.expires_at).getTime() <= Date.now())) {
      release(item.upload.id);
      const replacement = { key: crypto.randomUUID(), file: item.file, progress: 0 };
      setPending(previous => previous.map(value => value.key === item.key ? replacement : value));
      void attach(replacement);
    } else void attach(item);
  };
  function choose(files: FileList | null) {
    if (!files) return;
    setLoadError("");
    const items = Array.from(files).map(file => ({ key: crypto.randomUUID(), file, progress: 0 }));
    if (ids.length + pending.length + items.length > 5) { setLoadError("Attach up to five documents. Remove one before adding more."); return; }
    const bytes = ids.reduce((sum, id) => sum + (records[id]?.size_bytes || 0), 0) + pending.reduce((sum, item) => sum + item.file.size, 0) + items.reduce((sum, item) => sum + item.file.size, 0);
    if (items.some(item => item.file.size > 8_000_000) || bytes > 16_000_000) { setLoadError("Documents must be at most 8 MB each and 16 MB together."); return; }
    setPending(previous => [...previous, ...items]);
    for (const item of items) void attach(item);
  }
  const remove = (id: string) => { setLoadError(""); onChange(previous => ({ ...previous, upload_ids: (previous.upload_ids || []).filter(value => value !== id) })); release(id); };
  const hasInputs = !!(composer.research_direction || composer.context || composer.hypotheses || ids.length);
  const needsAttention = !!loadError || pending.some(item => item.error) || ids.some(id => records[id] && records[id].status !== "ready");
  const contextStatus = needsAttention ? "Needs attention" : pending.length ? "Uploading…" : ids.length ? `${ids.length} document${ids.length === 1 ? "" : "s"}` : hasInputs ? "Added" : "Notes and documents";
  if (readOnly && !hasInputs) return null;
  const content = <>
    {readOnly ? <div className="submitted-inputs">{composer.research_direction && <><h2>Submitted research direction</h2><p>{composer.research_direction}</p></>}{composer.context && <details><summary>Submitted context</summary><p>{composer.context}</p></details>}{composer.hypotheses && <details><summary>Submitted hypotheses</summary><p>{composer.hypotheses}</p></details>}</div> : <>
      <label className="sr-only" htmlFor="research-context">Additional context</label>
      <textarea id="research-context" rows={4} maxLength={20000} value={composer.context || ""} placeholder="Add a research direction, hypothesis, or anything else the agent should consider…" onChange={event => { const text = event.target.value; onChange(value => ({ ...value, context: text })); }} />
      {(legacyFields.current.direction || legacyFields.current.hypotheses) && <details className="previous-research-inputs"><summary>Previously saved research inputs</summary>
        {legacyFields.current.direction && <><label htmlFor="research-direction">Research direction</label><textarea id="research-direction" rows={2} maxLength={6000} value={composer.research_direction || ""} onChange={event => { const text = event.target.value; onChange(value => ({ ...value, research_direction: text })); }} /></>}
        {legacyFields.current.hypotheses && <><label htmlFor="research-hypotheses">Hypotheses</label><textarea id="research-hypotheses" rows={2} maxLength={12000} value={composer.hypotheses || ""} onChange={event => { const text = event.target.value; onChange(value => ({ ...value, hypotheses: text })); }} /></>}
      </details>}
    </>}
    {(!readOnly || ids.length > 0) && <div className="draft-attachments"><div className="attachment-heading">{readOnly && <h3>Submitted documents</h3>}{!readOnly && <label className={`attach-document${!draft ? " is-disabled" : ""}`}><svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m7 10 5-5a3 3 0 0 1 4 4l-7 7a4 4 0 0 1-6-6l7-7M6 12l6-6" /></svg>Attach documents<input type="file" multiple disabled={!draft} accept=".txt,.md,.csv,.tsv,.json,.yaml,.yml,.pdf,.docx" onChange={event => { choose(event.target.files); event.target.value = ""; }} /></label>}</div>
      {!readOnly && <p className="attachment-hint">Up to 5 documents · 8 MB each · PDF, Word, text, or tables</p>}
      <ul className="attachment-list">{ids.map(id => { const upload = records[id]; return <li key={id}><div className="attachment-row"><div><strong>{upload?.filename || "Loading document…"}</strong><small>{upload ? `${sizeLabel(upload.size_bytes)} · ${upload.status === "ready" ? "Ready for the agent" : upload.status}` : "Retrieving document details"}</small></div>{!readOnly && <button type="button" onClick={() => remove(id)} aria-label={`Remove ${upload?.filename || "document"}`}>×</button>}</div>{upload?.storage && <details><summary>Source and storage</summary><dl><dt>Location</dt><dd>{upload.storage.store === "s3" ? `s3://${upload.storage.bucket}/${upload.storage.key}` : upload.storage.key}</dd>{upload.storage.version_id && <><dt>Version</dt><dd>{upload.storage.version_id}</dd></>}<dt>SHA-256</dt><dd>{upload.sha256}</dd>{upload.extraction?.storage && <><dt>Extracted text</dt><dd>{upload.extraction.storage.store === "s3" ? `s3://${upload.extraction.storage.bucket}/${upload.extraction.storage.key}` : upload.extraction.storage.key}</dd></>}</dl><a href={`/api/backend/v1/uploads/${id}/download`} target="_blank" rel="noreferrer">Download original</a></details>}</li>; })}
      {pending.map(item => <li key={item.key}><div className="attachment-row"><div><strong>{item.file.name}</strong><small>{item.error ? "Needs attention" : item.progress === 100 ? "Verifying and preparing document…" : `Uploading ${item.progress}%`}</small></div><button type="button" aria-label={`Remove ${item.file.name}`} onClick={() => { transfers.current.get(item.key)?.abort(); if (item.upload) release(item.upload.id); setPending(previous => previous.filter(value => value.key !== item.key)); }}>×</button></div>{item.error ? <p className="error" role="alert">{item.error} <button type="button" onClick={() => retry(item)}>Retry</button></p> : <progress max={100} value={item.progress} aria-label={`Uploading ${item.file.name}`} />}</li>)}</ul>
      {loadError && <p className="error" role="alert">{loadError} <button type="button" onClick={() => { setLoadError(""); setLoadAttempt(value => value + 1); }}>Retry</button></p>}
    </div>}
  </>;
  return readOnly ? <section className="research-inputs is-readonly" aria-label="Submitted research inputs">{content}</section> : <details className="research-inputs additional-context">
    <summary>Additional context <span>{contextStatus}</span></summary>
    <div className="additional-context-body">{content}</div>
  </details>;
}
