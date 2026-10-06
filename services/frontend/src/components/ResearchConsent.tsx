"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { signIn } from "next-auth/react";
import { ProviderButtons, useIdentity } from "./Session";
import { localWorkApi, localWorkTitle, type LocalWork } from "@/lib/local-work";
import { consentLookup, consentPagePath, consentRedirect, consentScopeLabel, researchConsentApi, type ConsentLookup, type ResearchConsent } from "@/lib/research-consent";
import { invalidateWorkspace } from "@/lib/workspace-events";
import "./local-work.css";
import "./research-consent.css";

const errorMessage = (error: unknown) => error instanceof Error ? error.message : "This connection could not be completed. Please retry.";
export function ResearchConsentPage() {
  const params = useSearchParams(), router = useRouter(), { me, ready, status, refresh } = useIdentity();
  let lookup: ConsentLookup | null = null, inputError = "";
  try { lookup = consentLookup(params); } catch (error) { inputError = errorMessage(error); }
  const lookupKey = lookup ? new URLSearchParams(lookup).toString() : "";
  const registered = me?.principal_kind === "registered", scope = `${me?.user_id || ""}:${me?.principal_kind || ""}:${lookupKey}`;
  const [snapshot, setSnapshot] = useState<{ scope: string; value: ResearchConsent } | null>(null), [works, setWorks] = useState<LocalWork[]>([]), [selected, setSelected] = useState("");
  const request = snapshot?.scope === scope ? snapshot.value : null;
  const [error, setError] = useState(""), [code, setCode] = useState(""), [busy, setBusy] = useState(""), [loading, setLoading] = useState(false);
  const [complete, setComplete] = useState<boolean | null>(null), [expired, setExpired] = useState(false), [attempt, setAttempt] = useState(0);
  const currentScope = useRef(scope), active = useRef<AbortController | null>(null), action = useRef(false), claimKey = useRef<string | null>(null);
  currentScope.current = scope;
  useEffect(() => {
    const controller = new AbortController(); active.current = controller;
    setSnapshot(null); setWorks([]); setSelected(""); setError(""); setComplete(null); setExpired(false); setBusy(""); action.current = false;
    if (ready && registered && lookupKey && !inputError) {
      setLoading(true);
      void (async () => {
        const value = await researchConsentApi.get(consentLookup(new URLSearchParams(lookupKey))!, controller.signal);
        if (controller.signal.aborted) return;
        setSnapshot({ scope, value });
        if (value.status !== "pending" || new Date(value.expires_at).getTime() <= Date.now()) return;
        const found = new Map<string, LocalWork>(), cursors = new Set<string>(); let cursor: string | undefined;
        do {
          const page = await localWorkApi.list(cursor, controller.signal);
          page.items.filter(work => work.state === "ready").forEach(work => found.set(work.id, work));
          const next = page.page?.next_cursor || undefined;
          if (!next || cursors.has(next)) break;
          cursors.add(next); cursor = next;
        } while (!controller.signal.aborted);
        if (controller.signal.aborted) return;
        const owned = [...found.values()]; setWorks(owned);
        setSelected(value.requested_local_work_id && found.has(value.requested_local_work_id) ? value.requested_local_work_id : !value.requested_local_work_id && owned.length === 1 ? owned[0].id : "");
      })().catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    } else setLoading(false);
    return () => { controller.abort(); };
  }, [ready, registered, scope, attempt, inputError, lookupKey]);
  useEffect(() => {
    if (!request) return;
    const remaining = new Date(request.expires_at).getTime() - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) { setExpired(true); return; }
    const timer = setTimeout(() => setExpired(true), Math.min(remaining, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [request]);
  const pending = !!request && request.status === "pending" && !expired && complete === null;
  const owned = works.find(work => work.id === selected);
  const allowed = pending && !!owned && (!request.requested_local_work_id || request.requested_local_work_id === owned.id);
  async function login(provider: "google" | "orcid") {
    setError(""); setBusy("login");
    try { await signIn(provider, { callbackUrl: `${window.location.origin}${consentPagePath(lookup)}` }); }
    catch (failure) { setError(errorMessage(failure)); setBusy(""); }
  }
  async function claim() {
    if (action.current || !registered || !status.canClaim) return;
    action.current = true; setBusy("claim"); setError(""); claimKey.current ||= crypto.randomUUID();
    const operationScope = scope, controller = active.current;
    try {
      await researchConsentApi.claim(claimKey.current, controller?.signal);
      if (controller?.signal.aborted || currentScope.current !== operationScope) return;
      claimKey.current = null; invalidateWorkspace(); await refresh(); setAttempt(value => value + 1);
    } catch (failure) { if (!controller?.signal.aborted && currentScope.current === operationScope) setError(errorMessage(failure)); }
    finally { if (currentScope.current === operationScope) { setBusy(""); action.current = false; } }
  }
  async function decide(approve: boolean) {
    if (action.current || !registered || !request || !pending || approve && !allowed) return;
    action.current = true; setBusy(approve ? "approve" : "deny"); setError("");
    const operationScope = scope, controller = active.current;
    try {
      const result = await researchConsentApi.decide(request.request_id, approve, approve ? selected : undefined, controller?.signal);
      if (controller?.signal.aborted || currentScope.current !== operationScope) return;
      if ("redirect_url" in result) window.location.assign(consentRedirect(result.redirect_url));
      else setComplete(result.approved);
    } catch (failure) { if (!controller?.signal.aborted && currentScope.current === operationScope) setError(errorMessage(failure)); }
    finally { if (currentScope.current === operationScope) { setBusy(""); action.current = false; } }
  }
  return <main id="main" className="local-work-page research-consent-page"><nav><Link href="/workspace?tab=runs">← Research runs</Link></nav>
    <header><p className="local-eyebrow">Reveal connection</p><h1>Connect your research agent</h1><p>Public scientific data is available anonymously. Sign in and approve a connection to let your agent work with private research and submit findings.</p></header>
    {lookup && "user_code" in lookup && <p className="consent-device-code">Code from your agent: <strong>{lookup.user_code}</strong></p>}
    {inputError && <p className="local-error" role="alert">{inputError}</p>}
    {!lookup && <form className="consent-code-form" onSubmit={event => { event.preventDefault(); try { const next = consentLookup(new URLSearchParams({ user_code: code })); setError(""); router.replace(consentPagePath(next)); } catch (failure) { setError(errorMessage(failure)); } }}><label htmlFor="research-device-code">Device code shown by your agent</label><div className="local-actions"><input id="research-device-code" value={code} onChange={event => setCode(event.target.value)} autoCapitalize="characters" autoComplete="off" spellCheck={false} maxLength={32} required /><button type="submit">Find connection request</button></div></form>}
    {!ready && <p role="status">Checking your sign-in…</p>}
    {ready && !registered && <section className="local-section"><h2>Sign in to approve access</h2><p>Use Google or ORCID to identify the workspace that your agent may access. Signing in does not approve the connection.</p><ProviderButtons disabled={!!busy} onLogin={provider => void login(provider)} />{!status.providers.google && !status.providers.orcid && <p className="local-muted">Sign-in is unavailable on this Reveal instance. You can continue public research in your agent without connecting.</p>}</section>}
    {error && <p className="local-error" role="alert">{error} {registered && <button disabled={!!busy} onClick={() => { void refresh(); setAttempt(value => value + 1); }}>Retry</button>}</p>}
    {ready && registered && lookup && <>
      {loading && <p role="status">Checking the connection request and your research…</p>}
      {request && <section className="local-section"><h2>{request.client_name} requests access</h2><p className="local-id">Client: {request.client_id}</p><p className="local-muted">The client supplies its display name. Approve only the agent you intended to connect.</p><p className="local-muted">Signed in as {me?.display_name || "your registered Reveal account"}. Requested access expires {new Date(request.expires_at).toLocaleString()}.</p>
        <h3>Permissions</h3><ul className="consent-scopes">{request.scopes.map(scopeName => <li key={scopeName}>{consentScopeLabel(scopeName)}<small>{scopeName}</small></li>)}</ul><p className="local-id">Reveal service: {request.resource}</p>{request.redirect_uri && <p className="local-id">Client callback: {request.redirect_uri}</p>}
        {complete !== null ? <div role="status" className="local-notice"><h3>{complete ? "Connection approved" : "Connection declined"}</h3><p>{complete ? "Return to your agent to continue. You can disconnect later without removing your local files." : "Return to your agent. Public research remains available without a connection."}</p></div> : !pending ? <p role="status" className="local-notice">{expired ? "This connection request has expired." : `This connection request is ${request.status.replaceAll("_", " ")}.`} Return to your agent to start a new connection.</p> : <>
          {status.canClaim && <div className="consent-claim"><h3>Bring your anonymous research with you</h3><p>If you downloaded this research before signing in, move the work from this browser into your signed-in workspace before approving access. Existing scientific identities and attribution stay unchanged.</p><button disabled={!!busy} onClick={() => void claim()}>{busy === "claim" ? "Moving anonymous research…" : "Move my anonymous research"}</button><p className="local-muted">This moves the anonymous workspace associated with this browser. A link or device code does not grant ownership of research.</p></div>}
          {request.requested_local_work_id ? <div className="consent-work"><h3>Research for this connection</h3><p>{works.find(work => work.id === request.requested_local_work_id) ? localWorkTitle(works.find(work => work.id === request.requested_local_work_id)!) : "The requested research is not in your available workspace."}</p><p className="local-id">{request.requested_local_work_id}</p>{!selected && !loading && <p className="local-muted">Move your anonymous research into this account if it belongs to this browser, or sign in with the account that owns it.</p>}</div> : <div className="consent-work"><label htmlFor="consent-local-work">Choose the research this agent may access</label><select id="consent-local-work" disabled={!!busy || loading} value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select research…</option>{works.map(work => <option key={work.id} value={work.id}>{localWorkTitle(work)}</option>)}</select>{!works.length && !loading && <p>No ready local research is available. Create local research from a knowledge gap, then start a new connection from your agent.</p>}</div>}
          <details><summary>Use a different account or sign in again</summary><p>Sign in again if your workspace move needs a fresh login, or choose the account that owns this research.</p><ProviderButtons disabled={!!busy} onLogin={provider => void login(provider)} /></details><p>Approve only if you started this request from your agent. Your Google or ORCID login is never sent to the agent.</p><div className="local-actions"><button className="local-download-primary" disabled={!allowed || loading || !!busy} onClick={() => void decide(true)}>{busy === "approve" ? "Approving…" : "Approve connection"}</button><button disabled={!!busy} onClick={() => void decide(false)}>{busy === "deny" ? "Declining…" : "Decline"}</button></div>
        </>}
      </section>}
    </>}
  </main>;
}
