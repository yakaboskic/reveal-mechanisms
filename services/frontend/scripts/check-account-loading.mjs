#!/usr/bin/env node
/** Mocked local browser verification; never reads a real account or starts a job.
 * node services/frontend/scripts/check-account-loading.mjs
 * Optional ACCOUNT_LOADING_BASE_URL, ACCOUNT_LOADING_AUDIT_DIR,
 * ACCOUNT_LOADING_SCENARIO_FILTER, PLAYWRIGHT_MODULE, PLAYWRIGHT_EXECUTABLE_PATH.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.ACCOUNT_LOADING_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Use a local frontend only');
const output = resolve(process.env.ACCOUNT_LOADING_AUDIT_DIR || resolve(root, '.runtime/account-loading-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const account = fixture.account.document.scientific_accounts.find(item => item.id === fixture.account.root_id);
assert.ok(account?.name, 'Contract fixture must contain its named account');
const accountPath = `/accounts/${encodeURIComponent(account.id)}`;
const userId = '11111111-1111-4111-8111-111111111111';
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/')
    ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); }
  catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* use the installed Playwright default */ }
  }
}
async function until(check, description, timeout = 10000) {
  const started = performance.now();
  while (!await check()) {
    assert.ok(performance.now() - started < timeout, `Timed out: ${description}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked account-loading UI only. Controlled in-browser account responses; all other API requests intercepted. No real account/backend read, authentication, database write, or research job.',
  fixtureContractSha256: fixture.contractSha256, baseUrl: origin, status: 'running', scenarios: [] };

async function harness(name, viewport, { clock = false, reducedMotion = false } = {}) {
  const context = await browser.newContext({ viewport, serviceWorkers: 'block', reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  if (clock) { assert.equal(typeof page.clock?.install, 'function', 'Playwright clock is required for bounded timeout checks'); await page.clock.install(); }
  const state = { requests: [], errors: [], consoleErrors: [], unexpected: [] };
  page.on('pageerror', error => state.errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') state.consoleErrors.push(message.text()); });
  await context.addInitScript(accountId => {
    const original = window.fetch.bind(window);
    const mock = { calls: [], unexpected: [], respondPending(value) {
      for (const call of this.calls) if (!call.settled) {
        call.settled = true;
        call.resolve(new Response(JSON.stringify(value.body), { status: value.status, headers: { 'content-type': 'application/json' } }));
      }
    } };
    window.__accountLoadingFixture = mock;
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (decodeURIComponent(url.pathname) !== `/api/backend/v1/accounts/${accountId}`) return original(input, init);
      const method = init?.method || input?.method || 'GET';
      if (method !== 'GET') { mock.unexpected.push(`${method} ${url.pathname}`); return Promise.reject(new Error('Unexpected account mutation blocked by fixture')); }
      const signal = init?.signal || input?.signal;
      return new Promise(resolve => {
        const call = { method, path: url.pathname, resolve, settled: false, aborted: Boolean(signal?.aborted) };
        mock.calls.push(call);
        // Deliberately allow a late response after abort. The application deadline
        // must settle the UI independently and ignore that late response.
        signal?.addEventListener('abort', () => { call.aborted = true; }, { once: true });
      });
    };
  }, account.id);
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== origin) { state.unexpected.push(`${request.method()} external ${url.origin}${path}`); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/')) { assert.equal(request.method(), 'GET'); return route.continue(); }
    state.requests.push({ method: request.method(), path });
    if (request.method() === 'GET' && path === '/api/session/status') return route.fulfill({ status: 200, json: { principal: { user_id: userId }, canClaim: false, canAdmin: false, providers: { google: false, orcid: false } } });
    if (request.method() === 'GET' && path === '/api/backend/v1/me') return route.fulfill({ status: 200, json: { user_id: userId, display_name: 'Account loading fixture', email: null,
      email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'anonymous', workspace_expires_at: null } });
    state.unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 500, json: { code: 'UNEXPECTED_TEST_ROUTE', detail: 'Blocked unmocked API route.' } });
  });
  const result = { name, viewport, reducedMotion, checks: [] }; report.scenarios.push(result);
  return { page, state, result,
    count: () => page.evaluate(() => window.__accountLoadingFixture.calls.length),
    async respond(body = fixture.account, status = 200) { await page.evaluate(value => window.__accountLoadingFixture.respondPending(value), { body, status }); },
    async open() {
      await page.goto(`${origin}${accountPath}?view=conclusions`);
      await until(async () => await page.evaluate(() => window.__accountLoadingFixture.calls.length) > 0, 'intercepted account request is pending');
    },
    async close() {
      result.accountRequests = await page.evaluate(() => window.__accountLoadingFixture.calls.map(({ method, path, settled, aborted }) => ({ method, path, settled, aborted })));
      state.unexpected.push(...await page.evaluate(() => window.__accountLoadingFixture.unexpected));
      result.pageErrors = state.errors; result.consoleErrors = state.consoleErrors;
      result.blockedUnexpectedRequests = state.unexpected; result.otherApiMethods = [...new Set(state.requests.map(request => request.method))];
      await context.close();
    },
  };
}
const loading = h => h.page.locator('.account-loading');
const loadedAccount = h => h.page.locator('.account-study:not(.account-loading)');
async function noOverflow(h) {
  const dimensions = await h.page.evaluate(() => ({ pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth }));
  assert.ok(dimensions.pageWidth <= dimensions.viewportWidth + 1, 'Account screen has no horizontal overflow');
  return dimensions;
}
async function screenshot(h, suffix) { await h.page.screenshot({ path: resolve(output, `${h.result.name}-${suffix}.png`), fullPage: true }); }
async function pending(h, title = 'Opening scientific account') {
  await loading(h).waitFor();
  await loading(h).getByText(title, { exact: true }).waitFor();
  assert.equal(await loadedAccount(h).count(), 0, 'No scientific result is invented while loading');
  assert.equal(await loading(h).locator('.account-skeleton[aria-hidden="true"]').count(), 1, 'Decorative skeleton is hidden from assistive technology');
  const nav = loading(h).getByRole('navigation', { name: 'Account navigation' }); await nav.waitFor();
  assert.equal(await nav.getByRole('link', { name: /Your scientific accounts/ }).getAttribute('href'), '/workspace?tab=accounts');
  assert.equal(await nav.getByRole('link', { name: 'Search knowledge gaps', exact: true }).getAttribute('href'), '/');
  await noOverflow(h);
}
async function success(h) {
  await loadedAccount(h).waitFor();
  await h.page.getByRole('heading', { name: account.name, exact: true }).waitFor();
  assert.equal(await loading(h).count(), 0, 'Skeleton is replaced by the exact fixture account');
  assert.equal(new URL(h.page.url()).pathname, accountPath, 'Account URL stays stable');
  await noOverflow(h);
  assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.consoleErrors, []); assert.deepEqual(h.state.unexpected, []);
}
async function delayedScenario(name, viewport, reducedMotion = false) {
  const h = await harness(name, viewport, { clock: true, reducedMotion });
  try {
    await h.open(); await pending(h); await screenshot(h, 'opening');
    h.result.checks.push('Account-shaped skeleton and working navigation appear while account data is pending; no result text or horizontal overflow');
    if (reducedMotion) {
      const animations = await loading(h).evaluate(element => [element, ...element.querySelectorAll('*')].flatMap(node => [null, '::before', '::after'].map(pseudo => getComputedStyle(node, pseudo).animationName)));
      assert.ok(animations.every(value => value === 'none'), 'Reduced motion disables loading animations');
      h.result.checks.push('Reduced-motion preference disables skeleton and progress animation');
    }
    await h.page.clock.fastForward(8050);
    await pending(h, 'Still loading your account'); await screenshot(h, 'slow');
    assert.equal(await h.page.getByRole('button', { name: 'Retry', exact: true }).count(), 0, 'Slow response is not mislabeled a failed request');
    h.result.checks.push('Eight-second patience status appears using the browser clock while the account remains pending');
    await h.respond(); await success(h); await screenshot(h, 'loaded');
    h.result.checks.push('Exact contract AccountResult replaces loading on success without navigation or additional backend calls');
  } finally { await h.close(); }
}
async function errorRetryScenario() {
  const h = await harness('error-retry', { width: 1280, height: 900 });
  try {
    await h.open(); await pending(h);
    await h.respond({ code: 'SERVICE_UNAVAILABLE', detail: 'Fixture: account storage is temporarily unavailable.' }, 503);
    await loading(h).getByText('Couldn’t open this account', { exact: true }).waitFor();
    await loading(h).getByRole('alert').filter({ hasText: 'Fixture: account storage is temporarily unavailable.' }).waitFor();
    assert.equal(await loadedAccount(h).count(), 0);
    assert.equal(await loading(h).locator('.account-skeleton, .account-loading-pulse').count(), 0, 'Error removes loading skeleton and pulse');
    await noOverflow(h); await screenshot(h, 'error');
    const before = await h.count();
    await loading(h).getByRole('button', { name: 'Retry', exact: true }).click();
    await until(async () => await h.count() > before, 'retry account request');
    await pending(h); await h.respond(); await success(h);
    assert.equal(await h.count(), before + 1, 'Retry issues one account read');
    h.result.checks.push('Actual error message and Retry replace pending state; one retry returns to loading and then the saved account on the same URL');
  } finally { await h.close(); }
}
async function timeoutScenario() {
  const h = await harness('timeout-retry', { width: 390, height: 844 }, { clock: true });
  try {
    await h.open(); await pending(h);
    await h.page.clock.fastForward(30_050);
    await loading(h).getByText('Couldn’t open this account', { exact: true }).waitFor();
    const alert = loading(h).getByRole('alert'); await alert.waitFor();
    assert.match(await alert.innerText(), /longer|timed out|timeout/i);
    assert.ok(await h.page.evaluate(() => window.__accountLoadingFixture.calls.some(call => call.aborted)), 'Deadline aborts the account read');
    await screenshot(h, 'timeout'); await noOverflow(h);
    await h.respond();
    await h.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await loadedAccount(h).count(), 0, 'Late response cannot overwrite a settled timeout error');
    await loading(h).getByText('Couldn’t open this account', { exact: true }).waitFor();
    const before = await h.count();
    await loading(h).getByRole('button', { name: 'Retry', exact: true }).click();
    await until(async () => await h.count() > before, 'retry after timeout');
    await pending(h); await h.respond(); await success(h);
    assert.equal(await h.count(), before + 1);
    h.result.checks.push('Thirty-second deadline becomes a recoverable error without real waiting; late response cannot overwrite it; one retry succeeds');
  } finally { await h.close(); }
}
async function loadedPollTimeoutScenario() {
  const h = await harness('loaded-poll-timeout', { width: 1280, height: 900 }, { clock: true });
  try {
    await h.open(); await pending(h);
    const running = structuredClone(fixture.account);
    running.research_statement = { ...running.research_statement, status: 'running', paragraph_id: null };
    await h.respond(running); await success(h);
    const initialReads = await h.count();
    await h.page.clock.fastForward(2550);
    await until(async () => await h.count() === initialReads + 1, 'research-statement status poll');
    assert.equal(await loading(h).count(), 0, 'Background polling retains the already loaded account');
    await h.page.clock.fastForward(30_050);
    const error = h.page.locator('.account-refresh-error'); await error.waitFor();
    assert.equal(await error.getAttribute('role'), 'alert');
    assert.match(await error.innerText(), /longer|timed out|timeout/i);
    await h.page.getByRole('heading', { name: account.name, exact: true }).waitFor();
    assert.equal(await h.page.locator('.account-conclusions').innerText(), account.closing_remarks);
    assert.equal(await loading(h).count(), 0, 'Polling timeout does not replace conclusions with a loading or initial-error screen');
    await noOverflow(h); await screenshot(h, 'refresh-error');
    const before = await h.count();
    await error.getByRole('button', { name: 'Retry', exact: true }).click();
    await until(async () => await h.count() === before + 1, 'retry loaded-account refresh');
    assert.equal(await loading(h).count(), 0, 'Explicit same-account refresh preserves the loaded result while pending');
    assert.equal(await h.page.locator('.account-conclusions').innerText(), account.closing_remarks);
    await h.respond(); await success(h);
    await error.waitFor({ state: 'detached' });
    await h.page.clock.fastForward(3000);
    assert.equal(await h.count(), before + 1, 'Successful final paragraph status stops polling');
    await screenshot(h, 'refresh-recovered');
    h.result.checks.push('Loaded account survives a stalled background poll and its deadline; visible Retry preserves conclusions while pending and recovers without continued polling');
  } finally { await h.close(); }
}
try {
  const filter = process.env.ACCOUNT_LOADING_SCENARIO_FILTER ? new RegExp(process.env.ACCOUNT_LOADING_SCENARIO_FILTER) : null;
  const scenarios = [
    ['delayed-desktop', () => delayedScenario('delayed-desktop', { width: 1280, height: 900 })],
    ['delayed-mobile-reduced-motion', () => delayedScenario('delayed-mobile-reduced-motion', { width: 390, height: 844 }, true)],
    ['error-retry', errorRetryScenario], ['timeout-retry', timeoutScenario],
    ['loaded-poll-timeout', loadedPollTimeoutScenario],
  ].filter(([name]) => !filter || filter.test(name));
  assert.ok(scenarios.length, 'Scenario filter must select a test');
  for (const [, run] of scenarios) await run();
  report.status = 'passed';
} catch (error) { report.status = 'failed'; report.failure = error.stack || error.message; process.exitCode = 1; }
finally { await browser.close(); await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n'); console.log(JSON.stringify(report, null, 2)); }
