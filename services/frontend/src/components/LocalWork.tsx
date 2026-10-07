"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { downloadLocalBlob, localClientPreference, localClientPreferenceKey, localErrorMessage, localLaunchCommand, localResearchPrompt, localWorkApi, localWorkTitle, LocalWorkError, submissionAccounts, type LocalAgentClient, type LocalSetupProgress, type LocalWork, type LocalSubmission } from "@/lib/local-work";
import { createLocalWorkRefresher } from "@/lib/local-work-refresh";
import { onCollectionInvalidation } from "@/lib/collection-events";
import { LocalWorkspaceDownload } from "./LocalWorkspaceDownload";
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
  const [setupProgress, setSetupProgress] = useState<LocalSetupProgress | null>(null), [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState(""), [notice, setNotice] = useState(""), [attempt, setAttempt] = useState(0), [confirmClose, setConfirmClose] = useState(false);
  const alive = useRef(false), scope = useRef(id), flight = useRef(false), closeKey = useRef<string | null>(null);
  const downloadRequest = useRef<AbortController | null>(null);
  scope.current = id;
  useEffect(() => { try { setClient(localClientPreference(localStorage.getItem(localClientPreferenceKey))); } catch { /* A preference is optional. */ } }, []);
  function chooseClient(value: LocalAgentClient) {
    setClient(value); try { localStorage.setItem(localClientPreferenceKey, value); } catch { /* Private browsing may disable storage. */ }
  }
  useEffect(() => {
    alive.current = true; const controller = new AbortController();
    setWork(null); setError(""); setSetupError(""); setDownloadedClient(null); setSetupProgress(null); setBusy(""); flight.current = false; closeKey.current = null;
    const refresher = createLocalWorkRefresher({
      hidden: () => document.visibilityState === "hidden", terminal: accessLost,
      load: async () => {
        try {
          const value = await localWorkApi.get(id, controller.signal);
          if (!controller.signal.aborted) { setWork(value); setError(""); }
          return value;
        } catch (failure) {
          if (!controller.signal.aborted) { setError(message(failure)); if (accessLost(failure)) { downloadRequest.current?.abort(); setWork(null); } }
          throw failure;
        }
      },
    });
    refresher.refresh();
    // The workspace stream pushes local work and submission changes as 'jobs'.
    const unsubscribe = onCollectionInvalidation(["jobs"], () => refresher.refresh());
    const clearCredential = () => { downloadRequest.current?.abort(); };
    window.addEventListener("pagehide", clearCredential);
    document.addEventListener("visibilitychange", refresher.visible); window.addEventListener("online", refresher.visible);
    return () => {
      alive.current = false; refresher.stop(); unsubscribe(); controller.abort(); downloadRequest.current?.abort();
      window.removeEventListener("pagehide", clearCredential); document.removeEventListener("visibilitychange", refresher.visible); window.removeEventListener("online", refresher.visible);
    };
  }, [id, attempt]);
  useEffect(() => { if (work?.state === "closed") downloadRequest.current?.abort(); }, [work?.state]);
  const waiting = (!work && !error) || work?.state === "preparing" || busy === "setup-kit";
  useEffect(() => {
    setElapsed(0);
    if (!waiting) return;
    const started = Date.now(), timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1_000)), 1_000);
    return () => clearInterval(timer);
  }, [waiting, id]);
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
    downloadRequest.current = controller; flight.current = true; setBusy("setup-kit"); setSetupError(""); setDownloadedClient(null); setSetupProgress({ phase: "preparing", receivedBytes: 0 });
    try {
      const blob = await localWorkApi.setupKit(id, selected, controller.signal, progress => { if (!controller.signal.aborted && current()) setSetupProgress(progress); });
      if (controller.signal.aborted || !current()) return;
      downloadLocalBlob(blob, `reveal-${id.replace(/[^a-zA-Z0-9_-]/g, "_")}.zip`, controller.signal);
      setDownloadedClient(selected);
    } catch (failure) {
      if (!controller.signal.aborted && current()) {
        setSetupError(failure instanceof Error && failure.name === "TimeoutError" ? "Workspace preparation took too long. Try downloading again." : message(failure));
        if (accessLost(failure)) showFailure(failure);
      }
    } finally {
      if (downloadRequest.current === controller) { downloadRequest.current = null; flight.current = false; if (current()) { setBusy(""); setSetupProgress(null); } }
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
  const prompt = localResearchPrompt(id);
  const preparationError = work?.state === "preparation_failed" ? localErrorMessage(work.last_error) || "We could not prepare this workspace. Your question and selections are saved." : "";
  return <main id="main" className="local-work-page local-work-download-page"><nav><Link href="/workspace?tab=runs">← Research runs</Link><Link href="/">Explore another gap</Link></nav>
    {error && <p className="local-error" role="alert">{error} <button disabled={!!busy} onClick={() => setAttempt(value => value + 1)}>Refresh</button>{!work && <> <Link href="/">Return to your workspace connection</Link></>}</p>}
    {notice && <p className="local-notice" role="status">{notice}</p>}
    {(work || !error) && <LocalWorkspaceDownload
      title={work ? localWorkTitle(work) : "Opening your workspace"}
      state={work?.state || "loading"} client={client} blocked={!!busy && busy !== "setup-kit"}
      progress={setupProgress} elapsed={elapsed} error={setupError || preparationError} downloadedClient={downloadedClient}
      onClientChange={chooseClient} onDownload={() => void downloadWorkspace()}
      onCancel={() => downloadRequest.current?.abort()} onRefresh={() => setAttempt(value => value + 1)}
    />}
    {work && <details className="local-additional-details"><summary>Additional details</summary><div className="local-additional-content">
      <section aria-labelledby="local-requirements"><h2 id="local-requirements">Running your workspace</h2>
        <p>The ZIP contains your question, selected factors, evidence, agent configuration and launcher, without a Reveal credential. Downloading starts no agent or model run.</p>
        <p>Unzip it into its own folder. You need Python 3 and the installed, signed-in Codex or Claude Code command-line app. In a terminal in that folder, run:</p>
        <pre>{localLaunchCommand(client)}</pre><CopyButton value={localLaunchCommand(client)} label="Copy launch command" />
        <p>Your agent keeps its usual model, login, trust and permission settings. Public scientific data is available anonymously. Sign in to Reveal when you want to access private records, validate or submit findings.</p>
        <p>Connecting a Reveal account supports macOS Keychain or Linux Secret Service with <code>secret-tool</code>; anonymous research does not need a credential store. Keep using the same folder when you return.</p>
      </section>
      <section aria-labelledby="local-inputs"><h2 id="local-inputs">Frozen inputs and package identity</h2><p className="local-id">Work: {id}</p>{work.package_sha256 && <p className="local-id">Package SHA-256: {work.package_sha256}</p>}<pre>{JSON.stringify(work.request, null, 2)}</pre>{work.package_id && <button disabled={!!busy} onClick={() => void downloadSeed()}>{busy === "download" ? "Downloading research seed…" : "Download research seed and manifest"}</button>}</section>
      <section aria-labelledby="local-connect"><h2 id="local-connect">Connect only when you need to</h2>
        <p>Use the configuration included in the download. Both agents use the same local connection helper. Public scientific data is available without signing in; server validation, uploads, submissions and private records require your approval.</p>
        <p>Ask your agent to use <code>connect_reveal</code> to sign in, <code>get_reveal_connection</code> to check access, or <code>disconnect_reveal</code> to disconnect. You can also run these commands in your existing research folder:</p>
        <dl className="local-connection-commands"><dt>Connect or reconnect your Reveal account</dt><dd><code>{localLaunchCommand(client, "--login")}</code></dd><dt>Disconnect this workspace; public data stays available</dt><dd><code>{localLaunchCommand(client, "--logout")}</code></dd><dt>Check the public connection without starting an agent</dt><dd><code>{localLaunchCommand(client, "--check-only")}</code></dd><dt>Start offline with the downloaded evidence reader</dt><dd><code>{localLaunchCommand(client, "--offline")}</code></dd></dl>
        <p>Reconnect in the same folder to keep your inputs and outputs. Your provider login stays with Google or ORCID; it is not shared with your agent.</p>
        {work.grants.length > 0 && <><h3>Existing connections</h3><ul className="local-grants">{work.grants.map(grant => <li key={grant.grant_id}><span>Connection {grant.grant_id.slice(0, 8)} · {grant.revoked_at ? "Revoked" : new Date(grant.expires_at).getTime() <= Date.now() ? "Expired" : `Expires ${date(grant.expires_at)}`}</span>{!grant.revoked_at && <button disabled={!!busy} onClick={() => void revoke(grant.grant_id)}>Revoke</button>}</li>)}</ul></>}
      </section><section aria-labelledby="local-start"><h2 id="local-start">Research prompt</h2><p>The downloaded workspace includes these instructions. You can also paste them into your agent; they contain no credential.</p><pre>{prompt}</pre><CopyButton value={prompt} label="Copy research prompt" /></section>
      <section className="local-section" aria-labelledby="local-results"><h2 id="local-results">Submitted findings</h2><p>Accepted accounts stay private. Hosted research-statement generation is optional and starts only when you request it.</p><p className="local-muted">Last recorded action: {work.last_action?.replaceAll("_", " ") || "No agent action recorded"} · {date(work.last_activity)}. Local activity between MCP requests is not visible here.</p>{!work.submissions.length && <p className="local-muted">Awaiting a submission from your local agent. You can leave this page and return later.</p>}{work.submissions.map(submission => <SubmittedBatch key={submission.id} submission={submission} standalone={standalone} />)}</section>
      {work.state !== "closed" && <section className="local-close">{confirmClose ? <><p>Close this research to new agent work? Frozen inputs, received submissions and accepted accounts are retained.</p><div className="local-actions"><button disabled={!!busy} onClick={() => void close()}>{busy === "close" ? "Closing…" : "Close local research"}</button><button disabled={!!busy} onClick={() => setConfirmClose(false)}>Keep open</button></div></> : <button disabled={!!busy} onClick={() => setConfirmClose(true)}>Close local research</button>}</section>}
    </div></details>}
  </main>;
}
