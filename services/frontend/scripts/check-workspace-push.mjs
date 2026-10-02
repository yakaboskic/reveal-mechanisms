#!/usr/bin/env node
/** Real local SSE proof. Creates an expiring anonymous workspace and one draft;
 * never submits research. Raw fetch mutations bypass the app's local cache hook,
 * so both tabs must receive the server event to update their visible state.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const origin = new URL(process.env.DEPLOYMENT_BASE_URL || 'http://localhost:3100').origin;
const api = new URL(process.env.DEPLOYMENT_API_URL || 'http://localhost:18001').origin;
for (const url of [origin, api]) assert.ok(['localhost', '127.0.0.1'].includes(new URL(url).hostname), 'This mutation probe is local only.');
const idleMs = Number(process.env.WORKSPACE_IDLE_MS || 40_000);
assert.ok(Number.isFinite(idleMs) && idleMs > 35_000 && idleMs <= 90_000);
const output = resolve(import.meta.dirname, '../../../.runtime/workflow/browser-events');
await mkdir(output, { recursive: true });
// One readiness check before launching a browser. Do not repeatedly query dependencies.
const ready = await fetch(api + '/readyz', { signal: AbortSignal.timeout(90_000) });
assert.equal(ready.status, 200, 'Wait for the local Vector activation/readiness gate before running this browser proof.');
const readiness = await ready.json();
assert.ok(readiness, 'Readiness response required.');
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href);
  try { return await import('playwright'); }
  catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    for (const suffix of ['chrome-mac/Chromium.app/Contents/MacOS/Chromium', 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing']) {
      const path = resolve(cache, name, suffix); try { await access(path); return path; } catch {}
    }
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const errors = [], requests = [], tabs = [];
const listPaths = new Set(['/api/backend/v1/drafts', '/api/backend/v1/jobs', '/api/backend/v1/research-requests', '/api/backend/v1/me/explorations', '/api/backend/v1/accounts', '/api/backend/v1/analysis-outcomes']);
let draft = null, phase = 'setup', context;
async function json(page, path, method = 'GET', body) {
  const result = await page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, { method, cache: 'no-store', headers: { 'Content-Type': 'application/json', ...(method === 'GET' ? {} : { 'Idempotency-Key': crypto.randomUUID() }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { status: response.status, body: await response.json() };
  }, { path, method, body });
  assert.ok(result.status >= 200 && result.status < 300, `${method} ${path}: ${result.status} ${result.body.code || ''}`);
  return result.body;
}
async function waitDraft(page, id, name) {
  await page.waitForFunction(({ id, name }) => [...document.querySelectorAll('.workspace-draft-row')].some(option => option.dataset.draftId === id && option.textContent.includes(name)), { id, name }, { timeout: 25_000 });
}
try {
  context = await browser.newContext();
  await context.addInitScript(() => {
    window.__workspaceProof = { frames: [], streams: 0 };
    const original = window.fetch;
    window.fetch = async function (...args) {
      const response = await original.apply(this, args);
      const input = args[0];
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname === '/api/backend/v1/me/workspace/events' && response.ok) {
        window.__workspaceProof.streams++;
        const clone = response.clone();
        void (async () => {
          const reader = clone.body.getReader(), decoder = new TextDecoder(); let buffer = '';
          try {
            for (;;) {
              const { value, done } = await reader.read(); if (done) break;
              buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
              let end;
              while ((end = buffer.indexOf('\n\n')) >= 0) {
                const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
                const event = block.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
                if (!event) continue;
                const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
                let collections = []; try { collections = JSON.parse(data).collections || []; } catch {}
                // Record only event classes, never assertions/cursors/private payloads.
                window.__workspaceProof.frames.push({ event, collections, at: Date.now() });
              }
            }
          } catch { /* normal document/stream disposal */ }
          finally { reader.releaseLock(); }
        })();
      }
      return response;
    };
  });
  for (const label of ['tab_a', 'tab_b']) {
    const page = await context.newPage(); tabs.push(page);
    page.on('pageerror', error => errors.push({ tab: label, message: error.message }));
    page.on('request', request => {
      const url = new URL(request.url());
      if (request.method() === 'GET' && listPaths.has(url.pathname)) requests.push({ tab: label, phase, path: url.pathname, at: Date.now() });
    });
  }
  const [a, b] = tabs;
  await a.goto(origin + '/workspace?tab=drafts', { waitUntil: 'domcontentloaded' });
  const identity = await json(a, '/api/session/anonymous', 'POST', {});
  assert.equal(identity.principal_kind, 'anonymous');
  const gaps = await json(a, '/api/backend/v1/knowledge-gaps?limit=1');
  const gap = gaps.items[0]; assert.ok(gap?.object?.id, 'A real imported gap is required.');
  const sourceGap = { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision };
  await json(a, '/api/backend/v1/me/explorations', 'POST', { source_gap: sourceGap });
  await Promise.all(tabs.map(page => page.goto(origin + '/workspace?tab=drafts', { waitUntil: 'domcontentloaded' })));
  for (const page of tabs) {
    await page.waitForFunction(() => window.__workspaceProof.frames.some(frame => frame.event === 'ready'), null, { timeout: 30_000 });
    await page.locator('#workspace-results[aria-busy="false"]').waitFor({ timeout: 30_000 });
  }
  phase = 'create';
  const name = 'Event proof ' + Date.now();
  const composer = { source_gap: sourceGap, eaggl_anchors: [], dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg', 'prokn'] };
  draft = await json(a, '/api/backend/v1/drafts', 'POST', { composer, name });
  await Promise.all(tabs.map(page => waitDraft(page, draft.id, name)));
  phase = 'rename';
  const renamed = name + ' renamed from tab B';
  draft = await json(b, '/api/backend/v1/drafts/' + draft.id, 'PATCH', { expected_version: draft.version, name: renamed });
  await Promise.all(tabs.map(page => waitDraft(page, draft.id, renamed)));
  for (let index = 0; index < tabs.length; index++) {
    const page = tabs[index];
    await page.screenshot({ path: resolve(output, `workspace-push-tab-${index + 1}.png`), fullPage: true });
  }
  phase = 'delete';
  await json(a, '/api/backend/v1/drafts/' + draft.id, 'DELETE', { expected_version: draft.version });
  const deletedId = draft.id; draft = null;
  for (const page of tabs) await page.waitForFunction(id => ![...document.querySelectorAll('.workspace-draft-row')].some(option => option.dataset.draftId === id), deletedId, { timeout: 25_000 });
  for (const page of tabs) await page.locator('#workspace-results[aria-busy="false"]').waitFor({ timeout: 30_000 });
  // Allow already-pushed mutation invalidations to finish before the quiet window.
  await new Promise(resolve => setTimeout(resolve, 2_000));
  const pushes = await Promise.all(tabs.map(page => page.evaluate(() => window.__workspaceProof)));
  for (const proof of pushes) assert.ok(proof.frames.filter(frame => frame.event === 'workspace_change' && frame.collections.includes('drafts')).length >= 3, 'Each tab must see create, rename and delete through the actual SSE connection.');
  phase = 'idle'; const idleStarted = Date.now();
  await new Promise(resolve => setTimeout(resolve, idleMs));
  const idleRequests = requests.filter(request => request.phase === 'idle');
  assert.deepEqual(idleRequests, [], 'Idle tabs must not reload workspace collections on timers.');
  assert.deepEqual(errors, [], 'Browser must have no uncaught page errors.');
  const finalProof = await Promise.all(tabs.map(page => page.evaluate(() => window.__workspaceProof)));
  const report = { checked_at: new Date().toISOString(), origins: { frontend: origin, api }, real_gateway: true, anonymous_workspace: true,
    real_sse: true, intercepted_responses: 0, raw_mutations_bypass_client_invalidation: true,
    both_tabs_observed: { draft_create: true, draft_rename: true, draft_delete: true },
    per_tab: finalProof.map((proof, index) => ({ tab: index + 1, streams: proof.streams, draft_pushes: proof.frames.filter(frame => frame.event === 'workspace_change' && frame.collections.includes('drafts')).length })),
    idle_ms: Date.now() - idleStarted, idle_collection_requests: idleRequests.length, browser_errors: errors.length, scientific_jobs_submitted: 0,
    draft_removed: true, anonymous_workspace_expires_at: identity.workspace_expires_at,
    collection_requests_by_phase: Object.fromEntries(['setup', 'create', 'rename', 'delete', 'idle'].map(value => [value, requests.filter(request => request.phase === value).length])) };
  await writeFile(resolve(output, 'verification.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} finally {
  if (draft && tabs[0] && !tabs[0].isClosed()) {
    try {
      const current = await json(tabs[0], '/api/backend/v1/drafts/' + draft.id);
      await json(tabs[0], '/api/backend/v1/drafts/' + draft.id, 'DELETE', { expected_version: current.version });
    } catch { console.error('Temporary probe draft cleanup could not be confirmed.'); }
  }
  await browser.close();
}
