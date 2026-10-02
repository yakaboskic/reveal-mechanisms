"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, messageOf, type Schema } from "@/lib/client";
import { sourceDownloadPath } from "@/lib/source-download";
import { useIdentity } from "./Session";
import { PublicationSignIn } from "./PublicationSignIn";
import { LoadingStatus, LoadingSurface } from "./LoadingSurface";
import { ReferenceArchiveBanner, ReferenceBadge, requestSettings } from "./ReferenceArchive";
import "./analysis-outcome.css";

export const outcomeHref = (id: string) => `/analyses/${encodeURIComponent(id)}`;
export function outcomeAuthor(outcome: Pick<Schema<"AnalysisOutcome">, "attribution">) {
  return outcome.attribution?.display_name || (outcome.attribution?.principal_kind === "anonymous" ? "Anonymous researcher" : "Researcher");
}

export function JobOutcome({ jobId }: { jobId: string }) {
  const { me, ready } = useIdentity();
  const [record, setRecord] = useState<{ binding: string; value: Schema<"AnalysisOutcome"> } | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const binding = `${me?.user_id || "visitor"}:${jobId}`;
  useEffect(() => {
    let active = true; setError("");
    if (ready && me) void api.jobOutcome(jobId).then(value => { if (active) setRecord({ binding, value }); }).catch(failure => { if (active) setError(messageOf(failure)); });
    return () => { active = false; };
  }, [binding, ready, retry]);
  const outcome = record?.binding === binding ? record.value : null;
  if (ready && !me) return null;
  return <section className="outcome-job" aria-label="Saved exploration">
    {outcome ? <><div className="outcome-status"><span aria-hidden="true">✓</span> Exploration saved</div><ReferenceBadge archive={outcome.archive} /><h2>No supported scientific account</h2><p className="outcome-preview">{outcome.summary}</p><Link href={outcomeHref(outcome.id)}>View findings and evidence gaps <span aria-hidden="true">↗</span></Link><small>{outcome.publication.visibility === "public" ? "Published against this knowledge gap." : "Private to your workspace. Open the exploration to publish it for other researchers."}</small></> : <LoadingSurface compact title={error ? "The saved exploration could not be opened" : "Opening the exploration record"} description="Retrieving the findings and evidence gaps retained from this analysis." error={error} onRetry={() => setRetry(value => value + 1)} skeleton="none" />}
  </section>;
}

function OutcomePublication({ outcome, onRefresh }: { outcome: Schema<"AnalysisOutcome">; onRefresh: () => void }) {
  const { me, ready } = useIdentity();
  const signedIn = ready && me?.principal_kind === "registered";
  const [state, setState] = useState(outcome.publication), [confirm, setConfirm] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const pending = useRef<{ body: Schema<"PublicationInput">; key: string } | null>(null);
  const mounted = useRef(true), inflight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setState(current => outcome.publication.version >= current.version ? outcome.publication : current); }, [outcome.publication]);
  async function save(visibility: "public" | "private", retry = false) {
    if (!state.can_manage || inflight.current) return;
    if (visibility === "public" && !signedIn) { setConfirm(false); return; }
    if (!retry && pending.current && (pending.current.body.visibility !== visibility || pending.current.body.expected_version !== state.version)) {
      setError("The previous publication change is still unconfirmed. Retry it before choosing another change."); return;
    }
    if (!pending.current) pending.current = { body: { visibility, expected_version: state.version }, key: crypto.randomUUID() };
    const attempt = pending.current; inflight.current = true; setBusy(true); setError("");
    try {
      const publication = await api.setOutcomePublication(outcome.id, attempt.body, attempt.key);
      if (!mounted.current) return;
      setState(publication); pending.current = null; setConfirm(false); onRefresh();
    } catch (failure) {
      if (!mounted.current) return;
      if (failure instanceof ApiError && failure.status === 409) {
        pending.current = null;
        try { const current = await api.outcomePublication(outcome.id); if (mounted.current) setState(current); } catch { /* Keep conflict visible. */ }
        if (mounted.current) setError("Publication changed in another session. Review its visibility and try again.");
      } else setError(messageOf(failure));
    } finally { inflight.current = false; if (mounted.current) setBusy(false); }
  }
  if (!state.can_manage) return null;
  return <section className="outcome-publication" aria-label="Exploration publication">
    <div><h2>{state.visibility === "public" ? "Published exploration" : "Share what was explored"}</h2><p>{state.visibility === "public" ? "Other researchers can find this exploration under its knowledge gap." : "Publish this record to help others see what was tried and which evidence is still missing."}</p></div>
    {!signedIn && state.visibility !== "public" && <PublicationSignIn kind="exploration" />}
    {!confirm && (signedIn || state.visibility === "public") && <button disabled={busy} onClick={() => state.visibility === "public" ? void save("private") : setConfirm(true)}>{state.visibility === "public" ? "Unpublish" : "Publish exploration…"}</button>}
    {signedIn && confirm && <div className="outcome-publish-confirm"><p>The question, selected mechanisms, author attribution, findings, limitations, source references, and captured source evidence will be public and downloadable. Job activity, workspace drafts, and the complete private evidence package stay private.</p><div><button disabled={busy} onClick={() => void save("public")}>Publish exploration</button><button disabled={busy} onClick={() => setConfirm(false)}>Cancel</button></div></div>}
    {busy && <LoadingStatus>Saving publication settings…</LoadingStatus>}
    {error && <div className="error" role="alert">{error}{pending.current && <button disabled={busy} onClick={() => void save(pending.current!.body.visibility, true)}>Retry</button>}</div>}
  </section>;
}

function Findings({ title, items }: { title: string; items: string[] }) {
  return items.length ? <section className="outcome-findings"><h2>{title}</h2><ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul></section> : null;
}

function SourceDownloads({ artifacts }: { artifacts: Schema<"ArtifactAccess">[] }) {
  if (!artifacts.length) return null;
  return <details className="outcome-sources"><summary>Captured source evidence ({artifacts.length})</summary><ul>{artifacts.map((artifact, index) => {
    const href = typeof window === "undefined" ? null : sourceDownloadPath(artifact.download_url, window.location.origin);
    return <li key={index}>{href ? <a href={href} download>{artifact.file.filename || artifact.file.id}</a> : <span>{artifact.file.filename || artifact.file.id}</span>}</li>;
  })}</ul></details>;
}

export function AnalysisOutcomeView({ id }: { id: string }) {
  const { me, ready, status } = useIdentity();
  const binding = `${me?.user_id || "visitor"}:${status.canClaim}:${id}`;
  const [record, setRecord] = useState<{ binding: string; value: Schema<"AnalysisOutcome"> } | null>(null);
  const [failure, setFailure] = useState<{ binding: string; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setFailure(null);
    if (ready) void api.analysisOutcome(id).then(value => { if (active) setRecord({ binding, value }); }).catch(error => { if (active) setFailure({ binding, message: messageOf(error) }); });
    return () => { active = false; };
  }, [binding, ready, retry]);
  const outcome = record?.binding === binding ? record.value : null;
  const error = failure?.binding === binding ? failure.message : "";
  if (!outcome) return <LoadingSurface title={error ? "Exploration unavailable" : "Opening the exploration"} description="Retrieving the question, findings, and evidence gaps." error={error} onRetry={() => setRetry(value => value + 1)} skeleton="record" />;
  return <article className="outcome-page">
    <nav><Link href={`/?gap=${encodeURIComponent(outcome.knowledge_gap.id)}`}>← Knowledge gap</Link>{outcome.job_id && outcome.publication.can_manage && <Link href={`/?job=${encodeURIComponent(outcome.job_id)}`}>View private activity</Link>}</nav>
    <header><p className="outcome-status"><span aria-hidden="true">✓</span> Explored · Evidence insufficient</p><h1>{outcome.knowledge_gap.text}</h1><p className="outcome-attribution">Explored by <strong>{outcomeAuthor(outcome)}</strong> on <time dateTime={outcome.created_at}>{new Date(outcome.created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}</time>{outcome.publication.visibility === "public" && <span>Published exploration</span>}</p></header>
    {/* The anchors below already render from frozen data; the banner adds the reference snapshot. */}
    {outcome.archive && <ReferenceArchiveBanner archive={outcome.archive} subject="exploration" gapId={outcome.archive.gap?.id || outcome.source_gap.id} settings={requestSettings(outcome.archive, { selected_kgs: outcome.selected_kgs })} anchorsOpen={false} />}
    <section className="outcome-synthesis"><h2>No supported scientific account</h2><p>{outcome.summary}</p><p className="outcome-scope">{outcome.scope_note}</p></section>
    <section className="outcome-anchors" aria-label="Mechanisms explored"><h2>Mechanisms explored</h2><div>{outcome.anchors.map((anchor, index) => <span className="outcome-anchor" key={`${anchor.source_id}:${index}`}><span>{anchor.name}</span>{anchor.trait && <small>{anchor.trait}</small>}</span>)}</div></section>
    <Findings title="What was explored" items={outcome.explored_topics} />
    <Findings title="Evidence still needed" items={outcome.missing_evidence} />
    <Findings title="Limits of this analysis" items={outcome.limitations} />
    <Findings title="Possible next steps" items={outcome.next_steps} />
    <details className="outcome-details"><summary>Full recorded explanation</summary><p>{outcome.reason}</p>{outcome.record_format === "legacy" && <small>This explanation was recovered from the original saved outcome. Its wording has been preserved.</small>}</details>
    <details className="outcome-details"><summary>Sources and analysis scope</summary><dl><div><dt>Gap source revision</dt><dd>{outcome.source_gap.source_revision}</dd></div><div><dt>Selected knowledge graphs</dt><dd>{outcome.selected_kgs.join(", ") || "None"}</dd></div><div><dt>Evidence package SHA-256</dt><dd>{outcome.provenance.evidence_package_sha256}</dd></div><div><dt>Saved outcome SHA-256</dt><dd>{outcome.provenance.outcome_sha256}</dd></div></dl><p>These references identify the evidence retained for this attempt. An absence or unavailable query is limited to that scope.</p><SourceDownloads artifacts={outcome.provenance.source_artifacts} /><details><summary>Captured source references</summary><pre>{JSON.stringify({ source_bindings: outcome.provenance.source_bindings, evidence_refs: outcome.provenance.evidence_refs, graph_queries: outcome.provenance.graph_queries, coverage: outcome.provenance.coverage }, null, 2)}</pre></details></details>
    {error && <LoadingSurface compact title="The exploration could not be refreshed" error={error} onRetry={() => setRetry(value => value + 1)} skeleton="none" />}
    <OutcomePublication key={binding} outcome={outcome} onRefresh={() => setRetry(value => value + 1)} />
  </article>;
}
