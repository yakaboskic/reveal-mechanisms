#!/usr/bin/env node
/** Fully intercepted browser checks: no real authentication, writes, jobs or model calls. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = process.env.SESSION_MENU_BASE_URL || 'http://127.0.0.1:3000';
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(root, '.runtime/session-menu-audit');
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const userId = '11111111-1111-4111-8111-111111111111';
const factors = [structuredClone(fixture.suggestions.automatic_anchors[0].factor), structuredClone(fixture.suggestions.automatic_anchors[1].factor)];
Object.assign(factors[0].cfde_anchor, { label: 'P53 Pathway Regulation', subtitle: 'Coronary artery disease in type 2 diabetes (Factor1)' });
Object.assign(factors[1].cfde_anchor, { label: 'Liver Cell Gene Modules', subtitle: 'eGFRcys (Factor4)' });
const gap = fixture.gaps.items[0];
const composer = { source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision },
  eaggl_anchors: factors.map(factor => ({ reference: { source: 'eaggl', source_id: factor.source_id, source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null })),
  dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: [] };
const job = { id: '22222222-2222-4222-8222-222222222222', kind: 'analysis', owner_user_id: userId,
  status: 'insufficient_evidence', stage: 'authoring_account', research_request_id: 'mock-request', input_account_id: null,
  created_at: '2026-09-28T09:00:00Z', updated_at: '2026-09-28T09:01:00Z', completed_at: '2026-09-28T09:01:00Z',
  result: null, failure: null, warnings: [], last_event_id: '0', links: {} };
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache)).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch {}
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked browser/API fixtures only; displayed scientific labels are test data. No real backend, sign-in, jobs, or external calls.', status: 'running', scenarios: [] };
async function harness(name, { mobile = false, registered = false, google = false, chips = false, frozen = false } = {}) {
  const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(12000);
  const errors = [], unexpected = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  if (chips) await context.addInitScript(value => sessionStorage.setItem('reveal:composer', JSON.stringify(value)), {
    composer, gap, factors: Object.fromEntries(factors.map(factor => [factor.source_id, factor])), draft: null, owner: userId, job: frozen ? job : null });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname), method = request.method();
    if (url.origin !== origin) { unexpected.push(`${method} external ${path}`); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return respond({ principal: { user_id: userId }, canClaim: false, canAdmin: true, providers: { google, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: userId, principal_kind: registered ? 'registered' : 'anonymous', display_name: registered ? 'Researcher with a long display name for responsive testing' : null, workspace_expires_at: '2026-10-27T00:00:00Z', person: null, orcid: null, orcid_authenticated: false });
    if (path === '/api/backend/v1/knowledge-gaps') return respond({ ...fixture.gaps, items: [] });
    if (/^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(path)) return respond({ items: [], page: { has_more: false, next_cursor: null, snapshot_id: 'mock' } });
    if (path === '/api/backend/v1/knowledge-gaps/' + gap.object.id) return respond(gap);
    if (path === '/api/backend/v1/mechanisms/suggest') return respond({ ...fixture.suggestions, automatic_anchors: [] });
    if (path === '/api/backend/v1/mechanisms/search') return respond({ items: factors.map(record => ({ record, ranking: { rank: 1, value: 1, metric: 'lexical_rank' } })), next_cursor: null, search: fixture.suggestions.search });
    if (path.startsWith('/api/backend/v1/mechanisms/')) return respond(factors.find(factor => path.endsWith(factor.source_id)) || factors[0]);
    if (path === '/api/backend/v1/me/explorations') return respond(method === 'GET' ? { items: [], page: { has_more: false, next_cursor: null, snapshot_id: 'mock' } } : {});
    if (path === '/api/backend/v1/drafts' || path === '/api/backend/v1/drafts/mock-draft') return respond({ id: 'mock-draft', version: 1, composer: request.postDataJSON().composer, created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z' });
    if (path === `/api/backend/v1/jobs/${job.id}/events`) return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (path === `/api/backend/v1/jobs/${job.id}/outcome`) return respond({ id: 'mock-outcome', summary: 'Mocked insufficient-evidence explanation.', publication: { visibility: 'private', can_manage: true } });
    if (path === `/api/backend/v1/jobs/${job.id}`) return respond(job);
    unexpected.push(`${method} ${path}`); return route.fulfill({ status: 503, json: { title: 'Unexpected mocked request' } });
  });
  await page.goto(origin);
  return { context, page, async finish() {
    assert.deepEqual(unexpected, []); assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: resolve(output, name + '.png'), fullPage: true });
    report.scenarios.push({ name, status: 'passed', screenshot: name + '.png' }); await context.close();
  } };
}
try {
  for (const mobile of [false, true]) {
    const h = await harness(`anonymous-${mobile ? 'mobile' : 'desktop'}`, { mobile });
    await h.page.getByRole('button', { name: 'Your workspace', exact: true }).click();
    await h.page.getByText('Anonymous workspace', { exact: true }).waitFor();
    const menu = h.page.locator('.workspace-menu');
    assert.equal(await menu.locator('.provider-note').count(), 1);
    assert.equal(await menu.getByRole('button', { name: 'Continue with Google' }).isDisabled(), true);
    assert.equal(await menu.getByRole('button', { name: 'Continue with ORCID' }).isDisabled(), true);
    assert.equal(await menu.getByText('Admin telemetry', { exact: true }).count(), 1);
    const box = await menu.boundingBox();
    assert.ok(box.height < (mobile ? 380 : 350), `Menu stays compact with larger mobile touch targets (observed ${box.height}px)`);
    await h.page.keyboard.press('Escape'); assert.equal(await menu.count(), 0);
    assert.equal(await h.page.getByRole('button', { name: 'Your workspace', exact: true }).evaluate(button => button === document.activeElement), true);
    await h.page.getByRole('button', { name: 'Your workspace', exact: true }).click();
    await h.finish();
  }
  {
    const h = await harness('one-provider-available', { google: true });
    await h.page.getByRole('button', { name: 'Your workspace', exact: true }).click();
    await h.page.getByText('Anonymous workspace', { exact: true }).waitFor();
    const menu = h.page.locator('.workspace-menu');
    assert.equal(await menu.getByRole('button', { name: 'Continue with Google' }).isEnabled(), true);
    assert.equal(await menu.getByRole('button', { name: 'Continue with ORCID' }).isDisabled(), true);
    assert.match(await menu.locator('.provider-note').innerText(), /^ORCID sign-in/);
    await h.finish();
  }
  {
    const h = await harness('registered-mobile', { registered: true, mobile: true });
    await h.page.getByRole('button', { name: 'Your workspace', exact: true }).click();
    await h.page.getByText('Researcher with a long display name for responsive testing', { exact: true }).waitFor();
    assert.equal(await h.page.locator('.workspace-menu .provider-options').count(), 0);
    await h.page.getByRole('link', { name: 'Your scientific accounts', exact: true }).waitFor();
    await h.finish();
  }
  for (const frozen of [false, true]) {
    const h = await harness(frozen ? 'frozen-job-traits-mobile' : 'selected-and-picker-traits', { chips: true, frozen, mobile: frozen });
    await h.page.locator('.anchor-chips .mechanism-trait').first().waitFor();
    assert.deepEqual(await h.page.locator('.anchor-chips .mechanism-name').allTextContents(), ['P53 Pathway Regulation', 'Liver Cell Gene Modules']);
    assert.deepEqual(await h.page.locator('.anchor-chips .mechanism-trait').allTextContents(), ['Coronary artery disease in type 2 diabetes', 'eGFRcys']);
    if (frozen) assert.equal(await h.page.locator('.anchor-chips .remove').count(), 0);
    else {
      await h.page.getByRole('button', { name: 'Add a mechanism anchor', exact: true }).click();
      await h.page.getByLabel('Search possible genetic mechanisms', { exact: true }).fill('liver');
      await h.page.locator('.mechanism-match .mechanism-trait').first().waitFor();
      assert.deepEqual(await h.page.locator('.mechanism-match .mechanism-trait').allTextContents(), ['Coronary artery disease in type 2 diabetes', 'eGFRcys']);
    }
    await h.finish();
  }
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.error = error.stack; throw error; }
finally { await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
