"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { downloadLocalBlob, localClientPreference, localClientPreferenceKey, localErrorMessage, localLaunchCommand, localResearchPrompt, localStateLabel, localWorkApi, localWorkHref, localWorkTitle, LocalWorkError, submissionAccounts, type LocalAgentClient, type LocalWork, type LocalSubmission } from "@/lib/local-work";
import "./local-work.css";

const date = (value?: string | null) => value ? new Date(value).toLocaleString() : "Not yet recorded";
const message = (error: unknown) => error instanceof Error ? error.message : "Local research is unavailable. Please retry.";
const accessLost = (error: unknown) => error instanceof LocalWorkError && [401, 403, 404].includes(error.status);

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false), [error, setError] = useState(false);
  return <><button type="button" onClick={async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); setError(false); }
    catch { setError(true); }
  }}>{copied ? "Copied" : label}</button>{error && <small role="status">Copy is unavailable. Select and copy the text manually.</small>}</>;
}

export function LocalWorkHistory() {
  const [items, setItems] = useState<LocalWork[]>([]), [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(true), [attempt, setAttempt] = useState(0);
  const active = useRef(false);
  useEffect(() => {
    active.current = true; const controller = new AbortController(); setLoading(true); setError("");
    void localWorkApi.list(undefined, controller.signal).then(value => {
      if (!controller.signal.aborted) { setItems(value.items); setCursor(value.page?.next_cursor || null); }
    }).catch(failure => { if (!controller.signal.aborted) { setError(message(failure)); if (accessLost(failure)) { setItems([]); setCursor(null); } } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { active.current = false; controller.abort(); };
  }, [attempt]);
  async function loadMore() {
    if (!cursor || loading) return; setLoading(true); setError("");
    try { const value = await localWorkApi.list(cursor); if (active.current) { setItems(previous => [...new Map([...previous, ...value.items].map(item => [item.id, item])).values()]); setCursor(value.page?.next_cursor || null); } }
    catch (failure) { if (active.current) { setError(message(failure)); if (accessLost(failure)) { setItems([]); setCursor(null); } } }
    finally { if (active.current) setLoading(false); }
  }
  return <main id="main" className="local-work-page"><nav><Link href="/">← Explore knowledge gaps</Link></nav>
    <header><p className="local-eyebrow">Your workspace</p><h1>Local research</h1><p>Reconnect your agent, inspect submitted findings, or return to a frozen research seed.</p></header>
    {loading && !items.length && <p role="status">Loading local research…</p>}
    {error && <p className="local-error" role="alert">{error} <button onClick={() => setAttempt(value => value + 1)}>Retry</button> <Link href="/">Return to your workspace connection</Link></p>}
    {!loading && !error && !items.length && <section className="local-section"><h2>No local research yet</h2><p>Choose a knowledge gap, prepare your draft, then select <strong>Use my local agent</strong>.</p><Link href="/">Explore knowledge gaps →</Link></section>}
    {items.map(work => <article className="local-section" key={work.id}><p className="local-eyebrow">{localStateLabel[work.state]} · {date(work.created_at)}</p><h2><Link href={localWorkHref(work.id)}>{localWorkTitle(work)}</Link></h2><p>{work.submissions?.length || 0} submitted batches</p></article>)}
    {cursor && <button disabled={loading} onClick={() => void loadMore()}>{loading ? "Loading…" : "Load more"}</button>}
  </main>;
}

function SubmittedBatch({ submission, standalone }: { submission: LocalSubmission; standalone: boolean }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const keys = useRef(new Map<string, string>());
  const accounts = submissionAccounts(submission);
  const status = (submission.state || submission.status || "received").replaceAll("_", " ");
  const report = submission.report || submission.validation_report;
  async function generate(accountId: string) {
    if (busy) return; setBusy(true); setError("");
    let key = keys.current.get(accountId); if (!key) { key = crypto.randomUUID(); keys.current.set(accountId, key); }
    try { const job = await localWorkApi.generateStatement(accountId, key); window.location.assign(`/?job=${encodeURIComponent(job.id)}`); }
    catch (failure) { setError(message(failure)); setBusy(false); }
  }
  return <article className="local-submission"><div className="local-heading-row"><h3>{submission.validation_only ? "Validation check" : "Submission"} · {status}</h3><time>{date(submission.created_at)}</time></div>
    <p className="local-id">{submission.id}</p>
    {localErrorMessage(submission.error || submission.last_error) && <p className="local-error">{localErrorMessage(submission.error || submission.last_error)}</p>}
    {accounts.length > 0 && <ul className="local-account-list">{accounts.map(id => <li key={id}><Link prefetch={standalone ? false : undefined} href={standalone ? `/api/backend/v1/accounts/${encodeURIComponent(id)}` : `/accounts/${encodeURIComponent(id)}`}>{standalone ? "View scientific account JSON →" : "View scientific account →"}</Link><small>{submission.reused_account_ids?.includes(id) ? "Reused existing account · original authorship retained" : "Newly accepted account"}</small><small>{id}</small>{standalone && <button disabled={busy} onClick={() => void generate(id)}>Generate a hosted research statement</button>}</li>)}</ul>}
    {!!report && <details><summary>Validation report</summary><pre>{typeof report === "string" ? report : JSON.stringify(report, null, 2)}</pre></details>}
    {error && <p className="local-error" role="alert">{error}</p>}
  </article>;
}

export function LocalWorkView({ id, standalone = false }: { id: string; standalone?: boolean }) {
  const [work, setWork] = useState<LocalWork | null>(null), [error, setError] = useState("");
  const [client, setClient] = useState<LocalAgentClient>("codex"), [downloadedClient, setDownloadedClient] = useState<LocalAgentClient | null>(null);
  const [setupError, setSetupError] = useState("");
  const [busy, setBusy] = useState(""), [notice, setNotice] = useState(""), [attempt, setAttempt] = useState(0), [confirmClose, setConfirmClose] = useState(false);
  const alive = useRef(false), scope = useRef(id), flight = useRef(false), closeKey = useRef<string | null>(null);
  const downloadRequest = useRef<AbortController | null>(null);
  scope.current = id;
  useEffect(() => { try { setClient(localClientPreference(localStorage.getItem(localClientPreferenceKey))); } catch { /* A preference is optional. */ } }, []);
  function chooseClient(value: LocalAgentClient) {
    setClient(value); try { localStorage.setItem(localClientPreferenceKey, value); } catch { /* Private browsing may disable storage. */ }
  }
  useEffect(() => {
    alive.current = true; const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setWork(null); setError(""); setSetupError(""); setDownloadedClient(null); setBusy(""); flight.current = false; closeKey.current = null;
    async function refresh() {
      try { const value = await localWorkApi.get(id, controller.signal); if (!controller.signal.aborted) { setWork(value); setError(""); } }
      catch (failure) { if (!controller.signal.aborted) { setError(message(failure)); if (accessLost(failure)) { downloadRequest.current?.abort(); setWork(null); } } }
      if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 8_000);
    }
    void refresh();
    const clearCredential = () => { downloadRequest.current?.abort(); };
    window.addEventListener("pagehide", clearCredential);
    return () => { alive.current = false; controller.abort(); downloadRequest.current?.abort(); clearTimeout(timer); window.removeEventListener("pagehide", clearCredential); };
  }, [id, attempt]);
  useEffect(() => { if (work?.state === "closed") downloadRequest.current?.abort(); }, [work?.state]);
  const current = () => alive.current && scope.current === id;
  function showFailure(failure: unknown) {
    if (!current()) return;
    setError(message(failure));
    if (accessLost(failure)) { downloadRequest.current?.abort(); setWork(null); }
  }
  async function reload() { const value = await localWorkApi.get(id); if (current()) setWork(value); }
  async function downloadWorkspace() {
    if (flight.current || work?.state !== "ready") return;
    const controller = new AbortController(), selected = client;
    downloadRequest.current = controller; flight.current = true; setBusy("setup-kit"); setSetupError("");
    try {
      const blob = await localWorkApi.setupKit(id, selected, controller.signal);
      if (controller.signal.aborted || !current()) return;
      downloadLocalBlob(blob, `reveal-${id.replace(/[^a-zA-Z0-9_-]/g, "_")}.zip`, controller.signal);
      setDownloadedClient(selected);
    } catch (failure) {
      if (!controller.signal.aborted && current()) {
        setSetupError(failure instanceof Error && failure.name === "TimeoutError" ? "Workspace preparation took too long. Try downloading again." : message(failure));
        if (accessLost(failure)) showFailure(failure);
      }
    } finally {
      if (downloadRequest.current === controller) { downloadRequest.current = null; flight.current = false; if (current()) setBusy(""); }
    }
  }
  async function downloadSeed() {
    if (flight.current) return; flight.current = true; setBusy("download"); setError("");
    try {
      const value = await localWorkApi.package(id);
      if (!current()) return;
      downloadLocalBlob(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }), `reveal-${id}-seed.json`);
    } catch (failure) { showFailure(failure); }
    finally { flight.current = false; if (current()) setBusy(""); }
  }
  async function revoke(grantId: string) {
    if (flight.current) return; flight.current = true; setBusy(grantId); setError("");
    try { await localWorkApi.revoke(id, grantId); if (!current()) return; setNotice("Connection revoked. Your research and accepted accounts are retained."); await reload(); }
    catch (failure) { showFailure(failure); }
    finally { flight.current = false; if (current()) setBusy(""); }
  }
  async function close() {
    if (flight.current) return; flight.current = true; setBusy("close"); setError(""); closeKey.current ||= crypto.randomUUID();
    try { const value = await localWorkApi.close(id, closeKey.current); if (!current()) return; setWork(value); setConfirmClose(false); closeKey.current = null; setNotice("Local research closed. Submitted findings and accepted accounts are retained."); }
    catch (failure) { showFailure(failure); }
    finally { flight.current = false; if (current()) setBusy(""); }
  }
  const ready = work?.state === "ready";
  const prompt = localResearchPrompt(id);
  return <main id="main" className="local-work-page"><nav><Link href="/local-runs">← Local research</Link><Link href="/">Explore another gap</Link></nav>
    <header><p className="local-eyebrow">Local agent research{work && ` · ${localStateLabel[work.state]}`}</p><h1>{work ? localWorkTitle(work) : "Opening local research"}</h1><p>Work locally with public scientific data. Sign in to Reveal when you want to access private records, validate or submit findings.</p></header>
    {error && <p className="local-error" role="alert">{error} <button disabled={!!busy} onClick={() => setAttempt(value => value + 1)}>Refresh</button>{!work && <> <Link href="/">Return to your workspace connection</Link></>}</p>}
    {notice && <p className="local-notice" role="status">{notice}</p>}
    {!work && !error && <p role="status">Retrieving your saved research…</p>}
    {work && <>
      <section className="local-section local-workspace-setup" aria-labelledby="local-workspace"><h2 id="local-workspace">Work with your local agent</h2>
        {work.state === "preparing" ? <p role="status">Freezing your question, selected factors, notes and authoring instructions. No hosted research agent is starting.</p> : work.state === "preparation_failed" ? <p className="local-error">{localErrorMessage(work.last_error) || "The research seed could not be prepared. The frozen request is retained."}</p> : <p>Your frozen inputs stay in the downloaded folder. Your agent can explore public scientific data anonymously, then connect to Reveal when you are ready to save findings.</p>}
        <fieldset className="local-agent-choice" disabled={!!busy}><legend>Choose your agent</legend>{(["codex", "claude_code"] as const).map(value => <label key={value} className={client === value ? "selected" : ""}><input type="radio" name="local-agent-client" value={value} checked={client === value} onChange={() => chooseClient(value)} /><span>{value === "codex" ? "Codex" : "Claude Code"}</span></label>)}</fieldset>
        <div className="local-actions"><button className="local-download-primary" disabled={!ready || !!busy} onClick={() => void downloadWorkspace()}>{busy === "setup-kit" ? "Preparing workspace…" : "Download workspace"}</button></div>
        <p className="local-muted">The ZIP contains your frozen inputs, agent configuration and launcher, without a Reveal credential. Downloading starts no agent or model run.</p>
        <details className="local-setup-requirements"><summary>What you need to run it</summary><p>Python 3 and the installed, signed-in Codex or Claude Code command-line app. Your agent keeps its usual model, login, trust and permission settings. Connecting a Reveal account supports macOS Keychain or Linux Secret Service with <code>secret-tool</code>; anonymous research does not need a credential store.</p></details>
        {setupError && <p className="local-error" role="alert">{setupError}</p>}
        {downloadedClient && <div className="local-launch-steps" role="status"><h3>Open your downloaded workspace</h3><p>Unzip the download into its own folder. Open a terminal in that folder and run:</p><pre>{localLaunchCommand(downloadedClient)}</pre><CopyButton value={localLaunchCommand(downloadedClient)} label="Copy launch command" /><p>The launcher checks the files and starts {downloadedClient === "codex" ? "Codex" : "Claude Code"} with anonymous Reveal access. Your browser does not open it automatically.</p><p>When you want to validate or submit findings, ask your agent to connect to Reveal, or run <code>{localLaunchCommand(downloadedClient, "--login")}</code>. You will sign in and approve access in your browser.</p><p className="local-muted">Keep using this folder when you return; there is no setup ticket to renew. Your local files stay available if Reveal is offline.</p></div>}
        <details><summary>Frozen inputs and package identity</summary><p className="local-id">Work: {id}</p>{work.package_sha256 && <p className="local-id">Package SHA-256: {work.package_sha256}</p>}<pre>{JSON.stringify(work.request, null, 2)}</pre>{work.package_id && <button disabled={!!busy} onClick={() => void downloadSeed()}>{busy === "download" ? "Downloading research seed…" : "Download research seed and manifest"}</button>}</details>
      </section>
      <details className="local-section local-manual-setup"><summary>Manual setup and connection controls</summary><section aria-labelledby="local-connect"><h2 id="local-connect">Connect only when you need to</h2>
        <p>Use the configuration included in the download. Both agents use the same local connection helper. Public scientific data is available without signing in; server validation, uploads, submissions and private records require your approval.</p>
        <p>Ask your agent to use <code>connect_reveal</code> to sign in, <code>get_reveal_connection</code> to check access, or <code>disconnect_reveal</code> to disconnect. You can also run these commands in your existing research folder:</p>
        <dl className="local-connection-commands"><dt>Connect or reconnect your Reveal account</dt><dd><code>{localLaunchCommand(client, "--login")}</code></dd><dt>Disconnect this workspace; public data stays available</dt><dd><code>{localLaunchCommand(client, "--logout")}</code></dd><dt>Check the public connection without starting an agent</dt><dd><code>{localLaunchCommand(client, "--check-only")}</code></dd><dt>Start offline with the downloaded evidence reader</dt><dd><code>{localLaunchCommand(client, "--offline")}</code></dd></dl>
        <p>Reconnect in the same folder to keep your inputs and outputs. Your provider login stays with Google or ORCID; it is not shared with your agent.</p>
        {work.grants.length > 0 && <><h3>Existing connections</h3><ul className="local-grants">{work.grants.map(grant => <li key={grant.grant_id}><span>Connection {grant.grant_id.slice(0, 8)} · {grant.revoked_at ? "Revoked" : new Date(grant.expires_at).getTime() <= Date.now() ? "Expired" : `Expires ${date(grant.expires_at)}`}</span>{!grant.revoked_at && <button disabled={!!busy} onClick={() => void revoke(grant.grant_id)}>Revoke</button>}</li>)}</ul></>}
      </section><section aria-labelledby="local-start"><h2 id="local-start">Research prompt</h2><p>The downloaded workspace includes these instructions. You can also paste them into your agent; they contain no credential.</p><pre>{prompt}</pre><CopyButton value={prompt} label="Copy research prompt" /></section></details>
      <section className="local-section" aria-labelledby="local-results"><h2 id="local-results">Submitted findings</h2><p>Accepted accounts stay private. Hosted research-statement generation is optional and starts only when you request it.</p><p className="local-muted">Last recorded action: {work.last_action?.replaceAll("_", " ") || "No agent action recorded"} · {date(work.last_activity)}. Local activity between MCP requests is not visible here.</p>{!work.submissions.length && <p className="local-muted">Awaiting a submission from your local agent. You can leave this page and return later.</p>}{work.submissions.map(submission => <SubmittedBatch key={submission.id} submission={submission} standalone={standalone} />)}</section>
      {work.state !== "closed" && <section className="local-close">{confirmClose ? <><p>Close this research to new agent work? Frozen inputs, received submissions and accepted accounts are retained.</p><div className="local-actions"><button disabled={!!busy} onClick={() => void close()}>{busy === "close" ? "Closing…" : "Close local research"}</button><button disabled={!!busy} onClick={() => setConfirmClose(false)}>Keep open</button></div></> : <button disabled={!!busy} onClick={() => setConfirmClose(true)}>Close local research</button>}</section>}
    </>}
  </main>;
}
