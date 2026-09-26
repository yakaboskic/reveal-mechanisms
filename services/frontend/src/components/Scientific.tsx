"use client";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { api, messageOf, type Schema } from "@/lib/client";
import { Pulse } from "./Activity";
import "./scientific.css";

type Document = Schema<"DapperDocument">;
type Claim = Schema<"DapperClaim">;
type Proposition = Schema<"DapperProposition">;
const human = (value?: string | null) => value ? value.toLowerCase().replaceAll("_", " ").replace(/^./, c => c.toUpperCase()) : "Not specified";
const accountHref = (id: string) => `/accounts/${encodeURIComponent(id)}`;
const claimHref = (id: string, accountId?: string | null) => `/claims/${encodeURIComponent(id)}${accountId ? `?account=${encodeURIComponent(accountId)}` : ""}`;
const objectHref = (id: string) => `/id/${encodeURIComponent(id)}`;
const recordNodes = (document: Document) => Object.values(document).flatMap<unknown>(value => Array.isArray(value) ? value : []).filter(value => value && typeof value === "object" && "id" in value) as { id: string; name?: string | null }[];
const entityLabel = (document: Document, id?: string | null) => !id ? "Not specified" : recordNodes(document).find(node => node.id === id)?.name || (/^(urn:cfde:gene:|gene:)/.test(id) ? id.split(":").at(-1)! : id);
const entityKind = (id?: string | null) => !id ? null : /^(urn:cfde:gene:|gene:)/.test(id) ? "Gene" : id.startsWith("dapper:GeneSet.") ? "Gene set" : /^(dapper:Mechanism\.|factor:)/.test(id) ? "mechanism" : /^(urn:cfde:trait:|trait:|KPN.TRAIT:)/.test(id) ? "trait" : null;
function relationship(proposition?: Proposition) {
  const subject = entityKind(proposition?.subject_entity), object = entityKind(proposition?.object_entity);
  if (subject && object) return `${subject} → ${object}`;
  return proposition?.relation ? human(proposition.relation.split(/[\/#:]/).at(-1)?.replaceAll("-", " ")) : "Unspecified relationship";
}

export function Tabs({ labels, value, onChange, pending }: { labels: string[]; value: string; onChange: (value: string) => void; pending?: string }) {
  const id = useId();
  return <div className="reading-tabs" role="tablist" aria-label="Reading view">{labels.map((label, i) => <button key={label} role="tab" id={`${id}-${i}`} aria-label={label} aria-describedby={pending === label ? `${id}-pending-${i}` : undefined} aria-selected={value === label} tabIndex={value === label ? 0 : -1} onClick={() => onChange(label)} onKeyDown={e => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return; e.preventDefault();
    const index = e.key === "Home" ? 0 : e.key === "End" ? labels.length - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + labels.length) % labels.length;
    onChange(labels[index]); document.getElementById(`${id}-${index}`)?.focus();
  }}>{label}{pending === label && <><span className="statement-progress" aria-hidden="true">·</span><span id={`${id}-pending-${i}`} className="sr-only">Preparing cited research statement</span></>}</button>)}</div>;
}
function useAccount(id: string) {
  const [result, setResult] = useState<Schema<"AccountResult"> | null>(null), [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true; let timer: ReturnType<typeof setTimeout>;
    setResult(null); setError("");
    const load = async () => { try { const value = await api.account(id); if (!active) return; setResult(value); setError(""); if (["queued", "running"].includes(value.research_statement.status)) timer = setTimeout(load, 2500); } catch (failure) { if (active) setError(messageOf(failure)); } };
    void load(); return () => { active = false; clearTimeout(timer); };
  }, [id, refresh]);
  return { result, error, reload: () => setRefresh(n => n + 1) };
}
function paragraphStatus(state: Schema<"ParagraphState">) {
  return ({ succeeded: "Cited statement ready", queued: "Preparing cited statement…", running: "Preparing cited statement…", failed: "Statement needs attention", cancelled: "Statement stopped", not_requested: "Statement not requested" })[state.status];
}
export function AccountPreview({ id }: { id: string }) {
  const { result, error, reload } = useAccount(id);
  const account = result?.document.scientific_accounts?.find(item => item.id === id);
  if (!result || !account) return <section className="account-preview" aria-label="Scientific account"><p role={error || result ? "alert" : "status"}>{error || (result ? "The saved document did not contain the requested account." : "Loading saved scientific account…")}</p>{error && <button className="text-button" onClick={reload}>Retry</button>}</section>;
  const claims = result.document.claims?.filter(item => account.component_claims.includes(item.id)) || [];
  const evidence = new Set(claims.flatMap(claim => claim.has_evidence || []));
  return <section className="account-preview" aria-label="Scientific account"><div className="account-preview-heading"><h3>Scientific account</h3><span>Saved</span></div><article className="account-result"><h4>{account.name || "Scientific account"}</h4><p className="account-result-closing">{account.closing_remarks || account.context}</p><div className="account-facts"><span><b>{account.component_claims.length}</b> {account.component_claims.length === 1 ? "claim" : "claims"}</span><span><b>{evidence.size}</b> evidence {evidence.size === 1 ? "item" : "items"}</span></div><div className="account-result-actions"><Link className="inspect-account" href={`${accountHref(id)}?view=conclusions`}>View account <span aria-hidden="true">↗</span></Link><div className="account-parts"><Link href={`${accountHref(id)}?view=statement`}>Research statement</Link><span className="account-paragraph-status" role="status">{paragraphStatus(result.research_statement)}</span></div></div></article></section>;
}

type ReadingState = { tab: string; claimsOpen: boolean; expanded: string | null; search: string; relationship: string; filtersOpen: boolean };
const defaultReading: ReadingState = { tab: "Conclusions", claimsOpen: false, expanded: null, search: "", relationship: "All", filtersOpen: false };
function useReadingState(id: string) {
  const [view, setView] = useState<ReadingState>(defaultReading), [loaded, setLoaded] = useState("");
  useEffect(() => {
    let saved: Partial<ReadingState> = {};
    try { const raw = JSON.parse(sessionStorage.getItem(`reveal.account-reading.${id}`) || "{}"); saved = { tab: raw.tab === "Research Statement" ? raw.tab : "Conclusions", claimsOpen: raw.claimsOpen === true, expanded: typeof raw.expanded === "string" ? raw.expanded : null, search: typeof raw.search === "string" ? raw.search : "", relationship: typeof raw.relationship === "string" ? raw.relationship : "All", filtersOpen: raw.filtersOpen === true }; } catch { /* Reading preferences are optional. */ }
    const requestedView = new URLSearchParams(window.location.search).get("view");
    if (requestedView === "statement") saved.tab = "Research Statement";
    if (requestedView === "conclusions") saved.tab = "Conclusions";
    setView({ ...defaultReading, ...saved }); setLoaded(id);
  }, [id]);
  useEffect(() => { if (loaded === id) { try { sessionStorage.setItem(`reveal.account-reading.${id}`, JSON.stringify(view)); } catch { /* Storage may be unavailable. */ } } }, [id, loaded, view]);
  return { view, update: (patch: Partial<ReadingState>) => setView(previous => ({ ...previous, ...patch })) };
}
export function AccountView({ id, embedded = false }: { id: string; embedded?: boolean }) {
  const { result, error, reload } = useAccount(id), { view, update } = useReadingState(id);
  const account = result?.document.scientific_accounts?.find(item => item.id === id);
  const claims = account?.component_claims.flatMap(claimId => result?.document.claims?.find(claim => claim.id === claimId) || []) || [];
  const proposition = (claim: Claim) => result?.document.propositions?.find(item => item.id === claim.proposition);
  const groups = ["All", ...new Set(claims.map(claim => relationship(proposition(claim))))];
  const terms = view.search.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const visible = claims.filter(claim => (view.relationship === "All" || relationship(proposition(claim)) === view.relationship) && terms.every(term => `${claim.statement || ""} ${proposition(claim)?.statement || ""} ${relationship(proposition(claim))}`.toLocaleLowerCase().includes(term)));
  const expandedVisible = !view.expanded || visible.some(claim => claim.id === view.expanded);
  useEffect(() => { if (result && !expandedVisible) update({ expanded: null }); }, [result, expandedVisible]); // Filtered-out claims must not reopen invisibly.
  const returnFocus = useRef(false);
  useEffect(() => { if (result && view.expanded && !returnFocus.current) { returnFocus.current = true; requestAnimationFrame(() => document.getElementById(`claim-toggle-${view.expanded}`)?.focus({ preventScroll: true })); } }, [result, view.expanded]);
  if (!result) return <section className="scientific"><p role={error ? "alert" : "status"}>{error || "Loading scientific account…"}</p>{error && <button className="text-button" onClick={reload}>Retry</button>}</section>;
  if (!account) return <p role="alert">The saved document did not contain the requested account.</p>;
  const gap = result.document.knowledge_gaps?.find(item => item.id === account.question) || result.document.questions?.find(item => item.id === account.question);
  const mechanisms = result.document.mechanisms || [];
  const model = result.document.mechanistic_models?.find(item => item.id === account.mechanistic_model);
  const claimsBody = `claims-${id}`, filtersId = `filters-${id}`;
  return <section className={`scientific account-study ${embedded ? "embedded" : ""}`} aria-label="Scientific account">
    {!embedded && <><nav className="account-topbar" aria-label="Account navigation"><Link href="/workspace?tab=accounts">← Your scientific accounts</Link><Link href="/">Search knowledge gaps</Link></nav><header className="account-gap"><p className="gap-source">{gap ? `${gap.id.startsWith("dapper:KnowledgeGap.") ? "Knowledge gap" : "Question"}${gap.scope ? ` · ${gap.scope}` : ""}` : "Scientific account"}</p><h1>{gap?.text || account.name || "Scientific account"}</h1>{!!mechanisms.length && <div className="account-mechanisms" aria-label="Mechanism records">{mechanisms.map(mechanism => <Link key={mechanism.id} className="account-mechanism" href={objectHref(mechanism.id)} title={mechanism.id}>{mechanism.name || "Mechanism record"}</Link>)}</div>}{model && <Link className="account-mechanism" href={objectHref(model.id)}>{model.name || "Mechanistic model"}</Link>}</header></>}
    <section className="account-summary" aria-label="Account synthesis"><Tabs labels={["Conclusions", "Research Statement"]} pending={["queued", "running"].includes(result.research_statement.status) ? "Research Statement" : undefined} value={view.tab} onChange={tab => update({ tab })} /><h2>{account.name || "Scientific account"}</h2><div role="tabpanel" aria-label={view.tab}>{view.tab === "Conclusions" ? <><p className="account-conclusions">{account.closing_remarks || "This account has no separate closing synthesis. Inspect its component claims and scope below."}</p><details className="scope-disclosure"><summary>Approach and scope</summary><p>{account.context}</p>{account.assumptions?.map(assumption => <p key={assumption}>{assumption}</p>)}</details><Attribution document={result.document} ids={account.was_attributed_to} />{!result.coverage.complete && <p className="notice">This source view is bounded. Missing records are not evidence of absence.</p>}</> : <ParagraphView state={result.research_statement} accountId={id} onRefresh={reload} />}</div></section>
    <section className="account-claims-section"><button className="account-claims-toggle" aria-expanded={view.claimsOpen} aria-controls={claimsBody} onClick={() => update({ claimsOpen: !view.claimsOpen })}><span>Associated claims <small>{claims.length}</small></span><span className="claims-toggle-caret" aria-hidden="true">⌄</span></button>
      <div id={claimsBody} className="account-claims-body" hidden={!view.claimsOpen}><span className="sr-only" role="status">{visible.length} of {claims.length} matching claims</span><div className="claim-search-toolbar"><label className="claim-search"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg><input type="search" aria-label="Search associated claims" placeholder="Search claims…" value={view.search} onChange={event => update({ search: event.target.value })} /></label><button className={`claim-filter-toggle ${view.relationship !== "All" ? "has-filter" : ""}`} aria-expanded={view.filtersOpen} aria-controls={filtersId} aria-label={view.relationship === "All" ? "Filters" : `Filters, ${view.relationship} selected`} onClick={() => update({ filtersOpen: !view.filtersOpen })}><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M3 7h4m4 0h10M3 17h10m4 0h4" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="17" r="2" /></svg>Filters{view.relationship !== "All" && <span className="claim-filter-count">1</span>}</button></div><div id={filtersId} className="claim-filters" role="group" aria-label="Filter by relationship" hidden={!view.filtersOpen}>{groups.map(group => <button key={group} aria-pressed={view.relationship === group} onClick={() => update({ relationship: group })}>{group}</button>)}</div>
        <div className="account-claim-results">{visible.map(claim => <article className={`account-claim-card ${view.expanded === claim.id ? "is-expanded" : ""}`} key={claim.id}><button id={`claim-toggle-${claim.id}`} className="account-claim-option" aria-expanded={view.expanded === claim.id} aria-controls={`claim-inline-${claim.id}`} onClick={() => update({ expanded: view.expanded === claim.id ? null : claim.id })}><span className="claim-ref">C{claims.indexOf(claim) + 1}</span><span><span className="claim-option-text">{proposition(claim)?.statement || claim.statement || claim.proposition}</span><span className="claim-option-meta"><span>{relationship(proposition(claim))}</span><span>{claim.has_evidence?.length || 0} evidence items</span></span></span><span className="claim-selection-caret" aria-hidden="true">⌄</span></button><div id={`claim-inline-${claim.id}`} className="claim-inline-body" hidden={view.expanded !== claim.id}><p className="claim-inline-assessment">{claim.statement || "No separate assessment text was supplied."}</p><div className="claim-inline-status"><span>{human(claim.direction)}</span><span>{claim.status ? human(claim.status) : "Review status not specified"}</span></div><div className="claim-inline-sources">{(claim.has_evidence || []).map(evidenceId => { const evidence = result.document.evidence_items?.find(item => item.id === evidenceId); return <p key={evidenceId}>{evidence?.name || evidence?.reference_title || "Evidence item"}<small>{evidence?.direction ? human(evidence.direction) : "Direction not specified"}</small></p>; })}</div><Link className="claim-page-link" href={claimHref(claim.id, id)}>Open claim page <span aria-hidden="true">↗</span></Link></div></article>)}</div>
        {!visible.length && <div className="claim-empty"><p>No claims found</p><span>Try a different search or relationship.</span><button onClick={() => update({ search: "", relationship: "All", expanded: null })}>Clear search and filters</button></div>}
      </div></section>
    <details className="account-records"><summary>Scientific identity and provenance</summary><p>Scientific attribution and content identifiers are independent of workspace ownership.</p><Link href={objectHref(id)}>Account record</Link><p className="idline">{id}</p><Technical label="Schema and exact payload observations" value={{ schema: result.schema, payloads: result.payloads }} />{result.artifacts.map(artifact => <SourceArtifact key={artifact.file.id} artifact={artifact} />)}</details>{error && <p className="error" role="alert">{error}</p>}
  </section>;
}
function Attribution({ document, ids }: { document: Document; ids: string[] }) {
  return <p className="attribution">Attributed to {ids.map(id => { const person = document.persons?.find(item => item.id === id); const organization = document.organizations?.find(item => item.id === id); return person ? person.name || [person.given_name, person.family_name].filter(Boolean).join(" ") || id : organization?.name || id; }).join(", ")}</p>;
}
export function ParagraphView({ state, accountId, onRefresh }: { state: Schema<"ParagraphState">; accountId: string; onRefresh: () => void }) {
  const [result, setResult] = useState<Schema<"ParagraphObjectResult"> | null>(null);
  const [citations, setCitations] = useState<Schema<"CitationRendering"> | null>(null);
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [pending, setPending] = useState(false); const [exporting, setExporting] = useState(false); const [readAttempt, setReadAttempt] = useState(0);
  const [manualCopy, setManualCopy] = useState(""); const copyArea = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (manualCopy) { copyArea.current?.focus(); copyArea.current?.select(); } }, [manualCopy]);
  useEffect(() => {
    setResult(null); setCitations(null); setError(""); setManualCopy("");
    if (!state.paragraph_id) return; let active = true;
    void Promise.all([api.paragraph(state.paragraph_id), api.render(state.paragraph_id)]).then(([paragraph, rendering]) => { if (active) { setResult(paragraph); setCitations(rendering); } }).catch(e => { if (active) setError(messageOf(e)); });
    return () => { active = false; };
  }, [state.paragraph_id, readAttempt]);
  const generate = async () => { setPending(true); setError(""); try { await api.submit({ kind: "paragraph", account_id: accountId, language: "en", target_words: 180 }, crypto.randomUUID()); onRefresh(); } catch (failure) { setError(messageOf(failure)); } finally { setPending(false); } };
  const exportParagraph = async (format: Schema<"ParagraphExport">["format"]) => {
    if (!state.paragraph_id || exporting) return;
    setExporting(true); setError("");
    try {
      const value = await api.export(state.paragraph_id, format);
      if (format === "rich-text") {
        try {
          if (!navigator.clipboard?.write || typeof ClipboardItem === "undefined") throw new Error("Clipboard unavailable");
          await navigator.clipboard.write([new ClipboardItem({ "text/html": new Blob([value.content], { type: "text/html" }), "text/plain": new Blob([value.plain_text || ""], { type: "text/plain" }) })]);
          setManualCopy(""); setNotice("Copied with formatting and references. Paste into Word.");
        } catch {
          if (!value.plain_text) throw new Error("Automatic copy is unavailable. Download Markdown or LaTeX instead.");
          // Render the server's plain-text alternative as a textarea value, never as HTML.
          setManualCopy(value.plain_text); setNotice("Automatic formatted copy is unavailable. Copy the selected plain text below with ⌘C or Ctrl+C.");
        }
      } else {
        const url = URL.createObjectURL(new Blob([value.content], { type: value.media_type })); const anchor = document.createElement("a"); anchor.href = url; anchor.download = value.filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        setNotice(value.required_companions.length ? `Also download ${value.required_companions.join(", ")} for this export.` : `${value.filename} downloaded.`);
      }
      if (value.warnings.length) setNotice(n => `${n} ${value.warnings.join(" ")}`);
    } catch (failure) { setError(messageOf(failure)); } finally { setExporting(false); }
  };
  const paragraph = result?.document.paragraphs?.find(p => p.id === state.paragraph_id);
  const bibliography = Array.from(new Set((paragraph?.citations || []).map(c => `${c.target_id}@${c.citation_metadata_revision}`))).flatMap(key => citations?.bibliography.find(b => `${b.target_id}@${b.citation_metadata_revision}` === key) || []);
  const renderText = () => {
    if (!paragraph) return null;
    // DAPPER offsets count Unicode code points, not UTF-16 units.
    const points = Array.from(paragraph.text); const parts: ReactNode[] = []; let cursor = 0;
    const occurrences = (paragraph.citations || []).map((citation, index) => ({ ...citation, index })).sort((a, b) => a.end - b.end);
    for (const occurrence of occurrences) {
      parts.push(points.slice(cursor, occurrence.end).join("")); cursor = occurrence.end;
      const ref = bibliography.findIndex(b => b.target_id === occurrence.target_id && b.citation_metadata_revision === occurrence.citation_metadata_revision) ?? -1;
      parts.push(<sup key={`${occurrence.index}-${occurrence.end}`}><a href={`#reference-${ref + 1}`} aria-label={`Citation ${ref + 1}`}>[{ref + 1}]</a></sup>);
    }
    parts.push(points.slice(cursor).join("")); return parts;
  };
  return <div className="paragraph-view">
    {!paragraph && (["queued", "running"].includes(state.status) || pending ? <div className="statement-pending" role="status"><Pulse /><div><p>Preparing your research statement</p><small>The scientific account is saved. Its cited paragraph is generated separately.</small></div></div> : state.status === "succeeded" ? error ? <div className="empty"><p>The saved research statement could not be loaded.</p><button onClick={() => setReadAttempt(value => value + 1)}>Retry loading</button></div> : <p role="status">Loading cited research statement…</p> : <div className="empty"><p>{state.status === "failed" ? "Paragraph generation failed. Your accepted scientific account is retained." : state.status === "cancelled" ? "Paragraph generation was stopped." : "A research statement has not been requested."}</p><button onClick={generate} disabled={pending}>{state.status === "not_requested" ? "Generate research statement" : "Retry paragraph generation"}</button></div>)}
    {paragraph && <><p className="research-prose">{renderText()}</p><div className="research-downloads" aria-label="Copy and download research statement"><button className="copy-button" disabled={exporting} onClick={() => void exportParagraph("rich-text")}>Copy for Word</button>{(["markdown", "latex", "bibtex"] as const).map(format => <button className="text-button" key={format} disabled={exporting} onClick={() => void exportParagraph(format)}>{({ markdown: "Markdown", latex: "LaTeX", bibtex: "BibTeX" })[format]}</button>)}</div><p role="status" className="muted">{notice}</p>{manualCopy && <div className="manual-paragraph-copy"><label htmlFor={`copy-${state.paragraph_id}`}>Paragraph and references · plain text</label><textarea id={`copy-${state.paragraph_id}`} ref={copyArea} readOnly value={manualCopy} rows={9} /><div><button className="text-button" onClick={() => { copyArea.current?.focus(); copyArea.current?.select(); }}>Select all text</button><button className="text-button" onClick={() => setManualCopy("")}>Close copy preview</button></div></div>}<p className="paragraph-export-note">For LaTeX, also download references.bib and save both files in the same folder.</p><section className="research-references references"><h3>References <span>{bibliography.length}</span></h3><ol>{bibliography.map((reference, index) => {
      const metadata = result?.citation_metadata.find(item => item.target_id === reference.target_id && item.metadata_revision === reference.citation_metadata_revision);
      const href = reference.target_id.startsWith("dapper:Claim.") ? claimHref(reference.target_id, accountId) : objectHref(reference.target_id);
      const byline = metadata?.byline.map(person => person.display_name || [person.given_name, person.family_name].filter(Boolean).join(" ")).filter(Boolean).join("; ");
      return <li key={`${reference.target_id}-${reference.citation_metadata_revision}`} id={`reference-${index + 1}`} tabIndex={-1}><p>{byline && <span>{byline}{metadata?.issued_date ? ` (${metadata.issued_date.slice(0, 4)})` : ""}. </span>}<Link className="reference-title" href={href} aria-label="Inspect cited record">{metadata?.title || reference.text}</Link></p><div className="reference-meta">{metadata ? `DAPPER ${metadata.target_class} · ${metadata.issued_date || "Date unavailable"} · ` : ""}Metadata revision {reference.citation_metadata_revision}</div><details className="formatted-reference"><summary>Formatted reference</summary><p>{reference.text}</p></details></li>;
    })}</ol></section><details className="paragraph-technical technical-disclosure"><summary>Paragraph record and citation metadata</summary><p className="idline">{paragraph.id}</p><Link href={objectHref(paragraph.id)}>Inspect paragraph record</Link><pre className="scientific-json">{JSON.stringify({ paragraph, citation_metadata: result?.citation_metadata, rendering_manifest: citations?.rendering_manifest, schema: result?.schema }, null, 2)}</pre></details></>}
    {error && <p role="alert" className="error">{error}</p>}
  </div>;
}

function Technical({ label = "DAPPER record", value }: { label?: string; value: unknown }) {
  return <details className="technical-disclosure"><summary>{label}</summary><Record value={value} /></details>;
}
function safeDownload(value?: string | null) {
  if (!value || typeof window === "undefined") return null;
  try { const url = new URL(value, window.location.origin); return url.origin === window.location.origin && /^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(url.pathname) ? url.pathname : null; } catch { return null; }
}
function SourceArtifact({ artifact }: { artifact: Schema<"ArtifactAccess"> }) {
  const href = safeDownload(artifact.download_url), file = artifact.file;
  return <section className="source-artifact-card"><div className="source-artifact-heading"><div><Link href={objectHref(file.id)}>{file.filename || file.name || "Source file"}</Link><small>{human(artifact.availability)}{file.size_in_bytes != null ? ` · ${file.size_in_bytes.toLocaleString()} bytes` : ""} · {human(artifact.verification)}</small></div>{href && artifact.availability === "available" && <ArtifactDownload href={href} filename={file.filename || "source-artifact"} />}</div>{file.description && <p>{file.description}</p>}<Technical label="Source record and checksum" value={artifact} /></section>;
}
function ScoreList({ document, ids }: { document: Document; ids?: string[] | null }) {
  if (!ids?.length) return null;
  return <div className="evidence-metrics">{ids.map(id => { const score = document.claim_scores?.find(item => item.id === id); return score ? <div className="metric-observation" key={id}><span>{score.interpretation}<small>{score.metric} · {human(score.score_kind)}</small></span><b>{score.value}</b></div> : <p className="small-note" key={id}>Score not included in this view: {id}</p>; })}</div>;
}
function EvidenceView({ claim, result, accountId }: { claim: Claim; result: Schema<"ClaimResult">; accountId: string | null }) {
  const document = result.document;
  return <><h2>Evidence for this proposition</h2>{!claim.has_evidence?.length && <p className="small-note">This claim has no linked evidence-use records.</p>}{(claim.has_evidence || []).map(id => {
    const item = document.evidence_items?.find(evidence => evidence.id === id);
    if (!item) return <p className="notice" key={id}>This evidence record is not included in the authorized view: {id}</p>;
    const sourceClaims = (item.source_claims || []).map(sourceId => document.claims?.find(source => source.id === sourceId));
    const fileIds = new Set([...(item.was_derived_from || []), ...sourceClaims.flatMap(source => source?.was_derived_from || [])]);
    const artifacts = result.artifacts.filter(artifact => fileIds.has(artifact.file.id));
    return <section className="claim-evidence" key={id}><div className="evidence-heading"><h3>{item.name || item.reference_title || "Evidence item"}</h3>{item.direction && <span className="evidence-direction">{human(item.direction)}</span>}</div>{item.explanation && <p>{item.explanation}</p>}{item.context && <p className="small-note">{item.context}</p>}{item.assumptions?.length ? <details><summary>Interpretation assumptions</summary>{item.assumptions.map(assumption => <p key={assumption}>{assumption}</p>)}</details> : null}{sourceClaims.map((source, index) => <section className="source-assessment" key={item.source_claims![index]}><h4>Source assessment</h4>{source ? <><p>{source.statement || document.propositions?.find(proposition => proposition.id === source.proposition)?.statement || "No assessment text supplied."}</p><ScoreList document={document} ids={source.has_score} />{source.source_locator && <p className="source-locator">{source.source_locator}</p>}<Link className="claim-page-link" href={claimHref(source.id, accountId)}>Inspect source claim <span aria-hidden="true">↗</span></Link></> : <p className="small-note">Not included in this authorized view: {item.source_claims![index]}</p>}</section>)}{item.snippet && <details><summary>Observed source excerpt</summary><blockquote className="source-snippet">{item.snippet}</blockquote>{item.reference && <p className="small-note">{item.reference}</p>}</details>}{artifacts.map(artifact => <SourceArtifact key={artifact.file.id} artifact={artifact} />)}{[...fileIds].filter(fileId => !artifacts.some(artifact => artifact.file.id === fileId)).map(fileId => <p className="small-note" key={fileId}>Source record <Link href={objectHref(fileId)}>{entityLabel(document, fileId)}</Link> · bytes not included in this view</p>)}<Technical label="EvidenceItem record" value={item} /></section>;
  })}</>;
}
function ProvenanceView({ claim, result }: { claim: Claim; result: Schema<"ClaimResult"> }) {
  const document = result.document, activity = document.activities?.find(item => item.id === claim.was_generated_by);
  return <><h2>Follow the evidence to its source</h2><p className="small-note">{result.coverage.complete ? "Complete authorized upstream view." : "Bounded upstream view. Missing records are not evidence of absence."}</p>{result.artifacts.length ? result.artifacts.map(artifact => <SourceArtifact key={artifact.file.id} artifact={artifact} />) : <p className="small-note">No downloadable source artifacts are included in this view.</p>}<section className="construction-provenance"><h3>Construction provenance</h3>{activity ? <><p>{activity.name || "Generating activity"}</p>{activity.description && <p>{activity.description}</p>}<dl className="scientific-keyval">{activity.software_name && <><dt>Software</dt><dd>{activity.software_name}{activity.software_version ? ` ${activity.software_version}` : ""}</dd></>}{activity.generated_at_time && <><dt>Generated</dt><dd><time dateTime={activity.generated_at_time}>{activity.generated_at_time}</time></dd></>}</dl><Technical label="Activity record and execution details" value={activity} /></> : <p className="small-note">The generating activity is not included in this view. <Link href={objectHref(claim.was_generated_by)}>Inspect activity</Link></p>}</section>{document.gene_sets?.length ? <section className="referenced-records"><h3>Referenced gene sets</h3>{document.gene_sets.map(set => <Link key={set.id} href={objectHref(set.id)}>{set.name || set.id}</Link>)}</section> : null}<details className="technical-disclosure"><summary>Exact payload observations and traversal coverage</summary><Record value={result.schema} /><Record value={result.payloads} /><Record value={result.coverage} /></details><Technical label="Claim record" value={claim} /></>;
}
export function ClaimView({ id }: { id: string }) {
  const [result, setResult] = useState<Schema<"ClaimResult"> | null>(null), [error, setError] = useState("");
  const [tab, setTab] = useState("Assessment"), [accountId, setAccountId] = useState<string | null>(null), [refresh, setRefresh] = useState(0);
  useEffect(() => { let active = true; setAccountId(new URLSearchParams(window.location.search).get("account")); setResult(null); setError(""); void api.claim(id).then(value => { if (active) setResult(value); }).catch(failure => { if (active) setError(messageOf(failure)); }); return () => { active = false; }; }, [id, refresh]);
  if (!result) return <main id="main" className="reading-page claim-page"><p role={error ? "alert" : "status"}>{error || "Loading claim…"}</p>{error && <button className="text-button" onClick={() => setRefresh(value => value + 1)}>Retry</button>}</main>;
  const claim = result.document.claims?.find(item => item.id === id), proposition = result.document.propositions?.find(item => item.id === claim?.proposition);
  if (!claim) return <main id="main" className="reading-page"><p role="alert">The saved document did not contain this claim.</p></main>;
  const account = result.document.scientific_accounts?.find(item => item.id === accountId);
  return <main id="main" className="reading-page claim-page account-study"><nav className="account-topbar" aria-label="Claim navigation"><Link href={accountId ? accountHref(accountId) : "/workspace?tab=accounts"}>← Back to scientific account</Link><Link href={objectHref(id)}>DAPPER record</Link></nav><header className="claim-page-heading"><p className="claim-page-kind">Proposition</p><h1>{proposition?.statement || claim.statement || claim.proposition}</h1>{accountId && <p className="claim-page-account">From <Link href={accountHref(accountId)}>{account?.name || "scientific account"}</Link></p>}</header><section className="claim-detail-panel" aria-label="Claim details"><Tabs labels={["Assessment", "Proposition", "Evidence", "Provenance"]} value={tab} onChange={setTab} /><div role="tabpanel" aria-label={tab}>
    {tab === "Assessment" && <><h2>The assessment</h2><p className="detail-proposition">{claim.statement || "No separate assessment text was recorded."}</p><dl className="scientific-keyval"><dt>Assessment</dt><dd>{human(claim.direction)}</dd><dt>Review status</dt><dd>{human(claim.status)}</dd><dt>Evidence</dt><dd>{claim.has_evidence?.length || 0} linked items</dd></dl><ScoreList document={result.document} ids={claim.has_score} /><Attribution document={result.document} ids={claim.was_attributed_to} /><button className="text-button" onClick={() => setTab("Evidence")}>Inspect the evidence</button><Technical label="Claim record" value={claim} /></>}
    {tab === "Proposition" && <><h2>What is being assessed</h2><p className="detail-proposition">{proposition?.statement || "The proposition is not included in this view."}</p>{proposition && <><dl className="scientific-keyval"><dt>Content</dt><dd>{human(proposition.proposition_kind)}</dd>{proposition.subject_entity && <><dt>Subject</dt><dd>{entityLabel(result.document, proposition.subject_entity)}</dd></>}{proposition.relation && <><dt>Relationship</dt><dd>{human(proposition.relation.split(/[\/#:]/).at(-1)?.replaceAll("-", " "))}</dd></>}{proposition.object_entity && <><dt>Object</dt><dd>{entityLabel(result.document, proposition.object_entity)}</dd></>}{proposition.negated != null && <><dt>Negated</dt><dd>{proposition.negated ? "Yes" : "No"}</dd></>}</dl>{proposition.scope && <><h3>Scope</h3><p>{proposition.scope}</p></>}<Technical label="Proposition record" value={proposition} /></>}</>}
    {tab === "Evidence" && <EvidenceView claim={claim} result={result} accountId={accountId} />}
    {tab === "Provenance" && <ProvenanceView claim={claim} result={result} />}
  </div></section></main>;
}
function ArtifactDownload({ href, filename }: { href: string; filename: string }) {
  const [status, setStatus] = useState("");
  const download = async () => {
    setStatus("Downloading…");
    try {
      const response = await fetch(href, { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("The captured artifact is unavailable for this workspace.");
      const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement("a");
      anchor.href = url; anchor.download = filename.replace(/[/\\]/g, "_"); anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); setStatus("Downloaded.");
    } catch (failure) { setStatus(messageOf(failure)); }
  };
  return <><button className="text-button" onClick={() => void download()} disabled={status === "Downloading…"}>Download captured artifact</button>{status && <small role="status">{status}</small>}</>;
}
export function Record({ value }: { value: unknown }) {
  if (value == null) return null;
  if (Array.isArray(value)) return <div className="record-list">{value.map((item, index) => <Record key={index} value={item} />)}</div>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  return <dl className="record">{Object.entries(value).map(([key, item]) => {
    let download: string | null = null;
    if (key === "download_url" && typeof item === "string" && typeof window !== "undefined") {
      try { const url = new URL(item, window.location.origin); if (url.origin === window.location.origin && /^\/api\/backend\/v1\/artifacts\/[a-f0-9]{64}$/.test(url.pathname)) download = url.pathname; } catch { /* show invalid source values as text */ }
    }
    const file = (value as { file?: { filename?: string } }).file;
    return <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{download ? <ArtifactDownload href={download} filename={file?.filename || "source-artifact"} /> : typeof item === "object" && item !== null ? <Record value={item} /> : String(item ?? "Not recorded")}</dd></div>;
  })}</dl>;
}
