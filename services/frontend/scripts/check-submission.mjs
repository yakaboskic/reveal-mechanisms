#!/usr/bin/env node
/** Intercepted browser regression. No real login, draft write, or paid job.
 * Run against an already running development frontend:
 *   node services/frontend/scripts/check-submission.mjs
 * Optional: SUBMISSION_BASE_URL, SUBMISSION_AUDIT_DIR, PLAYWRIGHT_MODULE,
 * PLAYWRIGHT_EXECUTABLE_PATH, SUBMISSION_SCENARIO_FILTER (regular expression).
 * Playwright is an external test tool, not a runtime dependency.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.SUBMISSION_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Use a local frontend only');
const output = resolve(process.env.SUBMISSION_AUDIT_DIR || resolve(root, '.runtime/submission-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const gap = fixture.gaps.items[0];
const factor = fixture.suggestions.automatic_anchors[0].factor;
const suggestions = { ...fixture.suggestions, automatic_anchors: [fixture.suggestions.automatic_anchors[0]] };
const userId = '11111111-1111-4111-8111-111111111111';
const draftId = '22222222-2222-4222-8222-222222222222';
const jobId = '33333333-3333-4333-8333-333333333333';
const restoreDraftId = '55555555-5555-4555-8555-555555555555';
const now = '2026-09-27T12:00:00Z';
const identity = {
  user_id: userId, display_name: 'Browser regression fixture', email: null,
  email_verified: null, orcid: null, orcid_authenticated: false, person: null,
  principal_kind: 'anonymous', workspace_expires_at: '2026-10-01T12:00:00Z',
};
const job = {
  id: jobId, kind: 'analysis', owner_user_id: userId, status: 'queued', stage: 'queued',
  research_request_id: '44444444-4444-4444-8444-444444444444', input_account_id: null,
  created_at: now, updated_at: now, completed_at: null, result: null, failure: null,
  warnings: ['BROWSER REGRESSION FIXTURE — no research job was created.'], last_event_id: '0',
  links: { self: `/v1/jobs/${jobId}`, events: `/v1/jobs/${jobId}/events`, cancel: `/v1/jobs/${jobId}/cancel` },
};

async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/')
    ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); }
  catch {
    return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href);
  }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium');
    try { await access(path); return path; } catch { /* use the installed Playwright default below */ }
  }
  return undefined;
}
function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
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
const report = {
  scope: 'Intercepted browser UI regression only. Every API request is fulfilled or rejected locally; no real authentication, database write, embedding request, or paid research job.',
  fixtureContractSha256: fixture.contractSha256, baseUrl: origin, scenarios: [], status: 'running',
};

async function harness(name, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const state = {
    authenticated: Boolean(options.signed), oauthReturned: false, requests: [], errors: [], unexpected: [], draft: null,
    anonymous: 0, status: 0, authenticatedStatus: 0, me: 0, drafts: 0, jobs: 0, oauth: 0, csrf: 0,
    restoreReads: 0,
    gates: Object.fromEntries(['anonymous', 'status', 'me', 'draft', 'job', 'oauth', 'csrf', 'restore'].map(key => [key, gate()])),
  };
  if (!options.holdCsrf) state.gates.csrf.release();
  page.on('pageerror', error => state.errors.push(error.message));
  const fulfilled = (route, json, status = 200) => route.fulfill({ status, json }).catch(error => {
    if (!/closed|cancel|handled/i.test(error.message)) throw error;
  });
  await context.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (url.origin !== origin) {
      state.unexpected.push(`${request.method()} external ${url.origin}${path}`);
      return route.abort('blockedbyclient');
    }
    if (!path.startsWith('/api/')) {
      assert.equal(request.method(), 'GET', `Unexpected non-API mutation: ${path}`);
      return route.continue();
    }
    const body = request.postData();
    state.requests.push({ method: request.method(), path, key: request.headers()['idempotency-key'] || null,
                          body: body ? (() => { try { return JSON.parse(body); } catch { return body; } })() : null });
    if (path === '/api/session/status') {
      state.status++;
      if (state.authenticated) { state.authenticatedStatus++; await state.gates.status.promise; }
      return fulfilled(route, { principal: state.authenticated ? { user_id: userId } : null,
        canClaim: false, providers: { google: true, orcid: true } });
    }
    if (path === '/api/session/anonymous' && request.method() === 'POST') {
      state.anonymous++;
      await state.gates.anonymous.promise;
      if (options.fail === 'anonymous' && state.anonymous === 1)
        return fulfilled(route, { detail: 'Simulated anonymous sign-in failure.' }, 503);
      state.authenticated = true;
      return fulfilled(route, { user_id: userId });
    }
    if (path === '/api/backend/v1/me') {
      state.me++;
      await state.gates.me.promise;
      return fulfilled(route, { ...identity, principal_kind: state.oauthReturned || options.signed ? 'registered' : 'anonymous' });
    }
    if (path === '/api/backend/v1/knowledge-gaps') return fulfilled(route, fixture.gaps);
    if (path === '/api/backend/v1/knowledge-gaps/search') return fulfilled(route, {
      items: [{ gap, ranking: { rank: 1, value: 1, metric: 'lexical_rank' } }], next_cursor: null,
    });
    if (path.startsWith('/api/backend/v1/knowledge-gaps/')) return fulfilled(route, gap);
    if (path === '/api/backend/v1/mechanisms/suggest') return fulfilled(route, suggestions);
    if (path.startsWith('/api/backend/v1/mechanisms/')) return fulfilled(route, factor);
    if (path === '/api/backend/v1/me/explorations') return fulfilled(route, {});
    if (path === '/api/backend/v1/drafts' && request.method() === 'POST') {
      state.drafts++;
      await state.gates.draft.promise;
      if (options.fail === 'draft' && state.drafts === 1)
        return fulfilled(route, { code: 'UNAVAILABLE', detail: 'Simulated draft save failure.' }, 503);
      state.draft = { id: draftId, owner_user_id: userId, version: 1,
        composer: request.postDataJSON().composer, created_at: now, updated_at: now };
      return fulfilled(route, state.draft, 201);
    }
    if (path === `/api/backend/v1/drafts/${draftId}` && request.method() === 'GET') return fulfilled(route, state.draft);
    if (path === `/api/backend/v1/drafts/${restoreDraftId}` && request.method() === 'GET') {
      state.restoreReads++;
      await state.gates.restore.promise;
      return fulfilled(route, { id: restoreDraftId, owner_user_id: userId, version: 9,
        composer: state.restoreComposer, created_at: now, updated_at: now });
    }
    if (path === '/api/backend/v1/jobs' && request.method() === 'POST') {
      state.jobs++;
      await state.gates.job.promise;
      if (options.fail === 'lost' && state.jobs === 1) return route.abort('connectionreset');
      if ((options.fail === 'job' || options.fail === 'back') && state.jobs === 1)
        return fulfilled(route, { code: 'UNAVAILABLE', detail: 'Simulated research submission failure.' }, 503);
      return fulfilled(route, job, 202);
    }
    if (path === `/api/backend/v1/jobs/${jobId}` && request.method() === 'GET') return fulfilled(route, job);
    if (path === `/api/backend/v1/jobs/${jobId}/events`) return route.fulfill({
      status: 200, contentType: 'text/event-stream', body: ': intercepted fixture stream\n\n',
    }).catch(() => {});
    if (path === '/api/auth/providers') return fulfilled(route, {
      google: { id: 'google', name: 'Google', type: 'oauth', signinUrl: `${origin}/api/auth/signin/google`, callbackUrl: `${origin}/api/auth/callback/google` },
      orcid: { id: 'orcid', name: 'ORCID', type: 'oauth', signinUrl: `${origin}/api/auth/signin/orcid`, callbackUrl: `${origin}/api/auth/callback/orcid` },
    });
    if (path === '/api/auth/csrf') {
      state.csrf++;
      await state.gates.csrf.promise;
      return fulfilled(route, { csrfToken: 'intercepted-browser-test-token' });
    }
    if (['/api/auth/signin/google', '/api/auth/signin/orcid'].includes(path) && request.method() === 'POST') {
      state.oauth++;
      await state.gates.oauth.promise;
      if (state.oauth >= (options.oauthAuthenticateAfter || 1)) {
        state.authenticated = true; state.oauthReturned = true;
      }
      return fulfilled(route, { url: `${origin}/?submission-regression-oauth-return=1` });
    }
    if (path === '/api/auth/session') return fulfilled(route, {});
    state.unexpected.push(`${request.method()} ${path}`);
    return fulfilled(route, { code: 'TEST_UNEXPECTED_ROUTE', detail: 'Unmocked API request blocked by the browser regression.' }, 500);
  });
  const result = { name, checks: [] };
  report.scenarios.push(result);
  return { page, state, result, async close() {
    for (const value of Object.values(state.gates)) value.release();
    result.requestCounts = Object.fromEntries(['anonymous', 'status', 'me', 'drafts', 'jobs', 'oauth', 'csrf'].map(key => [key, state[key]]));
    result.pageErrors = state.errors; result.blockedUnexpectedRequests = state.unexpected;
    await context.close();
  } };
}
async function choose(h) {
  await h.page.goto(`${origin}/?gap=${encodeURIComponent(gap.object.id)}`);
  await h.page.locator('.anchor-chips .label').waitFor();
  const submit = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true });
  await until(() => submit.isEnabled(), 'composer ready');
  await submit.click();
  await h.page.getByRole('dialog', { name: 'Continue your exploration' }).waitFor();
}
async function rapidClick(locator) {
  await locator.evaluate(element => { element.click(); element.click(); element.click(); });
}
async function progress(h, stage) {
  const main = h.page.locator(`main.submission-page[data-submission-stage="${stage}"][data-submission-state="pending"]`);
  await main.waitFor({ timeout: 1500 });
  assert.equal(await main.getByRole('heading', { level: 1, name: 'Preparing your research' }).count(), 1);
  assert.equal(await h.page.locator('main.composer-page').count(), 0, 'Composer is removed during submission');
  assert.equal(await h.page.locator('dialog[open]').count(), 0, 'Sign-in dialog is closed during submission');
  h.result.checks.push(`Immediate full-page ${stage}`);
}
async function success(h) {
  await h.page.getByRole('region', { name: 'Research activity' }).waitFor();
  assert.equal(new URL(h.page.url()).searchParams.get('job'), jobId);
  assert.equal(new URL(h.page.url()).searchParams.get('draft'), draftId);
  assert.equal(await h.page.locator('main.submission-page').count(), 0);
  assert.deepEqual(h.state.errors, []);
  assert.deepEqual(h.state.unexpected, []);
  h.result.checks.push('Accepted fixture job opens activity and updates URL');
}
async function screenshot(h, name) {
  await h.page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true });
}
async function stagedAnonymous() {
  const h = await harness('anonymous-delayed-stages');
  try {
    await choose(h);
    const canAdvanceClock = typeof h.page.clock?.install === 'function';
    if (canAdvanceClock) await h.page.clock.install();
    const started = performance.now();
    await rapidClick(h.page.getByRole('button', { name: 'Continue anonymously', exact: true }));
    await progress(h, 'signing-in');
    h.result.immediateProgressMilliseconds = Math.round(performance.now() - started);
    await until(() => h.state.anonymous === 1, 'anonymous request');
    assert.equal(h.state.drafts, 0); assert.equal(h.state.jobs, 0);
    await screenshot(h, 'anonymous-desktop');
    if (canAdvanceClock) {
      const delay = h.page.getByText('This is taking a little longer. A slow connection can add to the wait.', { exact: true });
      assert.equal(await delay.count(), 0);
      await h.page.clock.fastForward(12_050);
      await delay.waitFor();
      assert.equal(h.state.drafts, 0); assert.equal(h.state.jobs, 0);
      await screenshot(h, 'anonymous-patience-note');
      h.result.checks.push('12-second patience note appears using Playwright clock advancement while authentication remains pending');
    } else h.result.clockCheckSkipped = 'This Playwright version has no page.clock API';
    h.state.gates.anonymous.release();
    await until(() => h.state.authenticatedStatus >= 1, 'session refresh');
    await progress(h, 'signing-in');
    h.state.gates.status.release();
    await until(() => h.state.me >= 1, 'identity refresh');
    await progress(h, 'signing-in');
    h.state.gates.me.release();
    await until(() => h.state.drafts === 1, 'draft save');
    await progress(h, 'saving');
    assert.equal(h.state.jobs, 0);
    await h.page.setViewportSize({ width: 390, height: 844 });
    await h.page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await h.page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await h.page.locator('.submission-progress-spinner').evaluate(element => getComputedStyle(element).animationName), 'none');
    await screenshot(h, 'anonymous-mobile-reduced-motion');
    h.result.checks.push('390px has no horizontal overflow; reduced motion disables spinner animation');
    h.state.gates.draft.release();
    await until(() => h.state.jobs === 1, 'job submission');
    await progress(h, 'submitting');
    h.state.gates.job.release();
    await success(h);
    assert.equal(h.state.anonymous, 1); assert.equal(h.state.drafts, 1); assert.equal(h.state.jobs, 1);
    h.result.checks.push('Rapid clicks issue exactly one anonymous login, draft save, and job POST');
  } finally { await h.close(); }
}
async function retryFailure(phase) {
  const h = await harness(`${phase}-failure-retry`, { fail: phase });
  try {
    Object.values(h.state.gates).forEach(value => value.release());
    await choose(h);
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    const error = h.page.locator('main.submission-page[data-submission-state="error"]');
    await error.waitFor();
    assert.match(await error.getByRole('alert').innerText(), /Simulated/);
    assert.equal(await h.page.locator('main.composer-page').count(), 0);
    await screenshot(h, `${phase}-error`);
    const path = phase === 'anonymous' ? '/api/session/anonymous'
      : phase === 'draft' ? '/api/backend/v1/drafts' : '/api/backend/v1/jobs';
    const before = h.state.requests.filter(request => request.path === path && request.method === 'POST');
    assert.equal(before.length, 1);
    await rapidClick(error.getByRole('button', { name: 'Retry', exact: true }));
    await success(h);
    const attempts = h.state.requests.filter(request => request.path === path && request.method === 'POST');
    assert.equal(attempts.length, 2, 'A deliberate retry sends one additional attempt');
    assert.ok(attempts[0].key);
    assert.equal(attempts[0].key, attempts[1].key, 'Safe retry reuses its idempotency key');
    assert.deepEqual(attempts[0].body, attempts[1].body, 'Safe retry preserves the exact request body');
    assert.equal(h.state.jobs, phase === 'job' ? 2 : 1);
    h.result.checks.push(`${phase} Retry preserves body/idempotency and rapid clicks do not duplicate the retry`);
  } finally { await h.close(); }
}
async function backToQuestion() {
  const h = await harness('uncertain-response-back-reload-resubmit', { fail: 'lost' });
  try {
    Object.values(h.state.gates).forEach(value => value.release());
    await choose(h);
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    await h.page.locator('main.submission-page[data-submission-state="error"]').waitFor();
    await h.page.getByRole('button', { name: 'Back to question', exact: true }).click();
    await h.page.locator('main.composer-page').waitFor();
    assert.equal(await h.page.locator('.selected-question').innerText(), gap.object.text);
    assert.equal(await h.page.locator('.anchor-chips .label').count(), 1);
    assert.equal(await h.page.locator('main.submission-page').count(), 0);
    assert.equal(h.state.jobs, 1);
    h.result.checks.push('Back retains question and selected anchor without another job POST');
    const before = h.state.requests.find(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    await h.page.reload();
    const submit = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true });
    await until(() => submit.isEnabled(), 'restored composer ready');
    await rapidClick(submit);
    await success(h);
    const attempts = h.state.requests.filter(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    assert.equal(attempts.length, 2);
    assert.equal(attempts[1].key, before.key, 'Back then reload retains the uncertain job idempotency key');
    assert.deepEqual(attempts[1].body, before.body);
    assert.equal(h.state.anonymous, 1); assert.equal(h.state.drafts, 1);
    h.result.checks.push('Back then reload and resubmit retains the original job key/body');
    assert.deepEqual(h.state.errors, []); assert.deepEqual(h.state.unexpected, []);
  } finally { await h.close(); }
}
async function oauthReturn(provider) {
  const h = await harness(`${provider}-oauth-pending-return`);
  try {
    await choose(h);
    await h.page.getByRole('button', { name: `Continue with ${provider === 'google' ? 'Google' : 'ORCID'}`, exact: true }).click();
    await until(() => h.state.oauth === 1, 'intercepted OAuth sign-in');
    await progress(h, 'signing-in');
    h.state.gates.oauth.release();
    await h.page.waitForURL('**/?submission-regression-oauth-return=1');
    await until(() => h.state.authenticatedStatus >= 1, 'OAuth return session lookup');
    await progress(h, 'signing-in');
    await screenshot(h, `${provider}-oauth-pending-return`);
    h.state.gates.status.release();
    await until(() => h.state.me >= 1, 'OAuth identity');
    await progress(h, 'signing-in');
    h.state.gates.me.release();
    await until(() => h.state.drafts === 1, 'OAuth saved draft');
    await progress(h, 'saving');
    h.state.gates.draft.release();
    await until(() => h.state.jobs === 1, 'OAuth job submission');
    await progress(h, 'submitting');
    h.state.gates.job.release();
    await success(h);
    assert.equal(h.state.anonymous, 0); assert.equal(h.state.oauth, 1);
    assert.equal(h.state.drafts, 1); assert.equal(h.state.jobs, 1);
    h.result.checks.push('OAuth return restores pending intent and full-page preparation before session lookup completes');
  } finally { await h.close(); }
}
async function oauthDeadline(provider, phase) {
  const h = await harness(`${provider}-${phase}-deadline-recovery`, {
    holdCsrf: phase === 'csrf', oauthAuthenticateAfter: phase === 'oauth' ? 2 : 1,
  });
  try {
    for (const [key, value] of Object.entries(h.state.gates)) if (key !== phase) value.release();
    const heldPath = phase === 'csrf' ? '/api/auth/csrf' : `/api/auth/signin/${provider}`;
    // Simulate a transport that still delivers its first response after abort.
    // The request deadline must win independently of the browser's cancellation.
    await h.page.addInitScript(path => {
      const original = window.fetch.bind(window); let held = false;
      window.fetch = async (input, init) => {
        const url = typeof input === 'string' ? input : input.url;
        if (!held && new URL(url, location.href).pathname === path) {
          held = true;
          const response = await original(input, { ...init, signal: undefined });
          window.__lateOAuthResponseDelivered = true;
          return response;
        }
        return original(input, init);
      };
    }, heldPath);
    await choose(h);
    await h.page.clock.install();
    const buttonName = `Continue with ${provider === 'google' ? 'Google' : 'ORCID'}`;
    await rapidClick(h.page.getByRole('button', { name: buttonName, exact: true }));
    await until(() => h.state[phase] === 1, `held ${provider} ${phase} request`);
    await progress(h, 'signing-in');
    await h.page.clock.fastForward(30_050);
    const error = h.page.locator('main.submission-page[data-submission-state="error"]');
    await error.waitFor({ timeout: 2000 });
    assert.match(await error.getByRole('alert').innerText(), /sign-in.*longer.*retry/i);
    assert.equal(h.state.drafts, 0); assert.equal(h.state.jobs, 0);
    await screenshot(h, `${provider}-${phase}-deadline`);
    if (phase === 'oauth') {
      const before = h.page.url();
      await error.getByRole('button', { name: 'Back to question', exact: true }).click();
      await h.page.locator('main.composer-page').waitFor();
      h.state.gates.oauth.release();
      await h.page.waitForFunction(() => window.__lateOAuthResponseDelivered === true);
      await h.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(h.page.url(), before, 'Late provider response cannot redirect after Back');
      assert.equal(await h.page.locator('main.submission-page').count(), 0);
      assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
      assert.equal(h.state.drafts, 0); assert.equal(h.state.jobs, 0);
      h.result.checks.push('Abort-insensitive late provider response cannot navigate or resume discarded intent after Back');
      await h.page.getByRole('button', { name: 'Let’s close this gap', exact: true }).click();
      await h.page.getByRole('dialog', { name: 'Continue your exploration' }).waitFor();
      await rapidClick(h.page.getByRole('button', { name: buttonName, exact: true }));
    } else {
      h.state.gates.csrf.release();
      await h.page.waitForFunction(() => window.__lateOAuthResponseDelivered === true);
      await rapidClick(error.getByRole('button', { name: 'Try again', exact: true }));
    }
    await success(h);
    assert.equal(h.state.csrf, 2);
    assert.equal(h.state.oauth, phase === 'oauth' ? 2 : 1);
    assert.equal(h.state.anonymous, 0); assert.equal(h.state.drafts, 1); assert.equal(h.state.jobs, 1);
    const handoffs = h.state.requests.filter(request => request.path === `/api/auth/signin/${provider}`);
    for (const handoff of handoffs) {
      const body = new URLSearchParams(handoff.body);
      assert.equal(body.get('csrfToken'), 'intercepted-browser-test-token');
      assert.equal(body.get('callbackUrl'), `${origin}/`); assert.equal(body.get('json'), 'true');
    }
    h.result.deadlineClockAdvances = 1;
    h.result.checks.push('Stalled OAuth request exits loading within one deadline and a deliberate retry creates one fixture job');
  } finally { await h.close(); }
}
async function delayedUrlRestore() {
  const h = await harness('late-url-restore-cannot-overwrite-accepted-job', { signed: true });
  try {
    for (const [key, value] of Object.entries(h.state.gates)) if (key !== 'restore') value.release();
    const composer = {
      source_gap: { id: gap.object.id, source_id: gap.source.source_id, source_revision: gap.source.source_revision },
      eaggl_anchors: [{ reference: { source: 'eaggl', source_id: factor.source_id,
        source_revision: factor.source_revision, dapper_id: factor.object.id }, origin: 'manual', suggestion_id: null }],
      dismissed_source_ids: [], mechanism_subquery: '', model: 'cfde-inc-v2', selected_kgs: ['biomarkerkg', 'prokn'],
    };
    h.state.restoreComposer = composer;
    await h.page.addInitScript(snapshot => sessionStorage.setItem('reveal:composer', JSON.stringify(snapshot)), {
      composer, gap, factors: { [factor.source_id]: factor }, draft: null, owner: userId, job: null,
    });
    await h.page.goto(`${origin}/?draft=${restoreDraftId}`);
    await until(() => h.state.restoreReads === 1, 'held URL draft restoration');
    const submit = h.page.getByRole('button', { name: 'Let’s close this gap', exact: true });
    await until(() => submit.isEnabled(), 'already authenticated composer ready');
    await rapidClick(submit);
    await success(h);
    assert.equal(h.state.anonymous, 0); assert.equal(h.state.drafts, 1); assert.equal(h.state.jobs, 1);
    const finalRestoreFetch = h.page.waitForResponse(response => new URL(response.url()).pathname.startsWith('/api/backend/v1/knowledge-gaps/'));
    h.state.gates.restore.release();
    await (await finalRestoreFetch).finished();
    await h.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await success(h);
    const stored = await h.page.evaluate(() => JSON.parse(sessionStorage.getItem('reveal:composer')));
    assert.equal(stored.job.id, jobId);
    assert.equal(stored.draft.id, draftId);
    assert.equal(await h.page.locator('.selected-question').innerText(), gap.object.text);
    await screenshot(h, 'late-url-restore-retains-job');
    h.result.checks.push('A URL restore begun before submission can finish after acceptance without replacing the confirmed job/draft');
  } finally { await h.close(); }
}
async function replayComposerBootEffect(page) {
  return page.evaluate(() => {
    const main = document.querySelector('main');
    let fiber = main[Object.keys(main).find(key => key.startsWith('__reactFiber$'))];
    while (fiber && fiber.type?.name !== 'Composer') fiber = fiber.return;
    if (!fiber) throw new Error('Composer fiber unavailable for controlled effect replay');
    let hook = fiber.memoizedState; let count = 0;
    while (hook) {
      const effect = hook.memoizedState;
      if (typeof effect?.create === 'function' && effect.create.toString().includes('restoreSubmission')) {
        effect.create(); count++;
      }
      hook = hook.next;
    }
    return { replayedBootEffects: count, mode: 'Direct replay of the actual mounted boot effect, preserving refs/state' };
  });
}
async function effectReplayRemainsRecoverable() {
  const h = await harness('boot-effect-replay-retains-recoverable-error', { fail: 'job' });
  try {
    Object.values(h.state.gates).forEach(value => value.release());
    await choose(h);
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    const error = h.page.locator('main.submission-page[data-submission-state="error"]');
    await error.waitFor();
    const before = h.state.requests.filter(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    const replay = await replayComposerBootEffect(h.page);
    assert.equal(replay.replayedBootEffects, 1);
    await h.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await error.waitFor({ timeout: 1500 });
    assert.equal(h.state.jobs, 1, 'Effect replay does not silently relaunch a failed request');
    assert.equal(await error.getByRole('button', { name: 'Retry', exact: true }).count(), 1);
    await screenshot(h, 'effect-replay-retains-retry');
    await rapidClick(error.getByRole('button', { name: 'Retry', exact: true }));
    await success(h);
    const after = h.state.requests.filter(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    assert.equal(after.length, 2); assert.equal(after[1].key, before[0].key);
    assert.deepEqual(after[1].body, before[0].body);
    h.result.checks.push('Replaying the mounted startup effect preserves error/Retry and same-key recovery');
  } finally { await h.close(); }
}
async function stalledRequestDeadline(phase) {
  const h = await harness(`${phase}-deadline-safe-retry`);
  try {
    const held = phase === 'job-body' ? null : phase;
    for (const [key, value] of Object.entries(h.state.gates)) if (key !== held) value.release();
    if (phase === 'job-body') {
      await h.page.addInitScript(() => {
        const original = window.fetch.bind(window); let stalled = false;
        window.fetch = async (...args) => {
          const response = await original(...args);
          const request = args[0];
          const url = typeof request === 'string' ? request : request.url;
          if (!stalled && new URL(url, location.href).pathname === '/api/backend/v1/jobs') {
            stalled = true;
            response.json = () => {
              window.__submissionBodyStalled = true;
              return new Promise(() => {});
            };
          }
          return response;
        };
      });
    }
    await choose(h);
    await h.page.clock.install();
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    const count = () => phase === 'anonymous' ? h.state.anonymous : phase === 'status' ? h.state.authenticatedStatus
      : phase === 'me' ? h.state.me : phase === 'draft' ? h.state.drafts : h.state.jobs;
    await until(() => count() >= 1, `${phase} request pending`);
    if (phase === 'job-body') await h.page.waitForFunction(() => window.__submissionBodyStalled === true);
    await progress(h, ['anonymous', 'status', 'me'].includes(phase) ? 'signing-in' : phase === 'draft' ? 'saving' : 'submitting');
    const error = h.page.locator('main.submission-page[data-submission-state="error"]');
    await h.page.clock.fastForward(30_050);
    await error.waitFor({ timeout: 2000 });
    assert.match(await error.getByRole('alert').innerText(), /retry|try again|confirm/i);
    assert.equal(await h.page.locator('main.composer-page').count(), 0);
    await screenshot(h, `${phase}-deadline`);
    const postsBefore = h.state.requests.filter(request => request.method === 'POST' &&
      ['/api/session/anonymous', '/api/backend/v1/drafts', '/api/backend/v1/jobs'].includes(request.path));
    const stalledPath = phase === 'anonymous' ? '/api/session/anonymous' : phase === 'draft'
      ? '/api/backend/v1/drafts' : phase.startsWith('job') ? '/api/backend/v1/jobs' : null;
    if (held) h.state.gates[held].release();
    await rapidClick(error.getByRole('button', { name: 'Retry', exact: true }));
    await success(h);
    if (stalledPath) {
      const attempts = h.state.requests.filter(request => request.method === 'POST' && request.path === stalledPath);
      assert.equal(attempts.length, 2);
      const original = postsBefore.find(request => request.path === stalledPath);
      assert.ok(original.key); assert.equal(attempts[1].key, original.key);
      assert.deepEqual(attempts[1].body, original.body);
    }
    assert.equal(h.state.jobs, phase.startsWith('job') ? 2 : 1);
    h.result.deadlineClockAdvances = 1;
    h.result.checks.push('Stalled request exits loading within its bounded deadline; rapid Retry safely recovers');
    if (phase === 'job-body') h.result.checks.push('Deadline includes JSON decoding after response headers arrive');
  } finally { await h.close(); }
}
async function explicitJobBeatsStaleIntent() {
  const h = await harness('explicit-job-url-beats-stale-pending-intent', { fail: 'job' });
  try {
    Object.values(h.state.gates).forEach(value => value.release());
    await choose(h);
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    await h.page.locator('main.submission-page[data-submission-state="error"]').waitFor();
    await h.page.evaluate(() => {
      const pending = JSON.parse(sessionStorage.getItem('reveal:submission'));
      pending.question = 'A stale unrelated pending question';
      pending.composer.source_gap.id = 'dapper:KnowledgeGap.stale-pending-browser-test';
      pending.gap.object.text = pending.question;
      sessionStorage.setItem('reveal:submission', JSON.stringify(pending));
    });
    await h.page.goto(`${origin}/?draft=${draftId}&job=${jobId}`);
    await success(h);
    assert.equal(h.state.jobs, 1, 'Opening an explicit job never retries stale pending intent');
    assert.equal(h.state.drafts, 1); assert.equal(h.state.anonymous, 1);
    assert.equal(await h.page.locator('.selected-question').innerText(), gap.object.text);
    await screenshot(h, 'explicit-job-beats-stale-intent');
    h.result.checks.push('Explicit job/draft URL opens saved activity despite unrelated pending submission storage');
  } finally { await h.close(); }
}
async function lostResponseReload(composerState) {
  const h = await harness(`lost-job-response-reload-${composerState}-composer`, { fail: 'lost' });
  try {
    Object.values(h.state.gates).forEach(value => value.release());
    await choose(h);
    await h.page.getByRole('button', { name: 'Continue anonymously', exact: true }).click();
    await h.page.locator('main.submission-page[data-submission-state="error"]').waitFor();
    const before = h.state.requests.filter(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    assert.equal(before.length, 1);
    const pending = await h.page.evaluate(() => JSON.parse(sessionStorage.getItem('reveal:submission')));
    assert.equal(pending.submitKey.key, before[0].key, 'Job key is persisted before its response arrives');
    assert.equal(pending.draft.id, draftId);
    assert.equal(pending.gap.object.text, gap.object.text, 'Pending intent contains the actual selected question');
    await h.page.evaluate(mode => {
      if (mode === 'missing') sessionStorage.removeItem('reveal:composer');
      else {
        const stale = JSON.parse(sessionStorage.getItem('reveal:composer'));
        stale.gap.object.text = 'Stale different question from another composer snapshot';
        stale.gap.object.id = 'dapper:KnowledgeGap.stale-browser-test';
        stale.composer.source_gap.id = stale.gap.object.id;
        stale.draft = null;
        sessionStorage.setItem('reveal:composer', JSON.stringify(stale));
      }
    }, composerState);
    h.state.gates.status = gate();
    await h.page.reload();
    await progress(h, 'signing-in');
    await screenshot(h, 'lost-response-reload');
    h.state.gates.status.release();
    await success(h);
    assert.equal(await h.page.locator('.selected-question').innerText(), gap.object.text);
    const attempts = h.state.requests.filter(request => request.path === '/api/backend/v1/jobs' && request.method === 'POST');
    assert.equal(attempts.length, 2);
    assert.equal(attempts[0].key, attempts[1].key);
    assert.deepEqual(attempts[0].body, attempts[1].body);
    assert.equal(h.state.anonymous, 1); assert.equal(h.state.drafts, 1);
    assert.equal(await h.page.evaluate(() => sessionStorage.getItem('reveal:submission')), null);
    h.result.checks.push('Lost response then reload reuses the persisted job key/body without repeating login or draft creation');
    h.result.checks.push(`Pending gap/composer snapshot wins over ${composerState} general composer storage`);
  } finally { await h.close(); }
}
const scenarios = [
  ['anonymous-delayed-stages', stagedAnonymous],
  ...['anonymous', 'draft', 'job'].map(phase => [`${phase}-failure-retry`, () => retryFailure(phase)]),
  ['uncertain-response-back-reload-resubmit', backToQuestion],
  ...['google', 'orcid'].map(provider => [`${provider}-oauth-pending-return`, () => oauthReturn(provider)]),
  ...['google', 'orcid'].flatMap(provider => ['csrf', 'oauth'].map(phase =>
    [`${provider}-${phase}-deadline-recovery`, () => oauthDeadline(provider, phase)])),
  ...['missing', 'stale'].map(mode => [`lost-job-response-reload-${mode}-composer`, () => lostResponseReload(mode)]),
  ['late-url-restore-cannot-overwrite-accepted-job', delayedUrlRestore],
  ['boot-effect-replay-retains-recoverable-error', effectReplayRemainsRecoverable],
  ...['anonymous', 'status', 'me', 'draft', 'job', 'job-body'].map(phase =>
    [`${phase}-deadline-safe-retry`, () => stalledRequestDeadline(phase)]),
  ['explicit-job-url-beats-stale-pending-intent', explicitJobBeatsStaleIntent],
];
try {
  const filter = process.env.SUBMISSION_SCENARIO_FILTER ? new RegExp(process.env.SUBMISSION_SCENARIO_FILTER) : null;
  const selected = scenarios.filter(([name]) => !filter || filter.test(name));
  assert.ok(selected.length, 'Scenario filter must select at least one test');
  report.scenarioFilter = process.env.SUBMISSION_SCENARIO_FILTER || null;
  for (const [, run] of selected) await run();
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.failure = error.stack || error.message;
  process.exitCode = 1;
} finally {
  await browser.close();
  await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}
