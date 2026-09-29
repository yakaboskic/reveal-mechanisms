#!/usr/bin/env node
/** Fully mocked public-reader/publication regression. Never publishes real data. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.PUBLICATION_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.PUBLICATION_AUDIT_DIR || resolve(root, '.runtime/publication-audit')); await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const spec = JSON.parse(await readFile(resolve(root, 'api/openapi.json'), 'utf8'));
const response = (path, method = 'get') => Object.values(spec.paths[path][method].responses['200'].content['application/json'].examples)[0].value;
const rendering = response('/v1/citations/render', 'post');
const accountId = fixture.account.root_id, account = fixture.account.document.scientific_accounts.find(node => node.id === accountId);
const ownerId = '11111111-1111-4111-8111-111111111111', otherId = '22222222-2222-4222-8222-222222222222';
const publication = (visibility = 'private', version = 0, can_manage = true) => ({ visibility, version, can_manage, published_at: visibility === 'public' ? '2026-09-28T12:00:00Z' : null, updated_at: '2026-09-28T12:00:00Z', has_unpublished_changes: false });
async function playwright() { if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE); try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); } }
async function executable() { if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH; const cache = resolve(homedir(), 'Library/Caches/ms-playwright'); for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) { const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'); try { await access(path); return path; } catch {} } }
async function until(check, description) { const started = performance.now(); while (!await check()) { assert.ok(performance.now() - started < 12000, `Timed out: ${description}`); await new Promise(resolve => setTimeout(resolve, 20)); } }
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked UI only. Publication, authentication, all account/claim/paragraph/citation/API responses are intercepted. No actual publication, write, model call or job.', status: 'running', scenarios: [] };
async function harness(name, { owner = false, signed = false, mobile = false, clock = false, existing = false, canClaim = false } = {}) {
  const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 980 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(12000); if (clock) await page.clock.install();
  const state = { user: owner ? ownerId : signed ? otherId : null, publication: publication(owner && !existing ? 'private' : 'public', existing ? 3 : 0, owner), requests: [], unexpected: [], errors: [], console: [], holdAccount: false, held: [], canClaim };
  page.on('pageerror', error => state.errors.push(error.message)); page.on('console', value => { if (value.type() === 'error') state.console.push(value.text()); });
  await context.addInitScript(() => {
    const original = fetch.bind(window); window.__publicationFixture = { calls: [] };
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href), method = init?.method || input?.method || 'GET';
      if (!url.pathname.endsWith('/publication') || method !== 'POST') return original(input, init);
      const headers = new Headers(init?.headers || input?.headers), body = init?.body;
      return new Promise(async resolve => { const value = body ? JSON.parse(body) : await input.clone().json(); window.__publicationFixture.calls.push({ body: value, key: headers.get('Idempotency-Key'), resolve, settled: false }); });
    };
  });
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), path = decodeURIComponent(url.pathname), method = req.method();
    if (url.origin !== origin) { state.unexpected.push(`${method} ${url.origin}${path}`); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/')) return route.continue();
    state.requests.push({ method, path }); const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return respond({ principal: state.user ? { user_id: state.user } : null, canClaim: state.canClaim, providers: { google: false, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: state.user, display_name: state.user === ownerId ? 'Owner fixture' : 'Other reader fixture', email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'registered', workspace_expires_at: null });
    if (path === '/api/session/claim') { state.user = otherId; state.publication = publication('public', state.publication.version, false); state.canClaim = false; return respond({}); }
    if (path === `/api/backend/v1/accounts/${accountId}`) {
      const result = structuredClone(fixture.account); result.publication = { ...state.publication, can_manage: state.user === ownerId };
      // Public fixtures deliberately retain a job ID: UI must still independently
      // gate private job access on ownership. Real backend strips these IDs.
      if (state.holdAccount) { state.held.push(() => respond(result)); return; }
      return respond(result);
    }
    if (path === `/api/backend/v1/accounts/${accountId}/publication`) return respond(state.publication);
    if (path.startsWith('/api/backend/v1/paragraphs/') && path.endsWith('/export')) return respond(response('/v1/paragraphs/{dapper_id}/export'));
    if (path.startsWith('/api/backend/v1/paragraphs/')) return respond(fixture.paragraph);
    if (path === '/api/backend/v1/citations/render') return respond(rendering);
    if (path.startsWith('/api/backend/v1/claims/')) return respond(fixture.claim);
    if (path.startsWith('/api/backend/v1/objects/')) return respond(response('/v1/objects/{dapper_id}'));
    state.unexpected.push(`${method} ${path}`); return route.fulfill({ status: 500, json: { code: 'UNEXPECTED_TEST_ROUTE', detail: 'Blocked unmocked API' } });
  });
  const result = { name, checks: [] }; report.scenarios.push(result);
  const h = { page, state, result,
    count: () => page.evaluate(() => window.__publicationFixture.calls.length),
    async respond(index, value, status = 200) { if (status === 200) state.publication = value; await page.evaluate(({ index, value, status }) => { const call = window.__publicationFixture.calls[index]; if (!call) throw Error("Missing publication fixture call"); call.settled = true; call.resolve(new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })); }, { index, value, status }); },
    async close() { result.publicationRequests = await page.evaluate(() => window.__publicationFixture.calls.map(({ body, key }) => ({ body, key }))); result.requests = state.requests; result.errors = state.errors; result.consoleErrors = state.console; result.unexpected = state.unexpected; await context.close(); },
  };
  await page.goto(`${origin}/accounts/${encodeURIComponent(accountId)}?view=conclusions`); await page.locator('.account-conclusions').waitFor(); return h;
}
async function publicRead() {
  for (const signed of [false, true]) {
    const h = await harness(signed ? 'signed-nonowner' : 'public-reader-mobile', { signed, mobile: !signed });
    try {
      await h.page.getByText('Published scientific account', { exact: true }).waitFor(); assert.equal(await h.page.locator('.account-publication').count(), 0);
      await h.page.getByRole('tab', { name: 'Research Statement', exact: true }).click(); await h.page.locator('.research-prose').waitFor();
      assert.ok(await h.page.locator('.research-references li').count() > 0); assert.equal(await h.page.locator('.paragraph-activity').count(), 0); assert.equal(await h.page.getByText('View statement activity', { exact: true }).count(), 0);
      assert.equal(await h.page.getByRole('button', { name: /Generate research statement|Retry paragraph generation|Unpublish|Publish account/ }).count(), 0);
      await h.page.getByRole('button', { name: 'Associated claims' }).click(); await h.page.locator('.account-claim-option').first().click();
      assert.ok((await h.page.locator('.claim-inline-body:not([hidden]) .claim-page-link').getAttribute('href')).startsWith('/claims/'));
      await h.page.getByText('Scientific identity and provenance', { exact: true }).click(); await h.page.getByRole('link', { name: 'Account record', exact: true }).waitFor();
      const width = await h.page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth })); assert.ok(width.page <= width.viewport + 1);
      assert.equal(await h.count(), 0); assert.ok(!h.state.requests.some(req => req.path.includes('/jobs')));
      await h.page.screenshot({ path: resolve(output, `${h.result.name}.png`), fullPage: true });
      h.result.checks.push('Published conclusions, cited statement, claim links and provenance visible without owner access', 'No job telemetry reads, research writes or publication controls', width);
      if (!signed) {
        const download = h.page.waitForEvent('download'); await h.page.getByRole('button', { name: 'Markdown', exact: true }).click(); await download;
        await h.page.locator('.claim-inline-body:not([hidden]) .claim-page-link').click(); await h.page.locator('.claim-detail-panel').waitFor();
        await h.page.getByRole('tab', { name: 'Evidence', exact: true }).click(); await h.page.getByRole('heading', { name: 'Evidence for this proposition', exact: true }).waitFor();
        await h.page.getByRole('tab', { name: 'Provenance', exact: true }).click(); await h.page.locator('.construction-provenance').waitFor();
        h.result.checks.push('Visitor downloads statement export and follows exact claim into Evidence and Provenance without login');
      }
    } finally { await h.close(); }
  }
}
async function publicationLifecycle() {
  const h = await harness('owner-publication-lifecycle', { owner: true, clock: true });
  try {
    const panel = h.page.locator('.account-publication'); await panel.getByText('Private account', { exact: true }).waitFor(); assert.equal(await h.count(), 0);
    await panel.getByRole('button', { name: 'Publish…', exact: true }).click(); await panel.getByText(/publicly viewable and downloadable/).waitFor(); await panel.getByText(/Job activity and workspace drafts stay private/).waitFor();
    await panel.getByRole('button', { name: 'Publish account', exact: true }).click(); await until(async () => await h.count() === 1, 'publication request');
    assert.ok(await panel.getByRole('button', { name: 'Publish account', exact: true }).isDisabled());
    await h.page.clock.fastForward(30100); await panel.getByRole('button', { name: 'Retry', exact: true }).waitFor();
    const publicState = publication('public', 1); await h.respond(0, publicState);
    await panel.getByRole('button', { name: 'Retry', exact: true }).click(); await until(async () => await h.count() === 2, 'same mutation retry'); await h.respond(1, publicState);
    await panel.getByText('Published account', { exact: true }).waitFor();
    const calls = await h.page.evaluate(() => window.__publicationFixture.calls.map(({ body, key }) => ({ body, key }))); assert.deepEqual(calls[0], calls[1]);
    await panel.getByRole('button', { name: 'Unpublish', exact: true }).click(); await until(async () => await h.count() === 3, 'unpublish'); await h.respond(2, publication('private', 2)); await panel.getByText('Private account', { exact: true }).waitFor();
    h.result.checks.push('No automatic publication; explicit snapshot disclosure precedes publishing', '30-second unknown response retries identical body/key; no duplicate click', 'Unpublish uses current version and restores private status');
  } finally { await h.close(); }
}
async function updateAndConflict() {
  const h = await harness('owner-update-conflict', { owner: true, existing: true });
  try {
    const panel = h.page.locator('.account-publication');
    await panel.getByRole('button', { name: 'Update published snapshot' }).click(); await panel.getByRole('button', { name: 'Update public snapshot', exact: true }).click(); await until(async () => await h.count() === 1, 'update publication request');
    h.state.publication = { ...publication('public', 4), has_unpublished_changes: true };
    await h.respond(0, { code: 'VERSION_CONFLICT', detail: 'Publication changed in another session.' }, 409);
    await panel.getByText(/Review its current visibility/).waitFor(); await panel.getByText(/A newer accepted research statement is still private/).waitFor();
    await panel.getByRole('button', { name: 'Update public snapshot', exact: true }).click(); await until(async () => await h.count() === 2, 'explicit refreshed update');
    const calls = await h.page.evaluate(() => window.__publicationFixture.calls.map(({ body, key }) => ({ body, key })));
    assert.equal(calls[0].body.expected_version, 3); assert.equal(calls[1].body.expected_version, 4); assert.notEqual(calls[0].key, calls[1].key);
    await h.respond(1, publication('public', 5)); await until(async () => await panel.locator('.publication-confirmation').count() === 0, 'updated snapshot confirmed');
    h.result.checks.push('Update is an explicit owner action; conflict refreshes version before a new choice/key', 'New accepted statements are visibly marked private until snapshot update');
  } finally { await h.close(); }
}
async function identityChange() {
  const h = await harness('owner-to-public-identity', { owner: true, existing: true, canClaim: true });
  try {
    await h.page.locator('.account-publication').waitFor(); h.state.holdAccount = true;
    await h.page.getByRole('button', { name: 'Move my anonymous work' }).click(); await until(() => h.state.held.length > 0, 'new identity account lookup');
    assert.equal(await h.page.locator('.account-conclusions').count(), 0); assert.equal(await h.page.locator('.account-publication').count(), 0);
    h.state.holdAccount = false; for (const release of h.state.held.splice(0)) await release();
    await h.page.getByText('Published scientific account', { exact: true }).waitFor(); assert.equal(await h.page.locator('.account-publication').count(), 0);
    await h.page.getByRole('tab', { name: 'Research Statement', exact: true }).click(); await h.page.locator('.research-prose').waitFor(); assert.equal(await h.page.locator('.paragraph-activity').count(), 0);
    h.result.checks.push('Identity change hides old private account/control state before fresh public response', 'Public reread cannot inherit owner paragraph telemetry');
  } finally { await h.close(); }
}
try {
  for (const [name, run] of Object.entries({ publicRead, publicationLifecycle, updateAndConflict, identityChange })) if (!process.env.PUBLICATION_SCENARIO_FILTER || name.includes(process.env.PUBLICATION_SCENARIO_FILTER)) await run();
  for (const result of report.scenarios) { assert.deepEqual(result.errors, []); assert.deepEqual(result.consoleErrors, []); assert.deepEqual(result.unexpected, []); assert.ok(!result.requests.some(req => req.path.includes('/jobs'))); }
  report.status = 'passed'; console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
} catch (failure) { report.status = 'failed'; report.failure = failure.stack; throw failure; }
finally { await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2)); await browser.close(); }
