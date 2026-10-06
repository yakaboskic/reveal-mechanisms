import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LocalWorkspaceDownload } from "../src/components/LocalWorkspaceDownload";

const defaults: React.ComponentProps<typeof LocalWorkspaceDownload> = {
  title: "Do these mechanisms converge?", state: "ready", client: "codex", blocked: false,
  progress: null, elapsed: 0, error: "", downloadedClient: null,
  onClientChange() {}, onDownload() {}, onCancel() {}, onRefresh() {},
};
const render = (overrides: Partial<typeof defaults> = {}) => renderToStaticMarkup(React.createElement(LocalWorkspaceDownload, { ...defaults, ...overrides }));

test("ready workspace centers the question, two agent choices and a single enabled download action", () => {
  const html = render();
  assert.match(html, /<h1>Do these mechanisms converge\?<\/h1>/);
  assert.equal((html.match(/type="radio"/g) || []).length, 2);
  assert.equal((html.match(/<button /g) || []).length, 1);
  assert.match(html, /<button class="local-download-primary">Download workspace<\/button>/);
  assert.doesNotMatch(html, /role="status"|disabled|spinner|manual setup|frozen inputs/i);
});

test("preparing the seed and waiting for ZIP headers both have visible accessible progress", () => {
  for (const overrides of [{ state: "preparing" as const }, { progress: { phase: "preparing" as const, receivedBytes: 0 } }]) {
    const html = render({ ...overrides, elapsed: 25 });
    assert.match(html, /aria-busy="true"/);
    assert.match(html, /local-workspace-spinner/);
    assert.match(html, /<button class="local-download-primary" disabled=""/);
    assert.match(html, /role="status" aria-live="polite"/);
    assert.match(html, /25s elapsed/);
    assert.match(html, /taking longer than usual/);
  }
});

test("download transfer can be cancelled and failures restore an explicit retry action", () => {
  const transferring = render({ progress: { phase: "downloading", receivedBytes: 2048 }, elapsed: 4 });
  assert.match(transferring, /Downloading workspace/);
  assert.match(transferring, /2 KB received/);
  assert.match(transferring, />Cancel download<\/button>/);
  const retry = render({ error: "Connection lost. Please retry." });
  assert.match(retry, /<button class="local-download-primary">Retry download<\/button>/);
  assert.match(retry, /role="alert">Connection lost/);
  assert.doesNotMatch(retry, /spinner|disabled/);
});

test("success gives the command for the actual downloaded agent and a closed workspace stays disabled", () => {
  const success = render({ client: "codex", downloadedClient: "claude_code" });
  assert.match(success, /Workspace downloaded/);
  assert.match(success, /python3 start.py claude/);
  assert.doesNotMatch(success, /python3 start.py codex/);
  const closed = render({ state: "closed" });
  assert.match(closed, /<button class="local-download-primary" disabled="">Workspace closed/);
  assert.doesNotMatch(closed, /spinner/);
});
