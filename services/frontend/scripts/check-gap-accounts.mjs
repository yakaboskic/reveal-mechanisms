#!/usr/bin/env node
/** Mocked browser regression for scoped gap ranking and scientific-account lists.
 * Every API request is intercepted. No real authentication, write, or paid job.
 * Optional GAP_ACCOUNTS_BASE_URL, GAP_ACCOUNTS_AUDIT_DIR,
 * GAP_ACCOUNTS_SCENARIO_FILTER, PLAYWRIGHT_MODULE, PLAYWRIGHT_EXECUTABLE_PATH.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.GAP_ACCOUNTS_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.GAP_ACCOUNTS_AUDIT_DIR || resolve(root, '.runtime/gap-accounts-audit'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const userId = '11111111-1111-4111-8111-111111111111';
const originalAuthor = '22222222-2222-4222-8222-222222222222';
const pageInfo = (more = false) => ({ has_more: more, next_cursor: more ? 'fixture-next' : null, snapshot_id: 'fixture-snapshot' });
const gaps = [9, 5, 2, 0].map((count, i) => ({ ...structuredClone(fixture.gaps.items[0]),
  object: { ...fixture.gaps.items[0].object, id: `${fixture.gaps.items[0].object.id}_${i}`, text: `Mocked ranked question ${i + 1}: what explains this observation?` },
  source: { ...fixture.gaps.items[0].source, source_id: `dismech:fixture/ranked-${i}` },
  scientific_accounts: { ...fixture.gaps.items[0].scientific_accounts, count, scope: 'owner_exact_gap', ranking: 'account_count' } }));
function account(gap, index = 1) { return { ...structuredClone(fixture.gapAccounts.items[0]),
  account: { ...fixture.gapAccounts.items[0].account, id: `dapper:ScientificAccount.mocked_${gap.object.id.split('_').at(-1)}_${index}`, question: gap.object.id,
    name: `Proposed explanation ${index}`, closing_remarks: 'Mocked synthesis for browser testing. The observations leave the causal direction unresolved.' },
  knowledge_gap: gap.object, claim_count: index + 1, created_at: '2026-09-28T09:30:00Z',
  attribution: { user_id: originalAuthor, person_id: null, display_name: index === 1 ? 'Original submitting researcher' : null, principal_kind: index === 2 ? 'anonymous' : 'registered', orcid: null, orcid_authenticated: false, observed_at: '2026-09-28T09:00:00Z' } }; }
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  const cache = resolve(homedir(), 'Library/Caches/ms-playwright');
  for (const name of (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse()) {
    const path = resolve(cache, name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'); try { await access(path); return path; } catch {}
  }
}
async function until(check, description) { const start = performance.now(); while (!await check()) { assert.ok(performance.now() - start < 12000, `Timed out: ${description}`); await new Promise(resolve => setTimeout(resolve, 20)); } }
const { chromium } = await playwright();
const browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Mocked browser fixtures; no real backend requests, authentication, database writes or jobs. Counts/IDs/scientific prose in scenarios are invented.', contractSha256: fixture.contractSha256, status: 'running', scenarios: [] };
async function harness(name, { mobile = false, visitor = false, clock = false, reducedMotion = false, canClaim = false } = {}) {
  const viewport = mobile ? { width: 390, height: 844 } : { width: 1280, height: 1000 };
  const context = await browser.newContext({ viewport, serviceWorkers: 'block', reducedMotion: reducedMotion ? 'reduce' : 'no-preference' });
  const page = await context.newPage(); page.setDefaultTimeout(12000);
  if (clock) await page.clock.install();
  const state = { principal: visitor ? null : userId, requests: [], unexpected: [], pageErrors: [], consoleErrors: [], canClaim };
  page.on('pageerror', error => state.pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') state.consoleErrors.push(message.text()); });
  await context.addInitScript(() => {
    const original = window.fetch.bind(window); const mock = { calls: [] }; window.__gapAccountsFixture = mock;
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (!/^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/accounts$/.test(url.pathname)) return original(input, init);
      const signal = init?.signal || input?.signal;
      return new Promise(resolve => {
        const call = { path: decodeURIComponent(url.pathname), sourceRevision: url.searchParams.get('source_revision'), cursor: url.searchParams.get('cursor'), resolve, settled: false, aborted: !!signal?.aborted };
        // Strict Mode immediately cleans up and repeats the same mount GET.
        // Keep that replacement in the same fixture slot, without conflating retries.
        const previous = mock.calls.at(-1);
        if (previous?.aborted && !previous.settled && previous.path === call.path && previous.cursor === call.cursor) mock.calls[mock.calls.length - 1] = call;
        else mock.calls.push(call);
        signal?.addEventListener('abort', () => { call.aborted = true; }, { once: true });
      });
    };
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname), method = request.method();
    if (url.origin !== origin) { state.unexpected.push(`${method} ${url.origin}${path}`); return route.abort('blockedbyclient'); }
    if (!path.startsWith('/api/')) { assert.equal(method, 'GET'); return route.continue(); }
    state.requests.push({ method, path, query: url.search });
    const respond = json => route.fulfill({ status: 200, json });
    if (path === '/api/session/status') return respond({ principal: state.principal ? { user_id: state.principal } : null, canClaim: state.canClaim, providers: { google: false, orcid: false } });
    if (path === '/api/backend/v1/me') return respond({ user_id: state.principal, display_name: 'Current workspace owner', email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'registered', workspace_expires_at: null });
    if (path === '/api/session/claim') { state.principal = '33333333-3333-4333-8333-333333333333'; state.canClaim = false; return respond({ claimed: true }); }
    if (method === 'GET' && path === '/api/backend/v1/knowledge-gaps') return respond({ items: gaps.map(gap => ({ ...gap, scientific_accounts: { ...gap.scientific_accounts, count: state.principal ? gap.scientific_accounts.count : 0, scope: state.principal ? 'owner_exact_gap' : 'public_exact_gap' } })), page: pageInfo() });
    if (method === 'GET' && path === '/api/backend/v1/knowledge-gaps/search') return respond({ items: [], page: pageInfo() });
    if (method === 'POST' && path === '/api/backend/v1/mechanisms/suggest') return respond({ ...fixture.suggestions, automatic_anchors: [], limitations: ['Mocked suggestions; no model executed.'] });
    if (method === 'POST' && path === '/api/backend/v1/me/explorations') return respond({});
    if ((method === 'POST' && path === '/api/backend/v1/drafts') || (method === 'PATCH' && path.startsWith('/api/backend/v1/drafts/'))) return respond({ id: '44444444-4444-4444-8444-444444444444', version: 1, composer: request.postDataJSON().composer, created_at: '2026-09-28T10:00:00Z', updated_at: '2026-09-28T10:00:00Z' });
    state.unexpected.push(`${method} ${path}`); return route.fulfill({ status: 500, json: { code: 'UNEXPECTED_TEST_ROUTE', detail: 'Blocked unmocked API request.' } });
  });
  const result = { name, viewport, checks: [] }; report.scenarios.push(result);
  const h = { page, state, result,
    count: () => page.evaluate(() => window.__gapAccountsFixture.calls.length),
    async respond(index, body, status = 200) { await page.evaluate(({ index, body, status }) => { const call = window.__gapAccountsFixture.calls[index]; if (!call || call.settled) throw Error('No pending fixture call'); call.settled = true; call.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })); }, { index, body, status }); },
    async select(index = 0) { const count = await h.count(); await page.locator('.trend').nth(index).click(); await until(async () => await h.count() > count, 'selected-gap account request'); },
    async close() { result.requests = state.requests; result.accountRequests = await page.evaluate(() => window.__gapAccountsFixture.calls.map(({ path, sourceRevision, cursor, settled, aborted }) => ({ path, sourceRevision, cursor, settled, aborted }))); result.pageErrors = state.pageErrors; result.consoleErrors = state.consoleErrors; result.unexpected = state.unexpected; await context.close(); },
  };
  await page.goto(origin); await until(async () => await page.locator('.trend').count() === 3, 'ranked top-three questions'); return h;
}
const list = h => h.page.locator('.gap-accounts');
async function noOverflow(h) { const dimensions = await h.page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth })); assert.ok(dimensions.page <= dimensions.viewport + 1); return dimensions; }
async function shot(h, suffix) { await h.page.screenshot({ path: resolve(output, `${h.result.name}-${suffix}.png`), fullPage: true }); }
async function rankedPagination() {
  const h = await harness('ranking-and-pagination');
  try {
    assert.deepEqual(await h.page.locator('.trend-question').evaluateAll(nodes => nodes.map(node => node.firstChild.textContent)), gaps.slice(0, 3).map(gap => gap.object.text));
    assert.match(await h.page.locator('.trend').first().innerText(), /9 scientific accounts in your workspace/);
    assert.equal(h.state.requests.filter(request => request.path.endsWith('/knowledge-gaps/search')).length, 0, 'No editorial topic searches override ranking');
    await h.select(); await list(h).getByText('Loading scientific accounts', { exact: true }).waitFor();
    await h.respond(0, { items: [account(gaps[0], 1), account(gaps[0], 2)], page: pageInfo(true) });
    await list(h).getByText('Original submitting researcher', { exact: true }).waitFor();
    assert.equal(await list(h).getByText('Current workspace owner').count(), 0);
    assert.equal(await list(h).getByText('Anonymous researcher', { exact: true }).count(), 1);
    assert.equal(await list(h).locator('.gap-accounts-count').innerText(), '2+');
    assert.equal(await list(h).locator('h3 a').first().getAttribute('href'), `/accounts/${encodeURIComponent(account(gaps[0], 1).account.id)}?view=conclusions`);
    assert.equal(await list(h).locator('time').first().getAttribute('datetime'), '2026-09-28T09:30:00Z');
    assert.match(await list(h).locator('.gap-account-synthesis').first().innerText(), /causal direction unresolved/);
    await list(h).getByRole('button', { name: 'Show more accounts' }).click(); await until(async () => await h.count() === 2, 'next-page request');
    assert.equal(await list(h).locator('.gap-account-card').count(), 2);
    await h.respond(1, { code: 'TEMPORARY_UNAVAILABLE', detail: 'Fixture page temporarily unavailable.' }, 503);
    await list(h).getByRole('button', { name: 'Retry' }).click(); await until(async () => await h.count() === 3, 'page retry');
    const third = account(gaps[0], 3); third.attribution = null;
    await h.respond(2, { items: [account(gaps[0], 2), third], page: pageInfo() });
    await until(async () => await list(h).locator('.gap-account-card').count() === 3, 'deduplicated pages');
    assert.equal(await list(h).locator('.gap-accounts-count').innerText(), '3');
    assert.equal(await list(h).getByText('Attribution unavailable', { exact: true }).count(), 1);
    assert.equal(await list(h).getByRole('button', { name: 'Show more accounts' }).count(), 0);
    const calls = await h.page.evaluate(() => window.__gapAccountsFixture.calls.map(call => ({ sourceRevision: call.sourceRevision, cursor: call.cursor })));
    assert.deepEqual(calls.map(call => call.cursor), [null, 'fixture-next', 'fixture-next']);
    assert.ok(calls.every(call => call.sourceRevision === gaps[0].source.source_revision));
    h.result.checks.push('Server ranking unchanged; no editorial searches', 'Original author, title, synthesis, claims, date and exact link', 'Pagination retry retains cards and cursor; duplicate boundary record suppressed', await noOverflow(h)); await shot(h, 'loaded');
  } finally { await h.close(); }
}
async function mobileSlow() {
  const h = await harness('mobile-slow-retry', { mobile: true, clock: true, reducedMotion: true });
  try {
    await h.select(); await list(h).getByText('Loading scientific accounts', { exact: true }).waitFor(); await h.page.clock.fastForward(8100);
    await list(h).getByText('This is taking a little longer. We’re still waiting for a response.').waitFor();
    const animations = await list(h).locator('.loading-surface-pulse i').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).animationName)); assert.ok(animations.every(name => name === 'none'));
    await shot(h, 'slow'); await h.respond(0, { code: 'TEMPORARY_UNAVAILABLE', detail: 'Fixture temporarily unavailable.' }, 503);
    await list(h).getByRole('button', { name: 'Retry' }).click(); await until(async () => await h.count() === 2, 'initial retry');
    const long = account(gaps[0]); long.account.name = 'An unusually long scientific account title examining several distinct observations and uncertainty'; long.attribution.display_name = 'Researcher with an intentionally lengthy displayed name';
    await h.respond(1, { items: [long], page: pageInfo() }); await list(h).locator('.gap-account-card').waitFor();
    h.result.checks.push('8-second patience message; reduced motion', 'Initial error recovers without inventing accounts', await noOverflow(h)); await shot(h, 'loaded');
  } finally { await h.close(); }
}
async function deadlineRecovery() {
  const h = await harness('deadline-recovery', { clock: true });
  try {
    await h.select(); await list(h).getByText('Loading scientific accounts', { exact: true }).waitFor();
    await h.page.clock.fastForward(30100);
    await list(h).getByText('The scientific accounts are taking longer than expected to load. Please retry.').waitFor();
    const late = account(gaps[0]); late.account.name = 'LATE TIMED-OUT ACCOUNT';
    await h.respond(0, { items: [late], page: pageInfo() });
    await list(h).getByRole('button', { name: 'Retry' }).click(); await until(async () => await h.count() === 2, 'deadline retry');
    await h.respond(1, { items: [account(gaps[0])], page: pageInfo() }); await list(h).locator('.gap-account-card').waitFor();
    assert.equal(await list(h).getByText(late.account.name).count(), 0);
    h.result.checks.push('30-second request deadline offers Retry, ignores late response and loads a fresh page');
  } finally { await h.close(); }
}
async function visitorPrivacy() {
  const h = await harness('visitor-empty', { visitor: true });
  try {
    assert.ok((await h.page.locator('.trend small').allTextContents()).every(text => text.startsWith('0 scientific accounts')));
    await h.select(); await h.respond(0, { items: [], page: pageInfo() }); await list(h).getByText(/No scientific accounts are available in this view/).waitFor();
    assert.equal(await list(h).locator('.gap-account-card').count(), 0); assert.equal(await list(h).locator('.gap-accounts-count').innerText(), '0');
    h.result.checks.push('Visitor sees zero scoped counts and no private account/author records');
  } finally { await h.close(); }
}
async function staleSelection() {
  const h = await harness('stale-selection');
  try {
    await h.select(); await h.page.getByRole('button', { name: 'Search for a different knowledge gap' }).click(); await h.select(1);
    await h.respond(1, { items: [account(gaps[1])], page: pageInfo() }); await list(h).locator('.gap-account-card').waitFor();
    const old = account(gaps[0]); old.account.name = 'STALE ACCOUNT MUST NEVER APPEAR'; await h.respond(0, { items: [old], page: pageInfo() });
    assert.equal(await list(h).getByText(old.account.name).count(), 0); assert.equal(await list(h).locator('h3 a').getAttribute('href'), `/accounts/${encodeURIComponent(account(gaps[1]).account.id)}?view=conclusions`);
    h.result.checks.push('Late previous-gap response ignored after selection changes');
  } finally { await h.close(); }
}
async function identityPrivacy() {
  const h = await harness('identity-change', { canClaim: true });
  try {
    await h.select(); await h.respond(0, { items: [account(gaps[0])], page: pageInfo() }); await list(h).locator('.gap-account-card').waitFor();
    await h.page.getByRole('button', { name: 'Move my anonymous work' }).click(); await until(async () => await h.count() === 2, 'new principal list request');
    assert.equal(await list(h).locator('.gap-account-card').count(), 0, 'Previous principal cards cleared before new response');
    assert.equal(await list(h).locator('.gap-accounts-count').count(), 0, 'Previous principal count cleared before new response');
    await h.respond(1, { items: [], page: pageInfo() }); await list(h).getByText(/No scientific accounts have been proposed/).waitFor();
    h.result.checks.push('Changed identity clears previous private cards/count and reloads scope');
  } finally { await h.close(); }
}
async function mismatchedResponse() {
  const h = await harness('wrong-gap-fails-closed');
  try {
    await h.select(); await h.respond(0, { items: [account(gaps[1])], page: pageInfo() }); await list(h).getByText(/could not be matched/).waitFor();
    assert.equal(await list(h).locator('.gap-account-card').count(), 0); await list(h).getByRole('button', { name: 'Retry' }).click(); await until(async () => await h.count() === 2, 'mismatch retry');
    await h.respond(1, { items: [account(gaps[0])], page: pageInfo() }); await list(h).locator('.gap-account-card').waitFor();
    h.result.checks.push('Mismatched exact-gap records rejected before rendering; retry recovers');
  } finally { await h.close(); }
}
try {
  const scenarios = { rankedPagination, mobileSlow, deadlineRecovery, visitorPrivacy, staleSelection, identityPrivacy, mismatchedResponse };
  for (const [name, run] of Object.entries(scenarios)) if (!process.env.GAP_ACCOUNTS_SCENARIO_FILTER || name.includes(process.env.GAP_ACCOUNTS_SCENARIO_FILTER)) await run();
  for (const scenario of report.scenarios) { assert.deepEqual(scenario.pageErrors, []); assert.deepEqual(scenario.consoleErrors, []); assert.deepEqual(scenario.unexpected, []); assert.ok(!scenario.requests.some(request => request.path.includes('/jobs'))); }
  report.status = 'passed'; console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
} catch (error) { report.status = 'failed'; report.failure = error.stack; throw error; }
finally { await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2)); await browser.close(); }
