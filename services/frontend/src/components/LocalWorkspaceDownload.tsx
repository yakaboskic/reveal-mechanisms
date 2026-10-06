import React from "react";
import { localLaunchCommand, type LocalAgentClient, type LocalSetupProgress, type LocalWorkState } from "../lib/local-work";

type Props = {
  title: string;
  state: LocalWorkState | "loading";
  client: LocalAgentClient;
  blocked: boolean;
  progress: LocalSetupProgress | null;
  elapsed: number;
  error: string;
  downloadedClient: LocalAgentClient | null;
  onClientChange: (client: LocalAgentClient) => void;
  onDownload: () => void;
  onCancel: () => void;
  onRefresh: () => void;
};

export function LocalWorkspaceDownload({ title, state, client, blocked, progress, elapsed, error, downloadedClient, onClientChange, onDownload, onCancel, onRefresh }: Props) {
  const pending = state === "loading" || state === "preparing" || !!progress;
  const transferring = progress?.phase === "downloading";
  const failed = state === "preparation_failed";
  const label = state === "loading" ? "Opening workspace…" : state === "preparing" ? "Preparing workspace…" : transferring ? "Downloading workspace…" : progress ? "Preparing download…" : state === "closed" ? "Workspace closed" : failed ? "Check preparation again" : error ? "Retry download" : "Download workspace";
  const status = state === "loading" ? "Retrieving your saved question and selections." : state === "preparing" ? "Preparing your question, selected factors and evidence." : transferring ? "Receiving your workspace ZIP." : progress ? "Building your workspace ZIP." : "";
  return <>
    <header className="local-workspace-question"><p className="local-eyebrow">Research question</p><h1>{title}</h1></header>
    <section className="local-workspace-setup" aria-label="Download your local agent workspace" aria-busy={pending}>
      <fieldset className="local-agent-choice" disabled={blocked || !!progress || state === "loading" || state === "closed"}>
        <legend className="local-sr-only">Choose your agent</legend>
        {(["codex", "claude_code"] as const).map(value => <label key={value} className={client === value ? "selected" : ""}>
          <input type="radio" name="local-agent-client" value={value} checked={client === value} onChange={() => onClientChange(value)} />
          <span>{value === "codex" ? "Codex" : "Claude Code"}</span>
        </label>)}
      </fieldset>
      <button className="local-download-primary" disabled={blocked || pending || state === "closed"} onClick={failed ? onRefresh : onDownload}>
        {pending && <span className="local-workspace-spinner" aria-hidden="true" />}{label}
      </button>
      {pending && <div className="local-download-status">
        <p role="status" aria-live="polite">{status}</p>
        <p className="local-muted">{transferring && progress && progress.receivedBytes > 0 ? `${Math.ceil(progress.receivedBytes / 1024).toLocaleString()} KB received · ` : ""}<span aria-hidden="true">{elapsed}s elapsed</span></p>
        {elapsed >= 20 && <p className="local-muted">This is taking longer than usual. You can leave this page and return to the same workspace.</p>}
        {progress && <button className="local-download-cancel" onClick={onCancel}>Cancel download</button>}
      </div>}
      {error && <p className="local-error" role="alert">{error}</p>}
      {state === "closed" && <p className="local-muted">This research is closed. Its saved inputs and findings are in Additional details.</p>}
      {downloadedClient && !pending && !error && <div className="local-launch-steps" role="status">
        <h2>Workspace downloaded</h2><p>Unzip it, open a terminal in the folder, and run:</p><pre>{localLaunchCommand(downloadedClient)}</pre>
      </div>}
    </section>
  </>;
}
