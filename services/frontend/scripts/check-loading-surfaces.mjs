#!/usr/bin/env node
/** Local browser UI fixtures only; every API request is intercepted. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.LOADING_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.LOADING_AUDIT_DIR || resolve(root, '.runtime/loading-surfaces-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0], account = fixture.account.document.scientific_accounts.find(a => a.id === fixture.account.root_id);
const principal = { user_id: '11111111-1111-4111-8111-111111111111', display_name: 'Loading fixture', principal_kind: 'anonymous', workspace_expires_at: null, email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null };
const empty = { items: [], page: { next_cursor: null, has_more: false } };
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(n => /^chromium-\d+$/.test(n)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* next browser */ }
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked public loading UI. All API calls intercepted in-browser; no real authentication, backend reads, database writes, research jobs or model calls.', fixtureContractSha256: fixture.contractSha256, status: 'running', scenarios: [] };
async function harness(name, viewport, held, anonymous = true) {
  const context = await browser.newContext({ viewport, reducedMotion: viewport.width < 500 ? 'reduce' : 'no-preference', serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = [], blocked = []; page.on('pageerror', e => errors.push(e.message));
  page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
  await context.addInitScript(({ fixture, principal, held, anonymous }) => {
    const original = window.fetch.bind(window);
    const empty = { items: [], page: { next_cursor: null, has_more: false } };
    const state = { calls: [], pending: [], unexpected: [], held: new Set(held), release(key, body, status = 200) {
      for (const item of this.pending.filter(item => item.key === key && !item.done)) { item.done = true; item.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })); }
    } };
    window.__loadingFixture = state;
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (!url.pathname.startsWith('/api/')) return original(input, init);
      const path = decodeURIComponent(url.pathname), method = init?.method || input?.method || 'GET';
      const key = path.endsWith('/knowledge-gaps/search') ? path + '?' + url.searchParams.get('q') : path;
      state.calls.push({ key, method });
      const answer = value => Promise.resolve(new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } }));
      if (state.held.has(key)) return new Promise(resolve => state.pending.push({ key, resolve, done: false }));
      if (method === 'GET' && path === '/api/session/status') return answer({ principal: anonymous ? { user_id: principal.user_id } : null, canClaim: false, canAdmin: false, providers: { google: false, orcid: false } });
      if (method === 'GET' && path === '/api/backend/v1/me') return answer(principal);
      if (method === 'GET' && path === '/api/backend/v1/knowledge-gaps') return answer(fixture.gaps);
      if (method === 'GET' && path === '/api/backend/v1/knowledge-gaps/search') return answer({ items: fixture.gaps.items.map(gap => ({ gap })), page: empty.page });
      if (method === 'GET' && /^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/accounts$/.test(path)) return answer(empty);
      if (method === 'GET' && ['/api/backend/v1/drafts', '/api/backend/v1/jobs', '/api/backend/v1/research-requests'].includes(path)) return answer(empty);
      state.unexpected.push(`${method} ${path}`);
      return Promise.resolve(new Response(JSON.stringify({ detail: 'Unmocked API request blocked.' }), { status: 500, headers: { 'content-type': 'application/json' } }));
    };
  }, { fixture, principal, held, anonymous });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { blocked.push(url.pathname); return route.abort('blockedbyclient'); }
    assert.equal(route.request().method(), 'GET'); return route.continue();
  });
  const result = { name, viewport, checks: [] }; report.scenarios.push(result);
  return { page, context, result,
    async release(key, body, status = 200) { await page.evaluate(({ key, body, status }) => window.__loadingFixture.release(key, body, status), { key, body, status }); },
    async waitRequest(key, count = 1) { await page.waitForFunction(({ key, count }) => window.__loadingFixture?.calls.filter(call => call.key === key).length >= count, { key, count }); },
    async shot(suffix) { await page.screenshot({ path: resolve(output, `${name}-${suffix}.png`), fullPage: true }); },
    async check() {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal overflow');
      const state = await page.evaluate(() => ({ calls: window.__loadingFixture.calls, unexpected: window.__loadingFixture.unexpected }));
      assert.deepEqual(errors, []); assert.deepEqual(blocked, []); assert.deepEqual(state.unexpected, []);
      result.calls = state.calls; result.pageErrors = errors; result.blockedUnexpected = blocked;
    },
    async close() { await context.close(); },
  };
}
async function loading(h, title) {
  const surface = h.page.locator('.loading-surface').filter({ has: h.page.getByText(title, { exact: true }) });
  await surface.waitFor();
  assert.equal(await surface.locator('[role="status"]').count(), 1);
  if (await surface.locator('.loading-surface-skeleton').count()) assert.equal(await surface.locator('.loading-surface-skeleton').getAttribute('aria-hidden'), 'true');
  if (h.result.viewport.width < 500) {
    const animations = await surface.evaluate(e => [...e.querySelectorAll('*')].map(node => getComputedStyle(node).animationName));
    assert.ok(animations.every(name => name === 'none'), 'Reduced motion disables animation');
  }
  await h.check(); return surface;
}
async function workspace(viewport) {
  const key = '/api/backend/v1/me/explorations', accounts = '/api/backend/v1/accounts';
  const h = await harness(`workspace-${viewport.width}`, viewport, [key, accounts]);
  try {
    await h.page.goto(origin + '/workspace?tab=gaps'); await h.waitRequest(key);
    await loading(h, 'Loading your knowledge gaps');
    assert.equal(await h.page.getByText('Your next question starts here.', { exact: true }).count(), 0);
    await h.shot('pending'); await h.release(key, empty);
    await h.page.getByText('Your next question starts here.', { exact: true }).waitFor();
    await h.page.getByRole('tab', { name: 'Scientific accounts' }).click(); await h.waitRequest(accounts);
    await loading(h, 'Loading your scientific accounts');
    await h.release(accounts, { ...empty, items: [{ account, created_at: '2026-09-28T12:00:00Z', knowledge_gap: gap.object, claim_count: account.component_claims.length, research_statement: { status: 'not_requested', job_id: null, paragraph_id: null } }] });
    await h.page.getByRole('link', { name: account.name, exact: true }).waitFor();
    await h.check(); await h.shot('loaded'); h.result.checks.push('No false empty state while pending; resolved empty state; tab-specific skeleton; loaded scientific account; responsive and reduced motion');
  } finally { await h.close(); }
}
async function record(viewport, kind) {
  const object = kind === 'object', id = object ? gap.object.id : fixture.claim.root_id;
  const key = `/api/backend/v1/${object ? 'objects' : 'claims'}/${id}`;
  const h = await harness(`${kind}-${viewport.width}`, viewport, [key]);
  try {
    await h.page.goto(origin + `/${object ? 'id' : 'claims'}/${encodeURIComponent(id)}`); await h.waitRequest(key);
    await loading(h, object ? 'Opening scientific record' : 'Opening scientific claim'); await h.shot('pending');
    await h.release(key, { detail: 'Fixture: temporarily unavailable.' }, 503);
    await h.page.getByRole('alert').filter({ hasText: 'Fixture: temporarily unavailable.' }).waitFor();
    assert.equal(await h.page.locator('.loading-surface-skeleton').count(), 0);
    await h.page.getByRole('button', { name: 'Retry' }).click(); await h.waitRequest(key, 2);
    await loading(h, object ? 'Opening scientific record' : 'Opening scientific claim');
    await h.release(key, object ? { ...fixture.claim, root_id: id, object: gap.object } : fixture.claim); await h.page.locator('.loading-surface').waitFor({ state: 'hidden' });
    await h.check(); await h.shot('loaded'); h.result.checks.push('Record skeleton; visible error without shimmer; retry read; exact fixture data; responsive and reduced motion');
  } finally { await h.close(); }
}
async function composer(viewport) {
  const catalog = '/api/backend/v1/knowledge-gaps', search = '/api/backend/v1/knowledge-gaps/search?fixture-search', suggest = '/api/backend/v1/mechanisms/suggest';
  const h = await harness(`composer-${viewport.width}`, viewport, [catalog, search, suggest], false);
  try {
    await h.page.goto(origin + '/'); await h.waitRequest(catalog); await loading(h, 'Loading knowledge gaps');
    await h.release(catalog, fixture.gaps); await h.page.locator('.trend').first().waitFor();
    await h.page.getByRole('combobox', { name: 'Search DisMech knowledge gaps' }).fill('fixture-search'); await h.waitRequest(search);
    await loading(h, 'Searching knowledge gaps'); await h.shot('search');
    await h.release(search, { ...empty, items: [{ gap }] }); await h.page.getByRole('option').first().click(); await h.waitRequest(suggest);
    await h.page.getByRole('status').filter({ hasText: 'Finding anchors…' }).waitFor();
    assert.equal(await h.page.getByRole('button', { name: 'Let’s close this gap' }).isDisabled(), true);
    await h.shot('anchors'); await h.release(suggest, fixture.suggestions);
    await h.page.waitForFunction(() => document.querySelectorAll('.anchor-chips .chip').length > 0);
    assert.equal(await h.page.getByRole('status').filter({ hasText: 'Finding anchors…' }).count(), 0);
    await h.check(); await h.shot('selected'); h.result.checks.push('Featured skeleton; typed search skeleton; automatic anchor pulse; submit disabled only while waiting; anchors replace pending; no backend writes');
  } finally { await h.close(); }
}
try {
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    await workspace(viewport); await record(viewport, 'claim'); await record(viewport, 'object'); await composer(viewport);
  }
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.failure = error.stack; process.exitCode = 1; }
finally { await browser.close(); await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report, null, 2)); }
