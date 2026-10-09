"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useIdentity, ProviderButtons } from "./Session";
import { LoadingSurface } from "./LoadingSurface";
import { onWorkspaceChange } from "@/lib/workspace-events";
import { onPageReturn } from "@/lib/page-return";
import { createLightningRefresher } from "@/lib/lightning-refresh";
import { lightningApi, lightningEnabled, lightningAccessLost, lightningDispatchRejected, lightningPending, lightningBrief, lightningStatusLabel, lightningAssessmentLabel,
  lightningContinuationHref, rememberLightningHandoff, restoreLightningHandoff, rememberLightningBrief, restoreLightningBrief,
  type LightningAudit, type LightningHandoff } from "@/lib/lightning-audit";
import "./lightning-audit.css";

const message = (error: unknown) => error instanceof Error ? error.message : "The audit is unavailable. Refresh to check its saved status.";
const date = (value: string) => new Date(value).toLocaleString();
const evidenceId = (id: string) => `audit-evidence-${encodeURIComponent(id)}`;

function AuditList({ title, items }: { title: string; items: string[] }) {
  return <section><h2>{title}</h2>{items.length ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="lightning-muted">None identified in this package.</p>}</section>;
}

export function LightningResult({ audit }: { audit: LightningAudit }) {
  const result = audit.status === "succeeded" ? audit.result : null;
  if (!result) return null;
  const coverage = audit.coverage;
  const missing = Array.isArray(coverage?.missing) ? coverage.missing.filter((item): item is string => typeof item === "string") : [];
  const truncations = Array.isArray(coverage?.truncations) ? coverage.truncations : [];
  return <div className="lightning-result">
    <section className={`lightning-summary is-${result.assessment}`} aria-labelledby="lightning-assessment-heading">
      <span className="lightning-eyebrow">Initial evidence assessment</span><h2 id="lightning-assessment-heading">{lightningAssessmentLabel[result.assessment]}</h2>
      <p>{result.summary}</p>
    </section>
    <section><h2>Supporting observations</h2>{result.observations.length ? <ol className="lightning-observations">{result.observations.map((observation, index) => <li key={index}>
      <p>{observation.text}</p>{observation.evidence_refs.length > 0 && <div className="lightning-evidence-links" aria-label="Supporting evidence">{observation.evidence_refs.map(ref => <a key={ref} href={`#${evidenceId(ref)}`}>{ref}</a>)}</div>}
    </li>)}</ol> : <p className="lightning-muted">No supporting observations identified.</p>}</section>
    <section><h2>Proposed direction</h2><p className="lightning-preserve">{result.recommended_direction}</p></section>
    <AuditList title="Missing evidence" items={result.missing_evidence} />
    <AuditList title="Next steps" items={result.next_steps} />
    <section><h2>Evidence scope</h2><p>This assessment uses the supplied evidence package. No fresh literature or connected-graph searches were performed.</p>
      {!!result.limitations.length && <ul>{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      {!!missing.length && <><h3>Unavailable in the package</h3><ul>{missing.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
      {!!truncations.length && <p>{truncations.length} {truncations.length === 1 ? "source was" : "sources were"} shortened to fit the evidence package. The retained excerpts are shown below.</p>}
      {coverage && <details className="lightning-source"><summary>Sampling and coverage details</summary><pre>{JSON.stringify(coverage, null, 2)}</pre></details>}
    </section>
    <section aria-labelledby="lightning-evidence-heading"><h2 id="lightning-evidence-heading">Retained evidence</h2>
      <p className="lightning-muted">References point to the exact evidence retained for this assessment.</p>
      {audit.evidence_references.map(ref => <details key={ref.id} id={evidenceId(ref.id)} className="lightning-source">
        <summary><strong>{ref.id}</strong> · {ref.label}</summary><div className="lightning-source-content">
          <p className="lightning-preserve">{typeof ref.value === "string" ? ref.value : JSON.stringify(ref.value, null, 2)}</p>
          <dl><dt>Snapshot location</dt><dd>{ref.pointer}</dd></dl>
          {Object.keys(ref.source).length > 0 && <><h3>Source provenance</h3><pre>{JSON.stringify(ref.source, null, 2)}</pre></>}
        </div>
      </details>)}
    </section>
  </div>;
}

export function LightningAuditPage({ id }: { id: string }) {
  const { me, ready } = useIdentity();
  if (!ready) return <main id="main" className="lightning-page"><LoadingSurface title="Opening your workspace" /></main>;
  if (!me) return <main id="main" className="lightning-page"><nav><Link href="/workspace?tab=runs">← Research runs</Link></nav><h1>Sign in to open this audit</h1><p>Lightning audits are private to the workspace that created them.</p><ProviderButtons /></main>;
  return <AuditView key={`${me.user_id}:${id}`} id={id} owner={me.user_id} />;
}

function AuditView({ id, owner }: { id: string; owner: string }) {
  const router = useRouter();
  const [audit, setAudit] = useState<LightningAudit | null>(null), [error, setError] = useState(""), [attempt, setAttempt] = useState(0);
  const [brief, setBrief] = useState(""), [handoff, setHandoff] = useState<LightningHandoff | null>(null);
  const [busy, setBusy] = useState(false), [launchError, setLaunchError] = useState("");
  const [now, setNow] = useState(Date.now);
  const initialized = useRef(false), alive = useRef(false), flight = useRef(false), receipt = useRef<LightningHandoff | null>(null);
  useEffect(() => {
    alive.current = true;
    receipt.current = restoreLightningHandoff(owner, id); setHandoff(receipt.current);
    const saved = receipt.current?.research_direction ?? restoreLightningBrief(owner, id);
    if (saved !== null) { initialized.current = true; setBrief(saved); }
    const refresher = createLightningRefresher({
      hidden: () => document.visibilityState === "hidden",
      load: (signal, wait) => lightningApi.get(id, signal, wait),
      onAudit: value => {
        setAudit(value); setError(""); setNow(Date.now());
        if (!initialized.current && value.result) { initialized.current = true; setBrief(lightningBrief(value)); }
      },
      onError: failure => { setError(message(failure)); if (lightningAccessLost(failure)) { initialized.current = false; setAudit(null); setBrief(""); } },
    });
    const unsubscribe = onWorkspaceChange((reset, event) => {
      if (reset) {
        refresher.stop(); initialized.current = false; setAudit(null); setBrief("");
        setError("Workspace access changed. Refresh to reopen this private audit."); return;
      }
      if (!event || event.collections.some(collection => ["audits", "identity", "catalog"].includes(collection))) {
        if (!event || event.entity_id === id || event.collections.some(collection => ["identity", "catalog"].includes(collection))) refresher.refresh();
      }
    });
    const visibility = () => { if (document.visibilityState === "hidden") refresher.visibility(); };
    document.addEventListener("visibilitychange", visibility);
    const removeReturn = onPageReturn(() => { setNow(Date.now()); refresher.visibility(); });
    refresher.refresh();
    return () => { alive.current = false; refresher.stop(); unsubscribe(); removeReturn(); document.removeEventListener("visibilitychange", visibility); };
  }, [id, owner, attempt]);
  useEffect(() => {
    if (!audit || audit.status !== "succeeded") return;
    const remaining = new Date(audit.continuation_expires_at).getTime() - Date.now();
    if (remaining <= 0) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(remaining + 10, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [audit?.continuation_expires_at, audit?.status, now]);

  async function continueAudit(mode?: "online" | "local") {
    if (flight.current || !audit || audit.status !== "succeeded") return;
    const dispatch = receipt.current || (mode ? { mode, research_direction: brief.trim(), key: crypto.randomUUID() } : null);
    if (!dispatch || !dispatch.research_direction || dispatch.research_direction.length > 6000) return;
    flight.current = true; setBusy(true); setLaunchError("");
    receipt.current = dispatch; setHandoff(dispatch); rememberLightningHandoff(owner, id, dispatch);
    try {
      const run = await lightningApi.continue(id, { mode: dispatch.mode, research_direction: dispatch.research_direction }, dispatch.key);
      if (!alive.current) return;
      rememberLightningHandoff(owner, id, null); receipt.current = null; setHandoff(null);
      router.push(lightningContinuationHref(run));
    } catch (failure) {
      if (!alive.current) return;
      setLaunchError(message(failure));
      // Admission rejection proves this dispatch did not create a run. Ambiguous failures keep the exact receipt.
      if (lightningDispatchRejected(failure)) {
        rememberLightningHandoff(owner, id, null); receipt.current = null; setHandoff(null);
      }
      if (lightningAccessLost(failure)) { initialized.current = false; setAudit(null); setBrief(""); setError(message(failure)); }
    } finally { flight.current = false; if (alive.current) setBusy(false); }
  }

  const expired = audit ? new Date(audit.continuation_expires_at).getTime() <= now : false;
  return <main id="main" className="lightning-page">
    <nav className="lightning-navigation"><Link href="/workspace?tab=runs">← Research runs</Link><span>Lightning audit · Private</span></nav>
    {error && <p className="lightning-error" role="alert">{error} <button onClick={() => setAttempt(value => value + 1)}>Refresh saved audit</button></p>}
    {!audit && !error && <LoadingSurface title="Opening your audit" description="Retrieving the saved assessment and evidence." />}
    {audit && <>
      <header className="lightning-header"><span className="lightning-eyebrow">Lightning audit</span><h1>{audit.question.text}</h1>
        <div className="lightning-meta"><span>{lightningStatusLabel[audit.status]}</span><time dateTime={audit.created_at}>{date(audit.created_at)}</time></div>
        <p>An initial assessment of the supplied evidence. A research agent can investigate the direction and establish scientific support.</p>
      </header>
      {lightningPending(audit) && <LoadingSurface title={lightningStatusLabel[audit.status]} description="Your audit is saved. You can return here while the assessment completes." skeleton="record" />}
      {(audit.status === "failed" || audit.status === "interrupted") && <section className="lightning-failure"><h2>{lightningStatusLabel[audit.status]}</h2>
        <p role="alert">{audit.error?.detail || "This assessment did not complete. The submitted evidence is retained."}</p>
        {lightningEnabled && <Link href={`/?gap=${encodeURIComponent(audit.question.id)}`}>Start a new audit from this gap →</Link>}
      </section>}
      <LightningResult audit={audit} />
      {audit.status === "succeeded" && <section className="lightning-handoff" aria-labelledby="lightning-handoff-heading">
        <span className="lightning-eyebrow">Continue the investigation</span><h2 id="lightning-handoff-heading">Research brief</h2>
        <p>Review this direction before starting an agent. The new run receives the original inputs and retained audit evidence.</p>
        <label htmlFor="lightning-research-brief">Direction and next steps for the agent</label>
        <textarea id="lightning-research-brief" rows={9} maxLength={6000} value={brief} disabled={busy || !!handoff || expired || !lightningEnabled}
          onChange={event => { const value = event.target.value; initialized.current = true; setBrief(value); rememberLightningBrief(owner, id, value); }} />
        {launchError && <p className="lightning-error" role="alert">{launchError}</p>}
        {handoff ? <div className="lightning-actions"><p role="status">{busy ? "Opening your research run…" : "A submission is awaiting confirmation. Check the same submission before editing or starting another run."}</p>
          <button disabled={busy} onClick={() => void continueAudit()}>Check submission</button></div>
          : expired ? <p>The continuation window has ended. <Link href={`/?gap=${encodeURIComponent(audit.question.id)}`}>Start a fresh audit with current evidence.</Link></p>
          : lightningEnabled ? <><div className="lightning-actions"><button disabled={busy || !brief.trim()} onClick={() => void continueAudit("online")}>Run online</button><button disabled={busy || !brief.trim()} onClick={() => void continueAudit("local")}>Use my local agent</button></div>
            <p className="lightning-muted">Available until {date(audit.continuation_expires_at)}. <Link href={`/?gap=${encodeURIComponent(audit.question.id)}`}>Choose new inputs</Link> to assess a different evidence package.</p></>
          : <p>Starting research from an audit is currently unavailable.</p>}
      </section>}
      {!!audit.continuations.length && <section className="lightning-following"><h2>Research started from this audit</h2><ul>{audit.continuations.map(run => <li key={`${run.mode}:${run.id}`}><Link href={lightningContinuationHref(run)}>{run.mode === "local" ? "Local" : "Online"} research · {date(run.created_at)} →</Link></li>)}</ul></section>}
      <details className="lightning-source lightning-provenance"><summary>Audit provenance</summary><dl><dt>Model</dt><dd>{audit.provenance.model}</dd><dt>Prompt version</dt><dd>{audit.provenance.prompt_version}</dd>
        <dt>Evidence snapshot checksum</dt><dd>{audit.provenance.source_state_sha256 || "Not yet available"}</dd><dt>Model input checksum</dt><dd>{audit.provenance.request_sha256 || "Not yet available"}</dd>
        {audit.usage && <><dt>Token usage</dt><dd>{audit.usage.input_tokens.toLocaleString()} input · {audit.usage.output_tokens.toLocaleString()} output</dd></>}</dl></details>
    </>}
  </main>;
}
