#!/usr/bin/env node
/** Local browser regression; all API calls mocked, including validation of retained output. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.REVIEW_RETRY_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname));
const output = resolve(root, '.runtime/review-retry/browser'); await mkdir(output, { recursive: true });
async function playwright() {
  try { return await import('playwright'); }
  catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache)).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch {}
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { status: 'running', scope: 'Mocked UI only: no real API writes, sign-in or model requests.', scenarios: [] };
const userId = '11111111-1111-4111-8111-111111111111', jobId = '33333333-3333-4333-8333-333333333333';
const requestId = '44444444-4444-4444-8444-444444444444';
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const exchange = JSON.parse(await readFile(resolve(root, 'api/examples/getResearchRequest.request.json'), 'utf8'));
const frozenRequest = { ...Object.values(exchange.responses['200'].examples)[0], id: requestId, owner_user_id: userId };
const gap = fixture.gaps.items.find(item => item.object.id === frozenRequest.composer.source_gap.id);
const factor = fixture.suggestions.automatic_anchors.map(item => item.factor).find(item => item.source_id === frozenRequest.composer.eaggl_anchors[0].reference.source_id);
assert.ok(gap && factor, 'Saved-input fixtures must match the frozen research request.');
const emptyPage = { items: [], page: { has_more: false, next_cursor: null, snapshot_id: 'retained-output-fixture' } };
const original = { id: jobId, kind: 'analysis', owner_user_id: userId, status: 'failed', stage: 'validating',
  research_request_id: requestId, input_account_id: null,
  created_at: '2026-09-29T12:00:00Z', updated_at: '2026-09-29T12:10:00Z', completed_at: '2026-09-29T12:10:00Z',
  result: null, failure: { code: 'REVIEW_BUDGET_EXCEEDED', retryable: true,
    message: 'Independent scientific review stopped to stay within its $0.30 budget. Recorded review spend: $0.2079. No scientific verdict was reached.',
    budget: { scope: 'review', limit_usd: .3, spent_usd: .207882, next_call_max_usd: .15 } }, warnings: [], last_event_id: '2', links: {} };
try {
  for (const mode of ['review-budget', 'review-unavailable', 'authoring-budget']) {
    const context = await browser.newContext({ viewport: mode === 'review-budget' ? { width: 390, height: 844 } : { width: 1280, height: 900 }, serviceWorkers: 'block' });
    const page = await context.newPage(); page.setDefaultTimeout(15000);
    const state = { job: structuredClone(original), retries: [], unexpected: [], errors: [] };
    if (mode === 'review-unavailable') state.job.failure = { code: 'REVIEW_UNAVAILABLE', retryable: true, message: 'The independent scientific review could not complete; no scientific verdict was reached.' };
    if (mode === 'authoring-budget') state.job.failure = { code: 'AUTHORING_BUDGET_EXCEEDED', retryable: true, message: 'The research agent stopped at its $25.00 authoring budget. Final authoring spend was not reported.' };
    let release; const held = new Promise(resolve => { release = resolve; });
    page.on('pageerror', error => state.errors.push(error.message));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      if (url.origin !== origin) { state.unexpected.push('external'); return route.abort(); }
      if (!path.startsWith('/api/')) return route.continue();
      const reply = (json, status = 200) => route.fulfill({ status, json });
      if (path === `/api/backend/v1/jobs/${jobId}/retry-review` && request.method() === 'POST') {
        state.retries.push({ key: request.headers()['idempotency-key'], body: request.postDataJSON() });
        if (state.retries.length === 1) { await held; return reply({ code: 'UNAVAILABLE', detail: 'Retry confirmation unavailable.' }, 503); }
        state.job = { ...state.job, status: 'queued', stage: 'validating', failure: null, completed_at: null, last_event_id: '3' };
        return reply(state.job, 202);
      }
      if (request.method() !== 'GET') { state.unexpected.push(request.method() + ' ' + path); return route.abort(); }
      if (path === '/api/session/status') return reply({ principal: { user_id: userId }, canClaim: false, providers: { google: false, orcid: false } });
      if (path === '/api/backend/v1/me') return reply({ user_id: userId, principal_kind: 'registered', display_name: 'Review fixture', workspace_expires_at: null });
      if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': local fixture heartbeat\n\n' });
      if (path === `/api/backend/v1/research-requests/${requestId}`) return reply(frozenRequest);
      if (decodeURIComponent(path) === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return reply(gap);
      if (decodeURIComponent(path) === `/api/backend/v1/knowledge-gaps/${gap.object.id}/vote`) return reply(gap.votes || { upvotes: 0, downvotes: 0, score: 0, user_vote: null, can_vote: false });
      if (['accounts', 'outcomes'].some(kind => decodeURIComponent(path) === `/api/backend/v1/knowledge-gaps/${gap.object.id}/${kind}`)) return reply(emptyPage);
      if (decodeURIComponent(path) === `/api/backend/v1/mechanisms/${factor.source_id}`) return reply(factor);
      if (path === `/api/backend/v1/jobs/${jobId}`) return reply(state.job);
      if (path === `/api/backend/v1/jobs/${jobId}/events`) return route.fulfill({ contentType: 'text/event-stream', body: '' });
      state.unexpected.push(request.method() + ' ' + path); return reply({}, 503);
    });
    await page.goto(`${origin}/?job=${jobId}`);
    await page.getByRole('alert').filter({ hasText: state.job.failure.message }).waitFor();
    const retry = page.getByRole('button', { name: 'Save existing output', exact: true });
    if (mode === 'authoring-budget') {
      assert.equal(await retry.count(), 0);
    } else {
      await page.getByText('Checks the saved output and sources, then saves the result if validation passes. No new research or AI review runs.', { exact: true }).waitFor();
      await retry.click();
      assert.equal(await page.getByRole('button', { name: 'Queueing validation…', exact: true }).isDisabled(), true);
      release();
      await page.getByText('Retry confirmation unavailable.', { exact: true }).waitFor();
      await retry.click();
      await page.getByRole('button', { name: 'Stop', exact: true }).waitFor();
      assert.equal(state.retries.length, 2);
      assert.deepEqual(state.retries[0], state.retries[1], 'Unknown response must reuse the same key and event version');
      assert.deepEqual(state.retries[0].body, { expected_last_event_id: '2' });
      assert.ok(state.retries[0].key);
      assert.equal(await retry.count(), 0);
      state.job = { ...state.job, status: 'failed', completed_at: '2026-09-29T12:11:00Z', last_event_id: '4',
        failure: { code: 'VALIDATION_FAILED', retryable: true, message: 'The account failed deterministic source validation.' } };
      await page.getByText(state.job.failure.message, { exact: true }).waitFor();
      assert.equal(await retry.count(), 0, 'Validation failure must not offer recovery for a historical incomplete review');
    }
    assert.deepEqual(state.unexpected, []); assert.deepEqual(state.errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: resolve(output, mode + '.png'), fullPage: true });
    report.scenarios.push({ name: mode, status: 'passed' }); await context.close();
  }
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.failure = error.stack || error.message; process.exitCode = 1; }
finally { await browser.close(); await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify(report, null, 2)); }
