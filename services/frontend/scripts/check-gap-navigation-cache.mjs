#!/usr/bin/env node
/** Client-navigation regression. Every API is mocked; no real session, vote or research write. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const origin = new URL(process.env.GAP_NAVIGATION_BASE_URL || 'http://127.0.0.1:3000').origin;
assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(new URL(origin).hostname), 'Only local renderers are supported');
const output = resolve(process.env.GAP_NAVIGATION_AUDIT_DIR || resolve(root, '.runtime/gap-navigation-cache'));
await mkdir(output, { recursive: true });
const fixture = JSON.parse(await readFile(resolve(root, 'services/frontend/src/lib/fixtures/contract.json'), 'utf8'));
const userA = '11111111-1111-4111-8111-111111111111', userB = '22222222-2222-4222-8222-222222222222';
const records = new Map();
const pageInfo = (sort, more = false, revision = 0) => ({ next_cursor: more ? `${sort}-page-2` : null, has_more: more, snapshot_id: `fixture-${sort}-${revision}` });
function gap(sort, index, revision, viewer, sharedIds = false) {
  const value = structuredClone(fixture.gaps.items[0]);
  const id = 'dapper:KnowledgeGap.' + createHash('sha256').update(`${sharedIds ? 'shared' : sort}:${index}`).digest('base64url').slice(0, 32);
  value.object = { ...value.object, id, text: `${sort === 'votes' ? 'Votes' : 'Accounts'}-ranked fixture question ${index + 1}${revision ? `, refreshed revision ${revision}` : ''}: which biological observations distinguish this proposed mechanism from the alternatives?` };
  value.source = { ...value.source, source_id: `dismech:fixture/${sort}-${index}`, disease_label: 'Illustrative navigation fixture' };
  value.scientific_accounts = { ...value.scientific_accounts, count: 40 - index, scope: 'public_exact_gap', ranking: 'account_count' };
  value.votes = { score: 50 - index, upvotes: 50 - index, downvotes: 0, user_vote: viewer === userA ? 1 : null };
  records.set(id, value);
  return value;
}
async function playwright() {
  if (process.env.PLAYWRIGHT_MODULE) return import(process.env.PLAYWRIGHT_MODULE.startsWith('/') ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : process.env.PLAYWRIGHT_MODULE);
  try { return await import('playwright'); } catch { return import(pathToFileURL(resolve(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs')).href); }
}
async function executable() {
  if (process.env.PLAYWRIGHT_EXECUTABLE_PATH) return process.env.PLAYWRIGHT_EXECUTABLE_PATH;
  for (const entry of (await readdir(resolve(homedir(), 'Library/Caches/ms-playwright')).catch(() => [])).filter(value => /^chromium-\d+$/.test(value)).sort().reverse()) {
    for (const suffix of ['chrome-mac/Chromium.app/Contents/MacOS/Chromium', 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing']) {
      const path = resolve(homedir(), 'Library/Caches/ms-playwright', entry, suffix); try { await access(path); return path; } catch { /* Try another installed binary. */ }
    }
  }
}
async function until(check, reason) {
  const began = performance.now();
  while (!await check()) { assert.ok(performance.now() - began < 15000, `Timed out: ${reason}`); await new Promise(resolve => setTimeout(resolve, 25)); }
}
const { chromium } = await playwright(), browser = await chromium.launch({ headless: true, executablePath: await executable() });
const report = { scope: 'Real local Next navigation with mocked read/vote APIs and controllable workspace event streams. No live sign-in, vote, publication, research job or data writes.', contractSha256: fixture.contractSha256, status: 'running', scenarios: [] };
let activePage;
async function harness(name, { visitor = false, sharedIds = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage(); activePage = page; page.setDefaultTimeout(15000);
  const state = { viewer: visitor ? null : userA, revision: 0, holdLists: false, pending: [], requests: [], unexpected: [], errors: [], eventCursor: 0, votes: new Map(), holdNextVoteRead: false, pendingVotes: [], completedVoteReads: 0 };
  page.on('pageerror', error => state.errors.push(error.message));
  await context.addInitScript(() => {
    // Explicit paging avoids depending on viewport-specific sentinel intersections.
    Object.defineProperty(window, 'IntersectionObserver', { value: undefined, configurable: true });
    const originalNow = Date.now;
    window.__gapNavigationClock = 0;
    Date.now = () => originalNow() + window.__gapNavigationClock;
    const originalFetch = window.fetch.bind(window), encoder = new TextEncoder();
    const streams = new Set(); window.__gapNavigationStreams = streams;
    window.fetch = (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname !== '/api/backend/v1/me/workspace/events') return originalFetch(input, init);
      const signal = init?.signal || (input instanceof Request ? input.signal : undefined);
      let controller;
      const stream = new ReadableStream({
        start(value) { controller = value; streams.add(value); value.enqueue(encoder.encode('event: ready\nid: fixture-ready\ndata: {}\n\n')); },
        cancel() { streams.delete(controller); },
      });
      signal?.addEventListener('abort', () => { streams.delete(controller); try { controller.close(); } catch { /* Already closed. */ } }, { once: true });
      return Promise.resolve(new Response(stream, { headers: { 'content-type': 'text/event-stream' } }));
    };
  });
  await context.route('**/*', async route => {
    try {
      const request = route.request(), url = new URL(request.url()), path = decodeURIComponent(url.pathname);
      if (url.origin !== origin) { state.unexpected.push(`${request.method()} ${url.origin}${path}`); return route.abort(); }
      const voteMatch = path.match(/^\/api\/backend\/v1\/knowledge-gaps\/([^/]+)\/vote$/);
      assert.ok(request.method() === 'GET' || (request.method() === 'POST' && voteMatch), 'Only intercepted fixture vote writes are allowed');
      if (!path.startsWith('/api/')) return route.continue();
      state.requests.push({ method: request.method(), path, query: url.search, viewer: state.viewer });
      if (path === '/api/session/status') return route.fulfill({ json: { principal: state.viewer ? { user_id: state.viewer } : null, canClaim: false, canAdmin: false, providers: { google: false, orcid: false } } });
      if (path === '/api/backend/v1/me') return route.fulfill({ json: { user_id: state.viewer, display_name: state.viewer === userA ? 'Fixture viewer A' : 'Fixture viewer B', email: null, email_verified: null, orcid: null, orcid_authenticated: false, person: null, principal_kind: 'registered', workspace_expires_at: null } });
      if (path === '/api/backend/v1/knowledge-gaps') {
        const sort = url.searchParams.get('sort') || 'accounts', cursor = url.searchParams.get('cursor');
        assert.ok(['accounts', 'votes'].includes(sort)); assert.equal(url.searchParams.get('scope'), 'public');
        assert.equal(url.searchParams.get('limit'), '20');
        if (cursor) assert.equal(cursor, `${sort}-page-2`, 'Pagination stays bound to the selected ranking');
        const offset = cursor ? 20 : 0;
        const body = { items: Array.from({ length: 20 }, (_, index) => {
          const item = gap(sort, index + offset, state.revision, state.viewer, sharedIds);
          if (state.votes.has(item.object.id)) item.votes = structuredClone(state.votes.get(item.object.id));
          return item;
        }), page: pageInfo(sort, !cursor, state.revision) };
        if (state.holdLists) await new Promise(resolve => state.pending.push(resolve));
        return route.fulfill({ json: body }).catch(error => { if (!/closed|cancel|intercept/i.test(String(error))) throw error; });
      }
      if (/^\/api\/backend\/v1\/knowledge-gaps\/[^/]+\/(accounts|outcomes)$/.test(path)) return route.fulfill({ json: { items: [], page: pageInfo('accounts') } });
      if (voteMatch) {
        const id = voteMatch[1];
        if (request.method() === 'POST') {
          const { vote } = request.postDataJSON(); assert.ok([-1, 0, 1].includes(vote));
          // Forty-nine other fixture voters plus this viewer's current ballot.
          const value = { score: 49 + vote, upvotes: 49 + (vote === 1 ? 1 : 0), downvotes: vote === -1 ? 1 : 0, user_vote: vote === 0 ? null : vote };
          state.votes.set(id, value);
          return route.fulfill({ json: value });
        }
        const body = structuredClone(state.votes.get(id) || records.get(id)?.votes || { score: 50, upvotes: 50, downvotes: 0, user_vote: state.viewer === userA ? 1 : null });
        if (state.holdNextVoteRead) { state.holdNextVoteRead = false; await new Promise(resolve => state.pendingVotes.push(resolve)); }
        await route.fulfill({ json: body }); state.completedVoteReads++;
        return;
      }
      const id = path.startsWith('/api/backend/v1/knowledge-gaps/') ? path.slice('/api/backend/v1/knowledge-gaps/'.length) : null;
      if (id && records.has(id)) return route.fulfill({ json: records.get(id) });
      state.unexpected.push(path); return route.fulfill({ status: 500, json: { detail: 'Unexpected navigation fixture API' } });
    } catch (error) { state.errors.push(String(error)); return route.fulfill({ status: 500, json: { detail: String(error) } }).catch(() => {}); }
  });
  const h = {
    page, state, lists: () => state.requests.filter(request => request.path === '/api/backend/v1/knowledge-gaps'),
    release() { state.holdLists = false; for (const resolve of state.pending.splice(0)) resolve(); },
    releaseVotes() { for (const resolve of state.pendingVotes.splice(0)) resolve(); },
    async emit(collections) {
      await page.waitForFunction(() => window.__gapNavigationStreams?.size > 0);
      const cursor = ++state.eventCursor;
      await page.evaluate(({ collections, cursor }) => {
        const event = { schema_version: 1, event_id: `fixture-event-${cursor}`, cursor: String(cursor), scope: 'public', committed_at: new Date().toISOString(), event_type: 'fixture_update', entity_id: 'fixture-record', entity_revision: cursor, operation: 'upsert', collections };
        const bytes = new TextEncoder().encode(`event: workspace_change\nid: fixture-${cursor}\ndata: ${JSON.stringify(event)}\n\n`);
        for (const controller of window.__gapNavigationStreams) controller.enqueue(bytes);
      }, { collections, cursor });
    },
    async open() { await page.goto(origin); await rows(page, 20); if (!visitor) await page.waitForFunction(() => window.__gapNavigationStreams?.size > 0); },
    async close(checks) { h.release(); h.releaseVotes(); assert.deepEqual(state.errors, []); assert.deepEqual(state.unexpected, []); report.scenarios.push({ name, checks, listRequests: h.lists(), allRequests: state.requests }); await context.close(); activePage = null; },
  };
  return h;
}
const list = page => page.locator('.gap-browser-list').first();
const rowTexts = page => page.locator('.gap-browser .trend-question').allTextContents();
const rows = (page, count) => page.waitForFunction(count => document.querySelectorAll('.gap-browser .trend').length === count, count);
async function setScroll(page, value) {
  await list(page).evaluate((element, value) => { element.scrollTop = value; element.dispatchEvent(new Event('scroll')); }, value);
  return list(page).evaluate(element => element.scrollTop);
}
async function scrollEquals(page, expected) {
  await page.waitForFunction(expected => Math.abs((document.querySelector('.gap-browser-list')?.scrollTop ?? -100) - expected) <= 2, expected);
}
async function loadMore(page) { await page.getByRole('button', { name: 'Load more questions', exact: false }).click(); await rows(page, 40); }
async function aboutBack(page) {
  await page.getByRole('link', { name: 'About', exact: true }).click();
  await page.getByRole('heading', { name: 'We are building a community of gap closers', exact: true }).waitFor();
  await page.getByRole('link', { name: 'Knowledge gaps', exact: false }).first().click();
}
async function neutralFirstVote(page) {
  await page.waitForFunction(() => {
    const first = document.querySelector('.gap-browser .trend');
    return first?.querySelector('.vote-score')?.textContent === '+49'
      && first.querySelector('.vote-up')?.getAttribute('aria-pressed') === 'false'
      && first.querySelector('.vote-down')?.getAttribute('aria-pressed') === 'false';
  });
}

try {
  const nav = await harness('retained pages, exact scroll and sort across client navigation');
  await nav.open(); await loadMore(nav.page); const expectedAccounts = await rowTexts(nav.page), accountScroll = await setScroll(nav.page, 720), beforeAbout = nav.lists().length;
  assert.ok(accountScroll > 500, 'Fixture must have enough rows to exercise nested scroll restoration');
  await nav.page.getByRole('link', { name: 'About', exact: true }).click(); await nav.page.getByRole('heading', { name: 'We are building a community of gap closers', exact: true }).waitFor();
  await nav.page.evaluate(() => { window.__gapNavigationClock += 120000; });
  await nav.page.getByRole('link', { name: 'Knowledge gaps', exact: false }).first().click(); await rows(nav.page, 40); await scrollEquals(nav.page, accountScroll);
  assert.deepEqual(await rowTexts(nav.page), expectedAccounts); assert.equal(nav.lists().length, beforeAbout, 'Returning after two minutes must reuse loaded pages without list GETs');
  const infoIndex = await list(nav.page).evaluate(element => {
    const bounds = element.getBoundingClientRect();
    return [...element.querySelectorAll('.trend-question-area')].findIndex(row => { const rect = row.getBoundingClientRect(); return rect.top >= bounds.top + 8 && rect.bottom <= bounds.bottom - 8; });
  });
  assert.ok(infoIndex >= 0); await nav.page.locator('.gap-browser .trend-question-area').nth(infoIndex).hover();
  await nav.page.locator('.gap-browser .trend-info').nth(infoIndex).click(); await nav.page.locator('.gap-detail').waitFor();
  await nav.page.getByRole('link', { name: 'All knowledge gaps', exact: true }).click(); await rows(nav.page, 40);
  await scrollEquals(nav.page, accountScroll);
  assert.deepEqual(await rowTexts(nav.page), expectedAccounts); assert.equal(nav.lists().length, beforeAbout, 'Reading gap provenance must not reload the discovery list');
  // Clicking a scrolled row may first move the nested list; record the current position anew.
  const finalAccountScroll = await setScroll(nav.page, 860);
  await nav.page.locator('#gap-sort').selectOption('votes'); await rows(nav.page, 20); const votesFirst = nav.lists().length;
  assert.match((await rowTexts(nav.page))[0], /^Votes-ranked/); await loadMore(nav.page); const expectedVotes = await rowTexts(nav.page), voteScroll = await setScroll(nav.page, 480), allRequests = nav.lists().length;
  assert.equal(allRequests, votesFirst + 1);
  await aboutBack(nav.page); await rows(nav.page, 40); await scrollEquals(nav.page, voteScroll);
  assert.equal(await nav.page.locator('#gap-sort').inputValue(), 'votes', 'Selected gap sort survives Composer remount');
  assert.deepEqual(await rowTexts(nav.page), expectedVotes); assert.equal(nav.lists().length, allRequests);
  await nav.page.locator('#gap-sort').selectOption('accounts'); await rows(nav.page, 40); await scrollEquals(nav.page, finalAccountScroll);
  assert.deepEqual(await rowTexts(nav.page), expectedAccounts); assert.equal(nav.lists().length, allRequests, 'Previously visited sort reuses its own pages');
  await nav.page.locator('#gap-sort').selectOption('votes'); await rows(nav.page, 40); await scrollEquals(nav.page, voteScroll);
  assert.equal(nav.lists().length, allRequests);
  await nav.page.screenshot({ path: resolve(output, 'retained-vote-sorted-list.png') });
  const preReload = nav.lists().length; nav.state.revision = 1; await nav.page.reload(); await rows(nav.page, 20);
  assert.ok(nav.lists().length > preReload, 'A full document reload creates a new memory cache and refetches');
  assert.ok((await rowTexts(nav.page)).every(text => text.includes('refreshed revision 1')));
  await nav.close(['About/back retains40rows and scroll after simulated2minutes', 'gap info/back makes no extra list GET', 'Accounts/Votes keep separate pages and scroll', 'selected sort persists across route remount', 'full reload refetches current data']);

  const refresh = await harness('events preserve rows and require explicit refresh');
  await refresh.open(); await loadMore(refresh.page); const retained = await rowTexts(refresh.page), beforeEvent = refresh.lists().length;
  await refresh.emit(['jobs']); await refresh.page.waitForTimeout(100); assert.equal(refresh.lists().length, beforeEvent);
  refresh.state.revision = 2; await refresh.emit(['catalog', 'gaps', 'accounts']);
  const refreshButton = refresh.page.getByRole('button', { name: /Refresh/i }); await refreshButton.waitFor();
  assert.deepEqual(await rowTexts(refresh.page), retained); assert.equal(refresh.lists().length, beforeEvent, 'A catalog event must not silently reshuffle the list');
  await aboutBack(refresh.page); await rows(refresh.page, 40); await refreshButton.waitFor();
  assert.deepEqual(await rowTexts(refresh.page), retained); assert.equal(refresh.lists().length, beforeEvent, 'Remounting stale data must still wait for explicit refresh');
  refresh.state.holdLists = true; await refreshButton.click(); await until(() => refresh.state.pending.length > 0, 'explicit refresh request');
  assert.deepEqual(await rowTexts(refresh.page), retained, 'Rows stay readable while the refresh is in flight');
  refresh.release(); await refresh.page.waitForFunction(() => [...document.querySelectorAll('.gap-browser .trend-question')].length === 20 && [...document.querySelectorAll('.gap-browser .trend-question')].every(node => node.textContent.includes('refreshed revision 2')));
  assert.equal(refresh.lists().length, beforeEvent + 1, 'Explicit refresh starts a new ranking snapshot from its first page');
  await scrollEquals(refresh.page, 0);
  await refresh.page.screenshot({ path: resolve(output, 'explicit-refresh-complete.png') });
  await refresh.close(['unrelated job events leave discovery untouched', 'catalog/gap/account invalidation marks retained rows stale', 'stale remount does not fetch', 'explicit refresh retains rows in flight', 'successful explicit refresh starts a fresh first page and scroll']);

  const identity = await harness('viewer change clears cached ballots before loading the new viewer');
  await identity.open(); assert.equal(await identity.page.locator('.gap-browser .vote-up.is-selected').count(), 20);
  const oldViewerRequests = identity.lists().length; identity.state.holdLists = true; identity.state.viewer = userB; await identity.emit(['identity']);
  await until(() => identity.state.pending.length > 0, 'new viewer list request');
  assert.equal(await identity.page.locator('.gap-browser .vote-up.is-selected').count(), 0, 'Previous viewer ballots must not remain visible');
  identity.release(); await rows(identity.page, 20);
  assert.ok(identity.lists().length > oldViewerRequests); assert.equal(identity.lists().at(-1).viewer, userB);
  assert.equal(await identity.page.locator('.gap-browser .vote-up.is-selected').count(), 0);
  const afterIdentity = identity.lists().length; await aboutBack(identity.page); await rows(identity.page, 20);
  assert.equal(identity.lists().length, afterIdentity, 'The new viewer gets an independent stable cache');
  await identity.close(['identity event checks current session', 'old vote state clears before new response', 'new viewer refetches once and caches independently']);

  const pending = await harness('pending list requests survive leaving the homepage');
  pending.state.holdLists = true; await pending.page.goto(origin); await until(() => pending.state.pending.length > 0, 'initial gated list request');
  const pendingRequests = pending.lists().length;
  await pending.page.getByRole('link', { name: 'About', exact: true }).click(); await pending.page.getByRole('heading', { name: 'We are building a community of gap closers', exact: true }).waitFor();
  const completed = pending.page.waitForResponse(response => new URL(response.url()).pathname === '/api/backend/v1/knowledge-gaps');
  pending.release(); await completed;
  await pending.page.getByRole('link', { name: 'Knowledge gaps', exact: false }).first().click(); await rows(pending.page, 20);
  assert.equal(pending.lists().length, pendingRequests, 'A request begun before navigation populates the persistent cache');
  await pending.close(['navigation does not abort the shared pending request', 'completed response populates cache while homepage is unmounted', 'returning uses that response without another request']);

  const voteRace = await harness('late vote read from an unmounted list cannot overwrite the newer ballot', { sharedIds: true });
  await voteRace.open(); await voteRace.page.locator('#gap-sort').selectOption('votes'); await rows(voteRace.page, 20);
  await voteRace.page.locator('#gap-sort').selectOption('accounts'); await rows(voteRace.page, 20);
  const beforeVoteNavigation = voteRace.lists().length;
  voteRace.state.holdNextVoteRead = true;
  await voteRace.page.locator('.gap-browser .trend').first().getByRole('button', { name: 'Downvote this knowledge gap', exact: true }).click();
  await until(() => voteRace.state.pendingVotes.length === 1, 'old component committed downvote read');
  // The downvote POST has committed. Its stale GET snapshot is deliberately held
  // while a new component removes the original cached upvote, committing neutral.
  await aboutBack(voteRace.page); await rows(voteRace.page, 20);
  await voteRace.page.locator('.gap-browser .trend').first().getByRole('button', { name: 'Remove upvote from this knowledge gap', exact: true }).click();
  await neutralFirstVote(voteRace.page);
  assert.equal(voteRace.state.completedVoteReads, 1, 'Only the newer committed ballot read has completed');
  voteRace.releaseVotes(); await until(() => voteRace.state.completedVoteReads === 2, 'delayed old component response completion');
  // Drain browser work after the stale response, then verify both retained sort
  // snapshots: an unmounted onChange used to mutate this shared cache here.
  await voteRace.page.waitForFunction(() => document.querySelector('.gap-browser .trend .vote-buttons')?.getAttribute('aria-busy') === 'false');
  await aboutBack(voteRace.page); await rows(voteRace.page, 20); await neutralFirstVote(voteRace.page);
  await voteRace.page.locator('#gap-sort').selectOption('votes'); await rows(voteRace.page, 20); await neutralFirstVote(voteRace.page);
  await aboutBack(voteRace.page); await rows(voteRace.page, 20); await neutralFirstVote(voteRace.page);
  assert.equal(await voteRace.page.locator('#gap-sort').inputValue(), 'votes');
  assert.equal(voteRace.lists().length, beforeVoteNavigation, 'Voting and navigation update both cached sorts without another ranking list request');
  assert.equal(voteRace.state.requests.filter(request => request.method === 'POST').length, 2, 'Both vote changes are intercepted fixture mutations');
  await voteRace.close(['hold old committed downvote read across unmount', 'new instance commits a neutral vote', 'late old read cannot overwrite newer cached ballot', 'Accounts and Votes snapshots both retain newest ballot on return']);

  const visitor = await harness('anonymous focus checks retain the navigation cache', { visitor: true });
  await visitor.open(); await visitor.page.locator('#gap-sort').selectOption('votes'); await rows(visitor.page, 20); await loadMore(visitor.page);
  const visitorRows = await rowTexts(visitor.page), visitorScroll = await setScroll(visitor.page, 530), beforeVisitorFocus = visitor.lists().length;
  const statusChecks = visitor.state.requests.filter(request => request.path === '/api/session/status').length;
  const focusResponse = visitor.page.waitForResponse(response => new URL(response.url()).pathname === '/api/session/status');
  await visitor.page.evaluate(() => { window.dispatchEvent(new Event('focus')); }); await focusResponse;
  await until(() => visitor.state.requests.filter(request => request.path === '/api/session/status').length > statusChecks, 'anonymous session focus check');
  await aboutBack(visitor.page); await rows(visitor.page, 40); await scrollEquals(visitor.page, visitorScroll);
  assert.equal(await visitor.page.locator('#gap-sort').inputValue(), 'votes');
  assert.deepEqual(await rowTexts(visitor.page), visitorRows);
  assert.equal(visitor.lists().length, beforeVisitorFocus, 'A visitor remaining anonymous is not an identity change and must not purge or refetch loaded pages');
  assert.equal(visitor.state.requests.filter(request => request.path === '/api/backend/v1/me').length, 0, 'Anonymous visitor checks must not request registered profile data');
  await visitor.close(['anonymous focus rechecks session status', 'unchanged visitor identity keeps both loaded pages and nested scroll', 'selected sort survives focus and navigation', 'no extra list GET or profile request']);
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = String(error);
  if (activePage) {
    await activePage.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {});
    report.pageText = await activePage.locator('body').innerText().catch(() => '');
  }
  throw error;
} finally { await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2) + '\n'); await browser.close(); }
console.log(JSON.stringify({ status: report.status, scenarios: report.scenarios.length, output }));
