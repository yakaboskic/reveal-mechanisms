#!/usr/bin/env node
/** Local renderer/interaction regression. All API responses are intercepted. */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.ACCOUNT_GRAPH_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname));
const output = resolve(process.env.ACCOUNT_GRAPH_AUDIT_DIR || resolve(root, '.runtime/account-graph-audit'));
await mkdir(output, { recursive: true });
const account = JSON.parse(await readFile(resolve(root, 'data/fixtures/bubble-account-v1/account-envelope.json'), 'utf8'));
account.publication = { visibility: 'private', version: 0, can_manage: true, published_at: null, updated_at: null, has_unpublished_changes: false };
account.research_statement = { status: 'not_requested', paragraph_id: null, job_id: null };
const accountPath = `/api/backend/v1/accounts/${account.root_id}`;
async function playwright() { if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE); try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); } }
async function executable() { if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH; for (const name of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(value => /^chromium-\d+$/.test(value)).sort().reverse()) { const path = resolve(homedir(), 'Library/Caches/ms-playwright', name, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'); try { await access(path); return path; } catch {} } }
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Local mocked APIs with the canonical dataset-rich account. Real Cytoscape canvas rendering and browser clicks; no database writes, downloads, publication or jobs.', scenarios: [] };
async function harness(name, { mobile = false, paged = false, conflict = false } = {}) {
  const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1050 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const errors = [], calls = [], unexpected = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) { unexpected.push(url.href); return route.abort(); }
    if (!path.startsWith('/api/')) return route.continue();
    calls.push(path + url.search); assert.equal(request.method(), 'GET', 'The explorer must never mutate records');
    if (path === '/api/session/status') return route.fulfill({ json: { principal: { user_id: '11111111-1111-4111-8111-111111111111' }, canClaim: false, canAdmin: false, providers: { google: false, orcid: false } } });
    if (path === '/api/backend/v1/me/workspace/events') return route.fulfill({ contentType: 'text/event-stream', body: ': local fixture heartbeat\n\n' });
    if (path === '/api/backend/v1/me') return route.fulfill({ json: { user_id: '11111111-1111-4111-8111-111111111111', display_name: 'Local fixture', email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'anonymous', workspace_expires_at: null } });
    if (path === accountPath) {
      if (url.searchParams.has('cursor') && conflict) return route.fulfill({ status: 409, json: { code: 'CURSOR_EXPIRED', detail: 'The account publication changed. Reload its first page.' } });
      const value = structuredClone(account);
      if (paged && !url.searchParams.has('cursor')) {
        value.document.datasets = []; value.document.files = [];
        value.coverage = { ...value.coverage, complete: false, missing_ids: [...account.document.datasets, ...account.document.files].map(node => node.id), next_cursor: 'same-account-snapshot' };
        value.artifacts = []; value.payloads = value.payloads.filter(item => !value.coverage.missing_ids.includes(item.object_id));
      }
      return route.fulfill({ json: value });
    }
    unexpected.push(path); return route.fulfill({ status: 500, json: { detail: 'Unexpected test route' } });
  });
  await page.goto(`${origin}/accounts/${encodeURIComponent(account.root_id)}`);
  await page.getByRole('tab', { name: 'Overview', exact: true }).click();
  await page.locator('.account-explorer').waitFor();
  await page.waitForFunction(() => document.querySelector('.account-bubbles')?._cyreg?.cy?.nodes().length > 0);
  return { page, calls, async close(checks) { assert.deepEqual(errors, []); assert.deepEqual(unexpected, []); report.scenarios.push({ name, checks, calls }); await context.close(); } };
}
try {
  const full = await harness('numbered claim circles, full-text hover, keyboard navigation and inspector');
  const { page } = full;
  const claims = account.document.scientific_accounts[0].component_claims;
  const numberedClaims = await page.locator('.account-bubbles').evaluate(element => element._cyreg.cy.nodes().filter(node => node.hasClass('claim')).map(node => ({ objectId: node.data('objectId'), label: node.data('label') })));
  assert.equal(numberedClaims.length, claims.length);
  claims.forEach((id, index) => assert.equal(numberedClaims.find(node => node.objectId === id)?.label, `C${index + 1}`, 'Claim numbering follows canonical component membership, not pack order'));
  await page.waitForFunction(count => document.querySelectorAll('.account-bubble-label').length === count, claims.length);
  const visibleNumbers = await page.locator('.account-bubble-label').allTextContents();
  assert.deepEqual(visibleNumbers.map(label => label.trim()).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1))), claims.map((_, index) => `C${index + 1}`), 'Every claim number is rendered inside the diagram');
  assert.equal(await page.locator('button.account-bubble-label').count(), claims.length, 'Each internal number selects its own claim rather than a nested file');
  assert.equal(await page.locator('.account-bubble-caption, .account-bubble-captions, .account-explorer-list, .account-explorer-heading, .account-explorer-mode').count(), 0);
  await page.locator('.account-explorer').screenshot({ path: resolve(output, 'account-bubbles.png') });
  const claim = account.document.claims.find(node => node.id === claims[0]);
  const fullLabel = claim.name || account.document.propositions.find(node => node.id === claim.proposition)?.statement || claim.statement;
  const firstNumber = page.locator('button.account-bubble-label').filter({ hasText: /^C1$/ });
  assert.equal(await firstNumber.getAttribute('aria-label'), `C1: ${fullLabel}`);
  await firstNumber.hover();
  await page.waitForFunction(label => document.querySelector('[role="tooltip"] p')?.textContent === label, fullLabel);
  await firstNumber.focus();
  assert.equal(await page.getByRole('tooltip').locator('p').innerText(), fullLabel);
  assert.equal(await page.locator('.account-bubbles').evaluate(element => element._cyreg.cy.$(':selected').id()), 'account', 'Hovering or focusing a claim number must not select it');
  const firstCircle = await page.evaluate(id => {
    const cy = document.querySelector('.account-bubbles')._cyreg.cy;
    const node = cy.nodes().filter(node => node.data('objectId') === id).first();
    const position = node.renderedPosition(), rect = cy.container().getBoundingClientRect();
    return { x: position.x + rect.left, y: position.y + rect.top - node.renderedHeight() / 2 + 3 };
  }, claims[0]);
  await page.mouse.move(firstCircle.x, firstCircle.y);
  assert.equal(await page.getByRole('tooltip').locator('p').innerText(), fullLabel);
  assert.equal(await page.locator('.account-bubbles').evaluate(element => element._cyreg.cy.$(':selected').id()), 'account', 'Hover must not select or zoom');
  const frame = await page.locator('.account-bubbles-frame').boundingBox(), tooltip = await page.getByRole('tooltip').boundingBox();
  assert.ok(tooltip.x >= frame.x && tooltip.x + tooltip.width <= frame.x + frame.width);
  assert.ok(tooltip.y >= frame.y && tooltip.y + tooltip.height <= frame.y + frame.height);
  assert.equal(await page.locator('.account-bubbles').evaluate(element => element._cyreg.cy.nodes().some(node => ['evidence', 'activity', 'record'].some(kind => node.hasClass(kind)))), false);
  await page.locator('.account-bubbles').focus();
  await page.keyboard.press('ArrowRight');
  await page.waitForFunction(label => document.querySelector('[role="tooltip"] p')?.textContent === label, fullLabel);
  assert.equal(await page.getByRole('tooltip').locator('p').innerText(), fullLabel);
  await page.keyboard.press('ArrowDown');
  await page.waitForFunction(id => document.querySelector('.account-bubbles')._cyreg.cy.$(':selected').data('objectId') === id, claims[1]);
  await page.keyboard.press('ArrowUp');
  await page.waitForFunction(id => document.querySelector('.account-bubbles')._cyreg.cy.$(':selected').data('objectId') === id, claims[0]);
  await page.keyboard.press('Escape'); assert.equal(await page.getByRole('tooltip').count(), 0);
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('.account-explorer-inspector>h3').evaluate(element => element === document.activeElement), true);
  assert.match(await page.locator('.account-explorer-kind').innerText(), /Claim/i);
  await page.locator('.account-explorer-inspector').getByRole('tab', { name: 'Evidence', exact: true }).click();
  assert.ok(await page.locator('.account-explorer-inspector .claim-evidence').count() > 0);
  // Coordinates come from the actual Cytoscape renderer; the click is dispatched
  // through the browser, checking nested hit testing rather than emitting events.
  const target = await page.evaluate(() => {
    const cy = document.querySelector('.account-bubbles')._cyreg.cy;
    const node = cy.nodes().filter(value => value.id().startsWith(cy.$(':selected').first().id() + '/') && value.hasClass('dataset')).first();
    if (!node.length) throw new Error('Dataset should be visible inside the first claim');
    const position = node.renderedPosition(), rect = cy.container().getBoundingClientRect();
    return { id: node.data('objectId'), x: position.x + rect.left, y: position.y + rect.top - node.renderedHeight() / 2 + 3 };
  });
  await page.mouse.click(target.x, target.y);
  await page.waitForFunction(id => document.querySelector('.account-bubbles')._cyreg.cy.$(':selected').data('objectId') === id, target.id);
  assert.match(await page.locator('.account-explorer-kind').innerText(), /Dataset/i);
  await page.locator('.account-explorer').screenshot({ path: resolve(output, 'dataset-inspector.png') });
  await page.getByRole('button', { name: 'Reset view', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.account-bubbles')._cyreg.cy.$(':selected').id() === 'account');
  await page.setViewportSize({ width: 967, height: 1050 });
  await page.waitForFunction(() => {
    const cy = document.querySelector('.account-bubbles')._cyreg.cy, root = cy.getElementById('account').renderedBoundingBox({ includeLabels: false });
    return root.x1 >= 0 && root.y1 >= 0 && root.x2 <= cy.width() && root.y2 <= cy.height();
  });
  await full.close(['canonical membership count and claim numbering', 'no external captions or duplicate list', 'full-text hover without selection and keyboard tooltip', 'tooltip stays inside diagram', 'four visible types only', 'keyboard drilldown and inspector focus', 'real canvas dataset hit testing', 'existing evidence inspector', 'reset']);

  const pages = await harness('same-account paging, missing-record recovery', { paged: true });
  await pages.page.getByRole('button', { name: 'Load more source records', exact: true }).click();
  await pages.page.getByRole('button', { name: 'Load more source records', exact: true }).waitFor({ state: 'hidden' });
  assert.ok(pages.calls.includes(accountPath + '?cursor=same-account-snapshot'));
  assert.equal(pages.calls.some(path => /\/objects\/|\/claims\//.test(path)), false);
  await pages.close(['signed account cursor used', 'source records merged', 'no independent claim/object snapshot requests']);

  const expired = await harness('expired continuation', { paged: true, conflict: true });
  await expired.page.getByRole('button', { name: 'Load more source records', exact: true }).click();
  await expired.page.getByRole('button', { name: 'Reload account', exact: true }).waitFor();
  await expired.close(['snapshot conflict preserves readable account and offers reload']);

  const mobile = await harness('narrow viewport', { mobile: true });
  assert.ok(await mobile.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await mobile.page.locator('.account-bubbles').focus(); await mobile.page.keyboard.press('ArrowRight');
  assert.match(await mobile.page.locator('.account-explorer-kind').innerText(), /Claim/i);
  await mobile.page.locator('.account-explorer').screenshot({ path: resolve(output, 'mobile-diagram.png') });
  await mobile.close(['no horizontal overflow', 'stacked inspector and keyboard diagram navigation']);
  report.status = 'passed';
} finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
