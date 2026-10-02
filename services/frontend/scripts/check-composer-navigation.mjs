#!/usr/bin/env node
/** Mocked browser regression: home must not reopen a cached research job.
 * All API requests are intercepted; no real login, writes, or paid runs.
 * Optional NAVIGATION_BASE_URL, NAVIGATION_AUDIT_DIR, PLAYWRIGHT_MODULE,
 * PLAYWRIGHT_EXECUTABLE_PATH. Playwright is an external test tool.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.NAVIGATION_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.NAVIGATION_AUDIT_DIR || resolve(root, '.runtime/composer-navigation'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0], factor = fixture.suggestions.automatic_anchors[0].factor;
const userId = '11111111-1111-4111-8111-111111111111';
const draftId = '22222222-2222-4222-8222-222222222222';
const jobId = '33333333-3333-4333-8333-333333333333';
const now = '2026-09-29T12:00:00Z';
const composer = {
  source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision },
  eaggl_anchors: [{ reference: { source: 'eaggl', source_id: factor.source_id,
    source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }],
  dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: [],
};
const draft = { id: draftId, owner_user_id: userId, version: 1, name: 'Navigation draft', lifecycle: 'saved', composer, created_at: now, updated_at: now };
const failedJob = { id: jobId, kind: 'analysis', owner_user_id: userId, status: 'failed', stage: 'authoring_account',
  research_request_id: '44444444-4444-4444-8444-444444444444', input_account_id: null,
  created_at: now, updated_at: now, completed_at: now, result: null,
  failure: { code: 'AGENT_EXECUTION_FAILED', message: 'Navigation fixture failure', retryable: true },
  warnings: [], last_event_id: '0', links: {} };
const saved = job => ({ composer, gap, factors: { [factor.source_id]: factor }, draft, owner: userId, job });
const paging = items => ({ items, page: { has_more: false, next_cursor: null, snapshot_id: 'navigation-fixture' } });
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); }
  catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* use Playwright default */ }
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked browser/API regression only; no real authentication, database writes, or model calls.', status: 'running', scenarios: [] };
async function harness(name, snapshot, mobile = false, deleted = false, writes = null) {
  const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const requests = [], errors = [], unexpected = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.addInitScript(value => {
    if (!sessionStorage.getItem('navigation-fixture-seeded')) {
      sessionStorage.setItem('reveal:composer', JSON.stringify(value));
      sessionStorage.setItem('navigation-fixture-seeded', 'true');
    }
  }, snapshot);
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) { unexpected.push(`external ${request.url()}`); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    requests.push({ path, method: request.method() });
    const respond = json => route.fulfill({ status: 200, json });
    // A scenario may override its reads and expected writes; all other writes stay blocked.
    if (writes && await writes(route, request, path)) return;
    if (path === '/api/backend/v1/mechanisms/suggest') return respond({ ...fixture.suggestions, automatic_anchors: [fixture.suggestions.automatic_anchors[0]] });
    if (path === '/api/backend/v1/me/explorations') return respond(paging([]));
    if (path === '/api/backend/v1/drafts' && request.method() === 'POST') return respond({ ...draft, id: '66666666-6666-4666-8666-666666666666', ...request.postDataJSON() });
    if (request.method() !== 'GET') { unexpected.push(`${request.method()} ${path}`); return route.abort(); }
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (/^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/vote$/.test(path)) return respond({ upvotes: 0, downvotes: 0, score: 0, user_vote: 0 });
    if (path === '/api/session/status') return respond({ principal: { user_id: userId }, canClaim: false, providers: { google: true, orcid: true } });
    if (path === '/api/backend/v1/me') return respond({ user_id: userId, principal_kind: 'registered', display_name: 'Navigation fixture', person: null, orcid: null, orcid_authenticated: false });
    if (path.endsWith('/vote')) return respond(gap.votes || { upvotes: 0, downvotes: 0, score: 0, user_vote: null, can_vote: false });
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': fixture heartbeat\n\n' });
    if (path === '/api/backend/v1/knowledge-gaps') return respond(paging([gap]));
    if (path === `/api/backend/v1/knowledge-gaps/${gap.object.id}`) return respond(gap);
    if (/^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(path)) return respond(paging([]));
    if (path === `/api/backend/v1/drafts/${draftId}`) return deleted ? route.fulfill({ status: 404, json: { code: 'NOT_FOUND', detail: 'This draft was deleted.' } }) : respond(draft);
    if (path === `/api/backend/v1/research-requests/${failedJob.research_request_id}`) return respond({ id: failedJob.research_request_id, source_draft_id: draftId, composer });
    if (path.startsWith('/api/backend/v1/mechanisms/')) return respond(factor);
    if (path === `/api/backend/v1/jobs/${jobId}`) return respond(failedJob);
    if (path === `/api/backend/v1/jobs/${jobId}/events`) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 503, json: { detail: 'Unmocked route blocked.' } });
  });
  return { page, requests, async finish() {
    // Ordinary edits must never resurrect the former one-second autosave.
    await page.waitForTimeout(1200);
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    await page.screenshot({ path: resolve(output, name + '.png'), fullPage: true });
    report.scenarios.push({ name, status: 'passed', screenshot: name + '.png' });
    await context.close();
  } };
}
async function home(h) {
  await h.page.goto(origin);
  await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
  assert.equal(await h.page.getByRole('region', { name: 'Research activity', exact: true }).count(), 0);
  assert.equal(await h.page.locator('.selected-question').count(), 0);
}
async function runLink(h) {
  await h.page.goto(`${origin}/?job=${jobId}&draft=${draftId}`);
  await h.page.getByRole('region', { name: 'Research activity', exact: true }).waitFor();
  await h.page.getByRole('button', { name: 'Edit these inputs', exact: true }).waitFor();
  await h.page.locator('.selected-question').waitFor();
}
async function draftOnly(h) {
  assert.equal(await h.page.locator('.gap-scope-selector, .gap-browser, .gap-accounts, .gap-outcomes, .invitation').count(), 0);
  assert.equal(h.requests.filter(r => r.path === '/api/backend/v1/knowledge-gaps' || /\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(r.path)).length, 0);
}
try {
  for (const status of ['failed', 'running', 'succeeded', 'insufficient_evidence']) {
    const h = await harness(`home-after-${status}`, saved({ ...failedJob, status }), status === 'failed');
    await home(h);
    await h.page.reload();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    assert.equal(h.requests.filter(r => /\/(jobs|drafts)\//.test(r.path)).length, 0);
    await h.finish();
  }
  {
    const h = await harness('run-home-run-refresh', saved(failedJob));
    await runLink(h);
    const before = h.requests.filter(r => r.path.includes('/jobs/')).length;
    await home(h);
    assert.equal(h.requests.filter(r => r.path.includes('/jobs/')).length, before);
    await runLink(h);
    await h.page.reload();
    await h.page.getByRole('button', { name: 'Edit these inputs', exact: true }).waitFor();
    await h.finish();
  }
  for (const selector of [`draft=${draftId}`, `gap=${encodeURIComponent(gap.object.id)}`]) {
    const h = await harness(`selection-link-${selector.split('=')[0]}`, saved(failedJob));
    await h.page.goto(`${origin}/?${selector}`);
    await h.page.getByRole('button', { name: 'Search for a different knowledge gap', exact: true }).waitFor();
    await h.page.waitForFunction(() => sessionStorage.getItem('reveal:composer') === null);
    assert.equal(await h.page.getByRole('region', { name: 'Research activity', exact: true }).count(), 0);
    assert.equal(h.requests.filter(r => r.path.includes('/jobs/')).length, 0);
    if (selector.startsWith('draft=')) await draftOnly(h);
    else await h.page.getByRole('navigation', { name: 'Draft navigation' }).waitFor();
    await h.finish();
  }
  {
    const h = await harness('cold-draft-to-browsing', null, true);
    await h.page.goto(`${origin}/?draft=${draftId}`);
    await h.page.getByRole('navigation', { name: 'Draft navigation' }).waitFor();
    await draftOnly(h);
    await h.page.reload();
    await h.page.getByRole('navigation', { name: 'Draft navigation' }).waitFor();
    await draftOnly(h);
    await h.page.getByRole('button', { name: 'Search for a different knowledge gap', exact: true }).click();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    await h.page.locator('#gap-list-scope').waitFor();
    await h.page.locator('.gap-browser').waitFor();
    assert.equal(new URL(h.page.url()).searchParams.has('draft'), false);
    await h.finish();
  }
  {
    const h = await harness('bare-home-ignores-unsubmitted-draft', saved(null));
    await home(h);
    await h.page.reload();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    assert.equal(h.requests.filter(r => /\/drafts\//.test(r.path)).length, 0);
    await h.finish();
  }
  {
    const h = await harness('explicit-unsubmitted-draft-refresh', saved(null));
    await h.page.goto(`${origin}/?draft=${draftId}`);
    await h.page.getByRole('button', { name: 'Search for a different knowledge gap', exact: true }).waitFor();
    await h.page.reload();
    await h.page.getByRole('button', { name: 'Search for a different knowledge gap', exact: true }).waitFor();
    await draftOnly(h);
    await h.finish();
  }
  {
    const h = await harness('same-component-draft-to-home', saved(null));
    await h.page.goto(`${origin}/?draft=${draftId}`);
    await h.page.getByRole('navigation', { name: 'Draft navigation' }).waitFor();
    await h.page.evaluate(() => window.history.pushState(null, '', '/'));
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    assert.equal(await h.page.locator('.selected-question').count(), 0);
    assert.equal(await h.page.getByRole('navigation', { name: 'Draft navigation' }).count(), 0);
    await h.finish();
  }
  {
    const h = await harness('missing-draft-recovery', null, false, true);
    await h.page.goto(`${origin}/?draft=${draftId}`);
    await h.page.getByText('This draft was deleted.', { exact: true }).waitFor();
    await draftOnly(h);
    assert.equal(await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).count(), 0);
    await h.page.getByRole('link', { name: 'Return to knowledge gaps', exact: true }).click();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).waitFor();
    await h.page.locator('.gap-browser').waitFor();
    await h.finish();
  }
  for (const includeDraft of [true, false]) {
    const h = await harness(`run-after-draft-deletion-${includeDraft ? 'old-link' : 'workspace-link'}`, saved(failedJob), false, true);
    await h.page.goto(`${origin}/?job=${jobId}&${includeDraft ? `draft=${draftId}` : `gap=${encodeURIComponent(gap.object.id)}`}`);
    await h.page.getByRole('button', { name: 'Edit these inputs', exact: true }).waitFor();
    await h.page.locator('.anchor-chips .chip').waitFor({ state: 'attached' });
    assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:composer')), null);
    assert.equal(await h.page.locator('.selected-question').textContent(), gap.object.text);
    assert.equal(await h.page.locator('.anchor-chips .chip').count(), 1);
    assert.equal(await h.page.getByText('This draft was deleted.', { exact: true }).count(), 0);
    assert.ok(h.requests.some(request => request.path.includes('/research-requests/')));
    await h.finish();
  }
  {
    // A reference cutover dropped the open draft: the next save creates a replacement, which is
    // adopted in place. Only explicit Save writes; the editor stays locked while it is pending.
    const replacementId = '55555555-5555-4555-8555-555555555555';
    const created = [], patched = []; let dropped = false;
    let release; const held = new Promise(resolve => { release = resolve; });
    const h = await harness('cutover-dropped-draft-adopted', null, false, false, async (route, request, path) => {
      const body = request.method() === 'GET' ? null : request.postDataJSON();
      if (request.method() === 'GET' && dropped && path === `/api/backend/v1/drafts/${draftId}`) {
        await route.fulfill({ status: 404, json: { code: 'NOT_FOUND', detail: 'This draft was deleted.' } }); return true;
      }
      if (request.method() === 'PATCH' && path === `/api/backend/v1/drafts/${draftId}`) {
        dropped = true;
        await route.fulfill({ status: 404, json: { code: 'NOT_FOUND', detail: 'This draft was deleted.' } }); return true;
      }
      if (request.method() === 'POST' && path === '/api/backend/v1/drafts') {
        created.push(body.composer); await held;
        await route.fulfill({ status: 201, json: { ...draft, id: replacementId, composer: body.composer } }); return true;
      }
      if (request.method() === 'PATCH' && path === `/api/backend/v1/drafts/${replacementId}`) {
        patched.push(body.composer);
        await route.fulfill({ status: 200, json: { ...draft, id: replacementId, version: body.expected_version + 1, composer: body.composer } }); return true;
      }
      return false;
    });
    await h.page.goto(`${origin}/?draft=${draftId}`);
    await h.page.getByRole('navigation', { name: 'Draft navigation' }).waitFor();
    await h.page.getByText('Additional knowledge graphs', { exact: true }).click();
    const posted = h.page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/backend/v1/drafts');
    await h.page.getByRole('checkbox', { name: 'BiomarkerKG', exact: true }).check();
    await h.page.waitForTimeout(1200);
    assert.equal(created.length, 0); assert.equal(patched.length, 0);
    await h.page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await posted;
    assert.equal(await h.page.getByRole('checkbox', { name: 'ProKN', exact: true }).isDisabled(), true);
    release();
    await h.page.waitForURL(url => new URL(url).pathname === `/drafts/${replacementId}`);
    await h.page.getByRole('checkbox', { name: 'ProKN', exact: true }).check();
    await h.page.waitForTimeout(1200);
    assert.equal(patched.length, 0);
    await h.page.getByRole('button', { name: 'Save draft', exact: true }).click();
    for (let attempt = 0; attempt < 50 && !patched.some(item => item.selected_kgs.includes('prokn')); attempt++) await h.page.waitForTimeout(100);
    assert.deepEqual(created.map(item => item.selected_kgs), [['biomarkerkg']]);
    assert.deepEqual(patched.at(-1)?.selected_kgs, ['biomarkerkg', 'prokn']);
    assert.equal(h.requests.filter(r => r.method === 'GET' && r.path === `/api/backend/v1/drafts/${replacementId}`).length, 0);
    assert.equal(await h.page.getByRole('checkbox', { name: 'ProKN', exact: true }).isChecked(), true);
    assert.equal(await h.page.getByText('Restoring your saved question').count(), 0);
    await h.finish();
  }
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.failure = error.stack || error.message; process.exitCode = 1;
} finally {
  await browser.close();
  await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
